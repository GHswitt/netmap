"""MAC vendor lookup from the IEEE OUI list, refreshed monthly into the data dir."""
import csv
import logging
import re
import threading
import time
import urllib.request

log = logging.getLogger(__name__)

URL = "https://standards-oui.ieee.org/oui/oui.csv"
MAX_AGE = 30 * 24 * 3600
SUFFIX = re.compile(r"[,.]?\s+(inc|corp|corporation|co|ltd|llc|gmbh|ag|limited|technology|technologies)\b.*$", re.I)


def is_private(mac):
    """Locally administered (randomized) MAC: second bit of the first octet set."""
    try:
        return bool(int(mac[:2], 16) & 2)
    except ValueError:
        return False


class Oui:
    def __init__(self, data_dir):
        self.path = data_dir / "oui.csv"
        self.vendors = {}
        self._load()

    def vendor(self, mac):
        if not mac or is_private(mac):
            return None
        return self.vendors.get(mac.replace(":", "")[:6].upper())

    def refresh_in_background(self):
        if self.path.exists() and time.time() - self.path.stat().st_mtime < MAX_AGE:
            return
        threading.Thread(target=self._download, name="oui", daemon=True).start()

    def _download(self):
        tmp = self.path.with_suffix(".tmp")
        try:
            req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0 netmap"})
            with urllib.request.urlopen(req, timeout=60) as r:
                tmp.write_bytes(r.read())
            tmp.replace(self.path)
            self._load()
            log.info("OUI list refreshed (%d vendors)", len(self.vendors))
        except Exception as e:
            log.warning("OUI download failed: %s", e)

    def _load(self):
        if not self.path.exists():
            return
        vendors = {}
        with self.path.open(encoding="utf-8", errors="replace", newline="") as f:
            for row in csv.DictReader(f):
                name = SUFFIX.sub("", (row.get("Organization Name") or "").strip()).strip(" ,.")
                if row.get("Assignment") and name:
                    vendors[row["Assignment"].upper()] = name
        self.vendors = vendors
