"""Take the README screenshots with a headless Chromium/Edge through the Chrome DevTools protocol.

  python tools/screenshots.py URL OUTDIR --device NAME --port GROUP_ID [--browser PATH]

URL is a netmap UI, normally the anonymizing proxy (tools/demo_proxy.py). --device picks the wifi
device for the details and chart views (by its shown name), --port the port for the port view
(group id, e.g. "192.168.88.1|ether4"). Needs `pip install websockets`.
"""
import argparse
import asyncio
import base64
import json
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

SHORT_LIST = {"listH": 230, "hidden": False}
SHOTS = [
    {"name": "overview", "prefs": {"theme": "light"}, "pane": SHORT_LIST},
    {"name": "overview-dark", "prefs": {"theme": "dark"}, "pane": SHORT_LIST},
    {"name": "traffic", "prefs": {"theme": "light", "traffic": True}, "pane": SHORT_LIST},
    {"name": "filter-iot", "prefs": {"theme": "light"}, "filter": {"off": [], "solo": "tag:IoT"}, "pane": SHORT_LIST},
    {"name": "device", "prefs": {"theme": "light", "histRange": "24h"}, "device": True,
     "pane": {"listH": None, "hidden": True}},
    {"name": "chart", "prefs": {"theme": "light", "histRange": "24h"}, "device": True, "zoom": True},
    {"name": "port", "prefs": {"theme": "light", "traffic": True}, "port": True},
    {"name": "list", "prefs": {"theme": "light", "traffic": True}, "sort": {"key": "traffic", "dir": 1},
     "pane": {"listH": 620, "hidden": False}},
]
BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "microsoft-edge", "chromium", "chromium-browser", "google-chrome",
]


def find_browser():
    for b in BROWSERS:
        if Path(b).exists() or shutil.which(b):
            return b
    raise SystemExit("no Chromium-based browser found; pass --browser")


async def run(a):
    url = a.url.rstrip("/") + "/"
    snap = json.load(urllib.request.urlopen(url + "api/snapshot"))
    device = next((d for d in snap["devices"] if d["name"] == a.device), None)
    if a.device and not device:
        raise SystemExit(f"no device named {a.device!r}")
    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    profile = tempfile.mkdtemp(prefix="netmap-shots-")
    proc = subprocess.Popen([a.browser or find_browser(), "--headless=new", "--disable-gpu",
                             f"--remote-debugging-port={a.debug_port}", f"--user-data-dir={profile}",
                             "--hide-scrollbars", "about:blank"])
    try:
        for _ in range(50):
            try:
                targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{a.debug_port}/json"))
                page = next(t for t in targets if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.2)
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=None) as ws:
            n = 0

            async def cmd(method, **params):
                nonlocal n
                n += 1
                await ws.send(json.dumps({"id": n, "method": method, "params": params}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == n:
                        if "error" in msg:
                            raise RuntimeError(f"{method}: {msg['error']}")
                        return msg.get("result", {})

            async def js(expr):
                r = await cmd("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
                return r.get("result", {}).get("value")

            async def until(expr, timeout=20):
                end = time.monotonic() + timeout
                while time.monotonic() < end:
                    if await js(expr):
                        return
                    await asyncio.sleep(0.25)
                raise RuntimeError(f"timeout waiting for {expr}")

            await cmd("Emulation.setDeviceMetricsOverride", width=a.width, height=a.height,
                      deviceScaleFactor=a.scale, mobile=False)
            await cmd("Page.navigate", url=url)
            await until("document.readyState === 'complete'")
            for shot in SHOTS:
                prefs = {"maxAge": "7", "traffic": False, "histRange": "24h", "theme": "light", **shot["prefs"]}
                store = {"netmap.prefs": prefs, "netmap.filter": shot.get("filter", {"off": [], "solo": None}),
                         "netmap.sort": shot.get("sort", {"key": "status", "dir": 1}),
                         "netmap.pane": shot.get("pane", {"listH": None, "hidden": False}),
                         "netmap.collapsed": {}}
                await js("localStorage.clear();" + "".join(
                    f"localStorage.setItem({json.dumps(k)}, {json.dumps(json.dumps(v))});" for k, v in store.items()))
                await cmd("Page.reload", ignoreCache=True)
                await until("typeof state !== 'undefined' && !!state.snap && !!cy && cy.nodes().length > 0")
                await asyncio.sleep(2)
                if shot.get("device"):
                    await js(f"select({{type: 'device', id: {json.dumps(device['mac'])}}}, true); 1")
                    await until("!!document.querySelector('#details .charts svg')")
                    await js("document.querySelector('#details .charts').scrollIntoView({block: 'end'}); 1")
                    await asyncio.sleep(1.5)
                if shot.get("zoom"):
                    await js("document.querySelector('#details .chart-zoom').click(); 1")
                    await until("!!document.querySelector('#chartModal .charts svg')")
                    await asyncio.sleep(1.5)
                if shot.get("port"):
                    await js(f"select({{type: 'group', id: {json.dumps(a.port)}}}, true); 1")
                    await asyncio.sleep(1.5)
                r = await cmd("Page.captureScreenshot", format=a.format, **({"quality": 90} if a.format != "png" else {}))
                path = out / f"{shot['name']}.{a.format}"
                path.write_bytes(base64.b64decode(r["data"]))
                print(f"{path} ({path.stat().st_size // 1024} KB)")
    finally:
        proc.terminate()
        shutil.rmtree(profile, ignore_errors=True)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("url")
    p.add_argument("outdir")
    p.add_argument("--device", required=True)
    p.add_argument("--port", required=True)
    p.add_argument("--browser")
    p.add_argument("--width", type=int, default=1440)
    p.add_argument("--height", type=int, default=900)
    p.add_argument("--scale", type=float, default=2)
    p.add_argument("--format", choices=["png", "webp", "jpeg"], default="png")
    p.add_argument("--debug-port", type=int, default=9341)
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
