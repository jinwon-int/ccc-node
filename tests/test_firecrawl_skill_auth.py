"""Offline regression tests: synthetic credentials and loopback only."""
import importlib.util
import io
import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "piri/skills/web"


@pytest.fixture
def helpers(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_URL", raising=False)
    monkeypatch.syspath_prepend(str(ROOT))
    modules = {}
    for name in ("web_search", "web_fetch", "web_developer"):
        spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        modules[name] = module
    return modules


def stored(tmp_path, text):
    folder = tmp_path / ".hermes"
    folder.mkdir(exist_ok=True)
    (folder / ".env").write_text(text)


@pytest.mark.parametrize("name", ["web_search", "web_fetch", "web_developer"])
@pytest.mark.parametrize("source", ["environment", "stored", "blank_env", "missing"])
def test_all_requests_share_key_resolution(helpers, monkeypatch, tmp_path, name, source):
    if source != "missing":
        stored(tmp_path, '# ignored\nOTHER=x\nFIRECRAWL_API_KEY="stored-test-only"\n')
    if source == "environment":
        monkeypatch.setenv("FIRECRAWL_API_KEY", " env-test-only ")
    elif source == "blank_env":
        monkeypatch.setenv("FIRECRAWL_API_KEY", "  ")
    expected = "env-test-only" if source == "environment" else (
        None if source == "missing" else "stored-test-only"
    )
    requests = []

    def respond(req, **kwargs):
        requests.append(req)
        response = {"success": True, "data": {}, "results": []}
        if name == "web_search":
            response["data"] = {"web": []}
        return io.BytesIO(json.dumps(response).encode())

    module = helpers[name]
    monkeypatch.setattr(module, "_firecrawl_urlopen", respond)
    if name == "web_search":
        module._search_firecrawl("synthetic", 1)
    elif name == "web_fetch":
        module._request({"url": "https://example.com"})
    else:
        module._post({"query": "synthetic", "k": 1})
    assert len(requests) == 1
    assert requests[0].get_header("Authorization") == (
        f"Bearer {expected}" if expected else None
    )


@pytest.mark.parametrize("name", ["web_search", "web_fetch", "web_developer"])
@pytest.mark.parametrize(
    "base,expected_prefix",
    [
        ("https://api.example.test/firecrawl", "https://api.example.test/firecrawl/v2"),
        ("https://api.example.test/firecrawl/v2/", "https://api.example.test/firecrawl/v2"),
    ],
)
def test_keyed_https_selfhost_preserves_api_path_and_auth(
    helpers, monkeypatch, name, base, expected_prefix
):
    monkeypatch.setenv("FIRECRAWL_API_URL", base)
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic-api-key")
    requests = []

    def respond(req, **kwargs):
        requests.append(req)
        response = {"success": True, "data": {}, "results": []}
        if name == "web_search":
            response["data"] = {"web": []}
        return io.BytesIO(json.dumps(response).encode())

    module = helpers[name]
    monkeypatch.setattr(module, "_firecrawl_urlopen", respond)
    if name == "web_search":
        assert module._search_firecrawl("synthetic", 1) == 0
        suffix = "/search"
    elif name == "web_fetch":
        assert module._request({}) is not None
        suffix = "/scrape"
    else:
        assert module._post({}) is not None
        suffix = "/search/developer"

    assert len(requests) == 1
    assert requests[0].full_url == expected_prefix + suffix
    assert requests[0].get_header("Authorization") == "Bearer synthetic-api-key"


@pytest.mark.parametrize("name", ["web_search", "web_fetch", "web_developer"])
def test_keyed_http_api_base_is_rejected_before_network(helpers, monkeypatch, capsys, name):
    base = "http://127.0.0.1:18080/firecrawl"
    key = "synthetic-api-key"
    monkeypatch.setenv("FIRECRAWL_API_URL", base)
    monkeypatch.setenv("FIRECRAWL_API_KEY", key)
    calls = []

    def unexpected(req, **kwargs):
        calls.append(req)
        raise AssertionError("network must not be attempted")

    module = helpers[name]
    monkeypatch.setattr(module, "_firecrawl_urlopen", unexpected)
    if name == "web_search":
        assert module._search_firecrawl("synthetic", 1) == 69
    elif name == "web_fetch":
        assert module._request({}) is None
    else:
        assert module._post({}) is None
    output = capsys.readouterr()
    assert calls == []
    assert "https-required" in output.err
    assert key not in output.out + output.err
    assert base not in output.out + output.err


@pytest.mark.parametrize("name", ["web_search", "web_fetch", "web_developer"])
@pytest.mark.parametrize(
    "base",
    [
        "https://api.example.test/firecrawl?query=synthetic",
        "https://api.example.test/firecrawl#fragment",
        "https://synthetic-user:synthetic-pass@api.example.test/firecrawl",
        "https://api.example.test:bad/firecrawl",
        "https://api.example.test:/firecrawl",
        "https://api.example.test/firecrawl\n",
        "https://0x7f.1/firecrawl",
    ],
)
def test_malformed_api_base_is_rejected_before_network(helpers, monkeypatch, capsys, name, base):
    key = "synthetic-api-key"
    monkeypatch.setenv("FIRECRAWL_API_URL", base)
    monkeypatch.setenv("FIRECRAWL_API_KEY", key)
    calls = []

    def unexpected(req, **kwargs):
        calls.append(req)
        raise AssertionError("network must not be attempted")

    module = helpers[name]
    monkeypatch.setattr(module, "_firecrawl_urlopen", unexpected)
    if name == "web_search":
        assert module._search_firecrawl("synthetic", 1) == 69
    elif name == "web_fetch":
        assert module._request({}) is None
    else:
        assert module._post({}) is None
    output = capsys.readouterr()
    assert calls == []
    assert "invalid Firecrawl API endpoint" in output.err
    assert key not in output.out + output.err
    assert base not in output.out + output.err


@pytest.mark.parametrize("name", ["web_search", "web_fetch", "web_developer"])
def test_keyless_explicit_http_api_base_is_allowed(helpers, monkeypatch, name):
    monkeypatch.setenv("FIRECRAWL_API_URL", "http://127.0.0.1:18080/firecrawl")
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    requests = []

    def respond(req, **kwargs):
        requests.append(req)
        response = {"success": True, "data": {}, "results": []}
        if name == "web_search":
            response["data"] = {"web": []}
        return io.BytesIO(json.dumps(response).encode())

    module = helpers[name]
    monkeypatch.setattr(module, "_firecrawl_urlopen", respond)
    if name == "web_search":
        assert module._search_firecrawl("synthetic", 1) == 0
        suffix = "/search"
    elif name == "web_fetch":
        assert module._request({}) is not None
        suffix = "/scrape"
    else:
        assert module._post({}) is not None
        suffix = "/search/developer"
    assert requests[0].full_url == f"http://127.0.0.1:18080/firecrawl/v2{suffix}"
    assert requests[0].get_header("Authorization") is None


@pytest.mark.parametrize("name", ["web_search", "web_fetch", "web_developer"])
@pytest.mark.parametrize("keyed", [False, True])
def test_http_error_is_diagnostic_but_never_leaks(helpers, monkeypatch, capsys, name, keyed):
    secret = "fake-test-only-do-not-log"
    if keyed:
        monkeypatch.setenv("FIRECRAWL_API_KEY", secret)

    def fail(*args, **kwargs):
        raise urllib.error.HTTPError(
            f"https://example.com/{secret}", 429, secret, {}, io.BytesIO(secret.encode())
        )

    module = helpers[name]
    monkeypatch.setattr(module, "_firecrawl_urlopen", fail)
    if name == "web_search":
        assert module._search_firecrawl("synthetic", 1) == 69
    elif name == "web_fetch":
        assert module._request({}) is None
    else:
        assert module._post({}) is None
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    assert f"HTTP 429; auth={'keyed' if keyed else 'keyless'}" in output.err


class _RedirectHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/target":
            self.server.target_methods.append("POST")
            self._respond(200, b'{"success": true}')
            return
        parts = self.path.split("/")
        if len(parts) != 4 or parts[1] != "redirect":
            self._respond(404, b"not found")
            return
        self.server.redirect_calls += 1
        self.server.auth_headers.append(self.headers.get("Authorization"))
        code = int(parts[2])
        location = "/target" if parts[3] == "same" else self.server.cross_target
        self.send_response(code)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path == "/target":
            self.server.target_methods.append("GET")
        self._respond(200, b'{"success": true}')

    def _respond(self, status, body):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _RedirectTargetHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.server.target_methods.append("POST")
        self._respond()

    def do_GET(self):
        self.server.target_methods.append("GET")
        self._respond()

    def _respond(self):
        body = b'{"success": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("origin", ["same", "cross"])
def test_firecrawl_transport_rejects_redirects(helpers, code, origin):
    origin_server = HTTPServer(("127.0.0.1", 0), _RedirectHandler)
    target_server = HTTPServer(("127.0.0.1", 0), _RedirectTargetHandler)
    origin_server.redirect_calls = 0
    origin_server.target_methods = []
    origin_server.auth_headers = []
    target_server.target_methods = []
    origin_server.cross_target = f"http://127.0.0.1:{target_server.server_port}/target"
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (origin_server, target_server)
    ]
    for thread in threads:
        thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{origin_server.server_port}/redirect/{code}/{origin}",
            data=b"{}",
            headers={"Authorization": "Bearer synthetic-api-key"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as failure:
            helpers["web_search"]._firecrawl_urlopen(request, timeout=5)
        assert failure.value.code == code
        assert origin_server.redirect_calls == 1
        assert origin_server.auth_headers == ["Bearer synthetic-api-key"]
        assert origin_server.target_methods == []
        assert target_server.target_methods == []
    finally:
        origin_server.shutdown()
        target_server.shutdown()
        origin_server.server_close()
        target_server.server_close()
        for thread in threads:
            thread.join(timeout=5)


@pytest.mark.parametrize("contents", [b"\xff", b"# comment\n", b"FIRECRAWL_API_KEY=\n"])
def test_unusable_stored_key_is_keyless(helpers, tmp_path, contents):
    folder = tmp_path / ".hermes"
    folder.mkdir()
    (folder / ".env").write_bytes(contents)
    assert helpers["web_search"]._firecrawl_key() == ""


def test_package_import_without_script_path_injection():
    result = subprocess.run(
        [sys.executable, "-c", "from piri.skills.web import web_fetch, web_developer"],
        cwd=ROOT.parents[2], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name,code", [("web_search", 64), ("web_fetch", 64), ("web_developer", 2)])
def test_installed_scripts_start_without_pythonpath(tmp_path, monkeypatch, name, code):
    for script in ROOT.glob("*.py"):
        (tmp_path / script.name).write_bytes(script.read_bytes())
    monkeypatch.delenv("PYTHONPATH", raising=False)
    result = subprocess.run(
        [sys.executable, str(tmp_path / f"{name}.py")],
        cwd=tmp_path, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == code, result.stderr
    assert "ModuleNotFoundError" not in result.stderr


def test_network_error_does_not_print_exception_details(helpers):
    error = urllib.error.URLError("fake-credential-in-url")
    assert helpers["web_search"]._firecrawl_error(error, "fake-key") == "URLError; auth=keyed"
