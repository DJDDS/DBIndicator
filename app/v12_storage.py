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
    "index_vol_state": ("V121_INDEX_VOL_STATE_FILE", "v121_index_vol_state.json"),
    "index_vol_backup_state": ("V121_INDEX_VOL_BACKUP_STATE_FILE", "v121_index_vol_backup_state.json"),
    "rv_lab_state": ("V121_RV_LAB_STATE_FILE", "v121_rv_lab_state.json"),
    "trial25_state": ("TRIAL25_STATE_FILE", "trial25/trial25_state.json"),
    "trial25_ledger": ("TRIAL25_LEDGER_FILE", "trial25/trial25_event_ledger.jsonl"),
    "trial25_raw_quotes": ("TRIAL25_RAW_QUOTES_FILE", "trial25/trial25_raw_quotes.jsonl"),
    "trial25_stage_d": ("TRIAL25_STAGE_D_FILE", "trial25/trial25_stage_d_calibration.json"),
    "trial25_stage_d_hash": ("TRIAL25_STAGE_D_HASH_FILE", "trial25/trial25_stage_d_calibration.sha256"),
    "trial25_universe_state": ("TRIAL25_UNIVERSE_STATE_FILE", "trial25/trial25_universe_state.json"),
    "trial25_universe_ledger": ("TRIAL25_UNIVERSE_LEDGER_FILE", "trial25/trial25_universe_ledger.jsonl"),
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
    out["index_vol_root"] = str(Path(root) / "index_vol") if mount else "index_vol"
    out["trial25_root"] = str(Path(root) / "trial25") if mount else "trial25"
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
