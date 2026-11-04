#!/usr/bin/env python3
"""LabMap: a live map of your home server - networks, disks, apps and the machine itself on one page.

Python standard library only. Endpoints:
  /           the page (index.html from this directory)
  /api/state  server state and alerts as JSON
  /api/test/… speed tests: browser <-> server and server <-> internet
Apps are listed in services.json - edits apply on the next page refresh, no restart needed.
Optional secrets.json (next to services.json): {"immich_api_key": "..."} - an Immich API key with the
server.statistics and job.read permissions; the Immich app then shows photo counts and its job queue.
"""
import collections
import datetime
import glob
import http.client
import http.server
import ipaddress
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
# The original files (and services.json, secrets.json) may live on the data disk. The service can run a copy from
# the system disk, so the map can still report a missing RAID array.
CONFIG = os.environ.get("LABMAP_CONFIG", HERE)
PORT = int(os.environ.get("LABMAP_PORT", "80"))
ADDRESS = os.environ.get("LABMAP_ADDRESS", "::")  # for testing: "::1"
RAID_MOUNT = os.environ.get("LABMAP_RAID_MOUNT", "/srv/data")
RAID_MD = os.environ.get("LABMAP_RAID_DEVICE", "md0")
LAN = os.environ.get("LABMAP_LAN_INTERFACE", "eth0")
WIFI = os.environ.get("LABMAP_WIFI_INTERFACE", "wlan0")
TAILSCALE = os.environ.get("LABMAP_TAILSCALE_INTERFACE", "tailscale0")
IMMICH_API = os.environ.get("LABMAP_IMMICH_URL", "http://127.0.0.1:2283") + "/api"
NEXTCLOUD_STATUS = os.environ.get("LABMAP_NEXTCLOUD_URL", "http://127.0.0.1:8081") + "/status.php"
IMMICH_BACKUPS = os.environ.get("LABMAP_IMMICH_BACKUPS", os.path.join(RAID_MOUNT, "immich/backups"))
MIN_CHARGER_W = 40  # below this a USB-C charger can't keep a busy laptop running
MDNS_HINTS = ("/etc/systemd/resolved.conf.d/10-mdns.conf", "/run/avahi-daemon/pid")
HISTORY = 150  # samples every 2 s = 5 minutes
SERIES = ("cpu", "temp", "raid_read", "raid_write", "wifi_rx", "wifi_tx", "lan_rx", "lan_tx", "ts_rx", "ts_tx")


def update_copy():
    """Copies changes from the original files to the copy the service runs from.

    index.html and services.json are swapped right away; a new labmap.py (only if it compiles) replaces the running
    process in place - so fixes apply without sudo and without restarting the service.
    """
    if os.path.realpath(CONFIG) == os.path.realpath(HERE):
        return
    for name in ("index.html", "services.json", "labmap.py"):
        source, target = os.path.join(CONFIG, name), os.path.join(HERE, name)
        try:
            with open(source, "rb") as f:
                new = f.read()
        except OSError:
            continue  # e.g. the data disk is gone - keep the current copy
        try:
            with open(target, "rb") as f:
                if f.read() == new:
                    continue
        except OSError:
            pass
        try:
            if name == "labmap.py":
                compile(new, source, "exec")
            elif name == "services.json":
                json.loads(new.decode("utf-8-sig"))  # the last good version stays as a fallback
        except (SyntaxError, ValueError):
            continue  # don't copy a half-written or broken version
        try:
            with open(target + ".new", "wb") as f:
                f.write(new)
            os.replace(target + ".new", target)
        except OSError:
            continue
        if name == "labmap.py":
            os.execv(sys.executable, [sys.executable, "-I", target])


update_copy()  # first thing: even a version that crashes later during startup can be replaced

lock = threading.Lock()
history = {k: collections.deque(maxlen=HISTORY) for k in SERIES}
fast = {}  # latest sample from fast_loop
slow = {}  # latest result of slow_loop


def plural(n, one, many):
    return one if abs(int(n)) == 1 else many


def read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def number(path, divide=1, default=None):
    try:
        return int(read(path)) / divide
    except (TypeError, ValueError):
        return default


def hwmon(name):
    for h in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        if read(h + "/name") == name:
            return h
    return None


def get_json(url, headers=None, timeout=4):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def port_open(port, host="127.0.0.1"):
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


# --- fast samples (every 2 s) ------------------------------------------------------------

def _cpu():
    with open("/proc/stat") as f:
        v = [int(x) for x in f.readline().split()[1:9]]
    return sum(v) - v[3] - v[4], sum(v)  # busy, total (without idle and iowait)


def _net():
    result = {}
    with open("/proc/net/dev") as f:
        for line in f.readlines()[2:]:
            name, data = line.split(":", 1)
            p = data.split()
            result[name.strip()] = (int(p[0]), int(p[8]))
    return result


def _disks():
    result = {}
    with open("/proc/diskstats") as f:
        for line in f:
            p = line.split()
            result[p[2]] = (int(p[5]) * 512, int(p[9]) * 512)
    return result


def fast_loop():
    global fast
    sensor = (hwmon("k10temp") or hwmon("coretemp") or "/none") + "/temp1_input"
    previous = None
    while True:
        try:
            t, cpu, net, disks = time.monotonic(), _cpu(), _net(), _disks()
            if previous:
                pt, pcpu, pnet, pdisks = previous
                dt = max(t - pt, 0.001)

                def rate(now, before, key, i):
                    if key in now and key in before:
                        return max(0, (now[key][i] - before[key][i]) / dt)
                    return 0

                total = cpu[1] - pcpu[1]
                sample = {
                    "cpu": round(100 * (cpu[0] - pcpu[0]) / total, 1) if total else 0,
                    "temp": number(sensor, 1000),
                    "raid_read": rate(disks, pdisks, RAID_MD, 0),
                    "raid_write": rate(disks, pdisks, RAID_MD, 1),
                    "wifi_rx": rate(net, pnet, WIFI, 0), "wifi_tx": rate(net, pnet, WIFI, 1),
                    "lan_rx": rate(net, pnet, LAN, 0), "lan_tx": rate(net, pnet, LAN, 1),
                    "ts_rx": rate(net, pnet, TAILSCALE, 0), "ts_tx": rate(net, pnet, TAILSCALE, 1),
                }
                with lock:
                    for k in SERIES:
                        v = sample[k]
                        history[k].append(None if v is None else round(v, 1))
                    fast = sample
            previous = (t, cpu, net, disks)
        except Exception:  # a single failed read must not stop the sampling
            pass
        time.sleep(2)


# --- slower data (every 10 s) ------------------------------------------------------------

def disk_temperatures():
    result = {}
    for h in glob.glob("/sys/class/hwmon/hwmon*"):
        if read(h + "/name") == "drivetemp":
            blocks = glob.glob(h + "/device/block/*")
            if blocks:
                result[os.path.basename(blocks[0])] = number(h + "/temp1_input", 1000)
    return result


def disk_model(block):
    vendor = read(f"/sys/block/{block}/device/vendor", "")
    model = read(f"/sys/block/{block}/device/model", "")
    if vendor in ("", "ATA"):
        return model
    # USB bridges split an ATA disk name into an 8-character "vendor" and the rest ("WDC WD40" + "EZAX-...")
    return vendor + model if len(vendor) == 8 else f"{vendor} {model}"


def raid():
    md = f"/sys/block/{RAID_MD}/md"
    result = {"mounted": os.path.ismount(RAID_MOUNT), "exists": os.path.isdir(md)}
    if result["exists"]:
        result.update(
            state=read(md + "/array_state"), level=read(md + "/level"),
            degraded=int(read(md + "/degraded", "0") or 0),
            disks_in_array=int(read(md + "/raid_disks", "0") or 0),
            action=read(md + "/sync_action"), mismatches=int(read(md + "/mismatch_cnt", "0") or 0))
        progress = read(md + "/sync_completed", "none") or "none"
        if "/" in progress:
            done, total = (int(x) for x in progress.split("/"))
            result["progress"] = round(100 * done / total, 1) if total else None
        temperatures = disk_temperatures()
        result["disks"] = []
        for d in sorted(glob.glob(md + "/dev-*")):
            name = os.path.basename(d)[4:]
            block = name if os.path.exists(f"/sys/block/{name}") else name.rstrip("0123456789")
            result["disks"].append({
                "name": name, "state": read(d + "/state"), "model": disk_model(block),
                "size": (number(f"/sys/block/{block}/size", default=0) or 0) * 512,
                "temp": temperatures.get(block)})
    if result["mounted"]:
        result["space"] = space(RAID_MOUNT)
    return result


def power():
    b = "/sys/class/power_supply/BAT0"
    result = {"charger": number("/sys/class/power_supply/AC/online") == 1}
    if os.path.isdir(b):
        now, full, design = (number(f"{b}/{k}", 1e6) for k in ("energy_now", "energy_full", "energy_full_design"))
        watts = number(f"{b}/power_now", 1e6)
        status = read(f"{b}/status")
        result["battery"] = {
            "percent": number(f"{b}/capacity"), "status": status, "power": watts,
            "health": round(100 * full / design) if full and design else None,
            "cycles": number(f"{b}/cycle_count"),
            "thresholds": [number(f"{b}/charge_control_start_threshold"), number(f"{b}/charge_control_end_threshold")],
            "hours_left": round(now / watts, 1) if status == "Discharging" and watts and now else None,
        }
    for p in sorted(glob.glob("/sys/class/power_supply/ucsi-source-psy-*")):
        if number(p + "/online") == 1:
            voltage, current = number(p + "/voltage_now", 1e6), number(p + "/current_now", 1e6)
            kind = read(p + "/usb_type", "") or ""
            result["usb_c"] = {
                "voltage": voltage, "current": current,
                "power": pd_power() or (round(voltage * current, 1) if voltage and current else None),
                "mode": next((t.strip("[]") for t in kind.split() if t.startswith("[")), None)}
    return result


def pd_power():
    """The highest power a USB-C PD charger advertises (its list of voltages and currents), in watts."""
    best = None
    for c in glob.glob("/sys/class/typec/port*-partner/usb_power_delivery/source-capabilities/*:*"):
        def value(name):
            t = (read(f"{c}/{name}", "") or "").rstrip("mVAW")
            return int(t) / 1000 if t.isdigit() else None
        v, i = value("voltage") or value("maximum_voltage"), value("maximum_current")
        w = v * i if v and i else value("maximum_power")
        if w and (best is None or w > best):
            best = w
    return round(best) if best else None


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, socket_path, timeout=5):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def docker_api(method, path, data=None, timeout=5):
    """A request to the Docker API; returns (status, body)."""
    conn = UnixHTTP("/var/run/docker.sock", timeout)
    try:
        body = json.dumps(data).encode() if data is not None else None
        conn.request(method, path, body=body, headers={"Content-Type": "application/json"} if body else {})
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def docker(path):
    status, data = docker_api("GET", path)
    if status != 200:
        raise RuntimeError(f"Docker answered {status}")
    return json.loads(data)


def container_start(stamp):
    try:
        return datetime.datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def containers():
    result = []
    for c in docker("/containers/json?all=1"):
        if (c.get("Labels") or {}).get("labmap") == "speedtest":
            continue  # the throwaway internet test container - gone in a moment
        try:
            details = docker(f"/containers/{c['Id']}/json")
        except RuntimeError:
            continue  # removed between the list and the details (e.g. during docker compose up)
        state = details.get("State", {})
        result.append({
            "name": c["Names"][0].lstrip("/"),
            "project": (c.get("Labels") or {}).get("com.docker.compose.project"),
            "state": c.get("State"),
            "health": (state.get("Health") or {}).get("Status"),
            "since": container_start(state.get("StartedAt")) if c.get("State") == "running" else None,
            "restarts": details.get("RestartCount", 0),
            "image": c.get("Image"),
            "ports": sorted({p["PublicPort"] for p in c.get("Ports", [])
                             if p.get("PublicPort") and p.get("Type") == "tcp"}),
        })
    return sorted(result, key=lambda k: (k["project"] or "~", k["name"]))


_immich_stats = {"time": 0, "data": None}


def immich(key):
    result = {"running": False}
    try:
        result["running"] = get_json(IMMICH_API + "/server/ping").get("res") == "pong"
        v = get_json(IMMICH_API + "/server/version")
        result["version"] = f"{v['major']}.{v['minor']}.{v['patch']}"
    except Exception:
        pass
    result["no_key"] = not key
    if key and result["running"]:
        headers = {"x-api-key": key}
        try:
            if time.time() - _immich_stats["time"] > 60:  # counting photos is heavier, once a minute is enough
                s = get_json(IMMICH_API + "/server/statistics", headers, timeout=10)
                _immich_stats.update(time=time.time(), data={
                    "photos": s["photos"], "videos": s["videos"],
                    "users": [{"name": u["userName"], "photos": u["photos"], "videos": u["videos"]}
                              for u in s.get("usageByUser", [])]})
            result["stats"] = _immich_stats["data"]
            queues = get_json(IMMICH_API + "/jobs", headers)
            result["queues"] = {name: q["jobCounts"]["active"] + q["jobCounts"]["waiting"]
                                for name, q in queues.items()
                                if q["jobCounts"]["active"] + q["jobCounts"]["waiting"]}
        except Exception as e:
            result["key_error"] = str(e)
    backups = sorted(glob.glob(IMMICH_BACKUPS + "/*.sql.gz"))
    if backups:
        result["last_backup"] = os.path.getmtime(backups[-1])
    return result


def nextcloud():
    try:
        s = get_json(NEXTCLOUD_STATUS)
        return {"running": bool(s.get("installed")), "version": s.get("versionstring"),
                "maintenance": bool(s.get("maintenance"))}
    except Exception:
        return {"running": False}


_ts_peers = {}  # tailnet address -> device name and connection type (for the speed test)


def tailscale():
    try:
        d = json.loads(subprocess.run(["tailscale", "status", "--json"], capture_output=True,
                                      timeout=5, check=True).stdout)
    except Exception:
        return {"running": False}
    me, peers = d.get("Self") or {}, (d.get("Peer") or {}).values()
    global _ts_peers
    _ts_peers = {ip: {"name": p.get("HostName"), "direct": bool(p.get("CurAddr")), "relay": p.get("Relay")}
                 for p in peers for ip in p.get("TailscaleIPs") or []}
    return {"running": d.get("BackendState") == "Running", "name": (me.get("DNSName") or "").rstrip("."),
            "ip": (me.get("TailscaleIPs") or [None])[0],
            "online": sum(1 for p in peers if p.get("Online")), "devices": len(peers)}


def lan():
    connected = read(f"/sys/class/net/{LAN}/carrier") == "1"
    result = {"connected": connected, "speed": number(f"/sys/class/net/{LAN}/speed") if connected else None,
              "clients": []}
    if connected:
        try:
            d = json.loads(subprocess.run(["networkctl", "status", LAN, "--json=short"],
                                          capture_output=True, timeout=5).stdout)
            result["clients"] = [{"name": l.get("Hostname"), "ip": ".".join(map(str, l.get("Address", [])))}
                                 for l in (d.get("DHCPServer") or {}).get("Leases", [])]
        except Exception:
            pass
    return result


def _attributes(b):
    result, i = {}, 0
    while i + 4 <= len(b):
        length, kind = struct.unpack_from("HH", b, i)
        if length < 4:
            break
        result[kind & 0x3fff] = b[i + 4:i + length]
        i += (length + 3) & ~3
    return result


def _attribute(kind, data):
    return struct.pack("HH", 4 + len(data), kind) + data + b"\0" * (-(4 + len(data)) % 4)


def _genl(s, family, command, flags, data):
    body = struct.pack("BBH", command, 1, 0) + data
    s.send(struct.pack("IHHII", 16 + len(body), family, flags, 1, 0) + body)
    results = []
    while True:
        buf, i = s.recv(65536), 0
        while i + 16 <= len(buf):
            length, kind = struct.unpack_from("IH", buf, i)
            if kind == 3:  # NLMSG_DONE
                return results
            if kind == 2:  # NLMSG_ERROR (0 = acknowledgement)
                code = struct.unpack_from("i", buf, i + 16)[0]
                if code:
                    raise OSError(-code, os.strerror(-code))
                return results
            results.append(_attributes(buf[i + 20:i + length]))
            i += (length + 3) & ~3
        if not flags & 0x300:  # without NLM_F_DUMP the answer is a single message
            return results


def wifi_link():
    """Wi-Fi link rate in Mb/s (what "iw dev wlan0 link" shows) - asks the kernel over nl80211."""
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 16) as s:  # NETLINK_GENERIC
        s.settimeout(2)
        s.bind((0, 0))
        family = struct.unpack("H", _genl(s, 0x10, 3, 1, _attribute(2, b"nl80211\0"))[0][1][:2])[0]
        stations = _genl(s, family, 17, 0x301, _attribute(3, struct.pack("I", socket.if_nametoindex(WIFI))))
    result = {}
    for station in stations[:1]:  # a Wi-Fi client has one "station": the access point
        info = _attributes(station.get(21, b""))  # NL80211_ATTR_STA_INFO
        for key, nr in (("upload", 8), ("download", 14)):  # TX_BITRATE, RX_BITRATE
            r = _attributes(info.get(nr, b""))
            v = struct.unpack("I", r[5][:4])[0] if 5 in r else struct.unpack("H", r[1][:2])[0] if 1 in r else None
            result[key] = v / 10 if v else None  # in units of 100 kb/s
    return result


def wifi():
    result = {"connected": read(f"/sys/class/net/{WIFI}/operstate") == "up"}
    if result["connected"]:
        try:
            result["link"] = wifi_link()
        except (OSError, KeyError, IndexError, struct.error):
            pass
    for line in (read("/proc/net/wireless", "") or "").splitlines()[2:]:
        if line.strip().startswith(WIFI + ":"):
            p = line.split()
            result["quality"] = float(p[2].rstrip("."))  # 0-70
            result["signal_dbm"] = float(p[3].rstrip("."))
    return result


_lan_network = None  # the LAN cable's subnet, to tell which link a browser uses


def ip_addresses():
    global _lan_network
    try:
        data = json.loads(subprocess.run(["ip", "-j", "-4", "addr", "show"], capture_output=True, timeout=5).stdout)
    except Exception:
        return {}
    result = {}
    for a in data:
        if a.get("addr_info"):
            info = a["addr_info"][0]
            result[a["ifname"]] = info["local"]
            if a["ifname"] == LAN:
                _lan_network = ipaddress.ip_network(f"{info['local']}/{info['prefixlen']}", strict=False)
    return result


_services_file = {"mtime": None, "data": [], "error": None}


def _load_services(path):
    with open(path, encoding="utf-8-sig") as f:
        content = json.load(f)
    items = content.get("services", []) if isinstance(content, dict) else None
    if not isinstance(items, list):
        raise ValueError("“services” must be a list of entries in [ ]")
    for s in items:
        if isinstance(s, dict) and isinstance(s.get("containers"), str):
            s["containers"] = [s["containers"]]  # "containers": "jellyfin" works too
    good = [s for s in items if isinstance(s, dict) and isinstance(s.get("name"), str)
            and isinstance(s.get("containers") or [], list)
            and all(isinstance(n, str) for n in s.get("containers") or [])]
    bad = len(items) - len(good)
    error = None if not bad else (f"services.json: skipped {bad} invalid {plural(bad, 'entry', 'entries')}"
                                  " – each entry is { … } with a “name”, and “containers” is a list of names in quotes.")
    return good, error


def services_from_file():
    paths = [os.path.join(CONFIG, "services.json"), os.path.join(HERE, "services.json")]
    path = next((p for p in paths if os.path.exists(p)), paths[0])  # without the data disk: the copy from startup
    try:
        mtime = (path, os.path.getmtime(path))
        if mtime != _services_file["mtime"]:
            data, error = _load_services(path)
            _services_file.update(mtime=mtime, data=data, error=error)
    except (OSError, ValueError) as e:
        _services_file["mtime"] = None  # try again in 10 s, also when someone restores the file with an old date
        if not _services_file["data"] and path != paths[1]:
            try:  # e.g. the server starts with a typo in the file: show the last good copy
                _services_file["data"] = _load_services(paths[1])[0]
            except (OSError, ValueError):
                pass
        meanwhile = ("Until it's fixed, the map shows the previous version." if _services_file["data"]
                     else "Until it's fixed, only auto-detected apps are shown.")
        if isinstance(e, json.JSONDecodeError):
            hint = ("there's an extra comma before ] or }" if "trailing comma" in e.msg
                    else "a comma between entries is missing" if "delimiter" in e.msg
                    else "a quote or a bracket is missing")
            _services_file["error"] = f"Error in services.json on line {e.lineno} (column {e.colno}) – {hint}. {meanwhile}"
        elif isinstance(e, FileNotFoundError):
            _services_file["error"] = f"services.json is missing. {meanwhile}"
        elif isinstance(e, OSError):
            _services_file["error"] = f"Can't read services.json ({e.strerror}). {meanwhile}"
        else:
            _services_file["error"] = f"Error in services.json: {e}. {meanwhile}"
    return _services_file["data"], _services_file["error"]


def logins():
    """Logins from logins.json (next to services.json) - the page asks for them only when an app's details open."""
    try:
        with open(os.path.join(CONFIG, "logins.json"), encoding="utf-8-sig") as f:
            data = json.load(f)
        items = data.get("logins", []) if isinstance(data, dict) else []
        return {"logins": [x for x in items if isinstance(x, dict)]}
    except (OSError, ValueError):
        return {"logins": []}


def immich_key():
    try:
        with open(os.path.join(CONFIG, "secrets.json"), encoding="utf-8") as f:
            return (json.load(f).get("immich_api_key") or "").strip() or None
    except (OSError, ValueError):
        return None


def slow_loop():
    global slow
    while True:
        try:
            update_copy()
            w, errors = {}, {}
            for name, function in (("containers", containers), ("raid", raid), ("power", power),
                                   ("tailscale", tailscale), ("lan", lan), ("wifi", wifi), ("ip", ip_addresses),
                                   ("nextcloud", nextcloud), ("immich", lambda: immich(immich_key()))):
                try:
                    w[name] = function()
                except Exception as e:
                    errors[name] = str(e)
            configured, _ = services_from_file()
            w["ports"] = {s["port"]: port_open(s["port"]) for s in configured
                          if isinstance(s.get("port"), int) and not s.get("containers")}
            w["ports"][445] = port_open(445)
            w["errors"], w["time"] = errors, time.monotonic()
            slow = w
        except Exception:  # an error here must not kill the thread - the data would freeze without a trace
            traceback.print_exc()
        time.sleep(10)


# --- building the response ---------------------------------------------------------------

def memory():
    m = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            m[k] = int(v.split()[0]) * 1024
    return {"total": m["MemTotal"], "available": m["MemAvailable"],
            "swap_total": m.get("SwapTotal", 0), "swap_free": m.get("SwapFree", 0)}


def space(path):
    st = os.statvfs(path)
    used, free = (st.f_blocks - st.f_bfree) * st.f_frsize, st.f_bavail * st.f_frsize
    # like df: ext4 reserves 5% of blocks for root, so a "full" disk is used / (used + free) = 100%
    return {"total": st.f_blocks * st.f_frsize, "free": free, "used": used,
            "percent": round(100 * used / (used + free), 1) if used + free else None}


def host_info():
    os_release = {}
    for line in (read("/etc/os-release", "") or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            os_release[k] = v.strip('"')
    cpu = ""
    with open("/proc/cpuinfo") as f:
        for line in f:
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip().replace(" with Radeon Graphics", "")
                break
    return {"name": socket.gethostname(),
            "model": read("/sys/class/dmi/id/product_version") or read("/sys/class/dmi/id/product_name"),
            "os": os_release.get("PRETTY_NAME"), "kernel": os.uname().release,
            "cpu": cpu, "threads": os.cpu_count()}


HOST = host_info()
NOT_WEB = {21, 22, 25, 53, 110, 139, 143, 445, 587, 993, 1883, 2222, 3306, 5432, 6379, 8883, 27017}


def build_services(w):
    configured, error = services_from_file()
    docker_ok = "containers" in w  # False: Docker didn't answer or the first sample is still running
    everything = w.get("containers") or []
    by_name = {k["name"]: k for k in everything}
    used_containers, used_projects, result = set(), set(), []
    for s in configured:
        names = s.get("containers") or []
        used_containers.update(names)
        conts = [by_name.get(n) or {"name": n, "state": "missing" if docker_ok else None, "health": None} for n in names]
        used_projects.update(k.get("project") for k in conts if k.get("project"))
        kind = s.get("type")
        data = w.get(kind) if kind in ("immich", "nextcloud") else None
        if kind == "samba" and 445 in (w.get("ports") or {}):
            data = {"running": w["ports"][445]}
        if docker_ok and any(k["state"] != "running" for k in conts):
            state = "error"
        elif data is not None and not data.get("running"):
            state = "error"
        elif any(k.get("health") == "unhealthy" for k in conts) or (data or {}).get("maintenance"):
            state = "warning"
        elif (conts and docker_ok) or data:
            state = "ok"
        elif isinstance(s.get("port"), int) and s["port"] in (w.get("ports") or {}):
            state = "ok" if w["ports"][s["port"]] else "error"
        else:
            state = None
        result.append({**s, "state": state, "data": data,
                       "containers": [{"name": k["name"], "state": k["state"], "health": k.get("health")} for k in conts]})
    # Docker apps with a published port that aren't in services.json show up on their own
    new = collections.OrderedDict()
    for k in everything:
        if k["name"] in used_containers or (k.get("project") and k["project"] in used_projects):
            continue
        new.setdefault(k.get("project") or k["name"], []).append(k)
    for project, conts in new.items():
        ports = [p for p in sorted(p for k in conts for p in k["ports"]) if p not in NOT_WEB]
        if not ports:
            continue  # no ports, or only ports like 445 or 1883 - an http:// link would be dead
        bad = any(k["state"] != "running" for k in conts)
        result.append({"name": project, "description": "Found automatically in Docker", "port": ports[0],
                       "https": ports[0] in (443, 8443, 9443),
                       "icon": "app", "auto": True, "state": "error" if bad else "ok",
                       "containers": [{"name": k["name"], "state": k["state"], "health": k.get("health")} for k in conts]})
    return result, error


RAID_ACTIONS = {"check": "checking the disks", "repair": "repairing mismatches", "resync": "syncing the disks",
                "recover": "rebuilding a disk", "reshape": "reshaping the array"}
CONTAINER_STATES = {"exited": "stopped", "restarting": "keeps restarting", "paused": "paused",
                    "created": "created but not started", "dead": "dead", "removing": "being removed"}
SOURCES = {"raid": "RAID", "power": "power", "tailscale": "Tailscale", "lan": "LAN cable", "wifi": "Wi-Fi",
           "ip": "IP addresses", "nextcloud": "Nextcloud", "immich": "Immich"}


def alerts(w, ram, system, services, config_error, temp=None):
    """Alerts for the map. "where" is the part of the map that lights up: server, power, raid, wifi,
    tailscale, lan or service:<name> - clicking it shows the alert text."""
    a = []

    def add(level, where, text):
        a.append({"level": level, "where": where, "text": text})

    service_of_container = {k["name"]: "service:" + s["name"] for s in services for k in s.get("containers", [])}
    service_of_type = {s["type"]: "service:" + s["name"] for s in services if s.get("type")}
    docker_ok = "containers" in w
    r = w.get("raid") or {}
    if r and not r.get("mounted"):
        add("error", "raid", "RAID is not mounted – apps can't reach their data.")
    if r.get("exists"):
        if r.get("degraded"):
            add("error", "raid", "RAID is running on one disk – check the disk enclosure.")
        if r.get("action") not in (None, "idle", "frozen"):
            progress = f" ({r['progress']:.1f}%)" if r.get("progress") is not None else ""
            add("info", "raid", f"{RAID_ACTIONS.get(r['action'], r['action']).capitalize()}{progress}.")
        if r.get("mismatches"):
            n = r["mismatches"]
            add("warning", "raid", f"{n} mismatched {plural(n, 'sector', 'sectors')}.")
    elif r:
        add("error", "raid", "Can't find the RAID array – is the disk enclosure disconnected?")
    for name, m, where in (("RAID", (r or {}).get("space"), "raid"), ("System disk", system, "server")):
        if m and m.get("percent") is not None:
            pct = m["percent"]
            if pct > 95:
                add("error", where, f"{name}: {pct:.0f}% full.")
            elif pct > 85:
                add("warning", where, f"{name}: {pct:.0f}% full.")
    z = w.get("power") or {}
    b = z.get("battery") or {}
    usb = z.get("usb_c") or {}
    pct, h = b.get("percent"), b.get("hours_left")
    left = ""
    if h:
        left = f", about {round(h * 60)} min left" if h < 1.5 else f", about {h:.1f} h left"
    battery = f" ({pct:.0f}%{left})" if pct is not None else ""
    start = (b.get("thresholds") or [None])[0]
    if z and not z.get("charger"):
        add("error" if pct is not None and pct < 30 else "warning", "power", f"Charger unplugged – running on battery{battery}.")
    elif b.get("status") == "Discharging":
        add("error" if pct is not None and pct < 50 else "warning", "power", f"The charger can't keep up – the battery is draining{battery}.")
    elif z.get("charger") and pct is not None and start and pct < start - 5 and b.get("status") != "Charging":
        add("warning", "power", f"Battery at {pct:.0f}% and not charging – charger too weak?")
    if z.get("charger") and usb.get("power") and usb["power"] < MIN_CHARGER_W:
        add("warning", "power", f"The {usb['power']:.0f} W charger is too weak – 45–65 W is needed. Under load the battery drains.")
    if temp and temp > 85:
        add("warning", "server", f"The CPU is hot – {temp:.0f} °C.")
    if ram["total"] and ram["available"] / ram["total"] < 0.08:
        add("warning", "server", "Running out of RAM.")
    if not docker_ok and "containers" in (w.get("errors") or {}):
        add("error", "server", "Docker isn't responding.")
    for k in w.get("containers") or []:
        where = service_of_container.get(k["name"], "server")
        if k["state"] != "running":
            add("error", where, f"Container {k['name']} isn't running ({CONTAINER_STATES.get(k['state'], k['state'])}).")
        elif k.get("health") == "unhealthy":
            add("warning", where, f"Container {k['name']} is failing its health check.")
    for s in services:
        missing = [k["name"] for k in s.get("containers", []) if k["state"] == "missing"]
        if missing:
            add("error", "service:" + s["name"], f"Missing {plural(len(missing), 'container', 'containers')} {', '.join(missing)}.")
        elif s["state"] == "error" and not any(k["state"] not in ("running", None) for k in s.get("containers", [])):
            add("error", "service:" + s["name"], f"{s['name']} isn't responding.")
    wf, ts = w.get("wifi"), w.get("tailscale")
    if wf is not None and not wf.get("connected"):
        add("warning", "wifi", "Wi-Fi disconnected – no internet and no Tailscale.")
    elif ts is not None and not ts.get("running"):
        add("warning", "tailscale", "Tailscale is down – no access from outside the home.")
    backup = (w.get("immich") or {}).get("last_backup")
    if backup and time.time() - backup > 2 * 86400:
        days = int((time.time() - backup) // 86400)
        add("warning", service_of_type.get("immich", "server"), f"The last database backup is {days} {plural(days, 'day', 'days')} old – check Backups in the Immich settings.")
    if (w.get("nextcloud") or {}).get("maintenance"):
        add("warning", service_of_type.get("nextcloud", "server"), "Nextcloud is in maintenance mode.")
    for source, text in (w.get("errors") or {}).items():
        if source != "containers":
            where = service_of_type.get(source) or (source if source in ("raid", "power", "tailscale", "lan", "wifi") else "server")
            add("warning", where, f"Couldn't check {SOURCES.get(source, source)} ({text}).")
    if config_error and not (r and not r.get("mounted")):  # without the RAID this error only repeats the RAID alert
        add("warning", "server", config_error)
    return a


def state_json():
    with lock:
        hist = {k: list(v) for k, v in history.items()}
        now = dict(fast)
    w = slow
    ram, system = memory(), space("/")
    services, config_error = build_services(w)
    recent = sorted(t for t in hist["temp"][-15:] if t is not None)
    temp_30s = recent[len(recent) // 2] if len(recent) >= 5 else None
    alert_list = alerts(w, ram, system, services, config_error, temp_30s) if w else []
    if w and time.monotonic() - w.get("time", 0) > 90:
        alert_list.append({"level": "warning", "where": "server", "text": "LabMap stopped refreshing its data – sudo systemctl restart labmap"})
    with open("/proc/uptime") as f:
        uptime = float(f.read().split()[0])
    thinkpad, nvme = hwmon("thinkpad"), hwmon("nvme")
    return {
        "time": time.time(), "port": PORT, "ready": bool(w),
        "host": {**HOST, "uptime": uptime, "load": os.getloadavg()},
        "now": now, "history": hist,
        "memory": ram, "system_disk": {**system, "temp": number(nvme + "/temp1_input", 1000) if nvme else None},
        "fan": number(thinkpad + "/fan1_input") if thinkpad else None,
        "raid": w.get("raid"), "power": w.get("power"),
        "network": {"ip": w.get("ip") or {}, "interfaces": {"lan": LAN, "wifi": WIFI},
                    "wifi": w.get("wifi"), "lan": w.get("lan"), "tailscale": w.get("tailscale"),
                    "mdns": any(os.path.exists(p) for p in MDNS_HINTS)},
        "containers": w.get("containers") or [],
        "services": services,
        "alerts": alert_list, "errors": w.get("errors") or {}, "temp_30s": temp_30s,
    }


# --- speed test --------------------------------------------------------------------------
# The browser measures the link it uses to reach the server (LAN cable or Tailscale): it downloads a stream from
# /api/test/download and uploads chunks to /api/test/upload. The server measures the internet itself - but the
# service is not allowed to reach the internet (IPAddressDeny in labmap.service), so curl runs in a throwaway
# container with host networking, from an image that is already on the server (Immich or Nextcloud).
TAILSCALE_NETWORKS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
RANDOM = os.urandom(256 * 1024)  # random, so nothing on the way can compress it
MAX_CHUNK = 64 * 2**20
_tests = threading.BoundedSemaphore(2)  # at most two browser tests at a time
SPEEDTEST_URL = "https://speed.cloudflare.com"
SPEEDTEST_SCRIPT = (  # $0 = SPEEDTEST_URL; ping is the time to open a TCP connection (without DNS)
    'p=""; for i in 1 2 3; do p="$p $(curl -s -o /dev/null --max-time 3 -w "%{time_namelookup},%{time_connect}" "$0/__down?bytes=0")"; done; '
    'd=$(curl -s -o /dev/null --max-time 6 -w "%{speed_download}" "$0/__down?bytes=50000000"); '
    'u=$(head -c 40000000 /dev/zero | curl -s -o /dev/null --max-time 6 -w "%{speed_upload}" --data-binary @- "$0/__up"); '
    'echo "RESULT $d $u$p"')
_internet = {"running": False, "result": None, "error": None}
_internet_lock = threading.Lock()


def who_is_asking(address):
    """Which link the browser uses to reach the server - that's the link the browser test measures."""
    ip = ipaddress.ip_address(address.split("%")[0])
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return {"via": "local"}
    if _lan_network and ip.version == _lan_network.version and ip in _lan_network:
        return {"via": "lan"}
    if any(ip in n for n in TAILSCALE_NETWORKS if n.version == ip.version):
        return {"via": "tailscale", **_ts_peers.get(str(ip), {})}
    return {"via": "other"}


def _docker_logs(b):  # without a TTY Docker prefixes every log chunk with an 8-byte header
    result, i = b"", 0
    while i + 8 <= len(b):
        length = struct.unpack(">I", b[i + 4:i + 8])[0]
        result += b[i + 8:i + 8 + length]
        i += 8 + length
    return result.decode(errors="replace")


def internet_test():
    image = next((c["ImageID"] for c in docker("/containers/json")
                  if any(n in c.get("Image", "") for n in ("immich-server", "nextcloud"))), None)
    if not image:
        raise RuntimeError("no image with curl (neither Immich nor Nextcloud is running)")
    for c in docker("/containers/json?all=1"):  # leftovers from a test cut short, e.g. by a restart
        if (c.get("Labels") or {}).get("labmap") == "speedtest":
            docker_api("DELETE", f"/containers/{c['Id']}?force=1")
    status, body = docker_api("POST", "/containers/create", {
        "Image": image, "Entrypoint": ["sh", "-c", SPEEDTEST_SCRIPT, SPEEDTEST_URL], "Cmd": [], "User": "65534:65534",
        "Labels": {"labmap": "speedtest"},
        "HostConfig": {"NetworkMode": "host", "ReadonlyRootfs": True, "CapDrop": ["ALL"],
                       "SecurityOpt": ["no-new-privileges"], "Memory": 256 * 2**20}})
    if status != 201:
        raise RuntimeError(f"Docker didn't create the container ({status})")
    container = json.loads(body)["Id"]
    try:
        status, _ = docker_api("POST", f"/containers/{container}/start")
        if status not in (204, 304):
            raise RuntimeError(f"Docker didn't start the container ({status})")
        docker_api("POST", f"/containers/{container}/wait", timeout=40)
        _, logs = docker_api("GET", f"/containers/{container}/logs?stdout=1")
    finally:
        docker_api("DELETE", f"/containers/{container}?force=1")
    line = next((l for l in _docker_logs(logs).splitlines() if l.startswith("RESULT ")), "")
    parts = line.split()
    if len(parts) < 3:
        raise RuntimeError("the test returned no result")
    download, upload = float(parts[1]), float(parts[2])
    pings = [float(b) - float(a) for a, b in (x.split(",") for x in parts[3:]) if float(b) > 0]
    if not download and not upload:
        raise RuntimeError("no internet connection")
    return {"download": download, "upload": upload, "ping_ms": min(pings) * 1000 if pings else None,
            "time": time.time()}


def _internet_test_in_background():
    try:
        result, error = internet_test(), None
    except Exception as e:
        result, error = None, {"text": str(e), "time": time.time()}
    with _internet_lock:
        _internet["running"], _internet["error"] = False, error
        if result:
            _internet["result"] = result


def start_internet_test():
    with _internet_lock:
        fresh = _internet["result"] and time.time() - _internet["result"]["time"] < 20
        if _internet["running"] or fresh:
            return  # several tabs or a double click don't start several tests
        _internet["running"] = True
    threading.Thread(target=_internet_test_in_background, daemon=True).start()


def internet_test_state():
    with _internet_lock:
        return {**_internet, "now": time.time()}


# --- web server --------------------------------------------------------------------------

ICON = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="15" fill="#0f1218"/>'
        b'<rect x="16.25" y="11" width="9" height="42" rx="4.5" fill="#fff"/><circle cx="39.25" cy="44.5" r="12" '
        b'fill="#12b312" opacity=".22"/><circle cx="39.25" cy="44.5" r="8.5" fill="#12b312"/></svg>')
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def host_allowed(header):
    if not header:
        return True  # browsers always send Host; without it it's e.g. curl from a terminal
    h = header.strip().lower()
    h = (h[1:h.find("]")] if h.startswith("[") else h.rsplit(":", 1)[0]).rstrip(".")
    try:
        ipaddress.ip_address(h.split("%")[0])
        return True  # an IP address typed directly - DNS can't swap it
    except ValueError:
        pass
    name = (HOST.get("name") or "").lower()
    ts = ((slow.get("tailscale") or {}).get("name") or "").lower()  # e.g. homelab.tail1234.ts.net
    return h in {"localhost", name, name + ".local", ts} - {""}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "labmap"
    sys_version = ""
    timeout = 20  # a connection that sends nothing doesn't hold a thread forever

    def do_HEAD(self):
        self.headers_only = True
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        try:
            if not host_allowed(self.headers.get("Host")):
                name = HOST.get("name") or "server"
                self.send(403, f"Unknown address – open http://{name}.local, http://{name} or the server's IP address.".encode(),
                          "text/plain; charset=utf-8")
            elif path == "/":
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    self.send(200, f.read(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self.send_json(state_json())
            elif path == "/api/logins":
                self.send_json(logins())
            elif path.startswith("/api/test/") and self.headers.get("X-Labmap") != "1":
                self.send(403, b"The speed test only works from the LabMap page.", "text/plain; charset=utf-8")
            elif path == "/api/test/whoami":
                self.send_json(who_is_asking(self.client_address[0]))
            elif path == "/api/test/internet":
                self.send_json(internet_test_state())
            elif path == "/api/test/download":
                self.test_stream()
            elif path == "/favicon.svg":
                self.send(200, ICON, "image/svg+xml")
            else:
                self.send(404, b"Not found.", "text/plain; charset=utf-8")
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception as e:
            self.send(500, json.dumps({"error": str(e)}, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            # the X-Labmap header forces a CORS preflight in the browser, so another website can't start a test
            if not host_allowed(self.headers.get("Host")) or self.headers.get("X-Labmap") != "1":
                self.send(403, b"The speed test only works from the LabMap page.", "text/plain; charset=utf-8")
            elif path == "/api/test/upload":
                self.receive_test_data()
            elif path == "/api/test/internet":
                start_internet_test()
                self.send_json(internet_test_state())
            else:
                self.send(404, b"Not found.", "text/plain; charset=utf-8")
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def test_stream(self):
        """Random data for 6 s - the browser reads as much as it can and stops by itself after 4 s."""
        if not _tests.acquire(blocking=False):
            return self.send(429, b"Another test is running.", "text/plain; charset=utf-8")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            end = time.monotonic() + 6
            while time.monotonic() < end:
                self.wfile.write(RANDOM)
        finally:
            _tests.release()

    def receive_test_data(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_CHUNK:
            return self.send(413, b"Chunk too large.", "text/plain; charset=utf-8")
        if not _tests.acquire(blocking=False):
            return self.send(429, b"Another test is running.", "text/plain; charset=utf-8")
        try:
            remaining = length
            while remaining:
                piece = self.rfile.read(min(remaining, 2**20))
                if not piece:
                    return
                remaining -= len(piece)
        finally:
            _tests.release()
        self.send_json({"received": length})

    def send_json(self, data):
        self.send(200, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if content_type.startswith("text/html"):
            self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        if not getattr(self, "headers_only", False):
            self.wfile.write(body)

    def log_message(self, *args):
        pass


class Server(http.server.ThreadingHTTPServer):
    address_family = socket.AF_INET6  # IPv6 and IPv4 at once
    daemon_threads = True

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


if __name__ == "__main__":
    threading.Thread(target=fast_loop, daemon=True).start()
    threading.Thread(target=slow_loop, daemon=True).start()
    Server((ADDRESS, PORT), Handler).serve_forever()
