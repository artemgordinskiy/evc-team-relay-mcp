"""Write the .env for compose.yml: pinned images from compat/manifest.json plus
throwaway secrets generated for this run.

Usage: python tests/integration/stack_env.py OUT_FILE

The same file carries the EVC_IT_* variables the integration tests read, so a
CI step can `set -a; . OUT_FILE` before running pytest. It is written 0600 and
must never be committed or uploaded.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def ed25519_pem() -> str:
    # openssl avoids a cryptography dependency in the test environment.
    return subprocess.run(
        ["openssl", "genpkey", "-algorithm", "ed25519"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def main(out: Path) -> None:
    manifest = json.loads((ROOT / "compat" / "manifest.json").read_text())
    postgres_password = secrets.token_hex(16)
    admin_email = "it-admin@example.com"
    admin_password = secrets.token_urlsafe(18)
    values = {
        "POSTGRES_IMAGE": manifest["dependencies"]["postgres"],
        "MINIO_IMAGE": manifest["dependencies"]["minio"],
        "MC_IMAGE": manifest["dependencies"]["mc"],
        "CONTROL_PLANE_IMAGE": manifest["server"]["images"]["control_plane"],
        "POSTGRES_PASSWORD": postgres_password,
        "JWT_SECRET": secrets.token_hex(32),
        "RELAY_PRIVATE_KEY": base64.b64encode(ed25519_pem().encode()).decode(),
        "MINIO_ROOT_USER": f"it{secrets.token_hex(6)}",
        "MINIO_ROOT_PASSWORD": secrets.token_hex(16),
        "ADMIN_EMAIL": admin_email,
        "ADMIN_PASSWORD": admin_password,
        "EVC_IT_CP_URL": "http://127.0.0.1:58000",
        "EVC_IT_DATABASE_URL": f"postgresql://relay:{postgres_password}@127.0.0.1:55432/relay",
        "EVC_IT_ADMIN_EMAIL": admin_email,
        "EVC_IT_ADMIN_PASSWORD": admin_password,
    }
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        for key, value in values.items():
            f.write(f"{key}={value}\n")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
