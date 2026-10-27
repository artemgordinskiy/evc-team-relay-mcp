"""Fixtures for the guarded-write integration tests.

The tests drive the MCP server as a real stdio subprocess against the
control plane on PostgreSQL. They need:

  EVC_IT_CP_URL          control plane base URL
  EVC_IT_DATABASE_URL    libpq URL of its PostgreSQL database
  EVC_IT_ADMIN_EMAIL     bootstrap admin, used once to create a share and keys
  EVC_IT_ADMIN_PASSWORD
  EVC_IT_MCP_COMMAND     MCP entry point to test (CI: the built wheel's script)

Rate limits on the server are per client IP and per worker, in memory:
login 10/min, share creation 20/min, sync-write 60/min. The whole suite makes
two logins, one share and fewer than 60 sync-write requests, so no test can
see a 429 even if every request lands on the same worker.
"""

from __future__ import annotations

import os
import shlex
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

_REQUIRED = (
    "EVC_IT_CP_URL",
    "EVC_IT_DATABASE_URL",
    "EVC_IT_ADMIN_EMAIL",
    "EVC_IT_ADMIN_PASSWORD",
    "EVC_IT_MCP_COMMAND",
)


@dataclass(frozen=True)
class Stack:
    cp_url: str
    database_url: str
    admin_email: str
    admin_password: str
    mcp_command: list[str]
    share_id: str
    rw_key: str
    ro_key: str

    def raw_write(self, path: str, body: bytes, content_type: str) -> httpx.Response:
        """Create a file through sync-write directly, bypassing the MCP."""
        return httpx.put(
            f"{self.cp_url}/v1/shares/{self.share_id}/sync-write",
            params={"path": path},
            headers={
                "X-Agent-Key": self.rw_key,
                "If-None-Match": "*",
                "Content-Type": content_type,
            },
            content=body,
            timeout=30,
        )

    def index(self) -> list[dict[str, Any]]:
        r = httpx.get(
            f"{self.cp_url}/v1/shares/{self.share_id}/files-index",
            headers={"X-Agent-Key": self.rw_key},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()


@pytest.fixture(scope="session")
def stack() -> Stack:
    missing = [name for name in _REQUIRED if not os.environ.get(name)]
    if missing:
        pytest.fail(f"integration environment incomplete, missing: {', '.join(missing)}")
    cp_url = os.environ["EVC_IT_CP_URL"].rstrip("/")
    email = os.environ["EVC_IT_ADMIN_EMAIL"]
    password = os.environ["EVC_IT_ADMIN_PASSWORD"]

    with httpx.Client(base_url=cp_url, timeout=30) as client:
        login = client.post("/v1/auth/login", json={"email": email, "password": password})
        login.raise_for_status()
        auth = {"Authorization": f"Bearer {login.json()['access_token']}"}
        share = client.post(
            "/v1/shares",
            headers=auth,
            json={"kind": "folder", "path": f"it-{uuid.uuid4().hex[:8]}", "visibility": "private"},
        )
        share.raise_for_status()
        share_id = share.json()["id"]

        def agent_key(scopes: list[str]) -> str:
            r = client.post(
                f"/v1/web/shares/{share_id}/agent-keys",
                headers=auth,
                json={"label": f"it-{'-'.join(scopes)}", "scopes": scopes},
            )
            r.raise_for_status()
            assert r.json()["scopes"] == scopes
            return r.json()["key"]

        rw_key = agent_key(["read", "write"])
        ro_key = agent_key(["read"])

    return Stack(
        cp_url=cp_url,
        database_url=os.environ["EVC_IT_DATABASE_URL"],
        admin_email=email,
        admin_password=password,
        mcp_command=shlex.split(os.environ["EVC_IT_MCP_COMMAND"]),
        share_id=share_id,
        rw_key=rw_key,
        ro_key=ro_key,
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def unique_dir() -> str:
    """A fresh folder per test, so tests never see each other's files."""
    return f"t-{uuid.uuid4().hex[:10]}"


class Mcp:
    """One MCP stdio server process and its client session."""

    def __init__(self, session: ClientSession) -> None:
        self.session = session

    async def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        result = await self.session.call_tool(tool, arguments)
        assert not result.isError, result.content
        assert result.structuredContent is not None
        return result.structuredContent

    async def call_error(self, tool: str, **arguments: Any) -> str:
        """Call a tool that must raise; return the error text."""
        result = await self.session.call_tool(tool, arguments)
        assert result.isError, result.structuredContent
        return " ".join(getattr(c, "text", "") for c in result.content)


@asynccontextmanager
async def mcp_client(stack: Stack, **env: str) -> AsyncIterator[Mcp]:
    params = StdioServerParameters(
        command=stack.mcp_command[0],
        args=stack.mcp_command[1:],
        env={"RELAY_CP_URL": stack.cp_url, **env},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield Mcp(session)


def agent_env(stack: Stack, key: str) -> dict[str, str]:
    return {"RELAY_AGENT_KEYS": f"{stack.share_id}:{key}"}
