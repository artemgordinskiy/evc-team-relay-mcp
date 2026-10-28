"""Agent-key list tools against a control plane without web publishing.

The CI control plane sets no web publish domain, so every /v1/web/shares/...
read route answers 404 there, as it does on a private deployment.
"""

from __future__ import annotations

import json

import pytest

from .conftest import agent_env, mcp_client

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def test_list_tools_see_a_created_file(stack, unique_dir):
    path = f"{unique_dir}/Findable-Note.md"
    async with (
        mcp_client(stack, **agent_env(stack, stack.ro_key)) as ro,
        mcp_client(stack, **agent_env(stack, stack.rw_key)) as rw,
    ):
        created = await rw.call(
            "create_file", share_id=stack.share_id, file_path=path, content="x\n"
        )

        shares = json.loads((await ro.call("list_shares"))["result"])
        listed = await ro.call("list_files", share_id=stack.share_id)
        found = json.loads(
            (
                await ro.call(
                    "tr_search", share_id=stack.share_id, query="findable-note"
                )
            )["result"]
        )

    assert [s["id"] for s in shares] == [stack.share_id]
    assert shares[0]["file_count"] >= 1
    assert listed["files"][path]["sha256"] == created["sha256"]
    assert [match["path"] for match in found] == [path]
