"""Turn raw RouterOS tables into a snapshot: router tree, ports/APs, and where each device is.

Routers are matched by MAC address, never by identity, so renaming a router changes nothing.
"""
import re
from collections import Counter, defaultdict

# CAPsMAN interfaces on the manager carry the remote CAP radio's MAC; that MAC belongs to the CAP.
NOT_OWNED_TYPES = {"cap"}
WIRED_TYPES = {"ether", "vlan", "eoip", "vpls", "gre-tunnel"}


def rows(raw, path):
    v = raw.get(path)
    return v if isinstance(v, list) else []


def mac_of(row, key="mac-address"):
    m = (row.get(key) or "").upper()
    return m if m and m != "00:00:00:00:00:00" else ""


def band_label(band):
    b = (band or "").lower()
    for prefix, label in (("2", "2.4 GHz"), ("5", "5 GHz"), ("6", "6 GHz")):
        if b.startswith(prefix):
            return label
    return ""


def signal_dbm(value):
    """-67, "-67", "-67@1Mbps" -> -67."""
    if value is None or value == "":
        return None
    m = re.match(r"\s*(-?\d+)", str(value))
    return int(m.group(1)) if m else None


def duration_s(text):
    """'1m37s480ms' / '980ms' / '2h3m' -> seconds."""
    units = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1, "ms": 0.001}
    return sum(float(n) * units[u] for n, u in re.findall(r"(\d+(?:\.\d+)?)(ms|w|d|h|m|s)", str(text)))


def v6_signal(row, max_age=60):
    """RouterOS v6 'signal-strength' is the last frame only, and some rates (seen: 11 Mbit/s CCK on
    an AR9300 radio) consistently read 15-20 dB low, so the value jumps with whatever rate the last
    frame used. Use the median over all rates heard in the last minute instead."""
    recent = []
    for part in str(row.get("strength-at-rates") or "").split(","):
        m = re.match(r"\s*(-?\d+)@\S+\s+(\S+)", part)
        if m and duration_s(m.group(2)) <= max_age:
            recent.append(int(m.group(1)))
    if not recent:
        return signal_dbm(row.get("signal-strength"))
    recent.sort()
    mid = len(recent) // 2
    return recent[mid] if len(recent) % 2 else round((recent[mid - 1] + recent[mid]) / 2)


def rate(value):
    if isinstance(value, int):
        return f"{value / 1e6:g} Mbps"
    return str(value or "").split("-")[0] or None


def short_uptime(value):
    return re.sub(r"\d+ms$", "", str(value or "")) or None


def natural_key(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s or "")]


class Router:
    def __init__(self, rc, raw):
        self.id = rc["host"]
        self.name = rc["name"]
        self.raw = raw or {}
        self.ifaces = {r["name"]: r for r in rows(self.raw, "interface")}
        self.bridges = {r["name"] for r in rows(self.raw, "interface/bridge")}
        self.bridge_of = {p["interface"]: p["bridge"] for p in rows(self.raw, "interface/bridge/port")}


def build(cfg, raws, errors=None, known_router_macs=None):
    """raws: {host: {path: rows}}. Returns the snapshot dict (current state only, no history).

    known_router_macs ({mac: router id}, from earlier polls) keeps a router that is unreachable for
    a moment (reboot, update) from turning into "devices" on its parent's port.
    """
    errors = errors or {}
    R = {rc["host"]: Router(rc, raws.get(rc["host"])) for rc in cfg["routers"]}
    order = list(R)
    root = cfg["root"] if cfg["root"] in R else order[0]
    pattern = (cfg.get("guest_pattern") or "").lower()

    # --- which MACs belong to which router -----------------------------------------------
    owner = {}
    for rid, r in R.items():
        for i in r.ifaces.values():
            if i.get("type") not in NOT_OWNED_TYPES and mac_of(i):
                owner.setdefault(mac_of(i), rid)
    for rid, r in R.items():
        for h in rows(r.raw, "interface/bridge/host"):
            if h.get("local") and mac_of(h):
                owner.setdefault(mac_of(h), rid)
    for mac, rid in (known_router_macs or {}).items():
        if rid in R:
            owner.setdefault(mac, rid)

    # --- toward[a][b]: port on a that leads to router b ------------------------------------
    toward = {}
    for rid, r in R.items():
        seen = defaultdict(Counter)
        for h in rows(r.raw, "interface/bridge/host"):
            o = owner.get(mac_of(h))
            if o and o != rid and not h.get("local"):
                seen[o][h.get("on-interface")] += 1
        for n in rows(r.raw, "ip/neighbor"):
            o = owner.get(mac_of(n))
            if o and o != rid and o not in seen:
                seen[o][re.split(r"[,/]", n.get("interface", ""))[0]] += 1
        toward[rid] = {o: c.most_common(1)[0][0] for o, c in seen.items()}

    # --- router tree: child -> (parent, parent_port, child_port) ---------------------------
    # Neighbor discovery also reports routers reachable *through* another bridge, so a router's
    # parent is the one on its root-side port that doesn't itself see another of those routers
    # on its port toward the child.
    links = {}
    for x in order:
        if x == root:
            continue
        rp = toward[x].get(root)
        if rp is None:
            if x in toward[root]:
                links[x] = (root, toward[root][x], None)
            continue
        side = [y for y, p in toward[x].items() if p == rp]
        cands = [
            y for y in side
            if toward[y].get(x) and not any(z != y and toward[y].get(z) == toward[y][x] for z in side)
        ]
        parent = root if root in cands or not cands else cands[0]
        links[x] = (parent, toward[parent].get(x), rp)
    for o in cfg.get("links") or []:
        links[o["child"]] = (o["parent"], o.get("parent_port"), o.get("child_port"))

    uplinks = {rid: set(toward[rid].values()) for rid in R}
    for child, (parent, pport, cport) in links.items():
        if pport:
            uplinks[parent].add(pport)
        if cport:
            uplinks[child].add(cport)

    # --- guest detection -------------------------------------------------------------------
    def is_guest_iface(r, iface):
        if not pattern or not iface:
            return False
        return pattern in iface.lower() or pattern in (r.bridge_of.get(iface) or "").lower()

    guest_servers = {
        (rid, s.get("name")) for rid, r in R.items() for s in rows(r.raw, "ip/dhcp-server")
        if is_guest_iface(r, s.get("interface")) or pattern in (s.get("name") or "").lower()
    }

    # --- groups: ports and APs -------------------------------------------------------------
    groups = {}
    wireless_group = {}  # (router, bridge-port iface) -> AP group id

    def port_group(rid, iface):
        gid = f"{rid}|{iface}"
        if gid not in groups:
            i = R[rid].ifaces.get(iface, {})
            comment = i.get("comment") or ""
            groups[gid] = {
                "id": gid, "router": rid, "on": rid, "kind": "port", "iface": iface,
                "label": f"{iface} · {comment}" if comment else iface, "comment": comment,
                "running": bool(i.get("running")), "guest": is_guest_iface(R[rid], iface),
            }
        return gid

    def ap_group(rid, key, iface, ssid, band, running, via, bridged_on=None):
        """bridged_on: (router, iface) where this radio is a bridge port, if not on rid itself."""
        bridged_on = bridged_on or (rid, iface)
        gid = f"{rid}|{key}"
        suffix = "CAPsMAN" if via == "capsman" else iface
        groups[gid] = {
            # "on": the router whose interface (and counters) this is; for CAPsMAN the manager
            "id": gid, "router": rid, "on": bridged_on[0], "kind": "ap", "iface": iface, "ssid": ssid or "",
            "band": band, "via": via, "running": bool(running),
            "label": " · ".join(x for x in (ssid, band) if x) + f" ({suffix})",
            "guest": is_guest_iface(R[bridged_on[0]], bridged_on[1]),
        }
        wireless_group[bridged_on] = gid
        return gid

    # RouterOS v6 local radios (also used to look up the band of a CAPsMAN-managed radio)
    for rid, r in R.items():
        wl = {w["name"]: w for w in rows(r.raw, "interface/wireless")}
        for w in wl.values():
            if w.get("disabled"):
                continue
            master = wl.get(w.get("master-interface"), {})
            band = band_label(w.get("band") or master.get("band"))
            running = r.ifaces.get(w["name"], {}).get("running", w.get("running"))
            ap_group(rid, w["name"], w["name"], w.get("ssid"), band, running, "wireless")

    # RouterOS v7 wifi
    for rid, r in R.items():
        bands = {x.get("interface"): x.get("bands", "") for x in rows(r.raw, "interface/wifi/radio")}
        reg_band = {x.get("interface"): x.get("band") for x in rows(r.raw, "interface/wifi/registration-table")}
        for w in rows(r.raw, "interface/wifi"):
            if w.get("disabled"):
                continue
            name = w["name"]
            band = band_label(w.get("channel.band") or reg_band.get(name) or bands.get(name))
            ap_group(rid, name, name, w.get("configuration.ssid"), band,
                     r.ifaces.get(name, {}).get("running"), "wifi")

    # RouterOS v6 CAPsMAN: interfaces live on the manager, radios physically on the CAP
    for mid, m in R.items():
        caps = {c["name"]: c for c in rows(m.raw, "caps-man/interface")}
        if not caps:
            continue
        radio_by_iface = {x.get("interface"): x for x in rows(m.raw, "caps-man/radio")}
        remote_by_name = {x.get("name"): x for x in rows(m.raw, "caps-man/remote-cap")}
        ssid_by_conf = {x.get("name"): x.get("ssid") for x in rows(m.raw, "caps-man/configuration")}
        for name, c in caps.items():
            if c.get("disabled"):
                continue
            master = c if c.get("master-interface") in (None, "", "none") else caps.get(c["master-interface"], c)
            radio = radio_by_iface.get(master["name"], {})
            radio_mac = mac_of(master, "radio-mac") or mac_of(radio, "radio-mac")
            cap = owner.get(radio_mac)
            if not cap:
                remote = remote_by_name.get(radio.get("remote-cap-name"), {})
                cap = owner.get(mac_of(remote, "base-mac")) or mid
            band = ""
            for w in rows(R[cap].raw, "interface/wireless"):
                if mac_of(w) == radio_mac:
                    band = band_label(w.get("band"))
            ap_group(cap, f"capsman:{name}", name, ssid_by_conf.get(c.get("configuration")), band,
                     c.get("running"), "capsman", bridged_on=(mid, name))

    # --- placement ---------------------------------------------------------------------------
    place = {}  # mac -> {group, kind, wifi}

    for rid, r in R.items():
        for x in rows(r.raw, "interface/wireless/registration-table"):
            if x.get("ap") or x.get("wds"):
                continue
            place[mac_of(x)] = {"group": wireless_group.get((rid, x.get("interface"))), "kind": "wifi", "wifi": {
                "signal": v6_signal(x), "tx_rate": rate(x.get("tx-rate")),
                "rx_rate": rate(x.get("rx-rate")), "uptime": short_uptime(x.get("uptime")),
                "last_ip": x.get("last-ip")}}
        for x in rows(r.raw, "caps-man/registration-table"):
            place[mac_of(x)] = {"group": wireless_group.get((rid, x.get("interface"))), "kind": "wifi", "wifi": {
                "signal": signal_dbm(x.get("rx-signal")), "tx_rate": rate(x.get("tx-rate")),
                "rx_rate": rate(x.get("rx-rate")), "uptime": short_uptime(x.get("uptime"))}}
        for x in rows(r.raw, "interface/wifi/registration-table"):
            place[mac_of(x)] = {"group": wireless_group.get((rid, x.get("interface"))), "kind": "wifi", "wifi": {
                "signal": signal_dbm(x.get("signal")), "tx_rate": rate(x.get("tx-rate")),
                "rx_rate": rate(x.get("rx-rate")), "uptime": short_uptime(x.get("uptime"))}}
    for p in place.values():
        if p["group"]:
            g = groups[p["group"]]
            p["wifi"].update(ssid=g["ssid"], band=g["band"])

    # A VLAN interface in a bridge (e.g. a guest VLAN between two routers) is a link to
    # another router, not a port with a device behind it. A device seen only there is online but
    # its location is unknown (it sits somewhere behind the other router).
    deferred = []
    for rid, r in R.items():
        for h in rows(r.raw, "interface/bridge/host"):
            mac, iface = mac_of(h), h.get("on-interface")
            if not mac or h.get("local") or mac in owner or mac in place or iface in uplinks[rid]:
                continue
            if r.ifaces.get(iface, {}).get("type") == "vlan":
                deferred.append((rid, iface, mac))
                continue
            gid = wireless_group.get((rid, iface))
            place[mac] = {"group": gid or port_group(rid, iface), "kind": "wifi" if gid else "wired"}
    for rid, iface, mac in deferred:
        if mac not in place:
            place[mac] = {"group": None, "kind": None}

    # Routed ports (e.g. WAN): no bridge host table, fall back to ARP on that interface.
    for rid, r in R.items():
        for a in rows(r.raw, "ip/arp"):
            mac, iface = mac_of(a), a.get("interface")
            if (not mac or mac in owner or mac in place or not a.get("complete")
                    or iface in r.bridges or iface in r.bridge_of or iface in uplinks[rid]):
                continue
            place[mac] = {"group": port_group(rid, iface), "kind": "wired"}

    # --- identity data: leases, ARP, DNS, LLDP -------------------------------------------------
    # A device can hold several leases, e.g. a phone tried the guest network and came back:
    # both leases stay bound for the lease time. Pick the one for the network it's on now.
    all_leases = defaultdict(list)
    for rid, r in R.items():
        for l in rows(r.raw, "ip/dhcp-server/lease"):
            mac = mac_of(l) or mac_of(l, "active-mac-address")
            if mac:
                all_leases[mac].append((rid, l))

    def pick_lease(mac, group):
        cands = all_leases.get(mac)
        if not cands:
            return None, {}
        here_guest = groups[group]["guest"] if group in groups else None

        def score(c):
            rid, l = c
            on_this_net = here_guest is None or ((rid, l.get("server")) in guest_servers) == here_guest
            return (not on_this_net, l.get("status") != "bound", duration_s(l.get("last-seen") or "9999w"))
        return min(cands, key=score)
    arp_ips = defaultdict(list)
    for rid in order:
        for a in rows(R[rid].raw, "ip/arp"):
            if a.get("complete") and mac_of(a) and a.get("address") not in arp_ips[mac_of(a)]:
                arp_ips[mac_of(a)].append(a.get("address"))
    dns = {}
    for rid in order:
        for d in rows(R[rid].raw, "ip/dns/static"):
            if d.get("address") and d.get("name") and not d.get("disabled"):
                dns.setdefault(d["address"], d["name"])
    lldp = {}
    for rid in order:
        for n in rows(R[rid].raw, "ip/neighbor"):
            if mac_of(n) and mac_of(n) not in owner:
                lldp.setdefault(mac_of(n), {k: n.get(k) for k in (
                    "identity", "platform", "board", "version", "system-description", "interface-name")
                    if n.get(k)})

    # --- devices -------------------------------------------------------------------------------
    devices = []
    for mac in (set(place) | set(all_leases) | set(arp_ips)) - set(owner) - {""}:
        p = place.get(mac)
        lrid, lease = pick_lease(mac, (p or {}).get("group"))
        ips = list(arp_ips.get(mac, []))
        ip = lease.get("active-address") or (lease.get("address") if not lease.get("dynamic") else None)
        ip = ip or (p or {}).get("wifi", {}).get("last_ip") or (ips[0] if ips else None)
        if ip and ip not in ips:
            ips.insert(0, ip)
        if lease.get("comment"):
            name, source = lease["comment"], "DHCP lease comment"
        elif ip and dns.get(ip):
            name, source = dns[ip], "DNS static"
        elif lease.get("host-name"):
            name, source = lease["host-name"], "DHCP host-name"
        else:
            name, source = None, None
        g = groups.get((p or {}).get("group"))
        # Where the device is now decides; the lease only when its location is unknown.
        guest = bool(g["guest"]) if g else (lrid, lease.get("server")) in guest_servers
        devices.append({
            "mac": mac, "name": name, "name_source": source, "ip": ip, "ips": ips,
            "hostname": lease.get("host-name"), "guest": guest,
            "online": p is not None, "group": (p or {}).get("group"), "kind": (p or {}).get("kind"),
            "wifi": (p or {}).get("wifi"), "lldp": lldp.get(mac),
            "lease": {k: lease.get(k) for k in (
                "server", "status", "dynamic", "comment", "host-name", "expires-after", "last-seen", "address")}
            if lease else None,
        })

    routers = []
    for rid in order:
        r = R[rid]
        ident = (rows(r.raw, "system/identity") or [{}])[0]
        res = (rows(r.raw, "system/resource") or [{}])[0]
        parent = links.get(rid)
        routers.append({
            "id": rid, "name": r.name, "identity": ident.get("name"),
            "board": res.get("board-name"), "version": res.get("version"),
            "uptime": short_uptime(res.get("uptime")), "cpu_load": res.get("cpu-load"),
            "online": rid in raws and rid not in errors, "error": errors.get(rid),
            "root": rid == root,
            "parent": parent[0] if parent else None,
            "parent_group": port_group(parent[0], parent[1]) if parent and parent[1] else None,
            "uplink_port": parent[2] if parent else None,
        })

    used = {d["group"] for d in devices if d["group"]} | {r["parent_group"] for r in routers}
    group_list = sorted(
        # Enabled APs always show: RouterOS v7 reports a wifi AP as "running" only while a client
        # is connected, so an idle radio would otherwise vanish from the map.
        (g for g in groups.values() if g["kind"] == "ap" or g["id"] in used),
        key=lambda g: (order.index(g["router"]), g["kind"] == "ap", natural_key(g["iface"])),
    )
    return {"root": root, "routers": routers, "groups": group_list, "devices": devices, "router_macs": owner}
