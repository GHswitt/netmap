"""Check the TLS certificates of the services in cert_watch: trusted by the CA, and how long they
stay valid."""
import logging
import socket
import ssl
import threading
import time

log = logging.getLogger(__name__)
EVERY_S = 6 * 3600        # all fine: check again in 6 h
RETRY_S = 600             # a problem: check again in 10 min (e.g. a service that was restarting)


def check(host, port, ca_file, timeout=5):
    """Connect, verify against ca_file (or the system store) including the host name, and return
    {"ok", "not_after", "error"}."""
    ctx = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
        return {"ok": True, "not_after": ssl.cert_time_to_seconds(cert["notAfter"]), "error": None}
    except ssl.SSLCertVerificationError as e:
        return {"ok": False, "not_after": None, "error": e.verify_message or str(e)}
    except (OSError, ssl.SSLError) as e:
        return {"ok": False, "not_after": None, "error": f"{type(e).__name__}: {e}"}


def status(name, target, result, now, warn_days):
    """One row for the snapshot: warn when not trusted/unreachable or expiring within warn_days."""
    days = (result["not_after"] - now) / 86400 if result["not_after"] else None
    return {"name": name, "target": target, "ok": result["ok"], "not_after": result["not_after"],
            "days": None if days is None else int(days), "error": result["error"],
            "warn": not result["ok"] or days < warn_days, "checked": now}


class CertWatch:
    """Checks every cert_watch entry in a background thread; results() is what the snapshot shows."""

    def __init__(self, cfg):
        self.targets = cfg.get("cert_watch") or []
        self.ca_file = cfg.get("ca_file")
        self.warn_days = cfg.get("cert_warn_days", 30)
        self.rows = []

    def results(self):
        return list(self.rows)

    def check_all(self):
        now = time.time()
        rows = []
        for t in self.targets:
            result = check(t["host"], t["port"], self.ca_file)
            rows.append(status(t["name"], f"{t['host']}:{t['port']}", result, now, self.warn_days))
            if rows[-1]["warn"]:
                log.warning("certificate %s (%s): %s", t["name"], rows[-1]["target"],
                            result["error"] or f"expires in {rows[-1]['days']} days")
        self.rows = rows
        return rows

    def run(self):
        while True:
            try:
                rows = self.check_all()
            except Exception:
                log.exception("certificate check failed")
                rows = [{"warn": True}]
            time.sleep(RETRY_S if any(r["warn"] for r in rows) else EVERY_S)

    def start(self):
        if self.targets:
            threading.Thread(target=self.run, name="certs", daemon=True).start()
