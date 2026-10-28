"""Traffic rates from RouterOS byte counters, link speeds, and relative "heat".

Rates are the counter difference between two polls. Directions are from the attached device's
point of view: an interface's tx is the device's download (↓). The WAN port is the exception:
there ↓ is the download from the internet (the port's rx).

Heat is relative: rate ÷ a reference that follows the busiest device (or port/AP) and decays
slowly, never below a floor. The WAN port's heat is absolute: % of the configured line speed.
"""
import re
from collections import Counter

REG_TABLES = ("interface/wireless/registration-table", "caps-man/registration-table",
              "interface/wifi/registration-table")
ACTIVE_BPS = 1000


def parse_rate(value):
    """'1Gbps', '100Mbps', '2.5Gbps' -> bits/s."""
    m = re.match(r"\s*([\d.]+)\s*([kMG]?)bps", str(value or ""))
    return float(m.group(1)) * {"": 1, "k": 1e3, "M": 1e6, "G": 1e9}[m.group(2)] if m else None


def max_advertised(advertise):
    """'10M-half,...,1000M-full' or '...,1G-baseT-full,2.5G-baseT' -> highest speed in bits/s."""
    best = None
    for token in str(advertise or "").split(","):
        m = re.match(r"\s*([\d.]+)([MG])", token)
        if m:
            best = max(best or 0, float(m.group(1)) * (1e6 if m.group(2) == "M" else 1e9))
    return best


def fmt_speed(bps):
    return f"{bps / 1e9:g}G" if bps >= 1e9 else f"{bps / 1e6:g}M"


def two_counters(value):
    """'123,456' -> (123, 456)."""
    try:
        a, b = str(value).split(",")[:2]
        return int(a), int(b)
    except ValueError:
        return None


class Counters:
    def __init__(self):
        self.prev = {}
        self.seen = set()

    def rate(self, key, now, a, b):
        """Byte counters -> (a, b) in bits/s since the last call; None on first sight or reset."""
        self.seen.add(key)
        prev, self.prev[key] = self.prev.get(key), (now, a, b)
        if prev is None:
            return None
        t0, a0, b0 = prev
        if now <= t0 or a < a0 or b < b0:
            return None
        return (a - a0) * 8 / (now - t0), (b - b0) * 8 / (now - t0)

    def forget_unseen(self):
        for key in set(self.prev) - self.seen:
            del self.prev[key]
        self.seen = set()


class Peak:
    """Highest recent value, halving every half_life seconds, never below floor."""

    def __init__(self, floor, half_life):
        self.floor, self.half_life = floor, half_life
        self.value, self.t = floor, None

    def update(self, current, now):
        if self.t is not None:
            self.value *= 0.5 ** ((now - self.t) / self.half_life)
        self.t = now
        self.value = max(self.value, current, self.floor)
        return self.value


class Traffic:
    def __init__(self, cfg):
        self.cfg = cfg["traffic"]
        floor = self.cfg["floor_mbps"] * 1e6
        self.counters = Counters()
        self.device_peak = Peak(floor, self.cfg["half_life_s"])
        self.infra_peak = Peak(floor, self.cfg["half_life_s"])

    def annotate(self, snap, raws, now):
        """Adds "traffic" to groups and devices, "link" to ports and snap["traffic"]."""
        iface, client, links = {}, {}, {}
        for rid, raw in raws.items():
            for i in table(raw, "interface"):
                r = self.counters.rate(("if", rid, i["name"]), now, i.get("tx-byte", 0), i.get("rx-byte", 0))
                if r:
                    iface[(rid, i["name"])] = r              # (down, up) as seen by the attached device
            for path in REG_TABLES:
                for x in table(raw, path):
                    counters = two_counters(x.get("bytes"))
                    mac = (x.get("mac-address") or "").upper()
                    if counters and mac:
                        r = self.counters.rate(("wifi", rid, path, mac), now, *counters)
                        if r:
                            client[mac] = r                  # bytes = to client, from client
            adv = {e.get("name"): max_advertised(e.get("advertise")) for e in table(raw, "interface/ethernet")}
            for m in table(raw, "ethernet/monitor"):
                links[(rid, m.get("name"))] = {
                    "status": m.get("status"), "rate": parse_rate(m.get("rate")), "max": adv.get(m.get("name"))}
        self.counters.forget_unseen()

        groups = {g["id"]: g for g in snap["groups"]}
        wan_id = self.wan_group_id(snap)
        for g in groups.values():
            on = g.get("on", g["router"])
            link = links.get((on, g["iface"]))
            if g["kind"] == "port" and link:
                g["link"] = {"status": link["status"], "rate": link["rate"], "max": link["max"],
                             "speed": fmt_speed(link["rate"]) if link["rate"] else None}
            r = iface.get((on, g["iface"]))
            if r is None:
                continue
            down, up = (r[1], r[0]) if g["id"] == wan_id else r
            t = {"down": down, "up": up}
            if link and link["rate"]:
                t["link_pct"] = 100 * max(down, up) / link["rate"]
            g["traffic"] = t
        self.apply_link_limits(snap, groups, links)

        online = Counter(d["group"] for d in snap["devices"] if d["online"] and d.get("group"))
        # A client's share of its AP is taken against the sum of that AP's clients, from the same
        # counters (registration table). The AP interface's own counters count differently (wifi
        # overhead, broadcasts) and are read at another moment, so a client could exceed "100 %".
        ap_clients = Counter()
        for d in snap["devices"]:
            r = client.get(d["mac"]) if d["online"] and d.get("kind") == "wifi" else None
            if r and d.get("group"):
                ap_clients[d["group"]] += r[0] + r[1]
        for d in snap["devices"]:
            g = groups.get(d.get("group"))
            if not d["online"] or not g or "traffic" not in g:
                continue
            if d.get("kind") == "wifi":
                r = client.get(d["mac"])
                if r is None:
                    continue
                t = {"down": r[0], "up": r[1]}
                total = ap_clients[g["id"]]
                t["ap_pct"] = 100 * (r[0] + r[1]) / total if total else 0
                d["traffic"] = t
            elif online[g["id"]] == 1:
                d["traffic"] = {k: v for k, v in g["traffic"].items() if k in ("down", "up", "link_pct")}
            else:
                d["traffic"] = {"shared": True}

        for g in groups.values():
            if g["kind"] == "ap" and "traffic" in g:
                g["traffic"]["clients"] = sum(
                    1 for d in snap["devices"] if d.get("group") == g["id"]
                    and (d.get("traffic") or {}).get("down", 0) + (d.get("traffic") or {}).get("up", 0) >= ACTIVE_BPS)

        def total(t):
            return t.get("down", 0) + t.get("up", 0)

        # The modem on the WAN port carries everyone's internet traffic: it gets the WAN's
        # absolute heat below and stays out of the device reference.
        on_wan = [d for d in snap["devices"] if wan_id and d.get("group") == wan_id and d.get("traffic")]
        dev = [d["traffic"] for d in snap["devices"]
               if d.get("traffic") and not d["traffic"].get("shared") and d not in on_wan]
        ref = self.device_peak.update(max(map(total, dev), default=0), now)
        for t in dev:
            t["heat"] = total(t) / ref
        infra = [g["traffic"] for g in groups.values() if "traffic" in g and g["id"] != wan_id]
        iref = self.infra_peak.update(max(map(total, infra), default=0), now)
        for t in infra:
            t["heat"] = total(t) / iref

        wan = None
        w = self.cfg.get("wan")
        if wan_id and "traffic" in groups.get(wan_id, {}):
            t = groups[wan_id]["traffic"]
            pct = 100 * max(t["down"] / (w["down_mbps"] * 1e6), t["up"] / (w["up_mbps"] * 1e6))
            t["heat"] = pct / 100
            t["line_pct"] = pct
            for d in on_wan:
                d["traffic"].update(heat=pct / 100, line_pct=pct)
            wan = {"group": wan_id, "down": t["down"], "up": t["up"], "pct": pct,
                   "down_mbps": w["down_mbps"], "up_mbps": w["up_mbps"]}
        snap["traffic"] = {"bands": self.cfg["bands"], "wan": wan, "ref": {"devices": ref, "infra": iref}}

    def wan_group_id(self, snap):
        w = self.cfg.get("wan")
        if not w:
            return None
        for r in snap["routers"]:
            if w["router"] in (r["name"], r["id"]):
                return f"{r['id']}|{w['interface']}"
        return None

    def apply_link_limits(self, snap, groups, links):
        """Notes for known bottlenecks (traffic.link_limits), and a warning for router-to-router
        links negotiated below what both ends support unless such a limit explains it."""
        rid = {name: r["id"] for r in snap["routers"] for name in (r["name"], r["id"])}
        limits = {(rid.get(x["router"]), x["interface"]): x for x in self.cfg.get("link_limits") or []}
        for g in groups.values():
            limit = limits.get((g.get("on", g["router"]), g["iface"]))
            if limit and "link" in g:
                g["link"]["limit"] = limit["mbps"] * 1e6
                g["link"]["note"] = limit.get("note") or f"limited to {limit['mbps']} Mbit/s"
        for r in snap["routers"]:
            g = groups.get(r.get("parent_group"))
            if not g or "link" not in g:
                continue
            child = links.get((r["id"], r.get("uplink_port")), {})
            limit = limits.get((r["id"], r.get("uplink_port")))
            if limit and "note" not in g["link"]:
                g["link"].update(limit=limit["mbps"] * 1e6, note=limit.get("note") or f"limited to {limit['mbps']} Mbit/s")
            rate, ends = g["link"]["rate"], [g["link"]["max"], child.get("max")]
            expected = min([e for e in ends if e] + ([g["link"]["limit"]] if "limit" in g["link"] else []), default=None)
            if rate and all(ends) and rate < expected:
                why = "the configured limit allows" if "limit" in g["link"] else "both ends allow"
                g["link"]["warning"] = (f"negotiated {fmt_speed(rate)}, but {why} "
                                        f"{fmt_speed(expected)}: check cable and ports")


def table(raw, path):
    v = (raw or {}).get(path)
    return v if isinstance(v, list) else []
