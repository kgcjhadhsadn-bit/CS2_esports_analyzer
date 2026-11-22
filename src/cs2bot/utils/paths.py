"""Path helpers.

Config files (``config/*.yaml``) and runtime dirs are given as repo-relative paths in settings. The
app normally runs from the repo root, but tests (and scheduled tasks) may run elsewhere, so relative
paths are resolved against the current directory first and the project root second.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def project_root() -> Path:
    """Directory containing pyproject.toml (source checkout), else the current directory."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "src" / "cs2bot").is_dir():
            return parent
    return Path.cwd()


def resolve_path(path: Path | str) -> Path:
    """Absolute paths unchanged; relative ones: CWD if it exists there, else under the project root."""
    p = Path(path)
    if p.is_absolute():
        return p
    if p.exists():
        return p.resolve()
    return project_root() / p
