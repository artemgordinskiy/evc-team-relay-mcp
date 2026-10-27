# EVC Team Relay - MCP Server

> **Homelab fork.** This branch (`homelab`) replaces the unconditional
> `upsert_file` with compare-and-swap writes, and is released only as tested
> wheels on this repository's GitHub Releases. PyPI, Docker Hub, Smithery and
> the `uvx evc-team-relay-mcp` snippets below install **upstream**, which still
> has the unconditional write. See [Homelab fork](#homelab-fork) first.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![MCP](https://img.shields.io/badge/MCP-server-4A90D9)](https://modelcontextprotocol.io)
[![Smithery](https://img.shields.io/badge/Smithery-listed-6f42c1)](https://smithery.ai/servers/entirevc/evc-team-relay-mcp)
[![Install via Spark](https://spark.entire.vc/badges/evc-team-relay-mcp/install.svg)](https://spark.entire.vc/assets/evc-team-relay-mcp?utm_source=github&utm_medium=readme)

**Give your AI agent read/write access to your Obsidian vault.**

> Your agent reads your notes, creates new ones, and stays in sync — all through the [Team Relay](https://github.com/entire-vc/evc-team-relay) API.

Works with **Claude Code**, **Codex CLI**, **OpenCode**, and any [MCP](https://modelcontextprotocol.io)-compatible client.

<a href="https://glama.ai/mcp/servers/@entire-vc/evc-team-relay-mcp">
  <img width="380" height="200" src="https://glama.ai/mcp/servers/@entire-vc/evc-team-relay-mcp/badge" alt="evc-team-relay-mcp MCP server" />
</a>

---

## Homelab fork

### What differs from upstream 1.0.9

- `read_file` hashes the raw downloaded bytes and returns them as `sha256` only
  when that hash, the download's ETag and the share's sync file index all
  agree. Otherwise no token is returned (`write_token_withheld`).
- `create_file` writes through `PUT /v1/shares/{id}/sync-write` with
  `If-None-Match: *`; `update_file` writes there with `If-Match: "<sha256>"`.
  A conflict returns a structured error and writes nothing. Nothing retries.
- `upsert_file` and its unconditional `sync-upload` path are gone.
  `write_document` and `delete_file` are gone; rename is not supported.
- Text only: content must be valid UTF-8 and round-trips byte-exact, including
  BOM and CRLF. Binary and invalid UTF-8 files are refused, never decoded lossily.

### Install a release

Each release carries the wheel and sdist that CI built and integration-tested,
`SHA256SUMS`, `compat-manifest.json` (the exact server images it was tested
against) and a GitHub build-provenance attestation. Install by pinned version
and verify the hash:

```bash
TAG=v1.0.9-homelab.1
gh release download "$TAG" --repo artemgordinskiy/evc-team-relay-mcp \
  --pattern '*.whl' --pattern SHA256SUMS --dir evc-mcp
(cd evc-mcp && sha256sum --ignore-missing --strict -c SHA256SUMS)
gh attestation verify evc-mcp/*.whl --repo artemgordinskiy/evc-team-relay-mcp
uv tool install evc-mcp/*.whl
```

Then use `"command": "evc-team-relay-mcp"` with no `args` in the client
configurations below.

### Agent keys and scopes

Writes need an agent key with the `write` scope; reads need `read`. Give each
agent its own key per share with `RELAY_AGENT_KEYS="<share-uuid>:<key>,..."`,
and a read-only key where the agent should not write. Email/password (JWT) mode
is read-only: write tools raise.

### Errors

Tools return `{"ok": false, "error": <code>, "message": ...}` for expected
failures:

| Code | Meaning | What the agent should do |
| --- | --- | --- |
| `stale` | The file changed since `expected_sha256` was read | `read_file` again and merge |
| `exists` | `create_file` on a path that already exists | `read_file`, then `update_file` |
| `not_found` | `update_file` on a missing file, or unknown share/path | Check the path; create it instead |
| `not_utf8_text` | Binary or invalid UTF-8 file | Leave it alone |
| `not_byte_exact` | The server re-encoded stored bytes (invalid UTF-8 stored inline) | Leave it alone |
| `forbidden` | Key lacks the scope or the share | Use the right key |
| `rate_limited`, `too_large`, `bad_request`, `precondition_required`, `unauthorized`, `invalid_share_id` | As named | Do not loop |

Server errors (5xx) raise. The write may or may not have happened, so read the
file again before deciding anything.

### Tested against

`compat/manifest.json` pins the server release and image digests CI runs
against. CI exercises the stock control plane on PostgreSQL and MinIO. It
does **not** exercise the relay server or Obsidian clients, so it proves the
control plane's compare-and-swap, not that clients converge on what was
written.

---

## Quick Start

### 1. Install

**Option A — from PyPI (recommended):**

No installation needed — `uvx` downloads and runs automatically. Skip to step 2.

**Option B — from source:**

```bash
git clone https://github.com/entire-vc/evc-team-relay-mcp.git
cd evc-team-relay-mcp
uv sync   # or: pip install .
```

### 2. Configure your AI tool

Add the MCP server to your tool's config. Choose one authentication method:

**Agent key** (recommended) — create a key in the Obsidian plugin → Team Relay settings → **Agent Keys**. Supports read and write: `list_files`, `read_file`, `tr_search`, `create_file` and `update_file` work with a single key that has both scopes. [Quickstart →](https://github.com/entire-vc/evc-team-relay/blob/main/docs/agent-keys.md)

**Email + password** — use a dedicated agent account on your Relay instance.

<details>
<summary><b>Claude Code — agent key</b></summary>

Add to `.mcp.json` in your project root or `~/.claude/.mcp.json`:

```json
{
  "mcpServers": {
    "evc-relay": {
      "command": "uvx",
      "args": ["evc-team-relay-mcp"],
      "env": {
        "RELAY_CP_URL": "https://cp.yourdomain.com",
        "RELAY_AGENT_KEY": "tr_agent_your_key_here"
      }
    }
  }
}
```

</details>

<details>
<summary><b>Claude Code — email/password</b></summary>

```json
{
  "mcpServers": {
    "evc-relay": {
      "command": "uvx",
      "args": ["evc-team-relay-mcp"],
      "env": {
        "RELAY_CP_URL": "https://cp.yourdomain.com",
        "RELAY_EMAIL": "agent@yourdomain.com",
        "RELAY_PASSWORD": "your-password"
      }
    }
  }
}
```

</details>

<details>
<summary><b>Codex CLI</b></summary>

Add to your `codex.json`:

```json
{
  "mcp_servers": {
    "evc-relay": {
      "type": "stdio",
      "command": "uvx",
      "args": ["evc-team-relay-mcp"],
      "env": {
        "RELAY_CP_URL": "https://cp.yourdomain.com",
        "RELAY_AGENT_KEY": "tr_agent_your_key_here"
      }
    }
  }
}
```

</details>

<details>
<summary><b>OpenCode</b></summary>

Add to `opencode.json`:

```json
{
  "mcpServers": {
    "evc-relay": {
      "command": "uvx",
      "args": ["evc-team-relay-mcp"],
      "env": {
        "RELAY_CP_URL": "https://cp.yourdomain.com",
        "RELAY_AGENT_KEY": "tr_agent_your_key_here"
      }
    }
  }
}
```

</details>

<details>
<summary><b>From source (all tools)</b></summary>

If you installed from source instead of PyPI, replace `"command": "uvx"` / `"args": ["evc-team-relay-mcp"]` with:

```json
"command": "uv",
"args": ["run", "--directory", "/path/to/evc-team-relay-mcp", "relay_mcp.py"]
```

</details>

**Environment variables:**

| Variable | Required | Description |
|----------|----------|-------------|
| `RELAY_CP_URL` | Yes | Control plane base URL |
| `RELAY_AGENT_KEY` | One of | Agent key from plugin settings — read + write (recommended) |
| `RELAY_EMAIL` | One of | Account email (email/password mode) |
| `RELAY_PASSWORD` | One of | Account password (email/password mode) |

Ready-to-copy config templates are also in `config/`.

### 3. Use it

Your AI agent now has these tools:

| Tool | Description |
|------|-------------|
| `authenticate` | Authenticate with credentials (auto-managed) |
| `list_shares` | List accessible shares (filter by kind, ownership) |
| `list_files` | List files in a folder share |
| `tr_search` | Search files in shares |
| **`read_file`** | Read a UTF-8 text file and get its `sha256` write token |
| `read_document` | Not implemented — no backend route in any auth mode, always raises |
| **`create_file`** | Create a new file; refuses an existing path (`exists`) — **agent-key mode only** |
| **`update_file`** | Replace a file only if its `sha256` still matches (`stale` otherwise) — **agent-key mode only** |

**Typical workflow:** `list_shares` -> `list_files` -> `read_file` -> `update_file`
with the returned `sha256`, or `create_file` for a new path.

Authentication is automatic — the server logs in and refreshes tokens internally.

### Tool availability matrix

| Group | Tools | Status |
|-------|-------|--------|
| **Agent key**, folder shares | `list_files`, `read_file`, `create_file`, `update_file` | Working; writes need the `write` scope |
| **JWT** (email/password) | `list_files`, `tr_search`, `read_file` | Working, **read-only** by design |
| **No backend route in either mode** | `read_document` | Always raises `ValueError` |

Delete and rename are not supported.

---

## Remote Deployment (HTTP Transport)

For shared or server-side deployments, run as an HTTP server:

```bash
# Direct
uv run relay_mcp.py --transport http --port 8888

# Docker (pulls from Docker Hub automatically)
RELAY_CP_URL=https://cp.yourdomain.com \
RELAY_EMAIL=agent@yourdomain.com \
RELAY_PASSWORD=your-password \
docker compose up -d

# Or pull explicitly
docker pull deadalusevc/evc-team-relay-mcp:latest
```

**By default the server binds to `127.0.0.1` (localhost-only)** — the endpoint is not
reachable over the network even if the host has a public IP. This matches the common
case of a single MCP client on the same machine as the server.

Then configure your MCP client to connect via HTTP:

```json
{
  "mcpServers": {
    "evc-relay": {
      "type": "streamable-http",
      "url": "http://127.0.0.1:8888/mcp"
    }
  }
}
```

### Remote access via SSH tunnel (recommended)

If your MCP client runs on a different machine than the server, tunnel to the
localhost-bound port instead of exposing it publicly:

```bash
# From the client machine, forward local 8888 to the server's localhost:8888
ssh -N -L 8888:127.0.0.1:8888 user@your-server
```

Then point the client config at `http://127.0.0.1:8888/mcp` as above — traffic
goes through the SSH tunnel, and the server's bind address never needs to change.

### Public / reverse-proxy binding (opt-in)

If you genuinely need the server to accept connections from other hosts directly
(e.g. it sits behind a reverse proxy that terminates TLS and handles auth), pass
`--host` explicitly:

```bash
uv run relay_mcp.py --transport http --port 8888 --host 0.0.0.0
```

Only do this behind a reverse proxy or firewall — the MCP HTTP endpoint itself
has no built-in authentication, so binding it to `0.0.0.0` on an open network
exposes every relay tool call to anyone who can reach the port.

---

## Security

The MCP server provides significant security advantages over shell-based integrations:

- **No shell execution** — all operations are Python function calls via JSON-RPC, eliminating command injection risks
- **No CLI arguments** — credentials and tokens are never passed as process arguments (invisible in `ps` output)
- **Automatic token management** — the server handles login, JWT refresh, and token lifecycle internally; the agent never touches raw tokens
- **Typed inputs** — all parameters are validated against JSON Schema before execution
- **Single persistent process** — no per-call shell spawning, no environment leakage between invocations

> **Note:** If you're using the [OpenClaw skill](https://github.com/entire-vc/evc-team-relay-openclaw-skill) (bash scripts), consider migrating to this MCP server for a more secure and maintainable integration.

---

## How It Works

```
┌─────────────┐      MCP        ┌──────────────┐     REST API     ┌──────────────┐     Yjs CRDT      ┌──────────────┐
│  AI Agent   │ ◄────────────► │  MCP Server  │ ◄─────────────► │  Team Relay  │ ◄──────────────► │   Obsidian   │
│ (any tool)  │  stdio / HTTP  │ (this repo)  │    read/write   │   Server     │    real-time     │    Client    │
└─────────────┘                └──────────────┘                 └──────────────┘      sync         └──────────────┘
```

The MCP server wraps Team Relay's REST API into standard MCP tools. Team Relay stores documents as Yjs CRDTs and syncs them to Obsidian clients in real-time. Changes made by the agent appear in Obsidian instantly — and vice versa.

---

## Prerequisites

- Python 3.10+ with [uv](https://docs.astral.sh/uv/) (recommended) or pip
- A running [EVC Team Relay](https://github.com/entire-vc/evc-team-relay) instance (self-hosted or [hosted](https://entire.vc))
- A user account on the Relay control plane

---

## Part of the Entire VC Toolbox

| Product | What it does | Link |
|---------|-------------|------|
| **Team Relay** | Self-hosted collaboration server | [repo](https://github.com/entire-vc/evc-team-relay) |
| **Team Relay Sync** | Obsidian plugin for Team Relay | [install](https://community.obsidian.md/plugins/team-relay-sync) / [repo](https://github.com/entire-vc/evc-team-relay-obsidian) |
| **Relay MCP** | MCP server for AI agents | this repo |
| **OpenClaw Skill** | OpenClaw agent skill (bash) | [repo](https://github.com/entire-vc/evc-team-relay-openclaw-skill) |
| **Local Sync** | Vault <-> AI dev tools sync | [repo](https://github.com/entire-vc/evc-local-sync-plugin) |
| **Spark MCP** | MCP server for AI workflow catalog | [repo](https://github.com/entire-vc/evc-spark-mcp) |

## Community

- [entire.vc](https://entire.vc)
- [Discussions](https://github.com/entire-vc/.github/discussions)
- <support@entire.vc>
- [Where to get help and what to expect](https://github.com/entire-vc/evc-team-relay-mcp/blob/main/SUPPORT.md)

[//]: # (mcp-name: io.github.entire-vc/evc-team-relay-mcp)

<details>
<summary>Find us in other directories</summary>

<br>

[![MCP Badge](https://lobehub.com/badge/mcp/entire-vc-evc-team-relay-mcp)](https://lobehub.com/mcp/entire-vc-evc-team-relay-mcp)
[![Glama](https://img.shields.io/badge/Glama-listed-1F2937)](https://glama.ai/mcp/servers/entire-vc/evc-team-relay-mcp)
[![Smithery](https://img.shields.io/badge/Smithery-listed-F26B21)](https://smithery.ai/servers/entirevc/evc-team-relay-mcp)

</details>

## License

MIT
