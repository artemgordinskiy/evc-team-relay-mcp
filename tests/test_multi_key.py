"""Tests for RELAY_AGENT_KEYS multi-share agent-key support."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

BASE_URL = "https://relay.example.com"
SHARE_MESH = "mesh"
SHARE_SPARK = "spark"
KEY_MESH = "key-for-mesh-abc"
KEY_SPARK = "key-for-spark-xyz"
MULTI_KEYS_ENV = f"{SHARE_MESH}:{KEY_MESH},{SHARE_SPARK}:{KEY_SPARK}"
UUID_MESH = "5ba2a6c4-3b9b-485b-b0e4-b8b8691dc49a"
UUID_SPARK = "f30af6ec-318b-4198-8c1c-b290b12e39d8"
UUID_KEYS_ENV = f"{UUID_MESH}:{KEY_MESH},{UUID_SPARK}:{KEY_SPARK}"


def _index(*paths: str) -> list[dict]:
    """A sync files-index response: a list of SyncArtifactItem."""
    return [
        {"path": path, "type": "doc", "size": 3, "sha256": "a" * 64, "updated_at": "2026-10-28T20:00:00Z"}
        for path in paths
    ]


def _mock_response(status: int, body: dict | list | str) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    if isinstance(body, str):
        m.text = body
        m.json.return_value = json.loads(body)
    else:
        m.text = json.dumps(body)
        m.json.return_value = body
    m.raise_for_status = MagicMock()
    return m


def _mock_client(get_resp=None, post_resp=None) -> MagicMock:
    c = MagicMock()
    c.__enter__ = MagicMock(return_value=c)
    c.__exit__ = MagicMock(return_value=False)
    if get_resp is not None:
        c.get.return_value = get_resp
    if post_resp is not None:
        c.post.return_value = post_resp
    return c


@pytest.fixture(autouse=True)
def base_env(monkeypatch):
    monkeypatch.setenv("RELAY_CP_URL", BASE_URL)
    monkeypatch.delenv("RELAY_AGENT_KEY", raising=False)
    monkeypatch.delenv("RELAY_AGENT_KEYS", raising=False)
    monkeypatch.delenv("RELAY_EMAIL", raising=False)
    monkeypatch.delenv("RELAY_PASSWORD", raising=False)


class TestParseAgentKeys:
    def test_empty_env_returns_empty_dict(self):
        import relay_mcp
        assert relay_mcp._parse_agent_keys() == {}

    def test_single_pair(self, monkeypatch):
        import relay_mcp
        monkeypatch.setenv("RELAY_AGENT_KEYS", f"{SHARE_MESH}:{KEY_MESH}")
        assert relay_mcp._parse_agent_keys() == {SHARE_MESH: KEY_MESH}

    def test_multiple_pairs(self, monkeypatch):
        import relay_mcp
        monkeypatch.setenv("RELAY_AGENT_KEYS", MULTI_KEYS_ENV)
        result = relay_mcp._parse_agent_keys()
        assert result == {SHARE_MESH: KEY_MESH, SHARE_SPARK: KEY_SPARK}

    def test_ignores_malformed_entry(self, monkeypatch):
        import relay_mcp
        monkeypatch.setenv("RELAY_AGENT_KEYS", f"{SHARE_MESH}:{KEY_MESH},bad-entry,{SHARE_SPARK}:{KEY_SPARK}")
        result = relay_mcp._parse_agent_keys()
        assert SHARE_MESH in result
        assert SHARE_SPARK in result
        assert len(result) == 2

    def test_key_with_colon_uses_first_colon_as_separator(self, monkeypatch):
        import relay_mcp
        monkeypatch.setenv("RELAY_AGENT_KEYS", f"{SHARE_MESH}:key:with:colons")
        result = relay_mcp._parse_agent_keys()
        assert result == {SHARE_MESH: "key:with:colons"}

    def test_strips_whitespace(self, monkeypatch):
        import relay_mcp
        monkeypatch.setenv("RELAY_AGENT_KEYS", f" {SHARE_MESH} : {KEY_MESH} , {SHARE_SPARK} : {KEY_SPARK} ")
        result = relay_mcp._parse_agent_keys()
        assert result == {SHARE_MESH: KEY_MESH, SHARE_SPARK: KEY_SPARK}


class TestGetKeyForShare:
    def test_multi_key_returns_share_specific_key(self, monkeypatch):
        import relay_mcp
        monkeypatch.setenv("RELAY_AGENT_KEYS", MULTI_KEYS_ENV)
        assert relay_mcp._get_key_for_share(SHARE_MESH) == KEY_MESH
        assert relay_mcp._get_key_for_share(SHARE_SPARK) == KEY_SPARK

    def test_falls_back_to_global_key_when_share_not_in_map(self, monkeypatch):
        import relay_mcp
        global_key = "global-key-xyz"
        monkeypatch.setenv("RELAY_AGENT_KEYS", f"{SHARE_MESH}:{KEY_MESH}")
        monkeypatch.setenv("RELAY_AGENT_KEY", global_key)
        assert relay_mcp._get_key_for_share(SHARE_SPARK) == global_key

    def test_returns_global_key_when_no_multi_key_map(self, monkeypatch):
        import relay_mcp
        global_key = "global-only-key"
        monkeypatch.setenv("RELAY_AGENT_KEY", global_key)
        assert relay_mcp._get_key_for_share(SHARE_MESH) == global_key

    def test_returns_none_when_no_keys_configured(self):
        import relay_mcp
        assert relay_mcp._get_key_for_share(SHARE_MESH) is None

    def test_map_takes_priority_over_global_key(self, monkeypatch):
        import relay_mcp
        monkeypatch.setenv("RELAY_AGENT_KEYS", MULTI_KEYS_ENV)
        monkeypatch.setenv("RELAY_AGENT_KEY", "global-fallback")
        assert relay_mcp._get_key_for_share(SHARE_MESH) == KEY_MESH
        assert relay_mcp._get_key_for_share(SHARE_SPARK) == KEY_SPARK


class TestIsAgentKeyMode:
    def test_true_when_global_key_set(self, monkeypatch):
        import relay_mcp
        monkeypatch.setenv("RELAY_AGENT_KEY", "some-key")
        assert relay_mcp._is_agent_key_mode() is True

    def test_true_when_multi_keys_set(self, monkeypatch):
        import relay_mcp
        monkeypatch.setenv("RELAY_AGENT_KEYS", MULTI_KEYS_ENV)
        assert relay_mcp._is_agent_key_mode() is True

    def test_false_when_no_keys(self):
        import relay_mcp
        assert relay_mcp._is_agent_key_mode() is False


class TestAuthenticateMultiKey:
    def test_describes_multi_key_shares(self, monkeypatch):
        import relay_mcp
        monkeypatch.setenv("RELAY_AGENT_KEYS", MULTI_KEYS_ENV)
        result = relay_mcp.authenticate()
        assert "2 share-specific keys" in result
        assert SHARE_MESH in result
        assert SHARE_SPARK in result

    def test_no_suffix_when_global_key_absent(self, monkeypatch):
        import relay_mcp
        monkeypatch.setenv("RELAY_AGENT_KEYS", MULTI_KEYS_ENV)
        result = relay_mcp.authenticate()
        assert "global fallback" not in result

    def test_suffix_when_global_key_also_set(self, monkeypatch):
        import relay_mcp
        monkeypatch.setenv("RELAY_AGENT_KEYS", MULTI_KEYS_ENV)
        monkeypatch.setenv("RELAY_AGENT_KEY", "global-key")
        result = relay_mcp.authenticate()
        assert "global fallback key" in result

    def test_single_key_mode_unchanged(self, monkeypatch):
        import relay_mcp
        monkeypatch.setenv("RELAY_AGENT_KEY", "single-key")
        result = relay_mcp.authenticate()
        assert "Agent key mode active" in result
        assert "share-specific" not in result


class TestListSharesMultiKey:
    def test_reports_each_share_from_its_sync_index(self, monkeypatch):
        import relay_mcp
        monkeypatch.setenv("RELAY_AGENT_KEYS", UUID_KEYS_ENV)

        mesh_client = _mock_client(get_resp=_mock_response(200, _index("a.md", "b.md")))
        spark_client = _mock_client(get_resp=_mock_response(200, _index()))
        clients = iter([mesh_client, spark_client])

        with patch.object(relay_mcp, "_get_client", side_effect=lambda: next(clients)):
            result = json.loads(relay_mcp.list_shares())

        assert result == [
            {"id": UUID_MESH, "kind": "unknown", "file_count": 2},
            {"id": UUID_SPARK, "kind": "unknown", "file_count": 0},
        ]

    def test_uses_per_share_key_on_the_non_web_route(self, monkeypatch):
        """/v1/web/* only exists when web publishing is enabled; the sync
        files-index route is the one an agent key can always reach."""
        import relay_mcp
        monkeypatch.setenv("RELAY_AGENT_KEYS", UUID_KEYS_ENV)

        mesh_client = _mock_client(get_resp=_mock_response(200, _index()))
        spark_client = _mock_client(get_resp=_mock_response(200, _index()))
        clients = iter([mesh_client, spark_client])

        with patch.object(relay_mcp, "_get_client", side_effect=lambda: next(clients)):
            relay_mcp.list_shares()

        mesh_client.get.assert_called_once()
        assert mesh_client.get.call_args[0][0] == f"{BASE_URL}/v1/shares/{UUID_MESH}/files-index"
        assert mesh_client.get.call_args[1]["headers"] == {"X-Agent-Key": KEY_MESH}
        spark_client.get.assert_called_once()
        assert spark_client.get.call_args[0][0] == f"{BASE_URL}/v1/shares/{UUID_SPARK}/files-index"
        assert spark_client.get.call_args[1]["headers"] == {"X-Agent-Key": KEY_SPARK}

    def test_kind_filter_excludes_shares_of_unknown_kind(self, monkeypatch):
        import relay_mcp
        monkeypatch.setenv("RELAY_AGENT_KEYS", UUID_KEYS_ENV)

        client = _mock_client(get_resp=_mock_response(200, _index("a.md")))

        with patch.object(relay_mcp, "_get_client", return_value=client):
            result = json.loads(relay_mcp.list_shares(kind="folder"))

        assert result == []

    def test_failed_share_fetch_reported_as_error_not_silently_dropped(self, monkeypatch):
        import relay_mcp
        monkeypatch.setenv("RELAY_AGENT_KEYS", UUID_KEYS_ENV)

        mesh_client = _mock_client(get_resp=_mock_response(403, {"error": "forbidden"}))
        spark_client = _mock_client(get_resp=_mock_response(200, _index("a.md")))
        clients = iter([mesh_client, spark_client])

        with patch.object(relay_mcp, "_get_client", side_effect=lambda: next(clients)):
            result = json.loads(relay_mcp.list_shares())

        # A real failure must change the response SHAPE (object with
        # errors), never just quietly shrink the array — otherwise "one
        # share is broken" and "you only have one share" look identical.
        assert "403" in result["errors"][UUID_MESH]
        assert result["shares"] == [{"id": UUID_SPARK, "kind": "unknown", "file_count": 1}]

    def test_share_genuinely_gone_reports_error(self, monkeypatch):
        import relay_mcp
        monkeypatch.setenv("RELAY_AGENT_KEYS", f"{UUID_MESH}:{KEY_MESH}")

        client = _mock_client(get_resp=_mock_response(404, {"error": {"message": "Share not found"}}))

        with patch.object(relay_mcp, "_get_client", return_value=client):
            result = json.loads(relay_mcp.list_shares())

        assert result["shares"] == []
        assert "404" in result["errors"][UUID_MESH]

    def test_slug_keyed_entry_reported_without_a_request(self, monkeypatch):
        import relay_mcp
        monkeypatch.setenv("RELAY_AGENT_KEYS", f"{SHARE_MESH}:{KEY_MESH},{UUID_SPARK}:{KEY_SPARK}")

        client = _mock_client(get_resp=_mock_response(200, _index()))

        with patch.object(relay_mcp, "_get_client", return_value=client):
            result = json.loads(relay_mcp.list_shares())

        assert "share UUID" in result["errors"][SHARE_MESH]
        assert result["shares"] == [{"id": UUID_SPARK, "kind": "unknown", "file_count": 0}]
        client.get.assert_called_once()

    def test_bad_key_reports_401_as_error(self, monkeypatch):
        import relay_mcp
        monkeypatch.setenv("RELAY_AGENT_KEYS", UUID_KEYS_ENV)

        client = _mock_client(get_resp=_mock_response(401, {"error": {"message": "Invalid agent key"}}))

        with patch.object(relay_mcp, "_get_client", return_value=client):
            result = json.loads(relay_mcp.list_shares())

        assert len(result["errors"]) == 2
        for msg in result["errors"].values():
            assert "401" in msg

    def test_single_key_mode_returns_empty_list(self, monkeypatch):
        import relay_mcp
        monkeypatch.setenv("RELAY_AGENT_KEY", "single-key")
        result = json.loads(relay_mcp.list_shares())
        assert result == []


class TestListFilesMultiKey:
    def test_uses_share_specific_key_on_the_non_web_route(self, monkeypatch):
        import relay_mcp
        monkeypatch.setenv("RELAY_AGENT_KEYS", UUID_KEYS_ENV)

        client = _mock_client(get_resp=_mock_response(200, _index("notes/hello.md")))

        with patch.object(relay_mcp, "_get_client", return_value=client):
            result = relay_mcp.list_files(UUID_SPARK)

        assert client.get.call_args[0][0] == f"{BASE_URL}/v1/shares/{UUID_SPARK}/files-index"
        assert client.get.call_args[1]["headers"] == {"X-Agent-Key": KEY_SPARK}
        assert result == {
            "share_id": UUID_SPARK,
            "files": {
                "notes/hello.md": {
                    "type": "doc",
                    "size": 3,
                    "modified_at": "2026-10-28T20:00:00Z",
                    "sha256": "a" * 64,
                }
            },
        }

    def test_slug_is_refused_without_a_request(self, monkeypatch):
        import relay_mcp
        monkeypatch.setenv("RELAY_AGENT_KEYS", f"{SHARE_SPARK}:{KEY_SPARK}")

        client = _mock_client()
        with patch.object(relay_mcp, "_get_client", return_value=client):
            result = relay_mcp.list_files(SHARE_SPARK)

        assert result["ok"] is False
        assert result["error"] == "invalid_share_id"
        client.get.assert_not_called()


class TestTrSearchMultiKey:
    def test_searches_the_sync_index_with_share_specific_key(self, monkeypatch):
        import relay_mcp
        monkeypatch.setenv("RELAY_AGENT_KEYS", UUID_KEYS_ENV)

        client = _mock_client(get_resp=_mock_response(200, _index("notes/hello.md", "notes/other.md")))

        with patch.object(relay_mcp, "_get_client", return_value=client):
            result = json.loads(relay_mcp.tr_search(UUID_MESH, "HELLO"))

        client.get.assert_called_once()
        assert client.get.call_args[0][0] == f"{BASE_URL}/v1/shares/{UUID_MESH}/files-index"
        assert client.get.call_args[1]["headers"] == {"X-Agent-Key": KEY_MESH}
        assert result == [
            {
                "id": None,
                "title": "hello",
                "path": "notes/hello.md",
                "relay_url": f"relay://{UUID_MESH}/notes/hello.md",
                "updated_at": "2026-10-28T20:00:00Z",
            }
        ]

    def test_slug_is_refused_without_a_request(self, monkeypatch):
        import relay_mcp
        monkeypatch.setenv("RELAY_AGENT_KEYS", f"{SHARE_MESH}:{KEY_MESH}")

        client = _mock_client()
        with patch.object(relay_mcp, "_get_client", return_value=client):
            result = json.loads(relay_mcp.tr_search(SHARE_MESH, "hello"))

        assert result["error"] == "invalid_share_id"
        client.get.assert_not_called()
