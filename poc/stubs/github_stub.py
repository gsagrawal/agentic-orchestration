"""Stand-in for api.github.com. Prints every header it receives and records it,
so a test (or you) can see exactly what credential reached the destination.
It also echoes the Authorization header back in the body, so the proxy's
response scrubbing has something to scrub."""
import base64
import datetime
import json
import ssl
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

HOST = "api.github.test"
OUT = Path("/stub")


def make_cert() -> None:
    """Self-signed cert for HOST; the public part is shared so the proxy can verify its upstream. Created once and
    kept: the proxy trusts this exact certificate, so a stub restart must not change it."""
    if (OUT / "cert.pem").exists() and (OUT / "key.pem").exists():
        return
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOST)]), critical=False)
            .sign(key, hashes.SHA256()))
    (OUT / "key.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    (OUT / "key.pem").chmod(0o600)
    (OUT / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))


class Handler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self._send(b"", content_length=5)

    def _send(self, body: bytes, content_length: int | None = None) -> None:
        """Two Set-Cookie headers and an echo of the credential in a header: awkward cases for a response scrubber."""
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Set-Cookie", "a=1")
        self.send_header("Set-Cookie", "b=2")
        self.send_header("X-Echo", self.headers.get("Authorization", ""))
        self.send_header("Content-Length", str(len(body) if content_length is None else content_length))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        headers = dict(self.headers.items())
        record = {"ts": datetime.datetime.now().isoformat(), "method": self.command, "path": self.path,
                  "headers": headers}
        print(json.dumps(record), flush=True)
        with (OUT / "received.jsonl").open("a") as f:
            f.write(json.dumps(record) + "\n")
        if self.path == "/headers":
            return self._send(b"hello")
        if self.path == "/weird":                         # a body in a content-coding the proxy cannot decode
            self.send_response(200)
            self.send_header("Content-Encoding", "x-custom")
            body = headers.get("Authorization", "").encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if self.path == "/encoded":                       # echoes the credential in forms a plain search would miss
            credential = headers.get("Authorization", "").split(None, 1)[-1]
            body = json.dumps({"blob": credential, "b64": base64.b64encode(credential.encode()).decode()}).encode()
            return self._send(body)
        body = json.dumps({"login": "octocat", "received_authorization": headers.get("Authorization")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    make_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(OUT / "cert.pem", OUT / "key.pem")
    server = HTTPServer(("0.0.0.0", 443), Handler)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    print(f"stub listening on :443 as {HOST}", file=sys.stderr, flush=True)
    server.serve_forever()
