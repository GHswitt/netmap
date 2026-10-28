"""Traffic and wifi-signal history in SQLite: 1-minute values for 7 days, hourly values for a year.

Series ids: "d:<MAC>" for devices, "g:<group id>" for ports/APs (the WAN port included).
Each row holds average and peak traffic (bit/s, down/up as in the snapshot), average and worst
signal (dBm) and the AP group id the client was on (last one seen in that interval).
"""
import sqlite3
import threading
from collections import defaultdict

MINUTE, HOUR = 60, 3600
KEEP_MINUTES = 7 * 86400
KEEP_HOURS = 365 * 86400

# range -> (table, bucket seconds, span seconds); about 1000-1500 points per chart at most
RANGES = {
    "1h": ("minute", MINUTE, 3600),
    "24h": ("minute", MINUTE, 86400),
    "7d": ("minute", 10 * MINUTE, 7 * 86400),
    "30d": ("hour", HOUR, 30 * 86400),
    "1y": ("hour", 6 * HOUR, 365 * 86400),
}

COLUMNS = "series TEXT, t INTEGER, down REAL, up REAL, down_max REAL, up_max REAL, sig REAL, sig_min REAL, ap TEXT"
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS minute ({COLUMNS}, PRIMARY KEY (series, t)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS hour ({COLUMNS}, PRIMARY KEY (series, t)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER);
"""


class Acc:
    """Samples of one series within the current minute."""

    __slots__ = ("n", "down", "up", "down_max", "up_max", "sn", "sig", "sig_min", "ap")

    def __init__(self):
        self.n = self.down = self.up = self.down_max = self.up_max = 0
        self.sn = self.sig = 0
        self.sig_min = None
        self.ap = None

    def traffic(self, down, up):
        self.n += 1
        self.down += down
        self.up += up
        self.down_max = max(self.down_max, down)
        self.up_max = max(self.up_max, up)

    def signal(self, dbm, ap):
        self.sn += 1
        self.sig += dbm
        self.sig_min = dbm if self.sig_min is None else min(self.sig_min, dbm)
        self.ap = ap

    def row(self, series, t):
        traffic = (self.down / self.n, self.up / self.n, self.down_max, self.up_max) if self.n else (None,) * 4
        signal = (self.sig / self.sn, self.sig_min, self.ap) if self.sn else (None, None, None)
        return (series, t, *traffic, *signal)


class History:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()
        self.acc = defaultdict(Acc)
        self.minute = None  # start of the minute being accumulated

    def record(self, snap, now):
        """Add one poll's traffic and signal values; writes finished minutes and hours."""
        minute = int(now) // MINUTE * MINUTE
        if self.minute is not None and minute != self.minute:
            self.flush()
        self.minute = minute
        for g in snap["groups"]:
            t = g.get("traffic")
            if t and "down" in t:
                self.acc["g:" + g["id"]].traffic(t["down"], t["up"])
        for d in snap["devices"]:
            if not d["online"]:
                continue
            t = d.get("traffic") or {}
            if "down" in t:
                self.acc["d:" + d["mac"]].traffic(t["down"], t["up"])
            sig = (d.get("wifi") or {}).get("signal")
            if sig is not None and d.get("group"):
                self.acc["d:" + d["mac"]].signal(sig, d["group"])
        self.rollup(now)

    def flush(self):
        if not self.acc:
            return
        rows = [a.row(series, self.minute) for series, a in self.acc.items()]
        with self.lock, self.db:
            self.db.executemany("INSERT OR REPLACE INTO minute VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        self.acc.clear()

    def rollup(self, now):
        """Aggregate every finished hour into the hour table, then drop expired rows."""
        with self.lock, self.db:
            row = self.db.execute("SELECT value FROM meta WHERE key='rolled'").fetchone()
            first = self.db.execute("SELECT min(t) FROM minute").fetchone()[0]
            start = row[0] if row else (first // HOUR * HOUR if first is not None else None)
            end = int(now) // HOUR * HOUR  # the current hour isn't finished
            if start is None or start >= end:
                return
            self.db.execute(f"""
                INSERT OR REPLACE INTO hour
                SELECT series, t / {HOUR} * {HOUR}, avg(down), avg(up), max(down_max), max(up_max),
                       avg(sig), min(sig_min),
                       (SELECT m2.ap FROM minute m2 WHERE m2.series = m.series AND m2.ap IS NOT NULL
                          AND m2.t / {HOUR} = m.t / {HOUR} ORDER BY m2.t DESC LIMIT 1)
                FROM minute m WHERE t >= ? AND t < ? GROUP BY series, t / {HOUR}""", (start, end))
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('rolled', ?)", (end,))
            self.db.execute("DELETE FROM minute WHERE t < ?", (int(now) - KEEP_MINUTES,))
            self.db.execute("DELETE FROM hour WHERE t < ?", (int(now) - KEEP_HOURS,))

    def forget(self, series_ids):
        with self.lock, self.db:
            for table in ("minute", "hour"):
                self.db.executemany(f"DELETE FROM {table} WHERE series = ?", [(s,) for s in series_ids])

    def query(self, series, range_, now):
        table, bucket, span = RANGES[range_]
        since = int(now) - span
        with self.lock:
            rows = self.db.execute(f"""
                SELECT t / {bucket} * {bucket} AS b, avg(down), avg(up), max(down_max), max(up_max),
                       avg(sig), min(sig_min), max(ap)
                FROM {table} WHERE series = ? AND t >= ? GROUP BY b ORDER BY b""", (series, since)).fetchall()
        # The minute still being collected isn't in the table yet; show it for the short ranges.
        a = self.acc.get(series) if table == "minute" and bucket == MINUTE else None
        if a and (a.n or a.sn):
            rows.append(a.row(series, self.minute)[1:])
        return {"series": series, "range": range_, "bucket": bucket, "from": since, "to": int(now),
                "points": [list(r) for r in rows]}
