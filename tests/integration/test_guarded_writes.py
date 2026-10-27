"""Guarded create/update through the MCP, against the stock control plane on
PostgreSQL. See conftest.py for the environment and the request budget.

These prove the control plane's compare-and-swap as seen through this MCP.
They do not prove that Obsidian clients converge on what was written.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import AsyncExitStack
from typing import Any

import anyio
import psycopg
import pytest

from .conftest import Mcp, Stack, agent_env, mcp_client

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def db_entries(stack: Stack, path: str) -> list[dict[str, Any]]:
    """The share's index entries for `path`, straight from PostgreSQL."""
    with psycopg.connect(stack.database_url) as conn:
        (items,) = conn.execute(
            "SELECT web_folder_items FROM shares WHERE id = %s", (stack.share_id,)
        ).fetchone()
    return [item for item in items or [] if item.get("path") == path]


def rw(stack: Stack):
    return mcp_client(stack, **agent_env(stack, stack.rw_key))


async def test_tool_surface_has_no_unconditional_writes(stack):
    async with rw(stack) as mcp:
        names = {tool.name for tool in (await mcp.session.list_tools()).tools}

    assert {"read_file", "create_file", "update_file"} <= names
    assert names.isdisjoint({"upsert_file", "write_document", "delete_file"})


async def test_read_token_chains_through_updates(stack, unique_dir):
    path = f"{unique_dir}/chain.md"
    async with rw(stack) as mcp:
        created = await mcp.call("create_file", share_id=stack.share_id, file_path=path, content="v1\n")
        read = await mcp.call("read_file", share_id=stack.share_id, file_path=path)
        assert read["sha256"] == created["sha256"] == sha(b"v1\n")

        updated = await mcp.call(
            "update_file",
            share_id=stack.share_id,
            file_path=path,
            content="v2\n",
            expected_sha256=read["sha256"],
        )
        reread = await mcp.call("read_file", share_id=stack.share_id, file_path=path)

    assert updated["ok"] is True
    assert updated["operation"] == "updated"
    assert reread["content"] == "v2\n"
    assert reread["sha256"] == updated["sha256"] == sha(b"v2\n")


async def test_stale_update_is_refused_and_changes_nothing(stack, unique_dir):
    path = f"{unique_dir}/stale.md"
    async with rw(stack) as mcp:
        created = await mcp.call("create_file", share_id=stack.share_id, file_path=path, content="base\n")
        await mcp.call(
            "update_file",
            share_id=stack.share_id,
            file_path=path,
            content="theirs\n",
            expected_sha256=created["sha256"],
        )
        stale = await mcp.call(
            "update_file",
            share_id=stack.share_id,
            file_path=path,
            content="mine\n",
            expected_sha256=created["sha256"],
        )
        after = await mcp.call("read_file", share_id=stack.share_id, file_path=path)

    assert stale["ok"] is False
    assert stale["error"] == "stale"
    assert stale["status"] == 412
    assert after["content"] == "theirs\n"
    assert after["sha256"] == sha(b"theirs\n")
    (entry,) = db_entries(stack, path)
    assert entry["sha256"] == sha(b"theirs\n")


async def test_create_on_existing_path_is_refused(stack, unique_dir):
    path = f"{unique_dir}/exists.md"
    async with rw(stack) as mcp:
        await mcp.call("create_file", share_id=stack.share_id, file_path=path, content="first\n")
        second = await mcp.call("create_file", share_id=stack.share_id, file_path=path, content="second\n")
        after = await mcp.call("read_file", share_id=stack.share_id, file_path=path)

    assert second["ok"] is False
    assert second["error"] == "exists"
    assert after["content"] == "first\n"


async def test_update_of_missing_file_is_not_found(stack, unique_dir):
    async with rw(stack) as mcp:
        result = await mcp.call(
            "update_file",
            share_id=stack.share_id,
            file_path=f"{unique_dir}/missing.md",
            content="x",
            expected_sha256=sha(b"anything"),
        )

    assert result["error"] == "not_found"
    assert db_entries(stack, f"{unique_dir}/missing.md") == []


_LARGE = "Zeile ü — 東京\r\n" * 15000  # > 256 KiB: stored in MinIO, not inline


@pytest.mark.parametrize(
    "name,content",
    [
        ("Ünïcode/naïve 東京.md", "Grüße, 東京 \U0001f600\n"),
        ("crlf.md", "line one\r\nline two\r\n"),
        ("bom-mixed.md", "﻿# Title\r\nlf line\nno final newline"),
        ("empty.md", ""),
        ("large.md", _LARGE),
    ],
    ids=["utf8", "crlf", "bom-mixed", "empty", "large"],
)
async def test_text_round_trips_byte_exact(stack, unique_dir, name, content):
    path = f"{unique_dir}/{name}"
    data = content.encode("utf-8")
    async with rw(stack) as mcp:
        created = await mcp.call("create_file", share_id=stack.share_id, file_path=path, content=content)
        read = await mcp.call("read_file", share_id=stack.share_id, file_path=path)
        edited = content + "appended\r\n"
        updated = await mcp.call(
            "update_file",
            share_id=stack.share_id,
            file_path=path,
            content=edited,
            expected_sha256=read["sha256"],
        )
        reread = await mcp.call("read_file", share_id=stack.share_id, file_path=path)

    assert created["sha256"] == sha(data)
    assert created["size"] == len(data)
    assert read["content"].encode("utf-8") == data
    assert read["sha256"] == sha(data)
    assert reread["content"].encode("utf-8") == edited.encode("utf-8")
    assert reread["sha256"] == updated["sha256"] == sha(edited.encode("utf-8"))
    (entry,) = db_entries(stack, path)
    assert entry["sha256"] == updated["sha256"]
    # Large text takes the object-store path; everything else is inline.
    assert (entry["content"] is None) == (len(edited.encode("utf-8")) >= 256 * 1024)


async def test_inline_invalid_utf8_is_refused(stack, unique_dir):
    path = f"{unique_dir}/latin1.md"
    seeded = stack.raw_write(path, b"caf\xe9 au lait\n", "text/plain; charset=utf-8")
    assert seeded.status_code == 200, seeded.text

    async with rw(stack) as mcp:
        read = await mcp.call("read_file", share_id=stack.share_id, file_path=path)
        create = await mcp.call("create_file", share_id=stack.share_id, file_path=path, content="x")

    # The server serves this file re-encoded with U+FFFD, so its bytes no
    # longer match the stored hash; the MCP refuses rather than return them.
    assert read["ok"] is False
    assert read["error"] == "not_byte_exact"
    assert "content" not in read
    assert create["error"] == "exists"


@pytest.mark.parametrize(
    "name,body,content_type",
    [
        ("large-latin1.md", b"caf\xe9\n" * 60000, "text/plain"),
        ("image.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256)), "application/octet-stream"),
    ],
    ids=["invalid-utf8-object-store", "binary"],
)
async def test_non_utf8_files_are_refused(stack, unique_dir, name, body, content_type):
    path = f"{unique_dir}/{name}"
    seeded = stack.raw_write(path, body, content_type)
    assert seeded.status_code == 200, seeded.text

    async with rw(stack) as mcp:
        read = await mcp.call("read_file", share_id=stack.share_id, file_path=path)

    assert read["ok"] is False
    assert read["error"] == "not_utf8_text"
    assert read["size"] == len(body)
    assert "content" not in read
    assert "sha256" not in read


async def test_read_only_key_cannot_write(stack, unique_dir):
    path = f"{unique_dir}/ro.md"
    async with rw(stack) as mcp:
        await mcp.call("create_file", share_id=stack.share_id, file_path=path, content="kept\n")

    async with mcp_client(stack, **agent_env(stack, stack.ro_key)) as mcp:
        read = await mcp.call("read_file", share_id=stack.share_id, file_path=path)
        update = await mcp.call(
            "update_file",
            share_id=stack.share_id,
            file_path=path,
            content="changed\n",
            expected_sha256=read["sha256"],
        )
        create = await mcp.call(
            "create_file", share_id=stack.share_id, file_path=f"{unique_dir}/new.md", content="x"
        )

    assert read["sha256"] == sha(b"kept\n")
    assert update["error"] == create["error"] == "forbidden"
    assert update["status"] == create["status"] == 403
    (entry,) = db_entries(stack, path)
    assert entry["sha256"] == sha(b"kept\n")
    assert db_entries(stack, f"{unique_dir}/new.md") == []


async def test_password_mode_reads_but_cannot_write(stack, unique_dir):
    path = f"{unique_dir}/jwt.md"
    async with rw(stack) as mcp:
        await mcp.call("create_file", share_id=stack.share_id, file_path=path, content="jwt\n")

    env = {"RELAY_EMAIL": stack.admin_email, "RELAY_PASSWORD": stack.admin_password}
    async with mcp_client(stack, **env) as mcp:
        read = await mcp.call("read_file", share_id=stack.share_id, file_path=path)
        error = await mcp.call_error(
            "create_file", share_id=stack.share_id, file_path=f"{unique_dir}/no.md", content="x"
        )

    assert read["sha256"] == sha(b"jwt\n")
    assert "create_file is not available in email/password (JWT) mode" in error


async def _race(
    stack: Stack, n: int, tool: str, arguments: Callable[[int], dict[str, Any]]
) -> list[dict[str, Any]]:
    """Start n MCP server processes, then release one call on each at once."""
    results: list[dict[str, Any]] = [{} for _ in range(n)]
    async with AsyncExitStack() as stack_cm:
        clients: list[Mcp] = [
            await stack_cm.enter_async_context(rw(stack)) for _ in range(n)
        ]
        go = anyio.Event()

        async def run(i: int) -> None:
            await go.wait()
            results[i] = await clients[i].call(tool, **arguments(i))

        async with anyio.create_task_group() as tg:
            for i in range(n):
                tg.start_soon(run, i)
            await anyio.sleep(0.2)
            go.set()
    return results


RACERS = 6


@pytest.mark.parametrize("round_", [1, 2])
async def test_concurrent_updates_have_exactly_one_winner(stack, unique_dir, round_):
    path = f"{unique_dir}/race.md"
    async with rw(stack) as mcp:
        base = await mcp.call("create_file", share_id=stack.share_id, file_path=path, content="base\n")

    results = await _race(
        stack,
        RACERS,
        "update_file",
        lambda i: {
            "share_id": stack.share_id,
            "file_path": path,
            "content": f"writer {i}\n",
            "expected_sha256": base["sha256"],
        },
    )

    winners = [i for i, r in enumerate(results) if r["ok"]]
    assert len(winners) == 1, results
    assert all(r["error"] == "stale" for r in results if not r["ok"]), results
    winner = f"writer {winners[0]}\n".encode()
    (entry,) = db_entries(stack, path)
    assert entry["sha256"] == results[winners[0]]["sha256"] == sha(winner)
    async with rw(stack) as mcp:
        read = await mcp.call("read_file", share_id=stack.share_id, file_path=path)
    assert read["content"].encode() == winner


async def test_concurrent_creates_have_exactly_one_winner(stack, unique_dir):
    path = f"{unique_dir}/new-race.md"

    results = await _race(
        stack,
        RACERS,
        "create_file",
        lambda i: {"share_id": stack.share_id, "file_path": path, "content": f"creator {i}\n"},
    )

    winners = [i for i, r in enumerate(results) if r["ok"]]
    assert len(winners) == 1, results
    assert all(r["error"] == "exists" for r in results if not r["ok"]), results
    (entry,) = db_entries(stack, path)
    assert entry["sha256"] == sha(f"creator {winners[0]}\n".encode())
    assert [i["path"] for i in stack.index()].count(path) == 1
