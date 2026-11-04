#!/usr/bin/env python3
"""Records the README screenshots and GIFs from the live demo (website/demo/demo.js), so they always show the
current app with demo data. Drives headless Google Chrome over the DevTools protocol.

    pip install websockets pillow        # plus ffmpeg on PATH and Google Chrome installed
    python3 tools/screenshots.py         # writes docs/*.png, docs/*.gif, logo/*.png and the copies in website/assets

Random values in the demo are seeded, so a run gives the same numbers every time.
"""
import asyncio, base64, contextlib, functools, http.server, io, json, os, shutil, socket, subprocess, sys, tempfile, threading, time
import urllib.request

import websockets
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
DOCS, LOGO, ASSETS = (os.path.join(ROOT, p) for p in ("docs", "logo", "website/assets"))

# Runs before the page's own scripts: a seeded Math.random and no demo badge (it's for visitors, not screenshots).
PRELUDE = """
(() => {
  let a = 1234567;
  Math.random = () => { a |= 0; a = a + 0x6D2B79F5 | 0; let t = Math.imul(a ^ a >>> 15, 1 | a);
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; };
  document.addEventListener('DOMContentLoaded', () => {
    const s = document.createElement('style'); s.textContent = '#demo-badge, #demo-toast { display: none !important; }';
    document.head.append(s);
  });
})();
"""


class CDP:
    def __init__(self, ws):
        self.ws, self.next_id, self.pending = ws, 0, {}
        self.reader = asyncio.ensure_future(self._read())

    async def _read(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            f = self.pending.pop(msg.get("id"), None)
            if f and not f.done():
                f.set_exception(RuntimeError(msg["error"])) if "error" in msg else f.set_result(msg.get("result", {}))

    async def __call__(self, method, **params):
        self.next_id += 1
        f = asyncio.get_running_loop().create_future()
        self.pending[self.next_id] = f
        await self.ws.send(json.dumps({"id": self.next_id, "method": method, "params": params}))
        return await f


class Page:
    def __init__(self, cdp):
        self.cdp, self.mouse = cdp, None

    async def setup(self, width, height, scale=1, dark=False, mobile=False):
        await self.cdp("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=scale, mobile=mobile)
        await self.cdp("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "dark" if dark else "light"}])
        self.size = (width, height)

    async def open(self, url, settle=4.5):
        self.mouse = None
        await self.cdp("Page.navigate", url=url)
        for _ in range(100):
            await asyncio.sleep(0.1)
            if await self.js("document.readyState === 'complete' && !!document.querySelector('#map svg')"):
                break
        await asyncio.sleep(settle)

    async def js(self, expr):
        r = await self.cdp("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")

    async def center(self, selector):
        """the middle of an element; "selector:last" takes the last match (the Wi-Fi box, not the Internet pill)"""
        last = selector.endswith(":last")
        pick = f"[...document.querySelectorAll({json.dumps(selector[:-5])})].pop()" if last else f"document.querySelector({json.dumps(selector)})"
        r = await self.js(f"(() => {{ const e = {pick}; if (!e) return null;"
                          f" const b = e.getBoundingClientRect(); return [b.x + b.width / 2, b.y + b.height / 2]; }})()")
        if not r:
            raise RuntimeError("not on the page: " + selector)
        return r

    async def move(self, target, duration=0.6, steps=None):
        x1, y1 = await self.center(target) if isinstance(target, str) else target
        x0, y0 = self.mouse or (self.size[0] * 0.6, self.size[1] * 0.9)
        n = steps or max(1, int(duration / 0.03))
        for i in range(1, n + 1):
            t = i / n
            t = t * t * (3 - 2 * t)  # ease in and out
            self.mouse = (x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)
            await self.cdp("Input.dispatchMouseEvent", type="mouseMoved", x=self.mouse[0], y=self.mouse[1], pointerType="mouse")
            await asyncio.sleep(duration / n)

    async def click(self, target=None, duration=0.6):
        if target is not None:
            await self.move(target, duration)
        x, y = self.mouse
        for kind in ("mousePressed", "mouseReleased"):
            await self.cdp("Input.dispatchMouseEvent", type=kind, x=x, y=y, button="left", clickCount=1, pointerType="mouse")
        await asyncio.sleep(0.05)

    async def shot(self, path, full_height=False):
        r = await self.cdp("Page.captureScreenshot", format="png", captureBeyondViewport=full_height)
        with open(path, "wb") as f:
            f.write(base64.b64decode(r["data"]))
        print("wrote", os.path.relpath(path, ROOT))


def cursor(img, x, y, s):
    """the macOS-style arrow, drawn into a GIF frame (headless Chrome has no visible cursor)"""
    pts = [(0, 0), (0, 16.5), (4.2, 12.6), (6.9, 18.6), (9.3, 17.6), (6.7, 11.8), (12.2, 11.8)]
    d = ImageDraw.Draw(img)
    d.polygon([(x + px * s, y + py * s) for px, py in pts], fill=(0, 0, 0), outline=(255, 255, 255), width=max(1, round(1.4 * s)))


async def record(page, path, script, width_out=1000, fps=14):
    """runs script(page) while taking frames, then writes a GIF of width width_out with a cursor drawn in"""
    frames, done = [], asyncio.Event()

    async def grab():
        while not done.is_set():
            t = time.monotonic()
            r = await page.cdp("Page.captureScreenshot", format="png")  # not JPEG: its noise breaks the GIF's frame diffs
            frames.append((t, page.mouse, base64.b64decode(r["data"])))
            await asyncio.sleep(max(0, 1 / fps - (time.monotonic() - t)))

    task = asyncio.ensure_future(grab())
    try:
        await script(page)
    finally:
        done.set()
        await task
    tmp = tempfile.mkdtemp()
    lines = []
    for i, (t, mouse, data) in enumerate(frames):
        img = Image.open(io.BytesIO(data)).convert("RGB")
        k = width_out / img.width
        img = img.resize((width_out, round(img.height * k)), Image.LANCZOS)
        if mouse:
            cursor(img, mouse[0] * k, mouse[1] * k, 1.0)
        name = os.path.join(tmp, f"{i:05d}.png")
        img.save(name)
        nxt = frames[i + 1][0] if i + 1 < len(frames) else t + 1.5
        lines += [f"file '{name}'", f"duration {max(0.02, nxt - t):.3f}"]
    lines.append(f"file '{name}'")
    with open(os.path.join(tmp, "list.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", os.path.join(tmp, "list.txt"),
                    "-vf", "fps=14,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle",
                    "-loop", "0", path], check=True)
    shutil.rmtree(tmp)
    print("wrote", os.path.relpath(path, ROOT), f"({len(frames)} frames, {os.path.getsize(path) // 1024} kB)")


# --- the scenes -----------------------------------------------------------------------------------------------

async def stills(page, demo):
    await page.setup(1360, 800, 2)
    await page.open(demo)
    await page.shot(os.path.join(DOCS, "map-light.png"))
    await page.setup(1360, 800, 2, dark=True)
    await page.open(demo)
    await page.shot(os.path.join(DOCS, "map-dark.png"))
    await page.setup(1360, 800, 2)
    await page.open(demo + "?failure")
    await page.shot(os.path.join(DOCS, "failure.png"))
    await page.click('[data-key="raid"]', 0.01)
    await asyncio.sleep(1)
    await page.shot(os.path.join(DOCS, "failure-raid.png"))
    await page.setup(1360, 900, 2)
    await page.open(demo)
    await page.click('[data-key="server"]', 0.01)
    await asyncio.sleep(1)
    await page.shot(os.path.join(DOCS, "details-server.png"))
    await page.setup(1360, 900, 2, dark=True)
    await page.open(demo)
    await page.click('[data-key="wifi"]:last', 0.01)
    await asyncio.sleep(0.5)
    await page.click("#d-test .d-button", 0.01)
    await asyncio.sleep(16)
    await page.move((1360 * 0.3, 860), 0.01)  # off the panel, so no hover state
    await asyncio.sleep(0.5)
    await page.shot(os.path.join(DOCS, "speed-test.png"))
    await page.setup(390, 865, 3, mobile=True)
    await page.open(demo)
    await page.shot(os.path.join(DOCS, "phone.png"))
    await page.setup(390, 844, 3, dark=True, mobile=True)
    await page.open(demo)
    await page.click('[data-key="service:Immich"]', 0.01)
    await asyncio.sleep(1)
    await page.shot(os.path.join(DOCS, "phone-immich.png"))


async def gif_map(p):
    await asyncio.sleep(0.8)
    await p.move('[data-key="raid"]', 0.9); await asyncio.sleep(1.2)
    await p.move('[data-key="power"]', 0.9); await asyncio.sleep(1.2)
    await p.move('[data-key="service:Immich"]', 0.9); await asyncio.sleep(0.8)
    await p.click(); await asyncio.sleep(2.6)
    await p.click("#d-close", 0.8); await asyncio.sleep(0.8)
    await p.click('[data-key="power"]', 0.9); await asyncio.sleep(2.6)
    await p.click("#d-close", 0.8); await asyncio.sleep(1.2)


async def gif_privacy(p):
    await asyncio.sleep(0.8)
    await p.click('[data-key="lan"]:last', 0.9); await asyncio.sleep(2)
    await p.click("#d-close", 0.8); await asyncio.sleep(0.6)
    await p.click("#privacy", 0.8); await asyncio.sleep(1.6)
    await p.click('[data-key="lan"]:last', 0.9); await asyncio.sleep(2)
    await p.click("#d-close", 0.8); await asyncio.sleep(0.6)
    await p.click("#privacy", 0.8); await asyncio.sleep(1.4)


async def gif_speed(p):
    await asyncio.sleep(0.8)
    await p.move('[data-key="wifi"]:last', 0.9); await asyncio.sleep(0.6)
    await p.click(); await asyncio.sleep(1)
    await p.click("#d-test .d-button", 0.9)
    for _ in range(80):  # until the server's internet test has a result
        await asyncio.sleep(0.25)
        if await p.js("!!document.querySelector('#d-test') && !/testing/i.test(document.querySelector('#d-test').textContent)"):
            break
    await asyncio.sleep(2.5)


async def gifs(page, demo):
    for name, script, dark in (("map", gif_map, False), ("privacy", gif_privacy, True), ("speed-test", gif_speed, False)):
        await page.setup(1360, 808, 1, dark=dark)
        await page.open(demo)
        await record(page, os.path.join(DOCS, name + ".gif"), script)


async def brand(page, site):
    await page.setup(1280, 640, 1)
    await page.cdp("Page.navigate", url=site + "logo/social-preview.html"); await asyncio.sleep(3)
    await page.shot(os.path.join(LOGO, "labmap-social-preview.png"))
    h = None
    await page.setup(1280, 800, 2)
    await page.cdp("Page.navigate", url=site + "logo/brand.html"); await asyncio.sleep(3)
    h = await page.js("Math.ceil(document.documentElement.scrollHeight)")
    await page.setup(1280, h, 2)
    await asyncio.sleep(0.5)
    await page.shot(os.path.join(LOGO, "brand.png"))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def main(only):
    tmp = tempfile.mkdtemp()
    site = os.path.join(tmp, "labmap")
    subprocess.run(["sh", os.path.join(ROOT, "website/build.sh"), site], check=True, stdout=subprocess.DEVNULL)
    # the brand pages next to the demo, with the docs/ screenshot they use
    shutil.copytree(LOGO, os.path.join(site, "logo"))
    shutil.copytree(DOCS, os.path.join(site, "docs"))
    port = free_port()
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a): pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), functools.partial(Quiet, directory=tmp))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://labmap-demo:{port}/labmap/"  # the app builds its links from the host name: labmap-demo:2283

    dport = free_port()
    chrome = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={dport}", f"--user-data-dir={tmp}/chrome",
                               "--hide-scrollbars", "--host-resolver-rules=MAP labmap-demo 127.0.0.1", "--force-color-profile=srgb", "--no-first-run", "--mute-audio", "about:blank"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            with contextlib.suppress(OSError):
                targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{dport}/json/list"))
                ws_url = next(t["webSocketDebuggerUrl"] for t in targets if t["type"] == "page")
                break
            time.sleep(0.1)
        async with websockets.connect(ws_url, max_size=64 * 2**20) as ws:
            cdp = CDP(ws)
            await cdp("Page.enable")
            await cdp("Page.addScriptToEvaluateOnNewDocument", source=PRELUDE)
            page = Page(cdp)
            if "stills" in only: await stills(page, base + "demo/")
            if "gifs" in only: await gifs(page, base + "demo/")
            if "brand" in only:
                # social-preview.html uses docs/map-dark.png: copy the new one in first
                shutil.copy(os.path.join(DOCS, "map-dark.png"), os.path.join(site, "docs", "map-dark.png"))
                await brand(page, base)
    finally:
        chrome.terminate()
        server.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
    # the landing page uses copies of some of these
    for f in ("map.gif", "privacy.gif", "speed-test.gif", "failure.png"):
        shutil.copy(os.path.join(DOCS, f), os.path.join(ASSETS, f))
    shutil.copy(os.path.join(LOGO, "labmap-social-preview.png"), os.path.join(ASSETS, "labmap-social-preview.png"))


if __name__ == "__main__":
    asyncio.run(main(set(sys.argv[1:]) or {"stills", "gifs", "brand"}))
