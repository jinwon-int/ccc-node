"""Fleet alert relay client (#2182) — signing, payload, HTTP outcomes, secret file hygiene."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_bot.core import fleet_alert_relay as R


def test_sign_is_deterministic_and_verify_round_trips() -> None:
    sig = R.sign("s3cret", 1700000000, b'{"a":1}')
    assert sig.startswith("sha256=") and len(sig) == 7 + 64
    assert sig == R.sign("s3cret", 1700000000, b'{"a":1}')
    assert R.verify("s3cret", 1700000000, b'{"a":1}', sig)
    assert not R.verify("s3cret", 1700000001, b'{"a":1}', sig), "timestamp is part of the signed string"
    assert not R.verify("other", 1700000000, b'{"a":1}', sig)
    assert not R.verify("s3cret", 1700000000, b'{"a":2}', sig)
    assert not R.verify("s3cret", 1700000000, b'{"a":1}', None)


def test_build_payload_namespaces_dedup_and_keeps_only_known_raw_keys() -> None:
    data = {"ts": "2026-10-08T00:00:00Z", "event": "SelfUpdate", "node": "node-a", "text": "x",
            "dedup": "SelfUpdate:pending-abc", "secret_looking": "nope"}
    p = R.build_payload("node-a", "r1.json", data, "[node-a] formatted")
    assert p["schema"] == R.SCHEMA and p["node"] == "node-a" and p["record"] == "r1.json"
    assert p["event"] == "SelfUpdate" and p["text"] == "[node-a] formatted"
    assert p["dedup"] == "node-a:SelfUpdate:pending-abc"
    assert "secret_looking" not in p["raw"] and p["raw"]["event"] == "SelfUpdate"
    assert R.build_payload("n", "r", {"text": "t"}, "t")["dedup"] == ""


def test_read_secret_requires_owner_only_regular_file(tmp_path: Path) -> None:
    f = tmp_path / "secret"
    f.write_text("  abc  \n", encoding="utf-8")
    os.chmod(f, 0o600)
    assert R.read_secret(f) == "abc"
    os.chmod(f, 0o640)
    with pytest.raises(ValueError, match="owner-only"):
        R.read_secret(f)
    os.chmod(f, 0o600)
    f.write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        R.read_secret(f)
    link = tmp_path / "link"
    link.symlink_to(f)
    with pytest.raises(ValueError, match="symlink"):
        R.read_secret(link)
    with pytest.raises(FileNotFoundError):
        R.read_secret(tmp_path / "missing")


def test_relay_from_settings_off_without_url_and_fails_closed_without_secret(tmp_path: Path) -> None:
    assert R.relay_from_settings(SimpleNamespace()) is None
    assert R.relay_from_settings(SimpleNamespace(push_fleet_relay_url="")) is None
    with pytest.raises(ValueError, match="SECRET_FILE"):
        R.relay_from_settings(SimpleNamespace(push_fleet_relay_url="http://relay:8795/v1/alerts"))
    f = tmp_path / "secret"
    f.write_text("k", encoding="utf-8")
    os.chmod(f, 0o600)
    relay = R.relay_from_settings(SimpleNamespace(
        push_fleet_relay_url="http://relay:8795/v1/alerts", push_fleet_relay_secret_file=f, push_fleet_node="node-b"))
    assert relay is not None and relay.node == "node-b" and relay.url.endswith("/v1/alerts")
    with pytest.raises(ValueError, match="http"):
        R.FleetAlertRelay("ftp://x", "k", "n")


class _Server:
    """Local HTTP server that records requests and answers with a scripted status."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.status = 200
        srv = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                srv.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                self.send_response(srv.status)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):  # silence
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1/alerts"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = _Server()
    yield s
    s.close()


def test_post_sends_signed_request_and_accepts_2xx(server: _Server) -> None:
    relay = R.FleetAlertRelay(server.url, "k", "node-a", clock=lambda: 1700000000.9)
    relay.post({"schema": R.SCHEMA, "text": "hi"})
    assert len(server.requests) == 1
    req = server.requests[0]
    assert req["path"] == "/v1/alerts"
    assert req["headers"][R.NODE_HEADER] == "node-a"
    assert req["headers"][R.TIMESTAMP_HEADER] == "1700000000"
    assert R.verify("k", 1700000000, req["body"], req["headers"][R.SIGNATURE_HEADER])
    assert json.loads(req["body"])["text"] == "hi"


def test_post_classifies_transient_and_rejected(server: _Server) -> None:
    relay = R.FleetAlertRelay(server.url, "k", "n", timeout=2)
    server.status = 503
    with pytest.raises(R.RelayError):
        relay.post({"text": "x"})
    server.status = 429
    with pytest.raises(R.RelayError):
        relay.post({"text": "x"})
    server.status = 401
    with pytest.raises(R.RelayRejected):
        relay.post({"text": "x"})
    server.status = 400
    with pytest.raises(R.RelayRejected):
        relay.post({"text": "x"})


def test_post_unreachable_is_transient() -> None:
    relay = R.FleetAlertRelay("http://127.0.0.1:9/v1/alerts", "k", "n", timeout=1)
    with pytest.raises(R.RelayError):
        relay.post({"text": "x"})
