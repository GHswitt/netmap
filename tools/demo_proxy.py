"""Anonymizing proxy in front of a running netmap, for screenshots and demos.

The browser talks to the proxy; the proxy fetches from netmap and replaces names, MACs, IPs, SSIDs
and comments in every API response, using a mapping file you review first. The UI files come from this
checkout (netmap/static); renaming (POST) is refused.

  python tools/demo_proxy.py -c config.yaml init  MAPPING   draft a mapping from the live data
  python tools/demo_proxy.py -c config.yaml serve MAPPING [--port 8099] [--log DIR]
  python tools/demo_proxy.py -c config.yaml check MAPPING DIR   leak check of the logged responses

config.yaml is netmap's config (for the address, login and CA). The mapping holds your real names:
keep it out of any public repository.
"""
import argparse
import base64
import json
import mimetypes
import random
import re
import ssl
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from netmap import config as netmap_config  # noqa: E402

STATIC = Path(__file__).resolve().parent.parent / "netmap" / "static"
MAC = re.compile(r"\b[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}\b")
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


# ---------- talking to netmap ----------

class Upstream:
    def __init__(self, cfg):
        s = cfg["server"]
        scheme = "https" if s["tls"] else "http"
        self.base = f"{scheme}://{s['bind']}:{s['port']}"
        self.auth = "Basic " + base64.b64encode(f"{s['username']}:{s['password']}".encode()).decode()
        if cfg.get("ca_file"):
            self.ctx = ssl.create_default_context(cafile=str(cfg["ca_file"]))
            self.ctx.check_hostname = False   # the config has the IP, the certificate may have names
        else:
            self.ctx = ssl._create_unverified_context()  # netmap's own self-signed certificate

    def get(self, path):
        req = urllib.request.Request(self.base + path, headers={"Authorization": self.auth})
        with urllib.request.urlopen(req, context=self.ctx, timeout=20) as r:
            return r.status, r.headers.get("Content-Type", "application/octet-stream"), r.read()

    def snapshot(self):
        return json.loads(self.get("/api/snapshot")[2])


# ---------- what is personal in a snapshot ----------

def personal_strings(snap):
    """Names, host names, comments, SSIDs and router names: the strings that need a fake."""
    out = {}
    for d in snap["devices"]:
        lease = d.get("lease") or {}
        if d.get("name_source") != "vendor + MAC":
            out.setdefault(d["name"], ("device", d))
        if d.get("auto_name_source") not in (None, "vendor + MAC"):
            out.setdefault(d["auto_name"], ("device", d))
        for v in (d.get("hostname"), lease.get("host-name")):
            if v:
                out.setdefault(v, ("hostname", d))
        if lease.get("comment"):
            out.setdefault(lease["comment"], ("device", d))
    for g in snap["groups"]:
        if g.get("ssid"):
            out.setdefault(g["ssid"], ("ssid", g))
        if g.get("comment"):
            out.setdefault(g["comment"], ("comment", g))
        if (g.get("link") or {}).get("note"):
            out.setdefault(g["link"]["note"], ("comment", g))
    for r in snap["routers"]:
        out.setdefault(r["name"], ("router", r))
        if r.get("identity"):
            out.setdefault(r["identity"], ("router", r))
    return out


KINDS = [  # vendor fragment -> fake base name (wired, wifi)
    ("espressif", "Smart plug", "Smart plug"), ("tuya", "Smart plug", "Smart plug"),
    ("shelly", "Relay", "Relay"), ("raspberry", "Raspberry Pi", "Raspberry Pi"),
    ("amazon", "Echo", "Echo"), ("apple", "Mac", "iPhone"), ("samsung", "TV", "Phone"),
    ("google", "Chromecast", "Pixel"), ("synology", "NAS", "NAS"), ("qnap", "NAS", "NAS"),
    ("sonos", "Speaker", "Speaker"), ("brother", "Printer", "Printer"), ("canon", "Printer", "Printer"),
    ("intel", "Desktop", "Laptop"), ("realtek", "Desktop", "Laptop"), ("asustek", "Desktop", "Laptop"),
    ("micro-star", "Desktop", "Laptop"), ("dell", "Desktop", "Laptop"), ("lenovo", "Desktop", "Laptop"),
    ("hewlett", "Desktop", "Laptop"), ("azurewave", "Desktop", "Laptop"), ("ampak", "Device", "Device"),
]


def fake_base(d):
    vendor = (d.get("vendor") or "").lower()
    wifi = d.get("kind") == "wifi"
    if "Phone" in (d.get("tags") or []):
        return "Phone"
    for frag, wired_name, wifi_name in KINDS:
        if frag in vendor:
            return wifi_name if wifi else wired_name
    if "IoT" in (d.get("tags") or []):
        return "Sensor"
    return "Laptop" if wifi else "Device"


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def fake_mac(mac, rnd, used):
    b = [int(x, 16) for x in mac.split(":")]
    while True:
        if b[0] & 0x02:   # locally administered (random): everything random, keep the LA bit
            first = (rnd.randrange(256) | 0x02) & 0xFE
            new = [first] + [rnd.randrange(256) for _ in range(5)]
        else:             # manufacturer MAC: keep the vendor part
            new = b[:3] + [rnd.randrange(256) for _ in range(3)]
        out = ":".join(f"{x:02X}" for x in new)
        if out not in used and out != mac.upper():
            used.add(out)
            return out


def fake_ips(ips, routers, rnd):
    """Same /24, new host part; routers get .1, .2, .3 … in their order."""
    out, used = {}, {}
    for i, ip in enumerate(routers):
        net = ip.rsplit(".", 1)[0]
        out[ip] = f"{net}.{i + 1}"
        used.setdefault(net, set()).add(i + 1)
    for ip in sorted(ips):
        if ip in out:
            continue
        net, host = ip.rsplit(".", 1)
        taken = used.setdefault(net, set())
        if host in ("0", "1", "255"):
            out[ip] = ip              # network, gateway, broadcast stay
            taken.add(int(host))
            continue
        choice = rnd.choice([h for h in range(10, 250) if h not in taken])
        taken.add(choice)
        out[ip] = f"{net}.{choice}"
    return out


def init(cfg, path):
    snap = Upstream(cfg).snapshot()
    rnd = random.Random(int(time.time()))
    used = set()
    macs = {d["mac"]: fake_mac(d["mac"], rnd, used) for d in snap["devices"]}
    text = json.dumps(snap)
    routers = [r["id"] for r in snap["routers"]]
    ips = fake_ips(set(IPV4.findall(text)), routers, rnd)

    names, counters, ssid_n = {}, {}, 0
    for real, (kind, obj) in personal_strings(snap).items():
        if kind in ("device", "hostname"):
            base = fake_base(obj)
            key = (obj["mac"], base)
            if key not in counters:
                counters[base] = counters.get(base, 0) + 1
                counters[key] = f"{base} {counters[base]}"
            fake = counters[key]
            names[real] = slug(fake) if kind == "hostname" and real == real.lower() else fake
        elif kind == "ssid":
            guest = obj.get("guest")
            ssid_n += 0 if guest else 1
            five = all(g.get("band", "").startswith("5") for g in snap["groups"] if g.get("ssid") == real)
            names[real] = "Guest" if guest else "HomeWiFi" + ("5" if five else "") + ("" if ssid_n == 1 else str(ssid_n))
        elif kind == "router":
            idx = routers.index(obj["id"])
            names[real] = "router" if idx == 0 else f"ap{idx}"
            if real == obj.get("identity") and real != obj["name"]:
                names[real] = names[real].capitalize()
        else:   # port comments, link notes: review these by hand
            names[real] = real
    data = {"names": dict(sorted(names.items(), key=lambda kv: kv[0].lower())), "macs": macs, "ips": ips}
    Path(path).write_text(
        "# demo_proxy mapping: real value -> shown value. Review the right-hand sides under names:\n"
        "# (port comments and link notes start unchanged). Keep this file private.\n"
        + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=200), encoding="utf-8")
    print(f"wrote {path}: {len(names)} names, {len(macs)} MACs, {len(ips)} IPs")


# ---------- replacing ----------

class Anonymizer:
    def __init__(self, mapping, rnd=None):
        self.names = {k: v for k, v in (mapping.get("names") or {}).items()}
        self.macs = {k.upper(): v.upper() for k, v in (mapping.get("macs") or {}).items()}
        self.ips = dict(mapping.get("ips") or {})
        self.rnd = rnd or random.Random(1)
        self.used = set(self.macs.values())
        self.rebuild()

    def rebuild(self):
        self.back = {v: k for k, v in self.macs.items()}
        self.suffix = {k[-8:]: v[-8:] for k, v in self.macs.items()}
        self.bare = {k.replace(":", ""): v.replace(":", "") for k, v in self.macs.items()}
        long_names = sorted((n for n in self.names if len(n) >= 3 and self.names[n] != n), key=len, reverse=True)
        self.name_re = re.compile("|".join(r"(?<!\w)" + re.escape(n) + r"(?!\w)" for n in long_names)) if long_names else None

    def mac(self, m):
        m = m.upper()
        if m not in self.macs:   # one the mapping doesn't know yet (new device): fake it on the fly
            self.macs[m] = fake_mac(m, self.rnd, self.used)
            self.rebuild()
        return self.macs[m]

    def ip(self, ip):
        if ip not in self.ips:
            net = ip.rsplit(".", 1)[0]
            taken = {int(v.rsplit(".", 1)[1]) for v in self.ips.values() if v.startswith(net + ".")}
            self.ips[ip] = f"{net}.{self.rnd.choice([h for h in range(10, 250) if h not in taken])}"
        return self.ips[ip]

    def text(self, s):
        if s in self.names:
            return self.names[s]
        s = MAC.sub(lambda m: self.mac(m.group()), s)
        if self.bare:
            s = re.sub("|".join(map(re.escape, self.bare)), lambda m: self.bare[m.group()], s, flags=0)
        s = re.sub(r"(?<![0-9A-Fa-f:])([0-9A-F]{2}:[0-9A-F]{2}:[0-9A-F]{2})(?![0-9A-Fa-f:])",
                   lambda m: self.suffix.get(m.group(1), m.group(1)), s)
        s = IPV4.sub(lambda m: self.ip(m.group()), s)
        if self.name_re:
            s = self.name_re.sub(lambda m: self.names[m.group()], s)
        return s

    def walk(self, x):
        if isinstance(x, dict):
            return {self.text(k): self.walk(v) for k, v in x.items()}
        if isinstance(x, list):
            return [self.walk(v) for v in x]
        if isinstance(x, str):
            return self.text(x)
        return x

    def snapshot(self, snap):
        snap = dict(snap)
        snap["certs"] = []   # service names and addresses; not needed for a demo
        for d in snap.get("devices", []):
            # tag rules come from the config and can contain names or MAC patterns
            d["tag_reasons"] = {t: f"{r.split(' ', 1)[0]} rule" for t, r in (d.get("tag_reasons") or {}).items()}
        return self.walk(snap)

    def real_series(self, series):
        """The UI asks for history by the fake MAC/IP; netmap knows the real one."""
        kind, _, key = series.partition(":")
        if kind == "d":
            return "d:" + self.back.get(key.upper(), key)
        back_ip = {v: k for k, v in self.ips.items()}
        return kind + ":" + IPV4.sub(lambda m: back_ip.get(m.group(), m.group()), key)


# ---------- the proxy ----------

def serve(cfg, path, port, log_dir):
    up = Upstream(cfg)
    anon = Anonymizer(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
    counter = [0]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            route, _, query = self.path.partition("?")
            try:
                if route == "/api/snapshot":
                    body = anon.snapshot(json.loads(up.get("/api/snapshot")[2]))
                elif route == "/api/history":
                    q = {k: v[0] for k, v in parse_qs(query).items()}
                    real = anon.real_series(q.get("id", ""))
                    raw = up.get(f"/api/history?id={quote(real)}&range={quote(q.get('range', '24h'))}")[2]
                    body = anon.walk(json.loads(raw))
                elif route.startswith("/api/") or route == "/ca.crt":
                    return self.reply(404, "text/plain", b"not available in the demo")
                else:   # the UI from this checkout, so screenshots show the code being committed
                    file = (STATIC / (route.lstrip("/") or "index.html")).resolve()
                    if STATIC not in file.parents or not file.is_file():
                        return self.reply(404, "text/plain", b"not found")
                    ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
                    return self.reply(200, ctype, file.read_bytes())
            except Exception as e:
                return self.reply(502, "text/plain", f"{type(e).__name__}: {e}".encode())
            data = json.dumps(body).encode()
            if log_dir:
                counter[0] += 1
                Path(log_dir, f"{counter[0]:04d}{route.replace('/', '_')}.json").write_bytes(data)
            self.reply(200, "application/json", data)

        def do_POST(self):
            self.reply(403, "text/plain", b"read-only demo")

        def reply(self, code, ctype, data):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    print(f"demo proxy for {up.base} on http://127.0.0.1:{port}/")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


# ---------- leak check ----------

def check(cfg, path, log_dir):
    """Every real value (from the mapping and from a fresh real snapshot) must be absent from every
    logged response. Addresses are compared as whole tokens, so a fake that happens to equal another
    device's real address isn't a leak; names as case-sensitive whole words. Values mapped to
    themselves on purpose are skipped."""
    mapping = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    snap = Upstream(cfg).snapshot()
    macs, ips, names = mapping.get("macs") or {}, mapping.get("ips") or {}, mapping.get("names") or {}
    real_macs = {m.upper() for m in list(macs) + [d["mac"] for d in snap["devices"]]}
    shown_macs = {v.upper() for v in macs.values()}
    real_ips = set(IPV4.findall(json.dumps(snap))) | set(ips)
    shown_ips = set(ips.values())
    kept = {k for k, v in names.items() if k == v}
    real_names = {n for n in list(names) + list(personal_strings(snap)) if len(n) >= 3 and n not in kept}
    name_re = re.compile("|".join(r"(?<!\w)" + re.escape(n) + r"(?!\w)" for n in sorted(real_names, key=len, reverse=True)))
    suffix_re = re.compile(r"(?<![0-9A-Fa-f:])([0-9A-F]{2}:[0-9A-F]{2}:[0-9A-F]{2})(?![0-9A-Fa-f:])")
    files = sorted(Path(log_dir).glob("*.json"))
    hits = {}
    for f in files:
        text = f.read_text(encoding="utf-8")
        found = {m.upper() for m in MAC.findall(text)} & (real_macs - shown_macs)
        found |= set(suffix_re.findall(text)) & ({m[-8:] for m in real_macs} - {m[-8:] for m in shown_macs})
        found |= {m.replace(":", "") for m in real_macs if m.replace(":", "") in text.upper()}
        found |= set(IPV4.findall(text)) & (real_ips - shown_ips)
        found |= set(name_re.findall(text))
        for r in found:
            hits.setdefault(r, []).append(f.name)
    print(f"checked {len(files)} responses: {len(real_macs)} MACs, {len(real_ips)} IPs, {len(real_names)} names")
    for r, where in sorted(hits.items()):
        print(f"  LEAK {r!r} in {len(where)} file(s), e.g. {where[0]}")
    if hits:
        sys.exit(1)
    print("no leaks")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").add_argument("mapping")
    s = sub.add_parser("serve")
    s.add_argument("mapping")
    s.add_argument("--port", type=int, default=8099)
    s.add_argument("--log")
    c = sub.add_parser("check")
    c.add_argument("mapping")
    c.add_argument("log")
    a = p.parse_args()
    cfg = netmap_config.load(a.config)
    if a.cmd == "init":
        init(cfg, a.mapping)
    elif a.cmd == "serve":
        serve(cfg, a.mapping, a.port, a.log)
    else:
        check(cfg, a.mapping, a.log)


if __name__ == "__main__":
    main()
