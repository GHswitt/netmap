import hashlib
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from netmap import certs
from netmap.collector import router_tls
from netmap.config import load_cert_watch


class CertWatchConfigTest(unittest.TestCase):
    def test_parses_host_port(self):
        self.assertEqual(load_cert_watch({"Pi-hole": "192.168.88.10:443", "netmap": "server.lan:8090"}), [
            {"name": "Pi-hole", "host": "192.168.88.10", "port": 443},
            {"name": "netmap", "host": "server.lan", "port": 8090}])

    def test_rejects_missing_port(self):
        for bad in ("192.168.88.10", "server.lan:http", ":443", "server.lan:70000"):
            with self.assertRaises(ValueError, msg=bad):
                load_cert_watch({"x": bad})


class StatusTest(unittest.TestCase):
    def test_warns_when_expiring_or_failing(self):
        now = 1_800_000_000
        ok = certs.status("a", "h:1", {"ok": True, "not_after": now + 90 * 86400, "error": None}, now, 30)
        soon = certs.status("a", "h:1", {"ok": True, "not_after": now + 10 * 86400, "error": None}, now, 30)
        bad = certs.status("a", "h:1", {"ok": False, "not_after": None, "error": "unable to get local issuer"}, now, 30)
        self.assertEqual((ok["warn"], ok["days"]), (False, 90))
        self.assertEqual((soon["warn"], soon["days"]), (True, 10))
        self.assertEqual((bad["warn"], bad["days"]), (True, None))


@unittest.skipUnless(shutil.which("openssl"), "needs the openssl CLI")
class CheckTest(unittest.TestCase):
    """A TLS server on 127.0.0.1 with a certificate from a throwaway CA."""

    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())
        d = cls.dir

        def run(*args):
            subprocess.run(["openssl", *args], check=True, capture_output=True, cwd=d)
        run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=Test CA",
            "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign",
            "-keyout", "ca.key", "-out", "ca.crt")
        run("req", "-new", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=127.0.0.1", "-keyout", "srv.key", "-out", "srv.csr")
        (d / "ext.cnf").write_text("subjectAltName = IP:127.0.0.1\nextendedKeyUsage = serverAuth\n")
        run("x509", "-req", "-in", "srv.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial",
            "-days", "20", "-extfile", "ext.cnf", "-out", "srv.crt")
        run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=Other CA",
            "-keyout", "other.key", "-out", "other.crt")

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(d / "srv.crt", d / "srv.key")
        cls.listener = socket.create_server(("127.0.0.1", 0))
        cls.port = cls.listener.getsockname()[1]

        def serve():
            while True:
                try:
                    conn, _ = cls.listener.accept()
                except OSError:
                    return
                try:
                    with ctx.wrap_socket(conn, server_side=True) as tls:
                        tls.recv(1)
                except (OSError, ssl.SSLError):
                    pass
        threading.Thread(target=serve, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.listener.close()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_trusted_certificate_reports_expiry(self):
        r = certs.check("127.0.0.1", self.port, self.dir / "ca.crt")
        self.assertTrue(r["ok"], r["error"])
        row = certs.status("srv", "x", r, __import__("time").time(), 30)
        self.assertTrue(row["warn"])                 # 20 days left < 30
        self.assertIn(row["days"], (19, 20))

    def test_other_ca_is_not_trusted(self):
        r = certs.check("127.0.0.1", self.port, self.dir / "other.crt")
        self.assertFalse(r["ok"])
        self.assertIn("certificate", r["error"])

    def test_unreachable(self):
        r = certs.check("127.0.0.1", 1, self.dir / "ca.crt", timeout=1)
        self.assertFalse(r["ok"])

    # router connections: pinned fingerprint, CA, or the pin with the CA as fallback
    def connect(self, fingerprint=None, ca_file=None):
        wrap = router_tls("127.0.0.1", fingerprint, ca_file)
        tls = wrap(socket.create_connection(("127.0.0.1", self.port), timeout=5))
        tls.close()

    def fingerprint(self):
        der = ssl.PEM_cert_to_DER_cert((self.dir / "srv.crt").read_text())
        return ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())

    def test_router_pinned(self):
        self.connect(fingerprint=self.fingerprint())

    def test_router_by_ca(self):
        self.connect(ca_file=self.dir / "ca.crt")

    def test_router_pin_mismatch_falls_back_to_ca(self):
        self.connect(fingerprint="00" * 32, ca_file=self.dir / "ca.crt")

    def test_router_rejected(self):
        with self.assertRaises(ssl.SSLError):
            self.connect(fingerprint="00" * 32)
        with self.assertRaises(ssl.SSLError):
            self.connect(fingerprint="00" * 32, ca_file=self.dir / "other.crt")
        with self.assertRaises(ssl.SSLError):
            self.connect(ca_file=self.dir / "other.crt")


if __name__ == "__main__":
    unittest.main()
