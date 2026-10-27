"""Unit tests for the guarded sync protocol: read_file, create_file, update_file.

The control plane is replaced by an httpx.MockTransport, so real request
headers and raw bytes flow through httpx. Server-side compare-and-swap is
exercised against the real control plane in tests/integration.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from unittest.mock import patch

import httpx
import pytest

import relay_mcp

BASE_URL = "https://relay.example.com"
SHARE = "11111111-1111-1111-1111-111111111111"
OTHER_SHARE = "22222222-2222-2222-2222-222222222222"
KEY = "key-for-share"
OTHER_KEY = "key-for-other-share"
PATH = "notes/hello.md"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def server_error(status: int, message: str) -> httpx.Response:
    """An error in the control plane's own envelope, as v1.13.7 sends it."""
    return httpx.Response(
        status, json={"error": {"code": status, "message": message, "request_id": "req-1"}}
    )


class FakeControlPlane:
    """Records requests; answers download/files-index/sync-write from canned state."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.download: Callable[[httpx.Request], httpx.Response] = lambda _r: httpx.Response(404)
        self.index: list[dict] = []
        self.write: Callable[[httpx.Request], httpx.Response] = lambda _r: httpx.Response(500)

    def serve(self, body: bytes, etag: str | None, indexed: str | None, mime="text/plain") -> None:
        headers = {"content-type": mime}
        if etag is not None:
            headers["etag"] = f'"{etag}"'
        self.download = lambda _r: httpx.Response(200, content=body, headers=headers)
        self.index = [] if indexed is None else [{"path": PATH, "sha256": indexed, "size": len(body)}]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/download"):
            return self.download(request)
        if path.endswith("/files-index"):
            return httpx.Response(200, json=self.index)
        if path.endswith("/sync-write"):
            return self.write(request)
        return httpx.Response(599)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def cp(monkeypatch):
    monkeypatch.setenv("RELAY_CP_URL", BASE_URL)
    monkeypatch.setenv("RELAY_AGENT_KEYS", f"{SHARE}:{KEY},{OTHER_SHARE}:{OTHER_KEY}")
    monkeypatch.delenv("RELAY_AGENT_KEY", raising=False)
    monkeypatch.delenv("RELAY_EMAIL", raising=False)
    monkeypatch.delenv("RELAY_PASSWORD", raising=False)
    fake = FakeControlPlane()
    with patch.object(relay_mcp, "_get_client", side_effect=fake.client):
        yield fake


class TestReadFile:
    @pytest.mark.parametrize(
        "body",
        [
            b"# Hello\n",
            "Grüße — \U0001f600\n".encode(),
            b"line one\r\nline two\r\n",
            b"\xef\xbb\xbf# BOM first\r\nmixed\nendings\r\n",
            b"",
        ],
        ids=["ascii", "utf8", "crlf", "bom-mixed", "empty"],
    )
    def test_returns_exact_text_and_token_when_snapshots_agree(self, cp, body):
        cp.serve(body, etag=sha(body), indexed=sha(body))

        result = relay_mcp.read_file(SHARE, PATH)

        assert result["ok"] is True
        assert result["content"].encode("utf-8") == body
        assert result["sha256"] == sha(body)
        assert result["write_token_withheld"] is None
        assert result["size"] == len(body)

    def test_uses_sync_protocol_routes_with_share_key(self, cp):
        cp.serve(b"x", etag=sha(b"x"), indexed=sha(b"x"))

        relay_mcp.read_file(OTHER_SHARE, PATH)

        assert [r.url.path for r in cp.requests] == [
            f"/v1/shares/{OTHER_SHARE}/download",
            f"/v1/shares/{OTHER_SHARE}/files-index",
        ]
        assert cp.requests[0].url.params["path"] == PATH
        assert all(r.headers["x-agent-key"] == OTHER_KEY for r in cp.requests)
        assert not any("/v1/web/" in r.url.path for r in cp.requests)

    def test_withholds_token_when_index_disagrees(self, cp):
        cp.serve(b"new", etag=sha(b"new"), indexed=sha(b"old"))

        result = relay_mcp.read_file(SHARE, PATH)

        assert result["ok"] is True
        assert result["content"] == "new"
        assert result["sha256"] is None
        assert result["write_token_withheld"] == "snapshot_mismatch"

    def test_refuses_bytes_that_do_not_match_their_etag(self, cp):
        # What the server does to invalid UTF-8 stored inline: it decodes with
        # errors="replace", so the served bytes no longer hash to the stored sha.
        stored = b"caf\xe9\n"
        served = stored.decode("utf-8", errors="replace").encode("utf-8")
        cp.serve(served, etag=sha(stored), indexed=sha(stored))

        result = relay_mcp.read_file(SHARE, PATH)

        assert result["ok"] is False
        assert result["error"] == "not_byte_exact"
        assert "content" not in result
        assert "sha256" not in result

    def test_withholds_token_without_etag(self, cp):
        cp.serve(b"x", etag=None, indexed=sha(b"x"))

        result = relay_mcp.read_file(SHARE, PATH)

        assert result["sha256"] is None
        assert result["write_token_withheld"] == "snapshot_mismatch"

    def test_withholds_token_when_not_in_sync_index(self, cp):
        cp.serve(b"x", etag=sha(b"x"), indexed=None)

        result = relay_mcp.read_file(SHARE, PATH)

        assert result["sha256"] is None
        assert result["write_token_withheld"] == "not_in_sync_index"

    @pytest.mark.parametrize(
        "body,mime",
        [(b"\x89PNG\r\n\x1a\n\x00\x00", "image/png"), (b"caf\xe9", "text/plain")],
        ids=["binary", "invalid-utf8"],
    )
    def test_refuses_non_utf8_without_content_or_token(self, cp, body, mime):
        cp.serve(body, etag=sha(body), indexed=sha(body), mime=mime)

        result = relay_mcp.read_file(SHARE, PATH)

        assert result["ok"] is False
        assert result["error"] == "not_utf8_text"
        assert "content" not in result
        assert "sha256" not in result

    def test_not_found_is_structured(self, cp):
        cp.download = lambda _r: server_error(404, "File not found: x")

        result = relay_mcp.read_file(SHARE, PATH)

        assert result == {
            "ok": False,
            "error": "not_found",
            "message": "File not found: x",
            "status": 404,
        }

    def test_slug_is_rejected_without_http_call(self, cp):
        result = relay_mcp.read_file("research-vault", PATH)

        assert result["error"] == "invalid_share_id"
        assert cp.requests == []

    def test_jwt_mode_reads_with_bearer(self, cp, monkeypatch):
        monkeypatch.delenv("RELAY_AGENT_KEYS")
        monkeypatch.setattr(relay_mcp, "_token", "fake-jwt")
        monkeypatch.setattr(relay_mcp, "_token_expires", float("inf"))
        cp.serve(b"x", etag=sha(b"x"), indexed=sha(b"x"))

        result = relay_mcp.read_file(SHARE, PATH)

        assert result["sha256"] == sha(b"x")
        assert all(r.headers["authorization"] == "Bearer fake-jwt" for r in cp.requests)


def _write_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "path": request.url.params["path"],
            "sha256": sha(request.content),
            "size": len(request.content),
            "updated_at": "2026-10-27T00:00:00+00:00",
        },
    )


class TestCreateFile:
    def test_sends_create_only_precondition_and_exact_bytes(self, cp):
        cp.write = _write_ok
        content = "﻿# Title\r\nGrüße\n"

        result = relay_mcp.create_file(SHARE, PATH, content)

        (req,) = cp.requests
        assert req.method == "PUT"
        assert req.url.path == f"/v1/shares/{SHARE}/sync-write"
        assert req.url.params["path"] == PATH
        assert req.headers["if-none-match"] == "*"
        assert "if-match" not in req.headers
        assert req.headers["x-agent-key"] == KEY
        assert req.headers["content-type"] == "text/plain; charset=utf-8"
        assert req.content == content.encode("utf-8")
        assert result["ok"] is True
        assert result["operation"] == "created"
        assert result["sha256"] == sha(content.encode("utf-8"))

    def test_existing_file_is_exists_error(self, cp):
        cp.write = lambda _r: server_error(412, f"File already exists: {PATH}")

        result = relay_mcp.create_file(SHARE, PATH, "x")

        assert result["ok"] is False
        assert result["error"] == "exists"
        assert result["status"] == 412
        assert len(cp.requests) == 1

    def test_lone_surrogate_is_refused_without_http_call(self, cp):
        result = relay_mcp.create_file(SHARE, PATH, "bad \ud800 text")

        assert result["error"] == "not_utf8_text"
        assert cp.requests == []

    def test_jwt_mode_cannot_write(self, cp, monkeypatch):
        monkeypatch.delenv("RELAY_AGENT_KEYS")
        monkeypatch.setenv("RELAY_EMAIL", "test@example.com")
        monkeypatch.setenv("RELAY_PASSWORD", "secret")

        with pytest.raises(ValueError, match="create_file"):
            relay_mcp.create_file(SHARE, PATH, "x")
        assert cp.requests == []


class TestUpdateFile:
    def test_sends_if_match_with_normalised_token(self, cp):
        cp.write = _write_ok
        token = sha(b"old")

        result = relay_mcp.update_file(SHARE, PATH, "new", f"  {token.upper()} ")

        (req,) = cp.requests
        assert req.headers["if-match"] == f'"{token}"'
        assert "if-none-match" not in req.headers
        assert req.content == b"new"
        assert result["ok"] is True
        assert result["operation"] == "updated"
        assert result["sha256"] == sha(b"new")

    def test_mismatch_is_stale_and_not_retried(self, cp):
        cp.write = lambda _r: server_error(412, "sha256 mismatch: the file changed since it was read")

        result = relay_mcp.update_file(SHARE, PATH, "new", sha(b"old"))

        assert result["ok"] is False
        assert result["error"] == "stale"
        assert len(cp.requests) == 1

    def test_missing_file_is_not_found(self, cp):
        cp.write = lambda _r: server_error(412, f"File not found: {PATH}")

        result = relay_mcp.update_file(SHARE, PATH, "new", sha(b"old"))

        assert result["error"] == "not_found"

    @pytest.mark.parametrize("token", ["", "*", "abc123", "g" * 64, sha(b"x") + "0"])
    def test_malformed_token_is_refused_without_http_call(self, cp, token):
        result = relay_mcp.update_file(SHARE, PATH, "new", token)

        assert result["error"] == "invalid_expected_sha256"
        assert cp.requests == []

    @pytest.mark.parametrize(
        "status,code",
        [(403, "forbidden"), (428, "precondition_required"), (429, "rate_limited"), (413, "too_large")],
    )
    def test_client_errors_are_structured(self, cp, status, code):
        cp.write = lambda _r: server_error(status, "nope")

        result = relay_mcp.update_file(SHARE, PATH, "new", sha(b"old"))

        assert result == {"ok": False, "error": code, "message": "nope", "status": status}

    def test_server_error_raises(self, cp):
        cp.write = lambda _r: server_error(500, "boom")

        with pytest.raises(httpx.HTTPStatusError):
            relay_mcp.update_file(SHARE, PATH, "new", sha(b"old"))


class TestToolSurface:
    def test_no_unconditional_write_tools(self):
        names = {t.name for t in asyncio.run(relay_mcp.mcp.list_tools())}

        assert {"read_file", "create_file", "update_file"} <= names
        assert names.isdisjoint({"upsert_file", "write_document", "delete_file"})

    def test_source_never_targets_unconditional_routes(self):
        source = open(relay_mcp.__file__, encoding="utf-8").read()
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))

        assert '/sync-upload"' not in code
        assert '/{upload_path}' not in code
        assert json.dumps("If-Match: *") not in code
