"""SQLite memory of every device ever seen: first/last seen and last known place."""
import sqlite3
import threading
import time

from .oui import is_private

NEW_FOR = 24 * 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    mac TEXT PRIMARY KEY,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    grp TEXT,
    kind TEXT,
    ip TEXT,
    name TEXT,
    guest INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value REAL);
CREATE TABLE IF NOT EXISTS names (mac TEXT PRIMARY KEY, name TEXT NOT NULL, updated REAL NOT NULL)
"""


def blank_device(mac):
    return {"mac": mac, "name": None, "name_source": None, "ip": None, "ips": [], "hostname": None,
            "guest": False, "online": False, "group": None, "kind": None, "wifi": None, "lldp": None,
            "lease": None}


def forgettable(d, cutoff):
    """Offline since before cutoff, with a random MAC or on the guest network, and not given an
    identity by you (a DHCP lease comment or a static lease)."""
    lease = d.get("lease") or {}
    if lease.get("comment") or lease.get("dynamic") is False or str(d.get("name_source", "")).startswith("netmap"):
        return False
    return (not d["online"] and d.get("last_seen") is not None and d["last_seen"] < cutoff
            and (is_private(d["mac"]) or d.get("guest")))


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        if "guest" not in [c[1] for c in self.db.execute("PRAGMA table_info(devices)")]:
            self.db.execute("ALTER TABLE devices ADD COLUMN guest INTEGER NOT NULL DEFAULT 0")
        self.lock = threading.Lock()
        self.online = None  # MACs online after the previous merge; None until the first one
        # Devices found by the very first poll are the existing network, not "new".
        row = self.db.execute("SELECT value FROM meta WHERE key='baseline'").fetchone()
        self.baseline = row[0] if row else None

    def merge(self, snap, offline_after, now=None):
        """Record who is seen now and fold history into snap["devices"]. Returns events."""
        now = now or time.time()
        events = []
        with self.lock, self.db:
            if self.baseline is None:
                self.baseline = now
                self.db.execute("INSERT INTO meta VALUES ('baseline', ?)", (now,))
            known = {r[0]: r[1:] for r in self.db.execute(
                "SELECT mac, first_seen, last_seen, grp, kind, ip, name, guest FROM devices")}
            by_mac = {d["mac"]: d for d in snap["devices"]}
            for mac, row in known.items():
                if mac not in by_mac:
                    d = by_mac[mac] = blank_device(mac)
                    d["name"], d["name_source"] = row[5], "last known" if row[5] else None
                    d["guest"] = bool(row[6])
                    snap["devices"].append(d)

            for d in snap["devices"]:
                row = known.get(d["mac"])
                if d["online"]:  # seen in a router table right now
                    if row is None and now > self.baseline:
                        events.append({"type": "new_device", "mac": d["mac"]})
                    self.db.execute(
                        "INSERT INTO devices (mac, first_seen, last_seen, grp, kind, ip, name, guest) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(mac) DO UPDATE SET "
                        "last_seen=excluded.last_seen, grp=excluded.grp, kind=excluded.kind, "
                        "ip=COALESCE(excluded.ip, ip), name=COALESCE(excluded.name, name), guest=excluded.guest",
                        (d["mac"], now, now, d["group"], d["kind"], d["ip"], d["name"], int(bool(d["guest"]))))
                    d["first_seen"], d["last_seen"] = row[0] if row else now, now
                elif row:
                    first, last, grp, kind, ip, _, guest = row
                    d.update(first_seen=first, last_seen=last, online=now - last < offline_after,
                             group=d["group"] or grp, kind=d["kind"] or kind, ip=d["ip"] or ip,
                             guest=d["guest"] or bool(guest))
                else:
                    d["first_seen"] = d["last_seen"] = None
                d["new"] = bool(d["first_seen"] and d["first_seen"] > self.baseline
                                and now - d["first_seen"] < NEW_FOR)

        online = {d["mac"] for d in snap["devices"] if d["online"]}
        if self.online is not None:
            events += [{"type": "online", "mac": m} for m in sorted(online - self.online)]
            events += [{"type": "offline", "mac": m} for m in sorted(self.online - online)]
        self.online = online
        return events

    def names(self):
        """Names given in the web UI: {mac: name}."""
        with self.lock:
            return dict(self.db.execute("SELECT mac, name FROM names"))

    def set_name(self, mac, name):
        """Empty name removes it (the automatic name comes back)."""
        with self.lock, self.db:
            if name:
                self.db.execute("INSERT OR REPLACE INTO names VALUES (?, ?, ?)", (mac, name, time.time()))
            else:
                self.db.execute("DELETE FROM names WHERE mac = ?", (mac,))

    def forget(self, macs):
        with self.lock, self.db:
            self.db.executemany("DELETE FROM devices WHERE mac = ?", [(m,) for m in macs])
