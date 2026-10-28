"""Read raw RouterOS tables over API-SSL. Only ever issues `print`."""
import hashlib
import logging
import socket
import ssl
import time

from librouteros import connect
from librouteros.exceptions import MultiTrapError, TrapError

log = logging.getLogger(__name__)

# Menus that don't exist on a given router (v6 vs v7, packages) fail once and are skipped after.
PATHS = [
    "system/identity",
    "system/resource",
    "interface",
    "interface/ethernet",
    "interface/bridge",
    "interface/bridge/port",
    "interface/bridge/host",
    "ip/neighbor",
    "ip/dhcp-server",
    "ip/dhcp-server/lease",
    "ip/arp",
    "ip/dns/static",
    "interface/wireless",
    "interface/wireless/registration-table",
    "interface/wifi",
    "interface/wifi/radio",
    "interface/wifi/registration-table",
    "caps-man/interface",
    "caps-man/radio",
    "caps-man/remote-cap",
    "caps-man/configuration",
    "caps-man/registration-table",
]


def router_tls(host, fingerprint=None, ca_file=None):
    """ssl_wrapper for a router: accepts the certificate with the pinned SHA-256 fingerprint, or one
    signed by ca_file for this host (IP or name). With both, the pin is tried first; on a mismatch a
    second connection checks against the CA, so a router can move to a CA certificate without
    netmap losing it."""
    want = fingerprint.replace(":", "").lower() if fingerprint else None
    loose = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    loose.check_hostname = False
    loose.verify_mode = ssl.CERT_NONE

    def by_ca(sock):
        try:
            return ssl.create_default_context(cafile=str(ca_file)).wrap_socket(sock, server_hostname=host)
        except Exception:
            sock.close()
            raise

    def wrap(sock):
        if not want:
            return by_ca(sock)
        tls = loose.wrap_socket(sock)
        got = hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
        if got == want:
            return tls
        addr, timeout = tls.getpeername()[:2], tls.gettimeout()
        tls.close()
        if not ca_file:
            raise ssl.SSLError(f"certificate fingerprint mismatch (got {got})")
        return by_ca(socket.create_connection(addr, timeout=timeout))

    return wrap


class RouterClient:
    """Keeps one API session open; RouterOS logs every login, so we don't reconnect per poll."""

    LINK_EVERY = 60  # seconds between negotiated-speed checks

    def __init__(self, rc):
        self.rc = rc
        self.api = None
        self.unsupported = set()
        self.links, self.links_at = [], 0.0

    def fetch(self, paths=PATHS):
        if self.api is None:
            self.api = connect(
                self.rc["host"], self.rc["username"], self.rc["password"],
                port=self.rc["port"],
                ssl_wrapper=router_tls(self.rc["host"], self.rc.get("fingerprint"), self.rc.get("ca_file")),
                encoding="latin-1", timeout=10,
            )
            self.unsupported.clear()
            log.info("connected to %s", self.rc["host"])
        tables = {}
        try:
            for p in paths:
                if p in self.unsupported:
                    continue
                try:
                    tables[p] = list(self.api.path(*p.split("/")))
                except (TrapError, MultiTrapError):
                    self.unsupported.add(p)
            if paths is PATHS and time.monotonic() - self.links_at > self.LINK_EVERY:
                self.links = self.link_status([i["name"] for i in tables.get("interface", []) if i.get("type") == "ether"])
                self.links_at = time.monotonic()
        except Exception:
            self.close()
            raise
        if paths is PATHS:
            tables["ethernet/monitor"] = self.links
        return tables

    def link_status(self, names):
        """Negotiated link rate per Ethernet port (read-only `monitor ... once`)."""
        if not names:
            return []
        try:
            return list(self.api("/interface/ethernet/monitor", numbers=",".join(names), once=True))
        except (TrapError, MultiTrapError):
            return []

    def close(self):
        if self.api is not None:
            try:
                self.api.close()
            except Exception:
                pass
            self.api = None
