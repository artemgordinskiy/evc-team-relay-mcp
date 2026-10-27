"""Tests for TR-05 (#0cdd5328): email/password (JWT) mode used to call
/v1/documents/*, which never existed on the control-plane (confirmed absent
across the full git history of evc-team-relay-cp). This suite covers the fix:

- list_files / tr_search (JWT mode) now hit the real, working
  /v1/shares/{id}/files-index route.
- read_document has no backend route to fall back to and raises a clear
  ValueError instead of silently 404ing.

read_file and the guarded writes are covered in test_guarded_sync.py.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

BASE_URL = "https://relay.example.com"
SHARE_ID = "11111111-1111-1111-1111-111111111111"
FILE_PATH = "notes/hello.md"


def _mock_response(status: int, body) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    if isinstance(body, str):
        m.text = body
    else:
        m.text = json.dumps(body)
        m.json.return_value = body
    m.raise_for_status = MagicMock()
    return m


def _mock_client(get_resp=None) -> MagicMock:
    c = MagicMock()
    c.__enter__ = MagicMock(return_value=c)
    c.__exit__ = MagicMock(return_value=False)
    if get_resp is not None:
        c.get.return_value = get_resp
    return c


@pytest.fixture(autouse=True)
def jwt_env(monkeypatch):
    monkeypatch.setenv("RELAY_CP_URL", BASE_URL)
    monkeypatch.setenv("RELAY_EMAIL", "test@example.com")
    monkeypatch.setenv("RELAY_PASSWORD", "secret")
    monkeypatch.delenv("RELAY_AGENT_KEY", raising=False)
    monkeypatch.delenv("RELAY_AGENT_KEYS", raising=False)

    import relay_mcp

    relay_mcp._token = "fake-jwt"
    relay_mcp._token_expires = float("inf")
    yield
    relay_mcp._token = None
    relay_mcp._token_expires = 0


SYNC_ARTIFACTS = [
    {"path": FILE_PATH, "sha256": "abc123", "size": 7, "updated_at": "2026-07-01T00:00:00Z", "type": "sync-artifact"},
]


class TestJwtListFiles:
    def test_hits_shares_files_index_not_documents(self):
        import relay_mcp

        client = _mock_client(get_resp=_mock_response(200, SYNC_ARTIFACTS))
        with patch.object(relay_mcp, "_get_client", return_value=client):
            result = relay_mcp._jwt_list_files(SHARE_ID)

        url = client.get.call_args[0][0]
        assert url == f"{BASE_URL}/v1/shares/{SHARE_ID}/files-index"
        assert "documents" not in url
        assert client.get.call_args[1]["headers"] == {"Authorization": "Bearer fake-jwt"}
        assert result[FILE_PATH]["sha256"] == "abc123"


class TestListFilesJwtMode:
    def test_returns_files_map_shape(self):
        import relay_mcp

        client = _mock_client(get_resp=_mock_response(200, SYNC_ARTIFACTS))
        with patch.object(relay_mcp, "_get_client", return_value=client):
            result = relay_mcp.list_files(SHARE_ID)

        assert result["share_id"] == SHARE_ID
        assert FILE_PATH in result["files"]
        assert client.get.call_args[0][0].endswith("/files-index")


class TestTrSearchJwtMode:
    def test_matches_and_never_calls_documents_endpoint(self):
        import relay_mcp

        files_index_resp = _mock_response(200, SYNC_ARTIFACTS)
        shares_resp = _mock_response(200, [{"id": SHARE_ID, "slug": "my-vault"}])
        clients = iter([_mock_client(get_resp=files_index_resp), _mock_client(get_resp=shares_resp)])

        with patch.object(relay_mcp, "_get_client", side_effect=lambda: next(clients)):
            result = json.loads(relay_mcp.tr_search(SHARE_ID, "hello"))

        assert len(result) == 1
        assert result[0]["path"] == FILE_PATH
        assert result[0]["relay_url"] == "relay://my-vault/notes/hello.md"

    def test_no_match_returns_empty(self):
        import relay_mcp

        files_index_resp = _mock_response(200, SYNC_ARTIFACTS)
        shares_resp = _mock_response(200, [])
        clients = iter([_mock_client(get_resp=files_index_resp), _mock_client(get_resp=shares_resp)])

        with patch.object(relay_mcp, "_get_client", side_effect=lambda: next(clients)):
            result = json.loads(relay_mcp.tr_search(SHARE_ID, "nonexistent"))

        assert result == []


class TestUnavailableDocumentTools:
    """No backend route exists for these — must raise, not silently 404."""

    def test_read_document_raises_without_http_call(self):
        import relay_mcp

        client = MagicMock()
        with patch.object(relay_mcp, "_get_client", return_value=client) as get_client:
            with pytest.raises(ValueError, match="read_document"):
                relay_mcp.read_document(SHARE_ID)
        get_client.assert_not_called()
