"""Load config.yaml and fill in defaults."""
import ipaddress
import re
from pathlib import Path

import yaml

DEFAULTS = {
    "poll_interval": 10,
    "offline_after": 60,
    "forget_private_after_days": 30,   # random-MAC and guest devices; 0 = keep forever
    "guest_pattern": "guest",
    "data_dir": "data",
    "links": [],
    "ca_file": None,       # own CA: offered at /ca.crt, used to check cert_watch
    "cert_watch": {},      # {name: "host:port"} TLS services whose certificates netmap checks
    "cert_warn_days": 30,
}

SERVER_DEFAULTS = {
    "bind": "127.0.0.1",
    "port": 8090,
    "tls": True,
    "cert_names": [],
    "username": "admin",
    "password": "",
}


COLOR_DEFAULTS = {
    "router": "#3b82f6",   # router card border and icon
    "port": "#14b8a6",     # Ethernet port cards
    "ap": "#8b5cf6",       # access point (SSID) cards
    "wired": "#22c55e",    # wired devices
    "wifi": "#f97316",     # wireless devices
    "guest": "#eab308",    # guest network devices (border)
    "new": "#ec4899",      # devices first seen < 24 h (border)
    "uplink": "#3b82f6",   # lines between routers
}
COLOR = re.compile(r"^(#[0-9a-fA-F]{3,8}|[a-zA-Z]+|(rgb|hsl)a?\([0-9.,%\s]+\))$")


def load_colors(colors):
    out = dict(COLOR_DEFAULTS)
    for key, value in (colors or {}).items():
        if key not in COLOR_DEFAULTS:
            raise ValueError(f"colors.{key}: unknown, expected one of {', '.join(COLOR_DEFAULTS)}")
        if not COLOR.match(str(value).strip()):
            raise ValueError(f"colors.{key}: {value!r} is not a color (use e.g. '#22c55e')")
        out[key] = str(value).strip()
    return out


REGEX_FIELDS = ("name", "hostname", "vendor", "router", "interface", "ssid")
RULE_FIELDS = REGEX_FIELDS + ("mac", "ip", "subnet", "kind")


def as_list(value):
    return value if isinstance(value, list) else [value]


def compile_rule(where, rule):
    if not isinstance(rule, dict) or not rule:
        raise ValueError(f"{where}: a rule is a mapping like {{vendor: Espressif}}")
    out = {}
    for field, value in rule.items():
        try:
            if field in REGEX_FIELDS:
                out[field] = re.compile(str(value), re.I)
            elif field == "mac":
                pats = [re.escape(str(m).strip().upper()).replace(r"\*", ".*") for m in as_list(value)]
                out[field] = re.compile("^(" + "|".join(pats) + ")$")
            elif field == "ip":
                out[field] = {str(ipaddress.ip_address(str(v).strip())) for v in as_list(value)}
            elif field == "subnet":
                out[field] = [ipaddress.ip_network(str(v).strip(), strict=False) for v in as_list(value)]
            elif field == "kind":
                if value not in ("wired", "wifi"):
                    raise ValueError("must be wired or wifi")
                out[field] = value
            else:
                raise ValueError(f"unknown field, expected one of {', '.join(RULE_FIELDS)}")
        except (re.error, ValueError) as e:
            raise ValueError(f"{where}.{field}: {value!r}: {e}") from None
    return {"test": out, "text": ", ".join(f"{k} {v}" for k, v in rule.items())}


def load_tags(tags):
    """tags: {name: {color, match: [rule], exclude: [rule]}} -> list in config order."""
    out = []
    for name, spec in (tags or {}).items():
        where = f"tags.{name}"
        spec = spec or {}
        color = str(spec.get("color", "#94a3b8")).strip()
        if not COLOR.match(color):
            raise ValueError(f"{where}.color: {color!r} is not a color")
        unknown = set(spec) - {"color", "match", "exclude"}
        if unknown:
            raise ValueError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
        out.append({
            "name": str(name), "color": color,
            "match": [compile_rule(f"{where}.match[{i}]", r) for i, r in enumerate(as_list(spec.get("match") or []))],
            "exclude": [compile_rule(f"{where}.exclude[{i}]", r) for i, r in enumerate(as_list(spec.get("exclude") or []))],
        })
    return out


TRAFFIC_DEFAULTS = {
    "floor_mbps": 1,       # heat reference never drops below this, so an idle network stays cold
    "half_life_s": 120,    # how fast the "busiest" reference decays
    "bands": {5: "#22c55e", 25: "#f59e0b", 60: "#ef4444"},   # heat % -> color; below the first: none
    "wan": None,
    "link_limits": [],     # known bottlenecks: [{router, interface, mbps, note}]
}


def load_traffic(traffic, routers):
    t = {**TRAFFIC_DEFAULTS, **(traffic or {})}
    unknown = set(t) - set(TRAFFIC_DEFAULTS)
    if unknown:
        raise ValueError(f"traffic: unknown key(s) {', '.join(sorted(unknown))}")
    for key in ("floor_mbps", "half_life_s"):
        if not isinstance(t[key], (int, float)) or t[key] <= 0:
            raise ValueError(f"traffic.{key}: must be a positive number")
    bands = []
    for at, color in (t["bands"] or {}).items():
        if not isinstance(at, (int, float)) or not 0 < at <= 100:
            raise ValueError(f"traffic.bands: {at!r} must be a percentage between 0 and 100")
        if not COLOR.match(str(color).strip()):
            raise ValueError(f"traffic.bands.{at}: {color!r} is not a color")
        bands.append([at, str(color).strip()])
    t["bands"] = sorted(bands)
    names = {r["name"] for r in routers} | {r["host"] for r in routers}
    for i, x in enumerate(t["link_limits"] or []):
        where = f"traffic.link_limits[{i}]"
        if not isinstance(x, dict) or x.get("router") not in names:
            raise ValueError(f"{where}.router: {(x or {}).get('router')!r} is not a configured router")
        if not x.get("interface"):
            raise ValueError(f"{where}.interface: required (e.g. ether4)")
        if not isinstance(x.get("mbps"), (int, float)) or x["mbps"] <= 0:
            raise ValueError(f"{where}.mbps: must be a positive number")
    w = t["wan"]
    if w:
        if w.get("router") not in names:
            raise ValueError(f"traffic.wan.router: {w.get('router')!r} is not a configured router")
        if not w.get("interface"):
            raise ValueError("traffic.wan.interface: required (e.g. ether1)")
        for key in ("down_mbps", "up_mbps"):
            if not isinstance(w.get(key), (int, float)) or w[key] <= 0:
                raise ValueError(f"traffic.wan.{key}: must be a positive number")
    return t


MAC = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")


def load_names(names):
    """names: {MAC: name} -> {upper-case MAC: name}."""
    out = {}
    for mac, name in (names or {}).items():
        m = str(mac).strip().upper().replace("-", ":")
        if not MAC.match(m):
            raise ValueError(f"names: {mac!r} is not a MAC address (use AA:BB:CC:DD:EE:FF)")
        if not str(name or "").strip():
            raise ValueError(f"names.{mac}: empty name")
        out[m] = str(name).strip()
    return out


def load_cert_watch(watch):
    """cert_watch: {name: "host:port"} -> [{name, host, port}]."""
    if not isinstance(watch or {}, dict):
        raise ValueError('cert_watch: a mapping like {Pi-hole: "192.168.88.10:443"}')
    out = []
    for name, target in (watch or {}).items():
        host, _, port = str(target).rpartition(":")
        if not host or not port.isdigit() or not 0 < int(port) < 65536:
            raise ValueError(f"cert_watch.{name}: {target!r} must be host:port")
        out.append({"name": str(name), "host": host, "port": int(port)})
    return out


def load(path):
    path = Path(path)
    cfg = {**DEFAULTS, **(yaml.safe_load(path.read_text(encoding="utf-8")) or {})}
    cfg["server"] = {**SERVER_DEFAULTS, **(cfg.get("server") or {})}
    cfg["colors"] = load_colors(cfg.get("colors"))
    cfg["tags"] = load_tags(cfg.get("tags"))
    cfg["names"] = load_names(cfg.get("names"))
    if not cfg.get("routers"):
        raise ValueError(f"{path}: no routers configured")
    for r in cfg["routers"]:
        r.setdefault("name", r["host"].split(".")[0])
        r.setdefault("port", 8729)
        r.setdefault("username", "netmap")
    cfg.setdefault("root", cfg["routers"][0]["host"])
    cfg["traffic"] = load_traffic(cfg.get("traffic"), cfg["routers"])
    days = cfg["forget_private_after_days"]
    if not isinstance(days, (int, float)) or days < 0:
        raise ValueError(f"forget_private_after_days: {days!r} must be a number >= 0 (0 = never)")
    data_dir = Path(cfg["data_dir"])
    cfg["data_dir"] = data_dir if data_dir.is_absolute() else path.parent / data_dir
    if cfg["ca_file"]:
        ca = Path(cfg["ca_file"])
        cfg["ca_file"] = ca if ca.is_absolute() else path.parent / ca
        if not cfg["ca_file"].is_file():
            raise ValueError(f"ca_file: {cfg['ca_file']} not found")
    for r in cfg["routers"]:
        r["ca_file"] = cfg["ca_file"]
        if not r.get("fingerprint") and not cfg["ca_file"]:
            raise ValueError(f"routers: {r['host']} needs a fingerprint, or set ca_file")
    cfg["cert_watch"] = load_cert_watch(cfg["cert_watch"])
    if not isinstance(cfg["cert_warn_days"], (int, float)) or cfg["cert_warn_days"] < 0:
        raise ValueError(f"cert_warn_days: {cfg['cert_warn_days']!r} must be a number >= 0")
    return cfg
