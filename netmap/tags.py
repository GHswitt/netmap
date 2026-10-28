"""Assign user tags (from config `tags:`) to devices by rule."""
import ipaddress


def facts(d, groups, routers):
    """The values a rule can test, for one device."""
    g = groups.get(d.get("group")) or {}
    r = routers.get(g.get("router")) or {}
    ips = []
    for ip in d.get("ips") or ([d["ip"]] if d.get("ip") else []):
        try:
            ips.append(ipaddress.ip_address(ip))
        except ValueError:
            pass
    return {
        "name": d.get("name") or "", "hostname": d.get("hostname") or "", "vendor": d.get("vendor") or "",
        "router": " ".join(x for x in (r.get("name"), r.get("id")) if x),
        "interface": g.get("iface") or "", "ssid": (d.get("wifi") or {}).get("ssid") or g.get("ssid") or "",
        "mac": d["mac"], "ips": ips, "kind": d.get("kind"),
    }


def rule_matches(test, f):
    for field, want in test.items():
        if field == "mac":
            ok = bool(want.match(f["mac"]))
        elif field == "ip":
            ok = any(str(ip) in want for ip in f["ips"])
        elif field == "subnet":
            ok = any(ip in net for ip in f["ips"] for net in want if ip.version == net.version)
        elif field == "kind":
            ok = f["kind"] == want
        else:
            ok = bool(want.search(f[field]))
        if not ok:
            return False
    return True


def apply(tags, devices, groups, routers):
    """Sets d["tags"] (names, config order) and d["tag_reasons"] ({name: matching rule})."""
    for d in devices:
        f = facts(d, groups, routers)
        d["tags"], d["tag_reasons"] = [], {}
        for t in tags:
            hit = next((r for r in t["match"] if rule_matches(r["test"], f)), None)
            if hit and not any(rule_matches(r["test"], f) for r in t["exclude"]):
                d["tags"].append(t["name"])
                d["tag_reasons"][t["name"]] = hit["text"]
