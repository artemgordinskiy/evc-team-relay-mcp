"""Exercise the constructed FastMCP app; no proxy rewriting or live relay needed."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest
from starlette.testclient import TestClient

SOURCE = Path(__file__).resolve().parents[1] / "relay_mcp.py"
PUBLIC_HOSTS = ["evc-mcp.example.com", "evc-mcp.example.com:443"]
PUBLIC_ORIGINS = ["https://evc-mcp.example.com"]


@pytest.fixture
def load_server(monkeypatch):
    def load(hosts=None, origins=None):
        for name, value in [
            ("RELAY_MCP_ALLOWED_HOSTS", hosts),
            ("RELAY_MCP_ALLOWED_ORIGINS", origins),
        ]:
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        spec = importlib.util.spec_from_file_location("relay_http_test", SOURCE)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
        return module
    return load


def initialize(client, host, origin=None):
    headers = {"Host": host, "Accept": "application/json, text/event-stream"}
    if origin is not None:
        headers["Origin"] = origin
    return client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "http-security-test", "version": "1"},
        },
    })


def assert_initialized(response):
    assert response.status_code == 200
    if "text/event-stream" in response.headers["content-type"]:
        payloads = [json.loads(line[5:].strip()) for line in response.text.splitlines()
                    if line.startswith("data:")]
        assert len(payloads) == 1
        payload = payloads[0]
    else:
        payload = response.json()
    assert payload["result"]["serverInfo"]["name"] == "evc-relay"


def run_main(monkeypatch, server, args):
    calls = []
    monkeypatch.setattr(server.mcp, "run", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(sys, "argv", ["evc-team-relay-mcp", *args])
    server.main()
    return calls


def test_stdio_keeps_sdk_localhost_defaults(load_server, monkeypatch):
    server = load_server()
    assert run_main(monkeypatch, server, []) == [{"transport": "stdio"}]
    with TestClient(server.mcp.streamable_http_app()) as client:
        assert_initialized(initialize(client, "localhost:8888", "http://localhost:8888"))
        assert initialize(client, PUBLIC_HOSTS[0]).status_code == 421


@pytest.mark.parametrize("bind,host,origin", [
    ("127.0.0.1", "127.0.0.1:8888", "http://127.0.0.1:8888"),
    ("localhost", "localhost:8888", "http://localhost:8888"),
    ("::1", "[::1]:8888", "http://[::1]:8888"),
])
def test_loopback_http_keeps_sdk_defaults(load_server, monkeypatch, bind, host, origin):
    server = load_server()
    assert run_main(monkeypatch, server, ["--transport", "http", "--host", bind]) == [
        {"transport": "streamable-http"}
    ]
    with TestClient(server.mcp.streamable_http_app()) as client:
        assert_initialized(initialize(client, host, origin))
        assert initialize(client, "foreign.example.com").status_code == 421


@pytest.mark.parametrize("transport", ["http", "streamable-http"])
@pytest.mark.parametrize("bind", ["0.0.0.0", "192.0.2.10", "::", "relay.example.com"])
def test_external_http_without_lists_fails_before_serving(load_server, monkeypatch, transport, bind):
    server = load_server()
    with pytest.raises(ValueError, match="External HTTP binds require both RELAY_MCP_ALLOWED_HOSTS"):
        run_main(monkeypatch, server, ["--transport", transport, "--host", bind])


@pytest.mark.parametrize("host,origin", [
    (PUBLIC_HOSTS[0], None),
    (PUBLIC_HOSTS[1], PUBLIC_ORIGINS[0]),
])
def test_exact_public_identity_initializes(load_server, monkeypatch, host, origin):
    server = load_server(json.dumps(PUBLIC_HOSTS), json.dumps(PUBLIC_ORIGINS))
    run_main(monkeypatch, server, ["--transport", "http", "--host", "0.0.0.0"])
    with TestClient(server.mcp.streamable_http_app()) as client:
        assert_initialized(initialize(client, host, origin))


@pytest.mark.parametrize("host,origin,status", [
    ("foreign.example.com", None, 421),
    ("localhost:8888", None, 421),
    ("evc-mcp.example.com:8443", None, 421),
    (PUBLIC_HOSTS[0], "https://foreign.example.com", 403),
    (PUBLIC_HOSTS[0], "http://evc-mcp.example.com", 403),
    (PUBLIC_HOSTS[0], "https://evc-mcp.example.com:8443", 403),
])
def test_foreign_host_or_origin_rejected(load_server, host, origin, status):
    server = load_server(json.dumps(PUBLIC_HOSTS), json.dumps(PUBLIC_ORIGINS))
    with TestClient(server.mcp.streamable_http_app()) as client:
        assert initialize(client, host, origin).status_code == status


@pytest.mark.parametrize("bind", ["0.0.0.0", "192.0.2.10", "::", "127.0.0.1"])
def test_bind_change_does_not_widen_http_identities(load_server, monkeypatch, bind):
    server = load_server(json.dumps(PUBLIC_HOSTS), json.dumps(PUBLIC_ORIGINS))
    run_main(monkeypatch, server, ["--transport", "http", "--host", bind])
    with TestClient(server.mcp.streamable_http_app()) as client:
        assert_initialized(initialize(client, PUBLIC_HOSTS[0]))
        bind_header = "[::]:8888" if bind == "::" else f"{bind}:8888"
        assert initialize(client, bind_header).status_code == 421


@pytest.mark.parametrize("hosts,origins", [
    (json.dumps(PUBLIC_HOSTS), None),
    (None, json.dumps(PUBLIC_ORIGINS)),
])
def test_partial_configuration_fails(load_server, hosts, origins):
    with pytest.raises(ValueError, match="Set both RELAY_MCP_ALLOWED_HOSTS"):
        load_server(hosts, origins)


@pytest.mark.parametrize("raw", ["", "not-json", "null", "{}", '"value"', "[]", "[null]", "[1]", "[true]"])
@pytest.mark.parametrize("setting", ["hosts", "origins"])
def test_invalid_json_arrays_fail(load_server, raw, setting):
    hosts = raw if setting == "hosts" else json.dumps(PUBLIC_HOSTS)
    origins = raw if setting == "origins" else json.dumps(PUBLIC_ORIGINS)
    with pytest.raises(ValueError, match="Invalid RELAY_MCP_ALLOWED_"):
        load_server(hosts, origins)


@pytest.mark.parametrize("host", [
    "", "*", "*.example.com", "example.com:*", "https://example.com", "user@example.com",
    "example.com/path", "example.com?x=1", "example.com#x", "example.com:",
    "example.com:0", "example.com:65536", "example.com:abc", "example.com\\bad",
    "example..com", "-example.com", "example.com ", "example.com\n", "éxample.com",
    "127.0.0.999", "::1", "[::1]garbage", "[::1", "[::1%eth0]",
])
def test_malformed_or_wildcard_hosts_fail(load_server, host):
    with pytest.raises(ValueError, match="Invalid RELAY_MCP_ALLOWED_HOSTS"):
        load_server(json.dumps([host]), json.dumps(PUBLIC_ORIGINS))


@pytest.mark.parametrize("origin", [
    "", "*", "https://*.example.com", "https://example.com:*", "http://example.com",
    "https://user@example.com", "https://example.com/", "https://example.com/path",
    "https://example.com?x=1", "https://example.com#x", "https://example.com:",
    "https://example.com:0", "https://example.com:65536", "https://example.com\n",
    "https://[::1]garbage", "null", "example.com",
])
def test_malformed_or_wildcard_origins_fail(load_server, origin):
    with pytest.raises(ValueError, match="Invalid RELAY_MCP_ALLOWED_ORIGINS"):
        load_server(json.dumps(PUBLIC_HOSTS), json.dumps([origin]))


@pytest.mark.parametrize("host,origin", [
    ("192.0.2.10:443", "https://192.0.2.10:443"),
    ("[2001:db8::1]:443", "https://[2001:db8::1]:443"),
])
def test_exact_ip_entries_initialize(load_server, host, origin):
    server = load_server(json.dumps([host]), json.dumps([origin]))
    with TestClient(server.mcp.streamable_http_app()) as client:
        assert_initialized(initialize(client, host, origin))
