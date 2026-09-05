"""V12 runtime storage resolution.

Railway service filesystems are ephemeral unless a Volume is attached. This
module centralises V12 research-state paths so a mounted Railway Volume is used
automatically, while explicit per-file environment overrides remain respected.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

_FILES = {
    "option_snapshots": ("V12_OPTION_SNAPSHOT_FILE", "v12_option_snapshots.jsonl"),
    "option_state": ("V12_OPTION_STATE_FILE", "v12_option_state.json"),
    "earnings_ledger": ("V12_EARNINGS_LEDGER_FILE", "v12_earnings_ledger.jsonl"),
    "earnings_state": ("V12_EARNINGS_STATE_FILE", "v12_earnings_state.json"),
}


def _under(path: str, root: str) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def resolve_v12_storage(environ: Mapping[str, str] | None = None) -> dict:
    env = dict(os.environ if environ is None else environ)
    mount = str(env.get("RAILWAY_VOLUME_MOUNT_PATH") or "").strip()
    root = str(Path(mount) / "v12") if mount else "."
    out = {"root": root, "volume_mount": mount or None}
    resolved = []
    for name, (env_key, filename) in _FILES.items():
        explicit = str(env.get(env_key) or "").strip()
        value = explicit or (str(Path(root) / filename) if mount else filename)
        out[name] = value
        resolved.append(value)
    persistent = bool(mount) and all(_under(path, mount) for path in resolved)
    out["persistent"] = persistent
    out["mode"] = "PERSISTENT_VOLUME" if persistent else "EPHEMERAL"
    out["storage_status"] = "PERSISTENT VOLUME" if persistent else "EPHEMERAL WARNING"
    return out
