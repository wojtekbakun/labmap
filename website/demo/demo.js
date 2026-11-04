/* LabMap demo: fake data for the GitHub Pages site.
 *
 * Loaded before the app's own script. It answers the app's /api/* requests in the browser (fetch and the
 * XMLHttpRequest used by the upload test), so the unchanged index.html behaves like a real install:
 * a laptop server with Wi-Fi, Tailscale and a LAN cable, a RAID1 array and a few Docker apps.
 * Every name, address and number here is made up. Nothing is sent anywhere.
 */
(function () {
  'use strict';
  const REPO = 'https://github.com/wojtekbakun/labmap';
  const STEP = 2000, HISTORY = 150;  // like labmap.py: a sample every 2 s, 5 minutes kept
  const SERIES = ['cpu', 'temp', 'raid_read', 'raid_write', 'wifi_rx', 'wifi_tx', 'lan_rx', 'lan_tx', 'ts_rx', 'ts_tx'];
  const realFetch = window.fetch.bind(window);
  const RealXHR = window.XMLHttpRequest;
  const nowS = () => Date.now() / 1000;
  const rnd = (a, b) => a + Math.random() * (b - a);
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const round = (v, d = 1) => Math.round(v * 10 ** d) / 10 ** d;
  const started = nowS();

  let failure = /[?&]failure\b/.test(location.search);
  let jellyfinSince = started - 3 * 86400 - 7200;

  // --- the demo setup ------------------------------------------------------------------------
  const HOST = {name: 'labmap-demo', model: 'ThinkPad T14 Gen 3', os: 'Ubuntu 24.04.1 LTS', kernel: '6.8.0-48-generic',
    cpu: 'AMD Ryzen 7 PRO 6850U', threads: 16};
  const BOOTED = started - (12 * 86400 + 5 * 3600 + 17 * 60);
  const SERVICES = [
    {name: 'Immich', description: "Photos and videos from everyone's phones", port: 2283, icon: 'photos', color: '#f59e0b', type: 'immich',
      containers: ['immich_server', 'immich_machine_learning', 'immich_postgres', 'immich_redis']},
    {name: 'Nextcloud', description: 'Files in the browser and on the phone', port: 8081, icon: 'cloud', color: '#0ea5e9', type: 'nextcloud',
      containers: ['nextcloud-app-1', 'nextcloud-db-1', 'nextcloud-redis-1']},
    {name: 'Samba', description: 'Network drive for Finder and Explorer', address: 'smb://{host}/media', icon: 'folder', color: '#8b5cf6', type: 'samba',
      containers: ['samba']},
    {name: 'Jellyfin', description: 'Movies and shows', port: 8096, icon: 'film', color: '#10b981', containers: ['jellyfin']},
    {name: 'Home Assistant', description: 'Lights, sensors and automations', port: 8123, icon: 'home', color: '#18bcf2', containers: ['homeassistant']},
  ];
  const day = 86400;
  const CONTAINERS = [  // [name, compose project, image, ports, running for (s), health]
    ['homeassistant', 'homeassistant', 'ghcr.io/home-assistant/home-assistant:stable', [], 6 * day, null],
    ['immich_machine_learning', 'immich', 'ghcr.io/immich-app/immich-machine-learning:release', [], 9 * day, 'healthy'],
    ['immich_postgres', 'immich', 'ghcr.io/immich-app/postgres:14-vectorchord0.4.3-pgvectors0.2.0', [], 9 * day, 'healthy'],
    ['immich_redis', 'immich', 'docker.io/valkey/valkey:8-bookworm', [], 9 * day, 'healthy'],
    ['immich_server', 'immich', 'ghcr.io/immich-app/immich-server:release', [2283], 9 * day, 'healthy'],
    ['jellyfin', 'jellyfin', 'jellyfin/jellyfin:latest', [8096], null, null],
    ['nextcloud-app-1', 'nextcloud', 'nextcloud:29-apache', [8081], 12 * day, null],
    ['nextcloud-db-1', 'nextcloud', 'mariadb:11', [], 12 * day, 'healthy'],
    ['nextcloud-redis-1', 'nextcloud', 'redis:7-alpine', [], 12 * day, null],
    ['samba', 'samba', 'dperson/samba:latest', [445], 12 * day, null],
    ['watchtower', null, 'containrrr/watchtower:latest', [], 12 * day, null],
  ];
  const LOGINS = [
    {service: 'Immich – admin', login: 'admin@labmap.demo', password: 'demo-password', notes: 'Demo login – not a real account.'},
    {service: 'Nextcloud – admin', login: 'admin', password: 'demo-password', notes: 'Demo login – not a real account.'},
    {service: 'Samba – share “media”', login: 'media', password: 'demo-password'},
    {service: 'Jellyfin', login: 'admin', password: 'demo-password'},
    {service: 'Home Assistant', login: 'admin', password: 'demo-password'},
  ];
  let photos = 8214, videos = 973;

  // --- live readings: a random walk every 2 s, history pre-filled so charts aren't empty -------
  const history = Object.fromEntries(SERIES.map(k => [k, []]));
  const w = {cpu: 9, temp: 47, mem: 9.6e9, fan: 2400, burst: {}};
  let live = false;  // false while the history is pre-filled
  let sample = {}, lastStep = Date.now() - HISTORY * STEP, traffic = null;  // traffic: a running speed test

  function bursty(key, base, high, chance, length) {  // mostly quiet, sometimes a burst of a few samples
    const b = w.burst;
    if (b[key] > 0) b[key]--;
    else if (Math.random() < chance) b[key] = Math.round(rnd(length / 2, length));
    return b[key] > 0 ? high * rnd(0.6, 1.1) : base * rnd(0.4, 1.6);
  }
  function step(t) {
    w.cpu = clamp(w.cpu + rnd(-2.5, 2.5) + (8 - w.cpu) * 0.12, 2, 60);
    const ml = bursty('ml', 0, 28, 0.025, 8);  // Immich's machine learning now and then
    const cpu = clamp(w.cpu + ml + rnd(-1, 1), 1, 98);
    w.temp += ((44 + cpu * 0.32) - w.temp) * 0.25 + rnd(-0.4, 0.4);
    w.fan = clamp(w.fan + ((w.temp - 40) * 160 - w.fan) * 0.2 + rnd(-60, 60), 0, 5200);
    w.mem = clamp(w.mem + rnd(-0.12e9, 0.12e9) + (9.6e9 - w.mem) * 0.1, 8.4e9, 10.8e9);
    const s = {
      cpu: round(cpu), temp: round(w.temp),
      raid_read: bursty('rr', 160e3, failure ? 6e6 : 38e6, 0.04, 6), raid_write: bursty('rw', 120e3, failure ? 3e6 : 22e6, 0.03, 5),
      wifi_rx: bursty('wr', 22e3, 1.8e6, 0.04, 5), wifi_tx: bursty('wt', 14e3, 450e3, 0.03, 4),
      lan_rx: rnd(9e3, 24e3), lan_tx: failure ? rnd(2e3, 6e3) : rnd(0.9e6, 1.6e6),  // a movie on Jellyfin to the desk PC
      ts_rx: bursty('tr', 9e3, 3.2e6, 0.035, 7), ts_tx: bursty('tt', 6e3, 380e3, 0.03, 4),  // phones backing up photos
    };
    if (traffic && t < traffic.until) s[traffic.key] = (s[traffic.key] || 0) + traffic.rate;
    if (live && s.ts_rx > 1e6 && Math.random() < 0.5) { photos += Math.random() < 0.85 ? 1 : 0; videos += Math.random() < 0.08 ? 1 : 0; }
    for (const k of SERIES) {
      history[k].push(round(s[k]));
      if (history[k].length > HISTORY) history[k].shift();
    }
    sample = s;
  }
  function catchUp() {
    const now = Date.now();
    if (now - lastStep > HISTORY * STEP) lastStep = now - HISTORY * STEP;  // e.g. a tab that slept for an hour
    while (lastStep + STEP <= now) { lastStep += STEP; step(lastStep); }
  }
  catchUp();
  live = true;

  // --- /api/state, shaped like state_json() in labmap.py ---------------------------------------
  function containers(t) {
    return CONTAINERS.map(([name, project, image, ports, age, health]) => {
      let state = 'running', since = t - age;
      if (name === 'jellyfin') { since = jellyfinSince; if (failure) { state = 'exited'; since = null; } }
      return {name, project, state, health: state === 'running' ? health : null, since, restarts: 0, image, ports};
    });
  }
  function space(total, used) {
    const free = total - used;
    return {total, free, used, percent: round(100 * used / (used + free))};
  }
  function raid() {
    const disk = (name, model, temp) => ({name, state: 'in_sync', model, size: 4000787030016, temp: round(temp + rnd(-0.4, 0.4), 0)});
    const disks = [disk('sda', 'WDC WD40EFPX-68C6CN0', 36), disk('sdb', 'WDC WD40EFPX-68C6CN0', 37)];
    if (failure) Object.assign(disks[1], {state: 'faulty', temp: null});
    return {mounted: true, exists: true, state: 'clean', level: 'raid1', degraded: failure ? 1 : 0, disks_in_array: 2,
      action: 'idle', mismatches: 0, disks, space: space(3.9e12, 1.2e12 + (nowS() - started) * 2e4)};
  }
  function power() {
    return {charger: true,
      battery: {percent: 78, status: 'Not charging', power: 0, health: 91, cycles: 142, thresholds: [75, 80], hours_left: null},
      usb_c: {voltage: 20, current: 1.5, power: 30, mode: 'PD'}};
  }
  function services(t, conts) {  // build_services(): an app's state from its containers and its own check
    const byName = Object.fromEntries(conts.map(k => [k.name, k]));
    const data = {
      immich: {running: true, version: '1.135.3', no_key: false,
        stats: {photos, videos, users: [{name: 'Alex', photos: photos - 3094, videos: videos - 362}, {name: 'Sam', photos: 3094, videos: 362}]},
        queues: {}, last_backup: t - 5.4 * 3600},
      nextcloud: {running: true, version: '29.0.8', maintenance: false},
      samba: {running: true},
    };
    return SERVICES.map(s => {
      const list = s.containers.map(n => byName[n] || {name: n, state: 'missing', health: null});
      const d = data[s.type] || null;
      let state = 'ok';
      if (list.some(k => k.state !== 'running')) state = 'error';
      else if (d && !d.running) state = 'error';
      else if (list.some(k => k.health === 'unhealthy') || (d && d.maintenance)) state = 'warning';
      return {...s, state, data: d, containers: list.map(k => ({name: k.name, state: k.state, health: k.health}))};
    });
  }
  function alerts(st) {  // the same rules as alerts() in labmap.py, for the parts this demo has
    const a = [], add = (level, where, text) => a.push({level, where, text});
    const ofContainer = {};
    for (const s of st.services) for (const k of s.containers) ofContainer[k.name] = 'service:' + s.name;
    const r = st.raid;
    if (r.degraded) add('error', 'raid', 'RAID is running on one disk – check the disk enclosure.');
    for (const [name, m, where] of [['RAID', r.space, 'raid'], ['System disk', st.system_disk, 'server']]) {
      if (m.percent > 95) add('error', where, `${name}: ${Math.round(m.percent)}% full.`);
      else if (m.percent > 85) add('warning', where, `${name}: ${Math.round(m.percent)}% full.`);
    }
    const usb = st.power.usb_c;
    if (st.power.charger && usb.power < 40)
      add('warning', 'power', `The ${usb.power} W charger is too weak – 45–65 W is needed. Under load the battery drains.`);
    if (st.temp_30s && st.temp_30s > 85) add('warning', 'server', `The CPU is hot – ${Math.round(st.temp_30s)} °C.`);
    if (st.memory.available / st.memory.total < 0.08) add('warning', 'server', 'Running out of RAM.');
    for (const k of st.containers) {
      const where = ofContainer[k.name] || 'server';
      if (k.state !== 'running') add('error', where, `Container ${k.name} isn't running (${k.state === 'exited' ? 'stopped' : k.state}).`);
      else if (k.health === 'unhealthy') add('warning', where, `Container ${k.name} is failing its health check.`);
    }
    return a;
  }
  function state() {
    catchUp();
    const t = nowS(), conts = containers(t), total = 16.48e9;
    const recent = history.temp.slice(-15).filter(x => x != null).sort((a, b) => a - b);
    const load = [sample.cpu / 100 * 16 * 0.55 + 0.3, 0.9 + rnd(-0.05, 0.05), 0.82 + rnd(-0.03, 0.03)].map(x => round(x, 2));
    const st = {
      time: t, port: 80, ready: true,
      host: {...HOST, uptime: t - BOOTED, load},
      now: {...sample}, history: Object.fromEntries(SERIES.map(k => [k, history[k].slice()])),
      memory: {total, available: Math.round(total - w.mem), swap_total: 4.29e9, swap_free: 4.29e9 - 3.1e7},
      system_disk: {...space(502.3e9, 71.6e9), temp: round(39 + (w.temp - 45) * 0.15, 0)},
      fan: Math.round(w.fan / 10) * 10,
      raid: raid(), power: power(),
      network: {
        ip: {wlan0: '192.168.1.42', eth0: '192.168.10.1', tailscale0: '100.101.102.103'},
        interfaces: {lan: 'eth0', wifi: 'wlan0'},
        wifi: {connected: true, link: {download: 866.7, upload: 780}, quality: 52, signal_dbm: -58 + Math.round(rnd(-2, 2))},
        lan: {connected: true, speed: 1000, clients: [{name: 'desk-pc', ip: '192.168.10.20'}]},
        tailscale: {running: true, name: 'labmap-demo.tail-demo.ts.net', ip: '100.101.102.103', online: 3, devices: 5},
        mdns: true},
      containers: conts, errors: {},
      temp_30s: recent.length >= 5 ? recent[recent.length >> 1] : null,
    };
    st.services = services(t, conts);
    st.alerts = alerts(st);
    return st;
  }

  // --- speed tests ------------------------------------------------------------------------------
  const LINKS = {  // bytes per second: [download from server, upload to server], latency in ms
    lan: {down: 124e6, up: 120e6, ping: 0.6},
    tailscale: {down: 37e6, up: 25e6, ping: 9},
  };
  let via = 'lan';
  function whoami() {
    let o = null;
    try { o = typeof opened !== 'undefined' ? opened : null; } catch (_) {}  // the app's open details (a global let)
    if (o === 'lan' || o === 'tailscale') via = o;  // pretend the visitor is on the link they look at, so both tests work
    return via === 'tailscale' ? {via, name: 'pixel-demo', direct: true, relay: null} : {via};
  }
  function downloadStream(signal) {
    const link = LINKS[via], rate = link.down * rnd(0.92, 0.99), chunk = new Uint8Array(1 << 20);
    const t0 = performance.now();
    let sent = 0, timer = null, stopped = false;
    traffic = {key: via === 'lan' ? 'lan_tx' : 'ts_tx', rate, until: Date.now() + 6000};
    const stop = () => { stopped = true; clearTimeout(timer); if (traffic && traffic.rate === rate) traffic.until = Date.now(); };
    if (signal) signal.addEventListener('abort', stop);
    return new ReadableStream({
      pull(ctrl) {
        return new Promise(done => {
          const elapsed = performance.now() - t0;
          if (stopped || elapsed > 6000) { stop(); try { ctrl.close(); } catch (_) {} return done(); }
          const ramp = elapsed < 300 ? 0.5 : 1;  // a slower start, like TCP
          const due = (sent + chunk.length) / (rate * ramp * rnd(0.9, 1.1)) * 1000 - elapsed;
          timer = setTimeout(() => {
            if (!stopped) { sent += chunk.length; ctrl.enqueue(chunk); }
            done();
          }, Math.max(0, due));
        });
      },
      cancel: stop,
    }, {highWaterMark: 0});
  }
  const internet = {running: false, result: null, error: null};
  function startInternetTest() {
    const fresh = internet.result && nowS() - internet.result.time < 20;
    if (internet.running || fresh) return;
    internet.running = true;
    const ms = rnd(11000, 14000);
    traffic = {key: 'wifi_rx', rate: 52e6, until: Date.now() + ms};
    setTimeout(() => {
      internet.running = false;
      internet.result = {download: rnd(58e6, 66e6), upload: rnd(26e6, 31e6), ping_ms: round(rnd(9, 13)), time: nowS()};
    }, ms);
  }

  // --- the fetch shim -----------------------------------------------------------------------------
  function apiPath(input) {  // "state" for /api/state, also for relative URLs or a page under /labmap/demo/
    let url;
    try { url = new URL(input instanceof Request ? input.url : String(input), location.href); } catch (_) { return null; }
    if (url.origin !== location.origin) return null;
    const m = url.pathname.match(/\/api\/(.+)$/);
    return m ? m[1] : null;
  }
  const json = (data, status = 200) => new Response(JSON.stringify(data), {status, headers: {'Content-Type': 'application/json; charset=utf-8'}});
  function route(path, method, signal) {
    if (path === 'state') return json(state());
    if (path === 'logins') return json({logins: LOGINS});
    if (path === 'test/whoami') return json(whoami());
    if (path === 'test/download') return new Response(downloadStream(signal), {status: 200, headers: {'Content-Type': 'application/octet-stream'}});
    if (path === 'test/internet') {
      if (method === 'POST') startInternetTest();
      return json({...internet, now: nowS()});
    }
    return new Response('Not found.', {status: 404});
  }
  window.fetch = function (input, init) {
    const path = apiPath(input);
    if (path === null) return realFetch(input, init);
    init = init || {};
    const method = String(init.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
    const signal = init.signal || (input instanceof Request ? input.signal : null);
    const latency = path === 'test/whoami' ? LINKS[via].ping * rnd(0.9, 1.4) : rnd(18, 45);
    return new Promise((resolve, reject) => {
      const abort = () => reject(new DOMException('The request was aborted.', 'AbortError'));
      if (signal && signal.aborted) return abort();
      if (signal) signal.addEventListener('abort', abort, {once: true});
      setTimeout(() => {
        if (signal) signal.removeEventListener('abort', abort);
        try { resolve(route(path, method, signal)); } catch (e) { reject(e); }
      }, latency);
    });
  };

  // --- the XMLHttpRequest shim (the upload test) ------------------------------------------------
  class DemoXHR extends RealXHR {
    open(method, url, ...rest) {
      const path = apiPath(url);
      if (path === null) { this._demo = null; return super.open(method, url, ...rest); }
      this._demo = {path, status: 0, readyState: 1, response: '', timer: null};
    }
    setRequestHeader(k, v) { if (!this._demo) super.setRequestHeader(k, v); }
    get status() { return this._demo ? this._demo.status : super.status; }
    get readyState() { return this._demo ? this._demo.readyState : super.readyState; }
    get responseText() { return this._demo ? this._demo.response : super.responseText; }
    get response() { return this._demo ? this._demo.response : super.response; }
    send(body) {
      const d = this._demo;
      if (!d) return super.send(body);
      const finish = (status, text) => {
        d.status = status; d.readyState = 4; d.response = text;
        for (const t of ['readystatechange', 'load', 'loadend']) this.dispatchEvent(new ProgressEvent(t));
      };
      if (d.path !== 'test/upload') return setTimeout(() => finish(404, 'Not found.'), 20);
      const size = body ? (body.size ?? body.byteLength ?? String(body).length) : 0;
      const rate = LINKS[via].up * rnd(0.9, 0.98), t0 = performance.now();
      traffic = {key: via === 'lan' ? 'lan_rx' : 'ts_rx', rate, until: Date.now() + 5000};
      const tick = () => {
        const loaded = Math.min(size, rate * (performance.now() - t0) / 1000);
        this.upload.dispatchEvent(new ProgressEvent('progress', {lengthComputable: true, loaded, total: size}));
        if (loaded >= size) finish(200, JSON.stringify({received: size}));
        else d.timer = setTimeout(tick, 60);
      };
      d.timer = setTimeout(tick, 30);
    }
    abort() {
      const d = this._demo;
      if (!d) return super.abort();
      clearTimeout(d.timer);
      d.status = 0; d.readyState = 4;
      for (const t of ['abort', 'loadend']) this.dispatchEvent(new ProgressEvent(t));
    }
  }
  window.XMLHttpRequest = DemoXHR;

  // --- the demo badge, the failure switch and dead links ---------------------------------------
  const CSS = `
#demo-badge { position: fixed; z-index: 20; left: 12px; bottom: 12px; display: flex; align-items: center; gap: 2px; padding: 3px;
  border-radius: 999px; border: 1px solid var(--line-2, #cdd2da); background: var(--surface, #fff); color: var(--ink-2, #4a5160);
  font: 600 12.5px/1 var(--sans, sans-serif); box-shadow: 0 6px 24px -10px rgba(0, 0, 0, .35); max-width: calc(100vw - 24px); }
#demo-badge > * { display: inline-flex; align-items: center; gap: 7px; padding: 6px 10px; border-radius: 999px; white-space: nowrap; }
#demo-badge .demo-label { color: var(--ink, #0f1218); }
#demo-badge .demo-label i { width: 7px; height: 7px; border-radius: 50%; background: var(--accent, #2a78d6); }
#demo-badge a, #demo-badge button { color: inherit; text-decoration: none; font: inherit; border: 0; background: none; cursor: pointer; }
#demo-badge a:hover, #demo-badge button:hover { background: var(--surface-2, #f5f6f8); color: var(--ink, #0f1218); }
#demo-badge a:focus-visible, #demo-badge button:focus-visible { outline: 2px solid var(--accent, #2a78d6); outline-offset: 1px; }
#demo-badge .switch { width: 26px; height: 15px; border-radius: 999px; background: var(--line-2, #cdd2da); position: relative; flex: none; transition: background .2s; }
#demo-badge .switch::after { content: ""; position: absolute; top: 2px; left: 2px; width: 11px; height: 11px; border-radius: 50%; background: #fff; transition: transform .2s; }
#demo-badge button[aria-pressed="true"] { color: var(--crit-ink, #b02a2a); }
#demo-badge button[aria-pressed="true"] .switch { background: var(--crit, #d03b3b); }
#demo-badge button[aria-pressed="true"] .switch::after { transform: translateX(11px); }
#demo-toast { position: fixed; z-index: 45; left: 50%; bottom: 64px; transform: translateX(-50%); max-width: calc(100vw - 32px); padding: 8px 14px;
  border-radius: 8px; background: var(--ink, #0f1218); color: var(--surface, #fff); font: 600 13px/1.4 var(--sans, sans-serif);
  box-shadow: 0 8px 24px -8px rgba(0, 0, 0, .4); pointer-events: none; opacity: 0; transition: opacity .2s; text-align: center; }
#demo-toast.show { opacity: 1; }
html:not(.demo-embedded) body { padding-bottom: 52px; }  /* room for the badge; embedded, the frame must not scroll */
@media (max-width: 600px) { #demo-badge { font-size: 12px; } #demo-badge > * { padding: 6px 8px; } #demo-badge .wide { display: none; } }
`;
  let toastTimer = null;
  function toast(text) {
    const t = document.getElementById('demo-toast');
    t.textContent = text;
    t.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove('show'), 2600);
  }
  function setFailure(v) {
    if (failure && !v) jellyfinSince = nowS();  // "restarted" just now
    failure = v;
    const b = document.getElementById('demo-failure');
    b.setAttribute('aria-pressed', String(v));
    b.title = v ? 'Bring the disk and the app back' : 'Drop a RAID disk and stop a container, to see how problems look';
    if (v) toast('A RAID disk failed and Jellyfin stopped – click the red parts.');
    try { if (typeof refresh === 'function') setTimeout(refresh, 50); } catch (_) {}  // the app's own refresh, not 3 s later
  }
  function badge() {
    const style = document.createElement('style');
    style.textContent = CSS;
    document.head.append(style);
    if (window.self !== window.top) document.documentElement.classList.add('demo-embedded');
    const e = document.createElement('div');
    e.id = 'demo-badge';
    e.setAttribute('role', 'note');
    e.innerHTML = '<span class="demo-label" title="This page runs in your browser with made-up data. No real server is involved."><i></i><span>Demo<span class="wide"> · fake data</span></span></span>'
      + '<button type="button" id="demo-failure" aria-pressed="false"><span class="switch" aria-hidden="true"></span>Simulate a failure</button>'
      + `<a href="${REPO}" target="_blank" rel="noopener">GitHub ↗</a>`;
    document.body.append(e);
    const t = document.createElement('div');
    t.id = 'demo-toast';
    t.setAttribute('role', 'status');
    document.body.append(t);
    document.getElementById('demo-failure').addEventListener('click', () => setFailure(!failure));
    setFailure(failure);
    if (!failure) toast('Demo with fake data – hover and click anything.');
    // links to the apps and to the server point at a machine that doesn't exist here
    document.addEventListener('click', ev => {
      const a = ev.target.closest && ev.target.closest('#map a[href], #details a[href]');
      if (!a) return;
      const url = new URL(a.href, location.href);
      if (url.origin === location.origin && /^https?:$/.test(url.protocol)) return;
      ev.preventDefault();
      toast('In a real install this opens ' + (url.protocol === 'smb:' ? 'the network share' : url.host.replace(location.hostname, HOST.name)) + '.');
    }, true);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', badge);
  else badge();
})();
