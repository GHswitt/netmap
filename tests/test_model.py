import unittest

from netmap import model, tags
from netmap.config import COLOR_DEFAULTS, load_colors, load_tags
from netmap.store import Store

# root (v6, CAPsMAN manager) --ether2/ether2-- cap (v6, CAPsMAN CAP)
# root                       --ether4/ether5-- wifi7 (v7 wifi)
R_MAC = {"ether1": "AA:00:00:00:00:01", "ether2": "AA:00:00:00:00:02", "ether4": "AA:00:00:00:00:04"}
C_MAC = {"ether2": "CC:00:00:00:00:02", "ether3": "CC:00:00:00:00:03", "wlan1": "CC:00:00:00:00:99"}
W_MAC = {"ether1": "EE:00:00:00:00:01", "ether5": "EE:00:00:00:00:05", "wifi1": "EE:00:00:00:00:10"}

LAPTOP, PHONE, CAMERA, PRINTER, GUEST, MODEM, OFFLINE = (
    "02:11:11:11:11:11", "00:22:22:22:22:22", "00:33:33:33:33:33", "00:44:44:44:44:44",
    "00:55:55:55:55:55", "00:66:66:66:66:66", "00:77:77:77:77:77",
)


def ifaces(macs, types=None, comments=None):
    return [{"name": n, "type": (types or {}).get(n, "ether"), "mac-address": m, "running": True,
             "comment": (comments or {}).get(n, "")} for n, m in macs.items()]


def host(mac, port, local=False, bridge="bridge"):
    return {"mac-address": mac, "on-interface": port, "bridge": bridge, "local": local}


RAW = {
    "root.lan": {
        "system/identity": [{"name": "MikroTik"}],
        "interface": ifaces(R_MAC, comments={"ether1": "WAN"}) + [
            {"name": "wlan1", "type": "wlan", "mac-address": "AA:00:00:00:00:A1", "running": True},
            {"name": "guest-ap", "type": "wlan", "mac-address": "AA:00:00:00:00:A2", "running": True},
            # CAPsMAN interface carries the CAP radio's MAC
            {"name": "cap1", "type": "cap", "mac-address": C_MAC["wlan1"], "running": True},
            {"name": "bridge", "type": "bridge", "mac-address": R_MAC["ether2"], "running": True},
            {"name": "guest-bridge", "type": "bridge", "mac-address": "AA:00:00:00:00:A2", "running": True},
        ],
        "interface/bridge": [{"name": "bridge"}, {"name": "guest-bridge"}],
        "interface/bridge/port": [
            {"interface": "ether2", "bridge": "bridge"}, {"interface": "ether4", "bridge": "bridge"},
            {"interface": "wlan1", "bridge": "bridge"}, {"interface": "cap1", "bridge": "bridge"},
            {"interface": "guest-ap", "bridge": "guest-bridge"},
        ],
        "interface/bridge/host": [
            host(R_MAC["ether2"], "bridge", local=True),
            host(C_MAC["wlan1"], "cap1", local=True),
            host(C_MAC["ether2"], "ether2"), host(CAMERA, "ether2"),
            host(W_MAC["ether1"], "ether4"), host(PRINTER, "ether4"),
            host(LAPTOP, "wlan1"), host(PHONE, "cap1"), host(GUEST, "guest-ap", bridge="guest-bridge"),
        ],
        "ip/neighbor": [
            {"interface": "ether2,bridge", "mac-address": C_MAC["ether2"], "identity": "AP1"},
            {"interface": "ether4,bridge", "mac-address": W_MAC["ether5"], "identity": "hAP"},
        ],
        "ip/dhcp-server": [{"name": "defconf", "interface": "bridge"},
                           {"name": "dhcp_guest", "interface": "guest-bridge"}],
        "ip/dhcp-server/lease": [
            {"mac-address": LAPTOP, "address": "192.168.88.10", "active-address": "192.168.88.10",
             "status": "bound", "dynamic": True, "host-name": "laptop", "server": "defconf"},
            {"mac-address": CAMERA, "address": "192.168.88.20", "status": "bound", "dynamic": False,
             "comment": "Garage camera", "host-name": "ipcam", "server": "defconf"},
            {"mac-address": PRINTER, "address": "192.168.88.30", "status": "bound", "dynamic": True,
             "server": "defconf"},
            {"mac-address": GUEST, "address": "192.168.100.5", "status": "bound", "dynamic": True,
             "host-name": "visitor", "server": "dhcp_guest"},
            {"mac-address": OFFLINE, "address": "192.168.88.40", "status": "waiting", "dynamic": False,
             "comment": "Old TV", "server": "defconf"},
        ],
        "ip/arp": [
            {"address": "192.168.0.1", "mac-address": MODEM, "interface": "ether1", "complete": True},
            {"address": "192.168.88.30", "mac-address": PRINTER, "interface": "bridge", "complete": True},
        ],
        "ip/dns/static": [{"name": "printer.lan", "address": "192.168.88.30"}],
        "interface/wireless": [
            {"name": "wlan1", "ssid": "Home5", "band": "5ghz-a/n/ac", "mac-address": "AA:00:00:00:00:A1"},
            {"name": "guest-ap", "ssid": "Guest", "master-interface": "wlan1", "mac-address": "AA:00:00:00:00:A2"},
        ],
        "interface/wireless/registration-table": [
            {"interface": "wlan1", "mac-address": LAPTOP, "signal-strength": "-55@HT40-7", "tx-rate": "300Mbps-40MHz/2S"},
            {"interface": "guest-ap", "mac-address": GUEST, "signal-strength": "-70@1Mbps"},
        ],
        "caps-man/interface": [{"name": "cap1", "master-interface": "none", "radio-mac": C_MAC["wlan1"],
                                "configuration": "home", "running": True}],
        "caps-man/radio": [{"interface": "cap1", "radio-mac": C_MAC["wlan1"], "remote-cap-name": "[CC]"}],
        "caps-man/configuration": [{"name": "home", "ssid": "Home"}],
        "caps-man/registration-table": [{"interface": "cap1", "mac-address": PHONE, "rx-signal": -60, "ssid": "Home"}],
    },
    "cap.lan": {
        "interface": ifaces(C_MAC, types={"wlan1": "wlan"}) + [
            {"name": "bridge", "type": "bridge", "mac-address": C_MAC["ether2"], "running": True}],
        "interface/bridge": [{"name": "bridge"}],
        "interface/bridge/port": [{"interface": "ether2", "bridge": "bridge"}, {"interface": "ether3", "bridge": "bridge"}],
        "interface/bridge/host": [
            host(R_MAC["ether2"], "ether2"), host(W_MAC["ether1"], "ether2"),
            host(LAPTOP, "ether2"), host(PRINTER, "ether2"), host(CAMERA, "ether3"),
        ],
        # MNDP leaks through root's bridge: cap "sees" wifi7 on ether2 too.
        "ip/neighbor": [
            {"interface": "ether2,bridge", "mac-address": R_MAC["ether2"], "identity": "MikroTik"},
            {"interface": "ether2,bridge", "mac-address": W_MAC["ether5"], "identity": "hAP"},
        ],
        "interface/wireless": [{"name": "wlan1", "ssid": "Home", "band": "2ghz-b/g/n",
                                "mac-address": C_MAC["wlan1"], "disabled": True}],
    },
    "wifi7.lan": {
        "interface": ifaces(W_MAC, types={"wifi1": "wifi"}) + [
            {"name": "bridge", "type": "bridge", "mac-address": W_MAC["ether1"], "running": True}],
        "interface/bridge": [{"name": "bridge"}],
        "interface/bridge/port": [{"interface": "ether5", "bridge": "bridge"}, {"interface": "wifi1", "bridge": "bridge"}],
        "interface/bridge/host": [
            host(R_MAC["ether2"], "ether5"), host(C_MAC["ether2"], "ether5"),
            host(CAMERA, "ether5"), host(PRINTER, "wifi1"),
        ],
        "ip/neighbor": [
            {"interface": "ether5,bridge", "mac-address": R_MAC["ether4"], "identity": "MikroTik"},
            {"interface": "ether5,bridge", "mac-address": C_MAC["ether2"], "identity": "AP1"},
        ],
        "interface/wifi": [{"name": "wifi1", "configuration.ssid": "Home"}],
        "interface/wifi/registration-table": [
            {"interface": "wifi1", "mac-address": PRINTER, "signal": -48, "band": "2ghz-n", "tx-rate": 72000000}],
    },
}

CFG = {
    "root": "root.lan",
    "guest_pattern": "guest",
    "routers": [{"host": h, "name": h.split(".")[0]} for h in RAW],
}


class ModelTest(unittest.TestCase):
    def setUp(self):
        self.snap = model.build(CFG, RAW)
        self.dev = {d["mac"]: d for d in self.snap["devices"]}
        self.routers = {r["id"]: r for r in self.snap["routers"]}

    def test_tree_ignores_indirect_neighbors(self):
        cap, w = self.routers["cap.lan"], self.routers["wifi7.lan"]
        self.assertEqual((cap["parent"], cap["parent_group"], cap["uplink_port"]),
                         ("root.lan", "root.lan|ether2", "ether2"))
        self.assertEqual((w["parent"], w["parent_group"], w["uplink_port"]),
                         ("root.lan", "root.lan|ether4", "ether5"))

    def test_capsman_client_is_placed_on_the_cap(self):
        self.assertEqual(self.dev[PHONE]["group"], "cap.lan|capsman:cap1")
        group = next(g for g in self.snap["groups"] if g["id"] == "cap.lan|capsman:cap1")
        self.assertEqual((group["ssid"], group["band"]), ("Home", "2.4 GHz"))

    def test_placement(self):
        self.assertEqual(self.dev[LAPTOP]["group"], "root.lan|wlan1")
        self.assertEqual(self.dev[LAPTOP]["wifi"]["signal"], -55)
        self.assertEqual(self.dev[CAMERA]["group"], "cap.lan|ether3")      # not root's uplink ether2
        self.assertEqual(self.dev[PRINTER]["group"], "wifi7.lan|wifi1")
        self.assertEqual(self.dev[PRINTER]["wifi"]["tx_rate"], "72 Mbps")
        self.assertEqual(self.dev[MODEM]["group"], "root.lan|ether1")      # routed port, via ARP
        self.assertFalse(self.dev[OFFLINE]["online"])
        self.assertIsNone(self.dev[OFFLINE]["group"])

    def test_vlan_link_is_not_a_location(self):
        """A guest that left ap1's AP still sits in both bridge tables for a while; it belongs to
        ap1's AP, not to router's VLAN link (which comes first in router order)."""
        import copy
        raw = copy.deepcopy(RAW)
        raw["root.lan"]["interface"].append({"name": "vlan-guest", "type": "vlan", "mac-address": R_MAC["ether2"]})
        raw["root.lan"]["interface/bridge/port"].append({"interface": "vlan-guest", "bridge": "guest-bridge"})
        raw["root.lan"]["interface/bridge/host"].append(host("00:99:99:99:99:99", "vlan-guest", bridge="guest-bridge"))
        raw["cap.lan"]["interface"].append({"name": "guest-ap", "type": "wlan", "mac-address": "CC:00:00:00:00:98"})
        raw["cap.lan"]["interface/bridge/port"].append({"interface": "guest-ap", "bridge": "guest-bridge"})
        raw["cap.lan"]["interface/wireless"] = [{"name": "guest-ap", "ssid": "Guest", "master-interface": "wlan1",
                                                 "mac-address": "CC:00:00:00:00:98"}]
        raw["cap.lan"]["interface/bridge/host"].append(host("00:99:99:99:99:99", "guest-ap", bridge="guest-bridge"))
        dev = {d["mac"]: d for d in model.build(CFG, raw)["devices"]}
        self.assertEqual(dev["00:99:99:99:99:99"]["group"], "cap.lan|guest-ap")
        # Seen only on the VLAN link: online, location unknown (never "on vlan-guest").
        raw["cap.lan"]["interface/bridge/host"].pop()
        snap = model.build(CFG, raw)
        d = next(x for x in snap["devices"] if x["mac"] == "00:99:99:99:99:99")
        self.assertEqual((d["online"], d["group"]), (True, None))
        self.assertNotIn("root.lan|vlan-guest", [g["id"] for g in snap["groups"]])

    def test_unreachable_router_stays_a_router(self):
        """wifi7 reboots: its MACs still sit in root's bridge table, but wifi7 itself can't be read."""
        raws = {k: v for k, v in RAW.items() if k != "wifi7.lan"}
        errors = {"wifi7.lan": "rebooting"}
        # Without memory, wifi7's MAC shows up as a device on root's ether4 ...
        naive = {d["mac"]: d for d in model.build(CFG, raws, errors)["devices"]}
        self.assertIn(W_MAC["ether1"], naive)
        # ... with the MACs remembered from an earlier poll it doesn't, and ether4 stays its uplink.
        known = model.build(CFG, RAW)["router_macs"]
        snap = model.build(CFG, raws, errors, known)
        self.assertNotIn(W_MAC["ether1"], {d["mac"] for d in snap["devices"]})
        w = next(r for r in snap["routers"] if r["id"] == "wifi7.lan")
        self.assertEqual((w["online"], w["parent_group"]), (False, "root.lan|ether4"))

    def test_routers_are_not_devices(self):
        for mac in [*R_MAC.values(), *C_MAC.values(), *W_MAC.values()]:
            self.assertNotIn(mac, self.dev)

    def test_v6_signal_ignores_rate_outliers(self):
        from netmap.model import v6_signal
        printer = {"signal-strength": "-89@11Mbps", "strength-at-rates":
                   "-72@1Mbps 1m42s520ms,-89@11Mbps 340ms,-69@24Mbps 24s170ms,-72@HT20-4 690ms,"
                   "-66@HT20-6 53s50ms,-71@HT20-7 680ms"}
        self.assertEqual(v6_signal(printer), -71)          # median of the 5 rates heard within 60 s
        self.assertEqual(v6_signal({"signal-strength": "-60@1Mbps"}), -60)          # no per-rate data
        self.assertEqual(v6_signal({"signal-strength": "-60@1Mbps", "strength-at-rates": "-80@6Mbps 5m"}), -60)

    def test_names(self):
        self.assertEqual((self.dev[CAMERA]["name"], self.dev[CAMERA]["name_source"]), ("Garage camera", "DHCP lease comment"))
        self.assertEqual(self.dev[PRINTER]["name"], "printer.lan")
        self.assertEqual(self.dev[LAPTOP]["name"], "laptop")

    def test_back_from_guest_network(self):
        """A phone tried the guest network and came back: both leases are still bound."""
        import copy
        raw = copy.deepcopy(RAW)
        raw["root.lan"]["ip/dhcp-server/lease"] += [
            {"mac-address": LAPTOP, "address": "192.168.100.20", "active-address": "192.168.100.20",
             "status": "bound", "dynamic": True, "server": "dhcp_guest", "last-seen": "20m"},
        ]
        # The main lease comes first, the guest lease last: the guest lease used to win.
        dev = {d["mac"]: d for d in model.build(CFG, raw)["devices"]}
        self.assertEqual((dev[LAPTOP]["guest"], dev[LAPTOP]["ip"]), (False, "192.168.88.10"))   # on wlan1
        # Now on the guest AP: guest, with the guest lease.
        raw["root.lan"]["interface/wireless/registration-table"][0]["interface"] = "guest-ap"
        dev = {d["mac"]: d for d in model.build(CFG, raw)["devices"]}
        self.assertEqual((dev[LAPTOP]["guest"], dev[LAPTOP]["ip"]), (True, "192.168.100.20"))

    def test_guest(self):
        self.assertTrue(self.dev[GUEST]["guest"])
        self.assertEqual(self.dev[GUEST]["group"], "root.lan|guest-ap")
        self.assertFalse(self.dev[LAPTOP]["guest"])


class StoreTest(unittest.TestCase):
    def test_baseline_history_and_events(self):
        store = Store(":memory:")
        snap = model.build(CFG, RAW)
        self.assertEqual(store.merge(snap, offline_after=60, now=1000), [])
        self.assertFalse(any(d["new"] for d in snap["devices"]))     # existing network isn't "new"

        # The camera disappears; a new device shows up.
        raw = {**RAW, "cap.lan": {**RAW["cap.lan"], "interface/bridge/host": [
            host(R_MAC["ether2"], "ether2"), host("00:88:88:88:88:88", "ether3")]}}
        snap = model.build(CFG, raw)
        events = store.merge(snap, offline_after=60, now=1030)
        dev = {d["mac"]: d for d in snap["devices"]}
        self.assertIn({"type": "new_device", "mac": "00:88:88:88:88:88"}, events)
        self.assertTrue(dev["00:88:88:88:88:88"]["new"])
        self.assertTrue(dev[CAMERA]["online"])                       # within grace period
        self.assertEqual(dev[CAMERA]["group"], "cap.lan|ether3")

        snap = model.build(CFG, raw)
        events = store.merge(snap, offline_after=60, now=1100)
        dev = {d["mac"]: d for d in snap["devices"]}
        self.assertFalse(dev[CAMERA]["online"])
        self.assertEqual((dev[CAMERA]["group"], dev[CAMERA]["last_seen"]), ("cap.lan|ether3", 1000))
        self.assertIn({"type": "offline", "mac": CAMERA}, events)


class ColorsTest(unittest.TestCase):
    def test_defaults_and_overrides(self):
        colors = load_colors({"wifi": "orange", "port": " #123 "})
        self.assertEqual((colors["wifi"], colors["port"]), ("orange", "#123"))
        self.assertEqual(colors["router"], COLOR_DEFAULTS["router"])

    def test_rejects_unknown_and_invalid(self):
        with self.assertRaises(ValueError):
            load_colors({"wireless": "#fff"})
        with self.assertRaises(ValueError):
            load_colors({"wifi": "red; background: url(x)"})


class TagsTest(unittest.TestCase):
    TAGS = {
        "IoT": {"color": "#06b6d4", "match": [
            {"vendor": "Espressif"}, {"name": "^IoT:"}, {"subnet": "192.168.66.0/24"}, {"mac": ["02:AA:BB:*"]},
        ], "exclude": [{"name": "Thinknode"}]},
        "Phone": {"match": [{"vendor": "Google|Samsung", "kind": "wifi"}, {"name": "pixel"}]},
        "Cellar": {"match": [{"router": "ap1", "interface": "ether[3-8]"}]},
    }

    def tag(self, **device):
        d = {"mac": "00:00:00:00:00:01", "name": "x", "group": None, "kind": None, **device}
        groups = {"k|ether3": {"id": "k|ether3", "router": "k", "iface": "ether3"}}
        tags.apply(load_tags(self.TAGS), [d], groups, {"k": {"id": "k", "name": "ap1"}})
        return d["tags"], d["tag_reasons"]

    def test_any_rule_matches(self):
        self.assertEqual(self.tag(vendor="Espressif Inc")[0], ["IoT"])
        self.assertEqual(self.tag(name="IoT: PV1")[0], ["IoT"])
        self.assertEqual(self.tag(ip="192.168.66.10", ips=["192.168.66.10"])[0], ["IoT"])
        self.assertEqual(self.tag(mac="02:AA:BB:00:00:01")[0], ["IoT"])
        self.assertEqual(self.tag(vendor="Espressif")[1], {"IoT": "vendor Espressif"})

    def test_fields_in_a_rule_are_anded(self):
        self.assertEqual(self.tag(vendor="Samsung", kind="wired")[0], [])
        self.assertEqual(self.tag(vendor="Samsung", kind="wifi")[0], ["Phone"])
        self.assertEqual(self.tag(group="k|ether3")[0], ["Cellar"])

    def test_exclude_and_multiple_tags(self):
        self.assertEqual(self.tag(vendor="Espressif", name="Thinknode M7")[0], [])
        self.assertEqual(self.tag(vendor="Espressif", name="Pixel-ish", group="k|ether3")[0], ["IoT", "Phone", "Cellar"])

    def test_invalid_config(self):
        for bad in ({"T": {"match": [{"vendr": "x"}]}}, {"T": {"match": [{"name": "("}]}},
                    {"T": {"match": [{"subnet": "300.1.1.0/24"}]}}, {"T": {"match": [{"kind": "lan"}]}},
                    {"T": {"color": "nope;", "match": []}}, {"T": {"matches": []}}):
            with self.assertRaises(ValueError, msg=bad):
                load_tags(bad)


class TrafficTest(unittest.TestCase):
    CFG = {"traffic": {"floor_mbps": 1, "half_life_s": 100, "bands": [[5, "#0f0"]],
                       "wan": {"router": "r", "interface": "ether1", "down_mbps": 50, "up_mbps": 5}}}

    def raw(self, k):
        """Counters after k seconds at fixed rates."""
        return {"r1": {
            "interface": [
                {"name": "ether1", "type": "ether", "tx-byte": 125_000 * k, "rx-byte": 3_125_000 * k},  # WAN: 25M down, 1M up
                {"name": "ether2", "type": "ether", "tx-byte": 250_000 * k, "rx-byte": 0},              # 2M to one device
                {"name": "ether3", "type": "ether", "tx-byte": 125_000 * k, "rx-byte": 0},              # 1M, two devices
                {"name": "wlan1", "type": "wlan", "tx-byte": 1_000_000 * k, "rx-byte": 0},              # AP: 8M
            ],
            "interface/ethernet": [{"name": n, "advertise": "100M-full,1000M-full"} for n in ("ether1", "ether2", "ether3")],
            "ethernet/monitor": [{"name": "ether1", "status": "link-ok", "rate": "1Gbps"},
                                 {"name": "ether2", "status": "link-ok", "rate": "100Mbps"}],
            "interface/wireless/registration-table": [
                {"mac-address": "00:00:00:00:00:0A", "bytes": f"{750_000 * k},{0}"},                   # 6M of the AP's 8M
            ],
        }}

    def snap(self):
        g = lambda iface, kind="port": {"id": f"r1|{iface}", "router": "r1", "on": "r1", "kind": kind, "iface": iface}
        d = lambda mac, grp, kind: {"mac": mac, "online": True, "group": f"r1|{grp}", "kind": kind}
        return {
            "routers": [{"id": "r1", "name": "r"}],
            "groups": [g("ether1"), g("ether2"), g("ether3"), g("wlan1", "ap")],
            "devices": [d("00:00:00:00:00:01", "ether1", "wired"), d("00:00:00:00:00:02", "ether2", "wired"),
                        d("00:00:00:00:00:03", "ether3", "wired"), d("00:00:00:00:00:04", "ether3", "wired"),
                        d("00:00:00:00:00:0A", "wlan1", "wifi")],
        }

    def run_polls(self):
        from netmap.traffic import Traffic
        t = Traffic(self.CFG)
        first = self.snap()
        t.annotate(first, self.raw(0), now=0)
        second = self.snap()
        t.annotate(second, self.raw(10), now=10)
        return first, second, t

    def test_first_poll_has_no_rates(self):
        first, _, _ = self.run_polls()
        self.assertFalse(any("traffic" in g for g in first["groups"]))

    def test_rates_shares_and_shared_ports(self):
        _, s, _ = self.run_polls()
        dev = {d["mac"][-2:]: d.get("traffic") for d in s["devices"]}
        self.assertAlmostEqual(dev["02"]["down"], 2e6)
        self.assertAlmostEqual(dev["02"]["link_pct"], 2.0)          # 2M of a 100M link
        self.assertEqual(dev["03"], {"shared": True})
        self.assertEqual(dev["04"], {"shared": True})
        self.assertAlmostEqual(dev["0A"]["down"], 6e6)
        self.assertAlmostEqual(dev["0A"]["ap_pct"], 100.0)     # the AP's only client (AP interface counts 8M)
        self.assertEqual(next(g for g in s["groups"] if g["iface"] == "wlan1")["traffic"]["clients"], 1)

    def test_ap_share_never_exceeds_100(self):
        """Two clients whose counters together exceed the AP interface's own counter."""
        from netmap.traffic import Traffic
        def raw(k):
            r = self.raw(k)
            r["r1"]["interface"][3]["tx-byte"] = 100_000 * k                      # AP interface: 0.8M
            r["r1"]["interface/wireless/registration-table"] = [
                {"mac-address": "00:00:00:00:00:0A", "bytes": f"{300_000 * k},0"},  # 2.4M
                {"mac-address": "00:00:00:00:00:0B", "bytes": f"{100_000 * k},0"},  # 0.8M
            ]
            return r
        snaps = []
        t = Traffic(self.CFG)
        for k in (0, 10):
            s = self.snap()
            s["devices"].append({"mac": "00:00:00:00:00:0B", "online": True, "group": "r1|wlan1", "kind": "wifi"})
            t.annotate(s, raw(k), now=k)
            snaps.append(s)
        pct = {d["mac"][-2:]: d["traffic"]["ap_pct"] for d in snaps[1]["devices"] if d.get("traffic", {}).get("ap_pct") is not None}
        self.assertAlmostEqual(pct["0A"], 75.0)
        self.assertAlmostEqual(pct["0B"], 25.0)

    def test_relative_heat_and_absolute_wan(self):
        _, s, _ = self.run_polls()
        dev = {d["mac"][-2:]: d["traffic"] for d in s["devices"]}
        self.assertAlmostEqual(dev["0A"]["heat"], 1.0)               # busiest device
        self.assertAlmostEqual(dev["02"]["heat"], 2 / 6)
        wan = s["traffic"]["wan"]
        self.assertAlmostEqual((wan["down"], wan["up"]), (25e6, 1e6))  # WAN: down = port rx
        self.assertAlmostEqual(wan["pct"], 50.0)                     # 25 of 50 Mbit/s
        self.assertAlmostEqual(dev["01"]["heat"], 0.5)               # modem on WAN port: line %, not relative
        self.assertAlmostEqual(s["traffic"]["ref"]["devices"], 6e6)  # modem not part of the reference

    def test_config_validation(self):
        from netmap.config import load_traffic
        routers = [{"host": "192.168.88.1", "name": "router"}]
        t = load_traffic({"bands": {60: "#f00", 5: "#0f0"}}, routers)
        self.assertEqual(t["bands"], [[5, "#0f0"], [60, "#f00"]])
        for bad in ({"bands": {150: "#f00"}}, {"bands": {5: "hot!"}}, {"floor_mbps": 0}, {"speed": 1},
                    {"wan": {"router": "nope", "interface": "ether1", "down_mbps": 50, "up_mbps": 5}},
                    {"wan": {"router": "router", "interface": "ether1", "down_mbps": 50}}):
            with self.assertRaises(ValueError, msg=bad):
                load_traffic(bad, routers)

    def test_counter_reset_and_decay(self):
        from netmap.traffic import Counters, Peak
        c = Counters()
        c.rate("k", 0, 1000, 1000)
        self.assertIsNone(c.rate("k", 10, 10, 10))                   # router rebooted
        p = Peak(floor=1e6, half_life=100)
        p.update(8e6, 0)
        self.assertAlmostEqual(p.update(0, 100), 4e6)                # halves per half-life
        self.assertAlmostEqual(p.update(0, 1000), 1e6)               # never below floor

    def uplink_setup(self):
        """r2 hangs off r1's ether2; the link negotiated 100M although both ends do 1G."""
        snap = {"routers": [{"id": "r1", "name": "r"}, {"id": "r2", "name": "w", "parent": "r1",
                                                        "parent_group": "r1|ether2", "uplink_port": "ether5"}],
                "groups": [{"id": "r1|ether2", "router": "r1", "on": "r1", "kind": "port", "iface": "ether2"}],
                "devices": []}
        raws = self.raw(0)
        raws["r2"] = {"interface/ethernet": [{"name": "ether5", "advertise": "1G-baseT-full"}],
                      "ethernet/monitor": [{"name": "ether5", "status": "link-ok", "rate": "100Mbps"}]}
        return snap, raws

    def test_uplink_link_warning(self):
        from netmap.traffic import Traffic
        snap, raws = self.uplink_setup()
        Traffic(self.CFG).annotate(snap, raws, now=0)
        self.assertIn("negotiated 100M, but both ends allow 1G", snap["groups"][0]["link"]["warning"])

    def test_link_limit_explains_slow_uplink(self):
        from netmap.traffic import Traffic
        snap, raws = self.uplink_setup()
        cfg = {"traffic": {**self.CFG["traffic"], "link_limits": [
            {"router": "r", "interface": "ether2", "mbps": 100, "note": "via DSL modem"}]}}
        Traffic(cfg).annotate(snap, raws, now=0)
        link = snap["groups"][0]["link"]
        self.assertEqual(link["note"], "via DSL modem")
        self.assertNotIn("warning", link)
        # Still warns when the link is slower than even the limit
        raws["r2"]["ethernet/monitor"][0]["rate"] = "10Mbps"
        raws["r1"]["ethernet/monitor"][1]["rate"] = "10Mbps"
        del snap["groups"][0]["link"]
        Traffic(cfg).annotate(snap, raws, now=0)
        self.assertIn("but the configured limit allows 100M", snap["groups"][0]["link"]["warning"])


class ForgetTest(unittest.TestCase):
    def dev(self, mac, last, guest=False, lease=None, online=False):
        return {"mac": mac, "online": online, "last_seen": last, "guest": guest, "lease": lease}

    def test_who_is_forgotten(self):
        from netmap.store import forgettable
        cutoff = 1000
        self.assertTrue(forgettable(self.dev("7E:00:00:00:00:01", 500), cutoff))              # random MAC, stale
        self.assertTrue(forgettable(self.dev("00:11:22:33:44:55", 500, guest=True), cutoff))  # guest, real MAC
        self.assertFalse(forgettable(self.dev("00:11:22:33:44:55", 500), cutoff))             # real MAC: forever
        self.assertFalse(forgettable(self.dev("7E:00:00:00:00:01", 1500), cutoff))            # seen recently
        self.assertFalse(forgettable(self.dev("7E:00:00:00:00:01", 500, online=True), cutoff))
        self.assertFalse(forgettable(self.dev("7E:00:00:00:00:01", 500, lease={"comment": "Anna phone", "dynamic": True}), cutoff))
        self.assertFalse(forgettable(self.dev("7E:00:00:00:00:01", 500, lease={"dynamic": False}), cutoff))  # static lease
        self.assertTrue(forgettable(self.dev("7E:00:00:00:00:01", 500, lease={"dynamic": True}), cutoff))

    def test_guest_flag_survives_and_forget_deletes(self):
        from netmap.history import History
        store, hist = Store(":memory:"), History(":memory:")
        guest = {"mac": "00:11:22:33:44:55", "name": "laptop", "ip": None, "group": "r|guest-ap", "kind": "wifi",
                 "online": True, "guest": True}
        store.merge({"devices": [dict(guest)]}, offline_after=60, now=100)
        snap = {"devices": []}                                           # gone from the router tables
        store.merge(snap, offline_after=60, now=200)
        self.assertTrue(snap["devices"][0]["guest"])                    # remembered from the store
        hist.db.execute("INSERT INTO hour VALUES ('d:00:11:22:33:44:55', 0, 1, 1, 1, 1, NULL, NULL, NULL)")
        store.forget(["00:11:22:33:44:55"])
        hist.forget(["d:00:11:22:33:44:55"])
        snap = {"devices": []}
        store.merge(snap, offline_after=60, now=300)
        self.assertEqual(snap["devices"], [])
        self.assertEqual(hist.db.execute("SELECT count(*) FROM hour").fetchone()[0], 0)


class NamesTest(unittest.TestCase):
    def test_precedence_ui_config_router(self):
        import tempfile
        from pathlib import Path
        from netmap.server import Poller
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            cfg = {**CFG, "data_dir": Path(tmp), "names": {"00:00:00:00:00:02": "From config", "00:00:00:00:00:03": "Cfg"},
                   "traffic": {"floor_mbps": 1, "half_life_s": 120, "bands": [], "wan": None}}
            p = Poller(cfg)
            p.store.set_name("00:00:00:00:00:03", "From UI")
            snap = {"devices": [
                {"mac": f"00:00:00:00:00:0{i}", "name": "lease comment", "name_source": "DHCP lease comment"}
                for i in (1, 2, 3)]}
            p.apply_names(snap)
            got = [(d["name"], d["name_source"], d["auto_name"]) for d in snap["devices"]]
            self.assertEqual(got, [("lease comment", "DHCP lease comment", "lease comment"),
                                   ("From config", "netmap (config)", "lease comment"),
                                   ("From UI", "netmap (UI)", "lease comment")])
            p.store.set_name("00:00:00:00:00:03", "")                   # cleared: config name again
            p.apply_names(snap)
            self.assertEqual(snap["devices"][2]["name"], "Cfg")
            p.store.db.close(); p.history.db.close()

    def test_rename_updates_published_snapshot_at_once(self):
        import json
        import tempfile
        from pathlib import Path
        from netmap.server import Poller
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            cfg = {**CFG, "data_dir": Path(tmp), "names": {}, "offline_after": 60, "forget_private_after_days": 0,
                   "poll_interval": 10, "colors": {}, "tags": [],
                   "traffic": {"floor_mbps": 1, "half_life_s": 120, "bands": [], "wan": None}}
            p = Poller(cfg, replay=RAW)
            p.poll_once()
            published = lambda: {d["mac"]: d for d in json.loads(p.snapshot)["devices"]}
            auto = published()[LAPTOP]["name"]
            self.assertEqual(p.rename(LAPTOP, "Office laptop"), ("Office laptop", "netmap (UI)"))
            self.assertEqual(published()[LAPTOP]["name"], "Office laptop")    # before any new poll
            self.assertEqual(p.rename(LAPTOP, ""), (auto, "DHCP host-name"))
            self.assertEqual(published()[LAPTOP]["name"], auto)
            p.store.db.close(); p.history.db.close()

    def test_held_by(self):
        from netmap.store import held_by
        base = {"mac": "00:11:22:33:44:55", "online": False, "lease": None}
        self.assertEqual(held_by({**base, "online": True}), [])
        self.assertEqual(held_by({**base, "stored_only": True}), [])
        self.assertEqual(held_by({**base, "lease": {"dynamic": False}}), ["static DHCP lease"])
        self.assertEqual(held_by({**base, "lease": {"dynamic": True}}), ["DHCP lease (expires by itself)"])
        self.assertEqual(held_by(base), ["ARP entry on a router (expires by itself)"])

    def test_forget_device(self):
        import json
        import tempfile
        import time
        from pathlib import Path
        from netmap.server import Poller
        gone = "00:99:99:99:99:01"   # in netmap's memory only, no router table knows it
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            cfg = {**CFG, "data_dir": Path(tmp), "names": {}, "offline_after": 60, "forget_private_after_days": 0,
                   "poll_interval": 10, "colors": {}, "tags": [],
                   "traffic": {"floor_mbps": 1, "half_life_s": 120, "bands": [], "wan": None}}
            p = Poller(cfg, replay=RAW)
            old = time.time() - 86400
            p.store.db.execute("INSERT INTO devices (mac, first_seen, last_seen, name) VALUES (?, ?, ?, 'Old TV')",
                               (gone, old, old))
            p.store.set_name(gone, "Old TV")
            p.history.db.execute("INSERT INTO minute (series, t) VALUES (?, ?)", ("d:" + gone, int(old)))
            p.poll_once()
            published = lambda: {d["mac"]: d for d in json.loads(p.snapshot)["devices"]}
            self.assertEqual((published()[gone]["forgettable"], published()[gone]["held_by"]), (True, []))
            self.assertFalse(published()[LAPTOP]["forgettable"])
            self.assertIn("still known", p.forget(LAPTOP))
            self.assertIsNone(p.forget(gone))
            self.assertNotIn(gone, published())                         # before any new poll
            self.assertNotIn(gone, p.store.names())
            self.assertEqual(p.history.db.execute("SELECT count(*) FROM minute WHERE series = ?", ("d:" + gone,)).fetchone()[0], 0)
            p.poll_once()
            self.assertNotIn(gone, published())
            p.store.db.close(); p.history.db.close()

    def test_named_devices_are_not_forgotten(self):
        from netmap.store import forgettable
        d = {"mac": "7E:00:00:00:00:01", "online": False, "last_seen": 0, "guest": True, "lease": None}
        self.assertTrue(forgettable(d, 100))
        self.assertFalse(forgettable({**d, "name_source": "netmap (UI)"}, 100))
        self.assertFalse(forgettable({**d, "name_source": "netmap (config)"}, 100))

    def test_config_names(self):
        from netmap.config import load_names
        self.assertEqual(load_names({"aa-bb-cc-dd-ee-ff": " TV "}), {"AA:BB:CC:DD:EE:FF": "TV"})
        for bad in ({"AA:BB": "x"}, {"AA:BB:CC:DD:EE:FF": ""}):
            with self.assertRaises(ValueError):
                load_names(bad)


class HistoryTest(unittest.TestCase):
    T0 = 1_800_000_000 // 3600 * 3600   # start of an hour

    def snap(self, down, up, signal=None, group="r|wlan1", online=True):
        dev = {"mac": "AA", "online": online, "group": group,
               "traffic": {"down": down, "up": up}, "wifi": {"signal": signal} if signal is not None else None}
        return {"groups": [{"id": "r|ether1", "traffic": {"down": down * 2, "up": up * 2}}], "devices": [dev]}

    def test_minute_average_peak_and_signal(self):
        from netmap.history import History
        h = History(":memory:")
        for i, (down, sig) in enumerate([(100, -60), (300, -70), (200, -65)]):
            h.record(self.snap(down, 10, sig), self.T0 + i * 10)
        h.record(self.snap(0, 0, -50, group="w|wifi2"), self.T0 + 60)          # next minute: flush
        (t, down, up, down_max, up_max, sig, sig_min, ap), = h.query("d:AA", "1h", self.T0 + 61)["points"][:1]
        self.assertEqual((t, down, down_max, up, sig, sig_min, ap), (self.T0, 200, 300, 10, -65, -70, "r|wlan1"))
        self.assertEqual(h.query("g:r|ether1", "1h", self.T0 + 61)["points"][0][1], 400)

    def test_current_minute_shown_and_offline_is_a_gap(self):
        from netmap.history import History
        h = History(":memory:")
        h.record(self.snap(100, 10), self.T0)
        h.record(self.snap(100, 10, online=False), self.T0 + 120)             # device offline: no device row
        pts = h.query("d:AA", "1h", self.T0 + 125)["points"]
        self.assertEqual([p[0] for p in pts], [self.T0])

    def test_hourly_rollup_and_retention(self):
        from netmap.history import History, KEEP_MINUTES
        h = History(":memory:")
        for m in range(60):                                                    # one value per minute, hour 0
            h.record(self.snap(m * 10, 1, -60 - m % 2, group="r|wlan1" if m < 59 else "k|wlan1"), self.T0 + m * 60)
        h.record(self.snap(0, 0), self.T0 + 3600 + 5)                          # into hour 1: flush + rollup
        rows = h.db.execute("SELECT t, down, down_max, sig_min, ap FROM hour WHERE series='d:AA'").fetchall()
        self.assertEqual(rows, [(self.T0, 295.0, 590, -61, "k|wlan1")])       # avg, peak, worst, last AP
        self.assertEqual(h.query("d:AA", "30d", self.T0 + 3605)["points"][0][1], 295.0)
        h.record(self.snap(0, 0), self.T0 + KEEP_MINUTES + 7200)
        self.assertEqual(h.db.execute("SELECT count(*) FROM minute WHERE t < ?", (self.T0 + 3600,)).fetchone()[0], 0)
        self.assertEqual(h.db.execute("SELECT count(*) FROM hour WHERE t = ?", (self.T0,)).fetchone()[0], 2)

    def test_range_buckets(self):
        from netmap.history import History
        h = History(":memory:")
        for m in range(30):
            h.record(self.snap(100, 1), self.T0 + m * 60)
        h.record(self.snap(100, 1), self.T0 + 1800)
        pts = h.query("d:AA", "7d", self.T0 + 1801)["points"]                  # 10-minute buckets
        self.assertEqual([p[0] - self.T0 for p in pts], [0, 600, 1200])       # running minute: 1h/24h only


if __name__ == "__main__":
    unittest.main()
