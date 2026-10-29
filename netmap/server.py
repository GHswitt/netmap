"""Poll the routers in the background and serve the map over HTTPS with basic auth."""
import base64
import hmac
import json
import logging
import mimetypes
import shutil
import ssl
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from . import model, tags
from .certs import CertWatch
from .config import MAC
from .history import RANGES, History
from .collector import RouterClient
from .oui import Oui, is_private
from .store import Store, forgettable, held_by
from .traffic import Traffic

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


class Poller:
    """Collects, builds and publishes the snapshot every poll_interval seconds."""

    def __init__(self, cfg, replay=None):
        self.cfg = cfg
        self.replay = replay
        cfg["data_dir"].mkdir(parents=True, exist_ok=True)
        self.store = Store(cfg["data_dir"] / "netmap.db")
        self.oui = Oui(cfg["data_dir"])
        self.clients = {rc["host"]: RouterClient(rc) for rc in cfg["routers"]}
        self.traffic = Traffic(cfg)
        self.history = History(cfg["data_dir"] / "history.db")
        self.certs = CertWatch(cfg)
        self.forgot_at = 0.0
        self.router_macs = {}  # mac -> router id, from every successful poll
        self.pool = ThreadPoolExecutor(max_workers=len(self.clients))
        self.listeners = [log_event]  # callables(event, device); MQTT/Home Assistant hooks in here later
        self.snapshot = b"{}"
        self.snap = None              # the dict behind self.snapshot, for in-place updates (renames)
        self.lock = threading.Lock()  # building a snapshot vs. renaming

    def collect(self):
        if self.replay is not None:
            return self.replay, {}
        raws, errors = {}, {}
        futures = {host: self.pool.submit(c.fetch) for host, c in self.clients.items()}
        for host, f in futures.items():
            try:
                raws[host] = f.result()
            except Exception as e:
                errors[host] = f"{type(e).__name__}: {e}"
                log.warning("%s: %s", host, errors[host])
        return raws, errors

    def poll_once(self):
        raws, errors = self.collect()
        with self.lock:
            events, snap = self.build(raws, errors)
        by_mac = {d["mac"]: d for d in snap["devices"]}
        for ev in events:
            for listener in self.listeners:
                try:
                    listener(ev, by_mac.get(ev["mac"]))
                except Exception:
                    log.exception("event listener failed")

    def build(self, raws, errors):
        snap = model.build(self.cfg, raws, errors, self.router_macs)
        self.remember_router_macs(snap.pop("router_macs"))
        events = self.store.merge(snap, self.cfg["offline_after"])
        for d in snap["devices"]:
            d["vendor"] = self.oui.vendor(d["mac"])
            d["private"] = is_private(d["mac"])
            if not d["name"]:
                prefix = d["vendor"] or ("private" if d["private"] else "unknown")
                d["name"], d["name_source"] = f"{prefix} {d['mac'][-8:]}", "vendor + MAC"
        self.apply_names(snap)
        for d in snap["devices"]:
            d["held_by"] = held_by(d)
            d["forgettable"] = not d["online"] and not d["held_by"]
            d.pop("stored_only", None)
        self.traffic.annotate(snap, raws, time.time())
        tags.apply(self.cfg["tags"], snap["devices"],
                   {g["id"]: g for g in snap["groups"]}, {r["id"]: r for r in snap["routers"]})
        now = time.time()
        self.history.record(snap, now)
        self.forget_stale(snap, now)
        snap.update(generated=now, poll_interval=self.cfg["poll_interval"], colors=self.cfg["colors"],
                    tags=[{"name": t["name"], "color": t["color"]} for t in self.cfg["tags"]],
                    certs=self.certs.results(), ca=bool(self.cfg.get("ca_file")))
        self.snap, self.snapshot = snap, json.dumps(snap).encode()
        return events, snap

    def rename(self, mac, name):
        """Store a UI name (empty = remove) and show it in the published snapshot right away,
        instead of only after the next poll. Returns the device's resulting name and source."""
        with self.lock:
            self.store.set_name(mac, name)
            d = next((x for x in (self.snap or {}).get("devices", []) if x["mac"] == mac), None)
            if d is None:
                return name, "netmap (UI)" if name else None
            if name:
                d["name"], d["name_source"] = name, "netmap (UI)"
            elif mac in self.cfg["names"]:
                d["name"], d["name_source"] = self.cfg["names"][mac], "netmap (config)"
            else:
                d["name"], d["name_source"] = d["auto_name"], d["auto_name_source"]
            self.snapshot = json.dumps(self.snap).encode()
            return d["name"], d["name_source"]

    def forget(self, mac):
        """Delete an offline device that no router table knows any more: the stored device, a name
        given in netmap and its history. Returns None, or why it can't be forgotten."""
        with self.lock:
            d = next((x for x in (self.snap or {}).get("devices", []) if x["mac"] == mac), None)
            if d is None:
                return "unknown device"
            if not d.get("forgettable"):
                return "still known to the routers: " + ", ".join(d.get("held_by") or ["online"])
            self.store.forget([mac])
            self.store.set_name(mac, "")
            self.history.forget(["d:" + mac])
            self.snap["devices"] = [x for x in self.snap["devices"] if x["mac"] != mac]
            self.snapshot = json.dumps(self.snap).encode()
        log.info("forgot %s (%s)", d["name"], mac)
        return None

    def apply_names(self, snap):
        """Names given in netmap win: web UI first, then names: in config.yaml. The automatic name
        stays available as auto_name (shown when editing)."""
        ui, cfg = self.store.names(), self.cfg["names"]
        for d in snap["devices"]:
            d["auto_name"], d["auto_name_source"] = d["name"], d["name_source"]
            if d["mac"] in ui:
                d["name"], d["name_source"] = ui[d["mac"]], "netmap (UI)"
            elif d["mac"] in cfg:
                d["name"], d["name_source"] = cfg[d["mac"]], "netmap (config)"

    def remember_router_macs(self, owner):
        """Keep every router MAC ever seen, and drop store/history entries for them: they were
        recorded as devices while their router was unreachable."""
        new = {m: r for m, r in owner.items() if self.router_macs.get(m) != r}
        if not new:
            return
        self.router_macs.update(new)
        self.store.forget(list(new))
        self.history.forget(["d:" + m for m in new])

    def forget_stale(self, snap, now):
        """Hourly: drop random-MAC and guest devices not seen for forget_private_after_days."""
        days = self.cfg["forget_private_after_days"]
        if not days or now - self.forgot_at < 3600:
            return
        self.forgot_at = now
        gone = [d["mac"] for d in snap["devices"] if forgettable(d, now - days * 86400)]
        if gone:
            self.store.forget(gone)
            self.history.forget(["d:" + m for m in gone])
            snap["devices"] = [d for d in snap["devices"] if d["mac"] not in set(gone)]
            log.info("forgot %d random-MAC/guest devices not seen for %d days", len(gone), days)

    def run(self):
        self.oui.refresh_in_background()
        self.certs.start()
        while True:
            started = time.monotonic()
            try:
                self.poll_once()
            except Exception:
                log.exception("poll failed")
            time.sleep(max(1.0, self.cfg["poll_interval"] - (time.monotonic() - started)))


def log_event(ev, device):
    name = (device or {}).get("name") or ev["mac"]
    log.info("%s: %s (%s)", ev["type"], name, ev["mac"])


class Handler(BaseHTTPRequestHandler):
    server_version = "netmap"

    def do_GET(self):
        if not self.authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="netmap"')
            self.end_headers()
            return
        path, _, query = self.path.partition("?")
        path = path.lstrip("/") or "index.html"
        if path == "api/snapshot":
            return self.reply(200, self.server.poller.snapshot, "application/json")
        if path == "api/history":
            q = {k: v[0] for k, v in parse_qs(query).items()}
            if not q.get("id") or q.get("range") not in RANGES:
                return self.reply(400, b'{"error": "need id and range (1h, 24h, 7d, 30d, 1y)"}', "application/json")
            data = self.server.poller.history.query(q["id"], q["range"], time.time())
            return self.reply(200, json.dumps(data).encode(), "application/json")
        if path == "ca.crt":
            ca = self.server.poller.cfg.get("ca_file")
            if not ca:
                return self.reply(404, b"no ca_file configured", "text/plain")
            return self.reply(200, Path(ca).read_bytes(), "application/x-x509-ca-cert",
                              {"Content-Disposition": 'attachment; filename="home-ca.crt"'})
        file = (STATIC / path).resolve()
        if STATIC.resolve() not in file.parents or not file.is_file():
            return self.reply(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        self.reply(200, file.read_bytes(), ctype)

    def do_POST(self):
        if not self.authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="netmap"')
            self.end_headers()
            return
        # A custom header can't be sent cross-site without a CORS preflight (which netmap never
        # allows), so another website can't make the logged-in browser rename or forget devices.
        if self.headers.get("X-Netmap") != "1":
            return self.reply(403, b'{"error": "missing X-Netmap header"}', "application/json")
        route = self.path.split("?")[0]
        if route not in ("/api/name", "/api/forget"):
            return self.reply(404, b'{"error": "not found"}', "application/json")
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 2048:
                raise ValueError("too long")
            body = json.loads(self.rfile.read(length) or b"{}")
            mac = str(body.get("mac", "")).upper()
            name = " ".join(str(body.get("name") or "").split())[:64]
            if not MAC.match(mac):
                raise ValueError("bad mac")
        except (ValueError, json.JSONDecodeError) as e:
            return self.reply(400, json.dumps({"error": str(e)}).encode(), "application/json")
        if route == "/api/forget":
            why = self.server.poller.forget(mac)
            if why:
                return self.reply(409, json.dumps({"error": why}).encode(), "application/json")
            return self.reply(200, json.dumps({"mac": mac, "forgotten": True}).encode(), "application/json")
        final, source = self.server.poller.rename(mac, name)
        log.info("name for %s %s", mac, f"set to {name!r}" if name else "cleared")
        return self.reply(200, json.dumps({"mac": mac, "name": final, "name_source": source}).encode(),
                          "application/json")

    def authorized(self):
        s = self.server.settings
        if not s["password"]:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
        except ValueError:
            return False
        ok_user = hmac.compare_digest(user.encode(), s["username"].encode())
        ok_pw = hmac.compare_digest(pw.encode(), s["password"].encode())
        return ok_user and ok_pw

    def reply(self, code, body, ctype, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("text", "application/j")) else ""))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        """Clients that hang up early (certificate checks, closed tabs) are normal, not a traceback."""
        if isinstance(sys.exc_info()[1], (ConnectionError, ssl.SSLError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def ensure_cert(data_dir, names):
    """Self-signed certificate for the web UI, created once with the openssl CLI."""
    crt, key = data_dir / "server.crt", data_dir / "server.key"
    if crt.exists() and key.exists():
        return crt, key
    if not shutil.which("openssl"):
        raise RuntimeError("openssl not found: install it, or set server.tls: false")
    san = ",".join(("IP:" if n.replace(".", "").isdigit() else "DNS:") + n for n in names)
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
        "-subj", f"/CN={names[0]}", "-addext", f"subjectAltName={san}",
        "-keyout", str(key), "-out", str(crt),
    ], check=True, capture_output=True)
    key.chmod(0o600)
    log.info("created self-signed certificate for %s", ", ".join(names))
    return crt, key


def serve(cfg, replay=None):
    s = cfg["server"]
    poller = Poller(cfg, replay)
    httpd = Server((s["bind"], s["port"]), Handler)
    httpd.poller, httpd.settings = poller, s
    scheme = "http"
    if s["tls"]:
        crt, key = ensure_cert(cfg["data_dir"], s["cert_names"] or [s["bind"]])
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(crt, key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"
    if not s["password"]:
        log.warning("server.password is empty: the UI is open to anyone who can reach it")
    threading.Thread(target=poller.run, name="poller", daemon=True).start()
    log.info("serving on %s://%s:%d/", scheme, s["bind"], s["port"])
    httpd.serve_forever()
