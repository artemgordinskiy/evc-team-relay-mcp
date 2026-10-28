"""Tests for guarded writes: read_file's sha256 token, create_file and update_file.

The control plane is an httpx.MockTransport holding real file bytes, so
request headers and bodies flow through httpx unchanged. Its sync-write
mirrors the server's compare-and-swap: If-Match / If-None-Match: * against
the stored file's sha256, 412 in the server's error envelope otherwise.
"""

from __future__ import annotations

import hashlib
from unittest.mock import patch

import httpx
import pytest

import relay_mcp

BASE_URL = "https://relay.example.com"
AGENT_KEY = "test-agent-key-abc123"
SHARE_ID = "11111111-1111-1111-1111-111111111111"
FILE_PATH = "notes/hello.md"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def server_error(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": status, "message": message}})


class FakeControlPlane:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.content_type = "text/markdown; charset=utf-8"
        self.requests: list[httpx.Request] = []
        self.fail_writes_with: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.params.get("path")
        if request.url.path.endswith("/download"):
            if path not in self.files:
                return server_error(404, "File not found")
            return httpx.Response(
                200,
                content=self.files[path],
                headers={"content-type": self.content_type},
            )
        if request.url.path.endswith("/sync-write"):
            return self.sync_write(request, path)
        return httpx.Response(599)

    def sync_write(self, request: httpx.Request, path: str) -> httpx.Response:
        if self.fail_writes_with is not None:
            return server_error(self.fail_writes_with, "boom")
        current = self.files.get(path)
        if request.headers.get("if-none-match") == "*":
            if current is not None:
                return server_error(412, f"File already exists: {path}")
        elif current is None:
            return server_error(412, f"File not found: {path}")
        elif request.headers.get("if-match") != f'"{sha(current)}"':
            return server_error(
                412, "sha256 mismatch: the file changed since it was read"
            )
        self.files[path] = request.content
        return httpx.Response(
            200,
            json={
                "path": path,
                "sha256": sha(request.content),
                "size": len(request.content),
                "updated_at": "2026-10-28T00:00:00+00:00",
            },
        )

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def writes(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/sync-write")]


@pytest.fixture
def cp(monkeypatch):
    monkeypatch.setenv("RELAY_CP_URL", BASE_URL)
    monkeypatch.setenv("RELAY_AGENT_KEY", AGENT_KEY)
    monkeypatch.delenv("RELAY_AGENT_KEYS", raising=False)
    monkeypatch.delenv("RELAY_EMAIL", raising=False)
    monkeypatch.delenv("RELAY_PASSWORD", raising=False)
    fake = FakeControlPlane()
    with patch.object(relay_mcp, "_get_client", side_effect=fake.client):
        yield fake


@pytest.fixture
def jwt_mode(monkeypatch):
    monkeypatch.delenv("RELAY_AGENT_KEY", raising=False)
    monkeypatch.setattr(relay_mcp, "_headers", lambda: {"Authorization": "Bearer jwt"})


class TestReadFileToken:
    @pytest.mark.parametrize(
        "body",
        [b"# Hello\n", "café — naïve\n".encode(), b"\xef\xbb\xbfBOM\r\nCRLF\r\n"],
        ids=["ascii", "non-ascii", "bom-crlf"],
    )
    def test_token_is_sha256_of_the_exact_text_returned(self, cp, body):
        cp.files[FILE_PATH] = body

        result = relay_mcp.read_file(SHARE_ID, FILE_PATH)

        assert result["content"].encode("utf-8") == body
        assert result["sha256"] == sha(body)

    def test_no_token_for_binary_content(self, cp):
        cp.files[FILE_PATH] = b"\x89PNG\r\n\x1a\n\x00\xff"
        cp.content_type = "application/octet-stream"

        assert relay_mcp.read_file(SHARE_ID, FILE_PATH)["sha256"] is None

    def test_no_token_when_content_was_decoded_from_another_charset(self, cp):
        # httpx decodes by the declared charset; editing that text and writing
        # it back as UTF-8 would change bytes the agent never touched.
        cp.files[FILE_PATH] = "café\n".encode("latin-1")
        cp.content_type = "text/plain; charset=latin-1"

        result = relay_mcp.read_file(SHARE_ID, FILE_PATH)

        assert result["content"] == "café\n"
        assert result["sha256"] is None

    def test_jwt_mode_returns_token_too(self, cp, jwt_mode):
        cp.files[FILE_PATH] = b"# Hello\n"

        result = relay_mcp.read_file(SHARE_ID, FILE_PATH)

        assert cp.requests[-1].headers["authorization"] == "Bearer jwt"
        assert result["sha256"] == sha(b"# Hello\n")


class TestCreateFile:
    def test_sends_create_only_precondition_and_exact_bytes(self, cp):
        content = "café\r\nline two\n"

        result = relay_mcp.create_file(SHARE_ID, FILE_PATH, content)

        (write,) = cp.writes()
        assert write.method == "PUT"
        assert write.url.path == f"/v1/shares/{SHARE_ID}/sync-write"
        assert write.url.params["path"] == FILE_PATH
        assert write.headers["x-agent-key"] == AGENT_KEY
        assert write.headers["if-none-match"] == "*"
        assert "if-match" not in write.headers
        assert write.content == content.encode("utf-8")
        assert result["operation"] == "created"
        assert result["sha256"] == sha(content.encode("utf-8"))

    def test_existing_file_is_left_alone(self, cp):
        cp.files[FILE_PATH] = b"theirs\n"

        result = relay_mcp.create_file(SHARE_ID, FILE_PATH, "mine\n")

        assert result["error"] == "exists"
        assert cp.files[FILE_PATH] == b"theirs\n"

    def test_slug_is_refused_without_http_call(self, cp):
        result = relay_mcp.create_file("research-vault", FILE_PATH, "x")

        assert result["error"] == "invalid_share_id"
        assert cp.requests == []

    def test_jwt_mode_cannot_write(self, cp, jwt_mode):
        with pytest.raises(ValueError, match="create_file"):
            relay_mcp.create_file(SHARE_ID, FILE_PATH, "x")
        assert cp.requests == []


class TestUpdateFile:
    def test_read_then_update_round_trip(self, cp):
        cp.files[FILE_PATH] = b"v1\n"
        token = relay_mcp.read_file(SHARE_ID, FILE_PATH)["sha256"]

        result = relay_mcp.update_file(SHARE_ID, FILE_PATH, "v2\n", token)

        assert result["operation"] == "updated"
        assert cp.files[FILE_PATH] == b"v2\n"
        assert cp.writes()[0].headers["if-match"] == f'"{token}"'
        # The result's sha256 is the token for the next update.
        assert relay_mcp.update_file(SHARE_ID, FILE_PATH, "v3\n", result["sha256"])[
            "sha256"
        ]
        assert cp.files[FILE_PATH] == b"v3\n"

    def test_change_since_read_is_stale_and_not_retried(self, cp):
        cp.files[FILE_PATH] = b"v1\n"
        token = relay_mcp.read_file(SHARE_ID, FILE_PATH)["sha256"]
        cp.files[FILE_PATH] = b"someone else's edit\n"

        result = relay_mcp.update_file(SHARE_ID, FILE_PATH, "agent's edit\n", token)

        assert result["error"] == "stale"
        assert cp.files[FILE_PATH] == b"someone else's edit\n"
        assert len(cp.writes()) == 1

    def test_deleted_since_read_is_not_found(self, cp):
        result = relay_mcp.update_file(SHARE_ID, FILE_PATH, "x", sha(b"gone\n"))

        assert result["error"] == "not_found"
        assert FILE_PATH not in cp.files

    def test_token_is_normalised(self, cp):
        cp.files[FILE_PATH] = b"v1\n"
        token = "  " + sha(b"v1\n").upper() + " "

        result = relay_mcp.update_file(SHARE_ID, FILE_PATH, "v2\n", token)

        assert result["operation"] == "updated"

    @pytest.mark.parametrize("token", ["", "*", "abc", "g" * 64, '"' + "0" * 64 + '"'])
    def test_malformed_token_is_refused_without_http_call(self, cp, token):
        result = relay_mcp.update_file(SHARE_ID, FILE_PATH, "x", token)

        assert result["error"] == "invalid_expected_sha256"
        assert cp.requests == []

    def test_other_errors_raise(self, cp):
        cp.files[FILE_PATH] = b"v1\n"
        cp.fail_writes_with = 500

        with pytest.raises(httpx.HTTPStatusError):
            relay_mcp.update_file(SHARE_ID, FILE_PATH, "v2\n", sha(b"v1\n"))

    def test_jwt_mode_cannot_write(self, cp, jwt_mode):
        with pytest.raises(ValueError, match="update_file"):
            relay_mcp.update_file(SHARE_ID, FILE_PATH, "x", sha(b"x"))
        assert cp.requests == []
