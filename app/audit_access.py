"""Read-only audit-file access for DBIndicator Phase A.

Auditors never receive Railway, broker, or secret access. This module exposes
only explicitly curated research/audit material from:
  * repository research/ files
  * a persistent audit export directory (default /data/audit_exports)
  * a very small allow-list of top-level research documents

All paths are resolved and checked to prevent traversal.
"""
from __future__ import annotations

import os
from pathlib import Path

_ALLOWED_SUFFIXES = {
    ".md", ".txt", ".json", ".jsonl", ".csv", ".gz", ".pdf", ".zip", ".tar"
}
_ALLOWED_TOP_LEVEL = {
    "README.md",
    "FROZEN_RULE.md",
    "BENCHMARK_RELEASE.md",
    "TRIAL25_PREREGISTRATION.md",
    "TRIAL25_STAGE_D_RELEASE.md",
    "RESEARCH_BUILD.txt",
    "PRODUCTION_BUILD.txt",
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _export_root() -> Path:
    return Path(os.getenv("DBI_AUDIT_ROOT", "/data/audit_exports")).expanduser()


def _safe_under(root: Path, candidate: Path) -> Path | None:
    try:
        resolved_root = root.resolve()
        resolved = candidate.resolve()
        resolved.relative_to(resolved_root)
        return resolved
    except (OSError, ValueError):
        return None


def _allowed_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in _ALLOWED_SUFFIXES


def list_audit_files() -> list[dict]:
    files: list[dict] = []
    repo = _repo_root()

    research = repo / "research"
    if research.exists():
        for path in sorted(research.rglob("*")):
            if _allowed_file(path):
                rel = path.relative_to(research).as_posix()
                files.append({
                    "id": f"research/{rel}",
                    "name": path.name,
                    "source": "repository-research",
                    "size_bytes": path.stat().st_size,
                })

    for name in sorted(_ALLOWED_TOP_LEVEL):
        path = repo / name
        if _allowed_file(path):
            files.append({
                "id": f"top/{name}",
                "name": name,
                "source": "repository-curated",
                "size_bytes": path.stat().st_size,
            })

    exports = _export_root()
    if exports.exists():
        for path in sorted(exports.rglob("*")):
            if _allowed_file(path):
                rel = path.relative_to(exports).as_posix()
                files.append({
                    "id": f"exports/{rel}",
                    "name": path.name,
                    "source": "persistent-audit-export",
                    "size_bytes": path.stat().st_size,
                })

    return files


def resolve_audit_file(file_id: str) -> Path | None:
    file_id = (file_id or "").strip().replace("\\", "/")
    if not file_id or file_id.startswith("/"):
        return None

    repo = _repo_root()
    if file_id.startswith("research/"):
        rel = file_id[len("research/"):]
        root = repo / "research"
        candidate = _safe_under(root, root / rel)
        return candidate if candidate and _allowed_file(candidate) else None

    if file_id.startswith("exports/"):
        rel = file_id[len("exports/"):]
        root = _export_root()
        candidate = _safe_under(root, root / rel)
        return candidate if candidate and _allowed_file(candidate) else None

    if file_id.startswith("top/"):
        name = file_id[len("top/"):]
        if name not in _ALLOWED_TOP_LEVEL:
            return None
        candidate = repo / name
        return candidate if _allowed_file(candidate) else None

    return None
