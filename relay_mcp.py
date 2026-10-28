"""EVC Team Relay MCP Server.

Provides MCP tools for reading and writing Obsidian vault documents
via the EVC Team Relay control plane REST API.

Supports stdio (local) and streamable-http (remote) transports.

Usage:
    uv run relay_mcp.py                    # stdio (default)
    uv run relay_mcp.py --transport http   # HTTP server on port 8888
    uv run relay_mcp.py --port 9000        # HTTP on custom port
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import sys
import re
import time
import uuid
from typing import Annotated, Any
from urllib.parse import urlsplit

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field


def _validate_http_authority(value: str) -> None:
    """Require an exact ASCII DNS name/IP, optionally with a numeric port."""
    if not value or not value.isascii() or any(c.isspace() for c in value):
        raise ValueError("expected an exact host[:port]")
    if any(c in value for c in "*/?#@\\%"):
        raise ValueError("expected an exact host[:port], without wildcards or URL components")
    parsed = urlsplit("//" + value)
    host = parsed.hostname
    port = parsed.port  # Also rejects non-numeric/out-of-range ports and unbracketed IPv6.
    if not host or parsed.netloc != value or value.endswith(":") or port == 0:
        raise ValueError("expected an exact host[:port] with a port between 1 and 65535")
    if value.startswith("["):
        if not re.fullmatch(r"\[[0-9a-fA-F:.]+\](?::[0-9]+)?", value):
            raise ValueError("expected a bracketed IPv6 address and optional numeric port")
        ipaddress.IPv6Address(host)
    elif re.fullmatch(r"[0-9.]+", host) and "." in host:
        ipaddress.IPv4Address(host)
    elif len(host) > 253 or any(
        not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
        for label in host.split(".")
    ):
        raise ValueError("expected a valid DNS name or bracketed IPv6 address")


def _parse_http_allowlist(name: str, raw: str, *, origins: bool = False) -> list[str]:
    try:
        values = json.loads(raw)
        if not isinstance(values, list) or not values:
            raise ValueError("expected a nonempty JSON array")
        for value in values:
            if not isinstance(value, str):
                raise ValueError("expected string entries")
            if origins:
                parsed = urlsplit(value)
                if not value.startswith("https://") or parsed.path or parsed.query or parsed.fragment:
                    raise ValueError("expected an HTTPS origin without a path, query or fragment")
                _validate_http_authority(value[len("https://"):])
            else:
                _validate_http_authority(value)
    except ValueError as exc:
        raise ValueError(f"Invalid {name}: {exc}") from exc
    return values


def _http_transport_security() -> TransportSecuritySettings | None:
    """Fix public HTTP identities at construction; leave local SDK defaults intact."""
    hosts = os.environ.get("RELAY_MCP_ALLOWED_HOSTS")
    origins = os.environ.get("RELAY_MCP_ALLOWED_ORIGINS")
    if hosts is None and origins is None:
        return None
    if hosts is None or origins is None:
        raise ValueError("Set both RELAY_MCP_ALLOWED_HOSTS and RELAY_MCP_ALLOWED_ORIGINS")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_parse_http_allowlist("RELAY_MCP_ALLOWED_HOSTS", hosts),
        allowed_origins=_parse_http_allowlist("RELAY_MCP_ALLOWED_ORIGINS", origins, origins=True),
    )


def _is_loopback_bind(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# ── Server setup ─────────────────────────────────────────────
_HTTP_TRANSPORT_SECURITY = _http_transport_security()
mcp = FastMCP(
    "evc-relay",
    instructions=(
        "EVC Team Relay tools for reading and writing Obsidian vault documents. "
        "Start by calling `authenticate` to get a token, then use `list_shares` "
        "to discover available shares. For folder shares, use `read_file`, "
        "`list_files` and `tr_search` with file paths. Writes are guarded "
        "compare-and-swap and agent-key-only: `read_file` returns the file's "
        "`sha256`; pass it to `update_file` as `expected_sha256`, or use "
        "`create_file` for a path that must not exist yet. A `stale` or "
        "`exists` result means someone else changed the file: read it again, "
        "merge, and decide yourself whether to write — nothing retries or "
        "overwrites automatically. `read_file`, `create_file` and "
        "`update_file` need the share UUID, not a web slug. Delete, rename "
        "and DOC-share live content are not supported."
    ),
    transport_security=_HTTP_TRANSPORT_SECURITY,
)

# Reused across the read-only tools below — one definition, one place to change.
_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
# Neither write is idempotent: a repeated call fails its own precondition.
_CREATE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)
_UPDATE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)

# ── Internal state ───────────────────────────────────────────
_token: str | None = None
_token_expires: float = 0
_refresh_token: str | None = None


def _get_base_url() -> str:
    url = os.environ.get("RELAY_CP_URL", "")
    if not url:
        raise ValueError("RELAY_CP_URL environment variable not set")
    return url.rstrip("/")


def _parse_agent_keys() -> dict[str, str]:
    """Parse RELAY_AGENT_KEYS=share1:key1,share2:key2 into {share_ref: key}.

    Reads fresh from env on each call (safe across monkeypatch in tests).
    In production the env never changes, so cost is negligible.
    """
    raw = os.environ.get("RELAY_AGENT_KEYS", "").strip()
    if not raw:
        return {}
    result: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if ":" not in entry:
            continue
        share_ref, _, key = entry.partition(":")
        share_ref = share_ref.strip()
        key = key.strip()
        if share_ref and key:
            result[share_ref] = key
    return result


def _get_agent_key() -> str | None:
    """Return RELAY_AGENT_KEY global single key, if set."""
    return os.environ.get("RELAY_AGENT_KEY", "").strip() or None


def _get_key_for_share(share_id: str) -> str | None:
    """Return agent key for a specific share.

    Lookup order: per-share RELAY_AGENT_KEYS map → global RELAY_AGENT_KEY fallback.
    """
    keys = _parse_agent_keys()
    if share_id in keys:
        return keys[share_id]
    return _get_agent_key()


def _is_agent_key_mode() -> bool:
    """True if any agent key is configured (RELAY_AGENT_KEYS or RELAY_AGENT_KEY)."""
    return bool(_parse_agent_keys() or _get_agent_key())


def _agent_headers(key: str) -> dict[str, str]:
    return {"X-Agent-Key": key}


def _get_client() -> httpx.Client:
    # Disable keep-alive: Caddy sends GOAWAY on idle connections,
    # httpx tries to reuse the stale socket and hangs.
    limits = httpx.Limits(max_keepalive_connections=0)
    return httpx.Client(timeout=30.0, limits=limits)


def _ensure_token() -> str:
    """Return a valid JWT token, refreshing if needed."""
    global _token, _token_expires, _refresh_token

    if _token and time.time() < _token_expires - 60:
        return _token

    # Try refresh first
    if _refresh_token:
        try:
            with _get_client() as client:
                r = client.post(
                    f"{_get_base_url()}/v1/auth/refresh",
                    json={"refresh_token": _refresh_token},
                )
                if r.status_code == 200:
                    data = r.json()
                    _token = data["access_token"]
                    _refresh_token = data.get("refresh_token", _refresh_token)
                    _token_expires = time.time() + data.get("expires_in", 3600)
                    return _token
        except Exception:
            pass

    # Full login
    email = os.environ.get("RELAY_EMAIL", "")
    password = os.environ.get("RELAY_PASSWORD", "")
    if not email or not password:
        raise ValueError(
            "No credentials configured. Set RELAY_AGENT_KEY (recommended — create "
            "one in Team Relay under Shares > Agent keys) for read/write access, "
            "or RELAY_EMAIL and RELAY_PASSWORD for read-only access."
        )

    with _get_client() as client:
        r = client.post(
            f"{_get_base_url()}/v1/auth/login",
            json={"email": email, "password": password},
        )
        r.raise_for_status()
        data = r.json()

    _token = data["access_token"]
    _refresh_token = data.get("refresh_token")
    _token_expires = time.time() + data.get("expires_in", 3600)
    return _token


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_ensure_token()}"}


def _normalize_files_index(items: list[dict[str, Any]]) -> dict[str, dict]:
    """Map the sync files-index list to {path: {type, size, modified_at, sha256}}."""
    return {
        item["path"]: {
            "type": item.get("type", "doc"),
            "size": item.get("size"),
            "modified_at": item.get("updated_at"),
            "sha256": item.get("sha256"),
        }
        for item in items
    }


def _list_sync_files(share_uuid: str, headers: dict[str, str]) -> dict[str, dict]:
    """Fetch the CAS sync-artifact file index for a folder share.

    Uses GET /v1/shares/{uuid}/files-index, which accepts either an agent key
    (read scope) or a Bearer JWT. The /v1/web/shares/... counterparts are only
    served when the control plane has web publishing enabled, so they are not
    used. Only items uploaded through the sync protocol (source=sync-artifact)
    are returned; this is a server-side filter, not a client limitation.

    Returns {path: {type, size, modified_at, sha256}}.
    """
    with _get_client() as client:
        r = client.get(f"{_get_base_url()}/v1/shares/{share_uuid}/files-index", headers=headers)
        r.raise_for_status()
    return _normalize_files_index(r.json())


_NO_BACKEND_ROUTE = (
    "{tool} is not available: the control-plane has no REST route for this "
    "operation in ANY auth mode (agent-key or JWT) — a genuine backend gap, "
    "not an agent-key-vs-JWT restriction. Were a route ever added, per the "
    "sanctioned write policy it would be agent-key-only — JWT/email-password "
    "mode stays read-only by design either way."
)

_JWT_WRITE_NOT_SANCTIONED = (
    "{tool} is not available in email/password (JWT) mode — by design, not a "
    "temporary gap. Write access to Team Relay is sanctioned only via "
    "agent-key auth (RELAY_AGENT_KEY / RELAY_AGENT_KEYS); JWT mode is "
    "intentionally read-only (list_files/read_file/tr_search all work fine "
    "there). Configure an agent key instead."
)


# ── Guarded sync protocol (homelab fork) ─────────────────────
#
# Reads and writes use the control plane's sync protocol only:
# GET /v1/shares/{uuid}/download + /files-index and PUT /v1/shares/{uuid}/sync-write.
# sync-write enforces If-Match / If-None-Match: * under a row lock and answers
# 412 instead of overwriting. The unconditional /v1/web/.../sync-upload and
# /upload routes are deliberately unreachable from this server.

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _result_error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": code, "message": message, **extra}


def _share_uuid(share_id: str) -> str | None:
    """Canonical share UUID, or None for a slug or malformed reference."""
    try:
        return str(uuid.UUID(share_id))
    except ValueError:
        return None


def _invalid_share_id(share_id: str) -> dict[str, Any]:
    return _result_error(
        "invalid_share_id",
        f"share_id must be the share UUID (got {share_id!r}); the guarded sync "
        "protocol does not resolve web slugs",
    )


def _sync_headers(share_id: str) -> dict[str, str]:
    agent_key = _get_key_for_share(share_id)
    return _agent_headers(agent_key) if agent_key else _headers()


def _etag_sha256(raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.strip()
    if value.startswith("W/"):
        value = value[2:].strip()
    return value.strip('"').lower() or None


def _server_detail(r: httpx.Response) -> str:
    """The message from the control plane's {"error": {"code", "message"}} envelope."""
    try:
        message = r.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return r.text[:200]
    return str(message)


_CLIENT_ERROR_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    413: "too_large",
    428: "precondition_required",
    429: "rate_limited",
}


def _client_error(r: httpx.Response) -> dict[str, Any] | None:
    """Structured result for an expected 4xx; None for anything else."""
    code = _CLIENT_ERROR_CODES.get(r.status_code)
    if code is None:
        return None
    return _result_error(code, _server_detail(r), status=r.status_code)


def _sync_write(
    share_id: str,
    file_path: str,
    content: str,
    precondition: dict[str, str],
    tool: str,
    operation: str,
) -> dict[str, Any]:
    share_uuid = _share_uuid(share_id)
    if share_uuid is None:
        return _invalid_share_id(share_id)
    agent_key = _get_key_for_share(share_id)
    if not agent_key:
        raise ValueError(_JWT_WRITE_NOT_SANCTIONED.format(tool=tool))
    try:
        body = content.encode("utf-8")
    except UnicodeEncodeError as e:
        return _result_error("not_utf8_text", f"content is not encodable as UTF-8: {e}")

    with _get_client() as client:
        r = client.put(
            f"{_get_base_url()}/v1/shares/{share_uuid}/sync-write",
            headers={
                **_agent_headers(agent_key),
                "Content-Type": "text/plain; charset=utf-8",
                **precondition,
            },
            params={"path": file_path},
            content=body,
        )
    if r.status_code == 200:
        return {"ok": True, "operation": operation, **r.json()}
    if r.status_code == 412:
        detail = _server_detail(r)
        if "If-None-Match" in precondition:
            code = "exists"
        elif detail.startswith("File not found"):
            code = "not_found"
        else:
            code = "stale"
        return _result_error(
            code,
            f"{detail}. Nothing was written. Read the file again and decide "
            "whether to write; this tool never retries or overwrites.",
            status=412,
        )
    structured = _client_error(r)
    if structured is not None:
        return structured
    r.raise_for_status()
    raise RuntimeError(f"Unexpected sync-write response: HTTP {r.status_code}")


# ── MCP Tools ────────────────────────────────────────────────


@mcp.tool(annotations=_READ_ONLY)
def authenticate() -> str:
    """Check which auth mode is active (agent key or email/password) and log in if needed.

    Takes no arguments; call it first, once, before any other tool.

    In agent-key mode (RELAY_AGENT_KEY or RELAY_AGENT_KEYS is set): no login
    is needed — each request carries X-Agent-Key directly, this just reports
    which mode is active.

    In email/password mode: logs in with the RELAY_EMAIL and RELAY_PASSWORD
    env vars. The resulting token is managed internally; subsequent tool
    calls use it automatically — you never need to pass it yourself.
    """
    if _is_agent_key_mode():
        multi = _parse_agent_keys()
        if multi:
            shares_desc = ", ".join(multi.keys())
            suffix = " + global fallback key" if _get_agent_key() else ""
            return (
                f"Agent key mode active — {len(multi)} share-specific keys "
                f"({shares_desc}){suffix}. Call list_shares to see accessible shares."
            )
        return "Agent key mode active — no email/password authentication required."
    token = _ensure_token()
    return f"Authenticated successfully. Token length: {len(token)}"


@mcp.tool(annotations=_READ_ONLY)
def list_shares(
    kind: Annotated[
        str, Field(description='Filter by share kind: "doc" or "folder". Empty string returns all kinds.')
    ] = "",
    owned_only: Annotated[
        bool,
        Field(
            description="Only return shares owned by the authenticated user. "
            "Email/password mode only; ignored in agent-key mode."
        ),
    ] = False,
) -> str:
    """List the shared folders and documents this key or account can access.

    Only lists what auth mode/key was granted — it does not create or change
    access. Example: `list_shares(kind="folder")`.

    In multi-key mode (RELAY_AGENT_KEYS set): returns metadata for every share
    that has a configured key, fetched via per-share agent-key auth.

    Each configured share is checked through its UUID-addressable sync
    files-index (/v1/shares/{uuid}/files-index), which proves the share is
    real and reachable with that key but can't report kind/path/visibility
    (reported as kind="unknown", plus a file_count). RELAY_AGENT_KEYS entries
    must be keyed by share UUID; a web-slug entry is reported as an error.

    A per-share failure (bad key, wrong share, share genuinely gone) is
    NEVER silently dropped — it lands in the response's `errors` field.
    "Couldn't check this share" and "checked, it doesn't exist" must stay
    distinguishable; collapsing both into an empty list is what let a
    config/auth problem read as "you have no shares".

    In single-key mode (RELAY_AGENT_KEY only): returns an empty list — the
    single key is tied to one share but its identity is unknown; call list_files
    or tr_search directly with the known share_id.

    In email/password mode: returns all shares the user has access to.

    Args:
        kind: Filter by share type — "doc" or "folder". Empty for all.
        owned_only: If true, only return shares owned by the user (email/password mode only).

    Returns:
        On full success: a bare JSON array of shares — id, kind, path,
        visibility, user_role in email/password mode; id, kind="unknown",
        file_count in multi-key mode.
        If any share failed to resolve: a JSON object
        {"shares": [...], "errors": {share_ref: "reason"}} instead of a
        silently-shortened array.
    """
    import json

    multi_keys = _parse_agent_keys()

    if multi_keys:
        shares: list[Any] = []
        errors: dict[str, str] = {}

        for share_ref, key in multi_keys.items():
            share_uuid = _share_uuid(share_ref)
            if share_uuid is None:
                errors[share_ref] = _invalid_share_id(share_ref)["message"]
                continue
            try:
                with _get_client() as client:
                    r = client.get(
                        f"{_get_base_url()}/v1/shares/{share_uuid}/files-index",
                        headers=_agent_headers(key),
                    )
            except Exception as e:
                errors[share_ref] = f"{type(e).__name__}: {e}"
                continue

            if r.status_code != 200:
                errors[share_ref] = f"HTTP {r.status_code}: {r.text[:200]}"
                continue

            if not kind:
                shares.append({"id": share_ref, "kind": "unknown", "file_count": len(r.json())})

        if errors:
            return json.dumps({"shares": shares, "errors": errors})
        return json.dumps(shares)

    if _get_agent_key():
        return json.dumps([])

    params: dict[str, Any] = {}
    if kind:
        params["kind"] = kind
    if owned_only:
        params["owned_only"] = "true"

    with _get_client() as client:
        r = client.get(
            f"{_get_base_url()}/v1/shares",
            headers=_headers(),
            params=params,
        )
        r.raise_for_status()
    return r.text


@mcp.tool(annotations=_READ_ONLY)
def list_files(
    share_id: Annotated[
        str,
        Field(description="UUID of the folder share, as returned by list_shares."),
    ],
) -> dict[str, Any]:
    """List every file in a shared folder with its metadata.

    Read-only; does not download file contents (use read_file for that).
    Works with an agent key (needs `read` scope) or in email/password mode;
    share_id must be the share UUID. Lists the files in the share's sync
    index, which are the files read_file and update_file can work on.

    Args:
        share_id: UUID of the folder share, e.g. "a1b2c3d4-...".

    Returns:
        {"share_id", "files": {path: {type, size, modified_at, sha256}}}, or
        {"ok": false, "error": "invalid_share_id", ...} for a non-UUID share_id.
    """
    share_uuid = _share_uuid(share_id)
    if share_uuid is None:
        return _invalid_share_id(share_id)
    return {"share_id": share_id, "files": _list_sync_files(share_uuid, _sync_headers(share_id))}


@mcp.tool(annotations=_READ_ONLY)
def tr_search(
    share_id: Annotated[
        str,
        Field(description="UUID of the folder share, as returned by list_shares."),
    ],
    query: Annotated[str, Field(description="Case-insensitive substring matched against file paths.")],
    limit: Annotated[int, Field(description="Maximum number of matches to return.")] = 20,
) -> str:
    """Find files in a shared folder by a case-insensitive match on path or name.

    Read-only; does not download file contents (use read_file for that).
    Example: `tr_search(share_id="a1b2c3d4-...", query="roadmap")`.

    Works with an agent key (needs `read` scope) or in email/password mode;
    share_id must be the share UUID.

    Args:
        share_id: UUID of the folder share.
        query: Search string matched case-insensitively against file paths.
        limit: Max results to return (default 20).

    Returns:
        JSON list of matching files:
        [{"id": doc_id_or_null, "title": filename_without_ext, "path": full_path,
          "relay_url": "relay://<slug_or_uuid>/<path>", "updated_at": ...}],
        or a JSON {"ok": false, "error": "invalid_share_id", ...} object.
    """
    import json
    from pathlib import Path

    share_uuid = _share_uuid(share_id)
    if share_uuid is None:
        return json.dumps(_invalid_share_id(share_id))
    agent_key = _get_key_for_share(share_id)
    files = _list_sync_files(share_uuid, _agent_headers(agent_key) if agent_key else _headers())

    # In email/password mode, resolve the share slug for the relay:// URL;
    # agent keys cannot list shares, so they keep the UUID.
    slug = share_id
    if not agent_key:
        try:
            with _get_client() as client:
                rs = client.get(f"{_get_base_url()}/v1/shares", headers=_headers())
                if rs.status_code == 200:
                    for s in rs.json():
                        if s.get("id") == share_id and s.get("slug"):
                            slug = s["slug"]
                            break
        except Exception:
            pass

    q = query.lower()
    matches = [
        {
            "id": None,
            "title": Path(path).stem,
            "path": path,
            "relay_url": f"relay://{slug}/{path}",
            "updated_at": meta.get("modified_at"),
        }
        for path, meta in sorted(files.items())
        if q in path.lower()
    ]
    return json.dumps(matches[:limit])


@mcp.tool(annotations=_READ_ONLY)
def read_file(
    share_id: Annotated[
        str,
        Field(description="UUID of the folder share, as returned by list_shares."),
    ],
    file_path: Annotated[
        str, Field(description='Path to the file within the share, e.g. "notes/todo.md".')
    ],
) -> dict[str, Any]:
    """Read one text file from a shared folder, with the token needed to update it.

    Read-only. Returns the full file content in one response — there is no
    partial/range read. Works with an agent key (needs `read` scope) or in
    email/password mode; share_id must be the share UUID.

    The raw downloaded bytes are hashed and compared with both the download's
    ETag and the share's sync file index. Only when all three agree is
    `sha256` returned; pass it unchanged to `update_file` as `expected_sha256`.
    Otherwise `sha256` is null and `write_token_withheld` says why:
    "snapshot_mismatch" (the file changed between the two calls) or
    "not_in_sync_index".

    Text must be valid UTF-8 and is returned exactly (BOM and CRLF preserved).
    Binary or invalid UTF-8 files are refused with error "not_utf8_text", and
    bytes that differ from the stored file's ETag with "not_byte_exact".

    Args:
        share_id: UUID of the folder share.
        file_path: File path within the folder (e.g. "Marketing/plan.md").

    Returns:
        {"ok": true, "path", "content", "format": "text", "size", "sha256",
        "write_token_withheld"} or {"ok": false, "error", "message", ...}.
    """
    share_uuid = _share_uuid(share_id)
    if share_uuid is None:
        return _invalid_share_id(share_id)
    headers = _sync_headers(share_id)
    base = f"{_get_base_url()}/v1/shares/{share_uuid}"

    with _get_client() as client:
        r = client.get(f"{base}/download", headers=headers, params={"path": file_path})
        if r.status_code != 200:
            structured = _client_error(r)
            if structured is not None:
                return structured
            r.raise_for_status()
        # Index after download: a write landing in between shows up as a
        # mismatch here, and one landing later fails the If-Match on update.
        idx = client.get(f"{base}/files-index", headers=headers)
        idx.raise_for_status()

    raw = r.content
    observed = hashlib.sha256(raw).hexdigest()
    etag = _etag_sha256(r.headers.get("etag"))
    # ETag and body come from the same index snapshot, so a mismatch is not a
    # race: the server altered the stored bytes (it re-encodes inline text
    # that is not valid UTF-8). Returning that text would invite a lossy write.
    if etag is not None and etag != observed:
        return _result_error(
            "not_byte_exact",
            f"The server returned different bytes from the ones it stored for "
            f"{file_path}, which happens when the stored file is not valid "
            "UTF-8. Refusing to return lossy content.",
            path=file_path,
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _result_error(
            "not_utf8_text",
            f"{file_path} is binary or not valid UTF-8; this server reads and "
            "writes UTF-8 text only",
            path=file_path,
            size=len(raw),
            content_type=r.headers.get("content-type"),
        )

    indexed = next((i.get("sha256") for i in idx.json() if i.get("path") == file_path), None)
    if indexed is None:
        withheld = "not_in_sync_index"
    elif etag is None or str(indexed).lower() != observed:
        withheld = "snapshot_mismatch"
    else:
        withheld = None

    return {
        "ok": True,
        "path": file_path,
        "content": text,
        "format": "text",
        "size": len(raw),
        "sha256": observed if withheld is None else None,
        "write_token_withheld": withheld,
    }


@mcp.tool(annotations=_READ_ONLY)
def read_document(
    share_id: Annotated[str, Field(description="UUID of the share, used for the access check.")],
    doc_id: Annotated[
        str, Field(description="Document UUID. Defaults to share_id when omitted.")
    ] = "",
    key: Annotated[str, Field(description="Yjs shared-type key holding the document content.")] = "contents",
) -> dict[str, Any]:
    """Reserved for live document content; not yet available on the server, use read_file.

    Always raises: the control-plane has no REST route for reading a
    document's live content by ID for either DOC-kind shares or a
    folder-share file's doc_id (that concept doesn't exist in the current
    CAS-based folder sync model). This is a genuine backend gap, not an
    agent-key-vs-JWT restriction. Use read_file for folder shares instead —
    it works in both agent-key and JWT mode.

    Args:
        share_id: UUID of the share (for ACL check).
        doc_id: Document UUID. Defaults to share_id for doc shares.
        key: Yjs shared type key. Default "contents".
    """
    if not doc_id:
        doc_id = share_id
    raise ValueError(_NO_BACKEND_ROUTE.format(tool="read_document"))


@mcp.tool(annotations=_CREATE)
def create_file(
    share_id: Annotated[str, Field(description="UUID of the folder share.")],
    file_path: Annotated[
        str, Field(description='Path of the new file within the share, e.g. "notes/todo.md".')
    ],
    content: Annotated[str, Field(description="Full UTF-8 text content of the new file.")],
) -> dict[str, Any]:
    """Create a new text file in a shared folder; fails if the path already exists. Agent key only.

    Sends If-None-Match: * to the guarded sync-write endpoint, so an existing
    file is never replaced: that case returns error "exists" and writes
    nothing. Needs an agent key with `write` scope; email/password mode is
    read-only by design.

    Args:
        share_id: UUID of the folder share.
        file_path: File path within the share (e.g. "notes/todo.md").
        content: Full text content, written as UTF-8 bytes exactly as given.

    Returns:
        {"ok": true, "operation": "created", "path", "sha256", "size",
        "updated_at"} — `sha256` is the token for the next update_file — or
        {"ok": false, "error", "message", ...}.
    """
    return _sync_write(share_id, file_path, content, {"If-None-Match": "*"}, "create_file", "created")


@mcp.tool(annotations=_UPDATE)
def update_file(
    share_id: Annotated[str, Field(description="UUID of the folder share.")],
    file_path: Annotated[
        str, Field(description='Path of the existing file within the share, e.g. "notes/todo.md".')
    ],
    content: Annotated[str, Field(description="Full UTF-8 text replacing the whole file.")],
    expected_sha256: Annotated[
        str,
        Field(description="The `sha256` returned by read_file (or a previous write) for this file."),
    ],
) -> dict[str, Any]:
    """Replace a text file only if it is unchanged since you read it. Agent key only.

    Sends If-Match: "<expected_sha256>" to the guarded sync-write endpoint.
    If anyone changed the file since that read, nothing is written and the
    result is error "stale"; read the file again and merge before deciding
    to write. Nothing retries automatically. Needs an agent key with `write`
    scope; email/password mode is read-only by design.

    Args:
        share_id: UUID of the folder share.
        file_path: File path within the share (e.g. "notes/todo.md").
        content: Full text content, written as UTF-8 bytes exactly as given.
        expected_sha256: Write token from read_file or the previous write.

    Returns:
        {"ok": true, "operation": "updated", "path", "sha256", "size",
        "updated_at"} — `sha256` is the token for the next update — or
        {"ok": false, "error", "message", ...}.
    """
    token = expected_sha256.strip().lower()
    if not _SHA256_HEX.match(token):
        return _result_error(
            "invalid_expected_sha256",
            "expected_sha256 must be the 64-character hex sha256 returned by "
            "read_file; there is no unconditional write",
        )
    return _sync_write(share_id, file_path, content, {"If-Match": f'"{token}"'}, "update_file", "updated")


# ── Entry point ──────────────────────────────────────────────

def main():
    transport = "stdio"
    port = 8888
    host = "127.0.0.1"  # localhost-only by default; use --host 0.0.0.0 behind a reverse proxy

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] in ("--transport", "-t"):
            transport = args[i + 1]
            i += 2
        elif args[i] in ("--port", "-p"):
            port = int(args[i + 1])
            i += 2
        elif args[i] in ("--host",):
            host = args[i + 1]
            i += 2
        else:
            i += 1

    if transport in ("http", "streamable-http"):
        if not _is_loopback_bind(host) and _HTTP_TRANSPORT_SECURITY is None:
            raise ValueError(
                "External HTTP binds require both RELAY_MCP_ALLOWED_HOSTS and "
                "RELAY_MCP_ALLOWED_ORIGINS as nonempty JSON arrays"
            )
        mcp.settings.host = host
        mcp.settings.port = port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
