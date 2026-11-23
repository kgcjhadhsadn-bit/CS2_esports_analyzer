"""Archive extraction and demo-file <-> map matching.

HLTV serves one ``.rar`` per match (``team-a-vs-team-b-m1-mirage.dem``, ``...-m2-inferno.dem``) and,
for newer matches, optionally one archive per map. Other sources use ``.zip``, ``.dem.zst``,
``.dem.gz`` or ``.dem.bz2`` (Valve matchmaking). The archive type is detected from the file's magic
bytes first (a browser download may have no useful extension) and from the extension second.

Only ``.dem`` members are extracted, always *flattened* into ``dest_dir`` under a sanitised basename,
so a hostile archive cannot write outside the destination (zip-slip) and cannot fill the disk past
``max_member_bytes`` per file.

RAR needs an external tool (the format is proprietary). The member list is read with the pure-Python
``rarfile`` package; the data is extracted by running a tool directly, trying every tool found until
one succeeds, because no free tool reads every RAR variant: checked on this project's build machine,
``bsdtar`` (libarchive 3.7) refuses solid RAR3 archives and ``unar`` 1.10 fails on solid RAR5 ones,
while each handles the other. (``rarfile``'s own piping is not used: with the ``bsdtar`` backend
rarfile 4.5 builds ``bsdtar -x --to-stdout -f -- <archive>``, which bsdtar reads as an archive named
``--``.) :func:`find_rar_tools` looks for ``unrar`` / ``7z`` / ``7zz`` / ``unar`` / ``bsdtar`` on PATH,
in the usual Windows install folders (7-Zip, WinRAR) and for Windows' built-in ``tar.exe``, which is
bsdtar.
"""

from __future__ import annotations

import bz2
import gzip
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Literal

import rarfile
import zstandard

from cs2bot.ingest.demos.download import safe_filename
from cs2bot.ingest.demos.errors import ArchiveError, ArchiveToolMissingError
from cs2bot.utils.logging import get_logger
from cs2bot.utils.names import normalize_map_name

log = get_logger(__name__)

ArchiveKind = Literal["rar", "zip", "zst", "gz", "bz2", "dem", "unknown"]
HeaderReader = Callable[[Path], str | None]

# A single CS2 GOTV demo is 100-700 MB (triple overtime ~1 GB).
MAX_DEM_BYTES = 4 * 1024**3
_COPY_CHUNK = 1 << 20

_RAR_MAGIC = b"Rar!\x1a\x07"
_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_GZIP_MAGIC = b"\x1f\x8b"
_BZ2_MAGIC = b"BZh"
# "PBDEMS2\0" = Source 2 (CS2); "HL2DEMO\0" = Source 1 (CS:GO, not parseable by demoparser2).
_DEMO_MAGICS = (b"PBDEMS2\x00", b"HL2DEMO\x00")

_RAR_TOOL_HELP = (
    "No RAR extraction tool was found. HLTV demo archives are .rar files: install 7-Zip "
    "(https://www.7-zip.org) or WinRAR on Windows (7z.exe / UnRAR.exe on PATH or in the default "
    "'Program Files' folder; Windows 10 and later also ship a usable tar.exe), or 'unrar' / '7zip' / "
    "'unar' / 'libarchive-tools' (bsdtar) on Linux and macOS."
)


# -------------------------------------------------------------------------------------------- kinds


def detect_archive_kind(path: Path) -> ArchiveKind:
    """Archive type from magic bytes, falling back to the file extension."""
    try:
        with path.open("rb") as fh:
            head = fh.read(8)
    except OSError as exc:
        raise ArchiveError(f"cannot read {path}: {exc}") from exc
    if head.startswith(_RAR_MAGIC):
        return "rar"
    if head.startswith(_ZIP_MAGICS):
        return "zip"
    if head.startswith(_ZSTD_MAGIC):
        return "zst"
    if head.startswith(_GZIP_MAGIC):
        return "gz"
    if head.startswith(_DEMO_MAGICS):
        return "dem"
    name = path.name.lower()
    if head.startswith(_BZ2_MAGIC) and name.endswith((".bz2", ".bz")):
        return "bz2"
    by_suffix: dict[str, ArchiveKind] = {
        ".rar": "rar",
        ".zip": "zip",
        ".zst": "zst",
        ".zstd": "zst",
        ".gz": "gz",
        ".bz2": "bz2",
        ".dem": "dem",
    }
    return by_suffix.get(path.suffix.lower(), "unknown")


# ---------------------------------------------------------------------------------------- rar tools

RarToolKind = Literal["unrar", "7z", "unar", "bsdtar"]
RAR_TOOL_TIMEOUT_S = 1800.0


@dataclass(frozen=True)
class RarTool:
    """An external program that can extract RAR archives."""

    kind: RarToolKind
    executable: str

    def command(self, archive: Path, dest_dir: Path, members: Sequence[str] = ()) -> list[str]:
        """Command line extracting ``members`` (everything when empty) of ``archive`` into ``dest_dir``."""
        if self.kind == "unrar":
            # -p-: never ask for a password, -o+: overwrite; the target needs a trailing separator
            return [
                self.executable,
                "x",
                "-inul",
                "-y",
                "-o+",
                "-p-",
                str(archive),
                *members,
                f"{dest_dir}{os.sep}",
            ]
        if self.kind == "7z":
            return [self.executable, "x", "-y", "-bd", "-p", f"-o{dest_dir}", str(archive), *members]
        if self.kind == "unar":
            # -D: no extra containing directory, -f: overwrite
            return [self.executable, "-q", "-f", "-D", "-o", str(dest_dir), str(archive), *members]
        return [self.executable, "-x", "-f", str(archive), "-C", str(dest_dir), *members]


def _is_bsdtar(executable: str) -> bool:
    try:
        out = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "bsdtar" in (out.stdout + out.stderr).lower()


def _windows_install_candidates() -> list[tuple[RarToolKind, str]]:
    """Tools in their default Windows install folders (they are usually not on PATH)."""
    roots = [os.environ.get(var) for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)")]
    found: list[tuple[RarToolKind, str]] = []
    for root in dict.fromkeys(r for r in roots if r):
        layout: tuple[tuple[RarToolKind, str], ...] = (("unrar", "WinRAR/UnRAR.exe"), ("7z", "7-Zip/7z.exe"))
        for kind, relative in layout:
            candidate = Path(root) / relative
            if candidate.is_file():
                found.append((kind, str(candidate)))
    return found


def find_rar_tools() -> list[RarTool]:
    """Every usable RAR extraction tool on this machine, best first (empty list: none installed).

    Order: ``unrar`` (the reference implementation), ``7z`` / ``7zz``, ``unar``, ``bsdtar`` and finally a
    ``tar`` that turns out to be bsdtar (Windows 10+ ships one as ``System32\\tar.exe``).
    """
    tools: list[RarTool] = []
    seen: set[str] = set()

    def add(kind: RarToolKind, executable: str | None) -> None:
        if executable and executable not in seen:
            seen.add(executable)
            tools.append(RarTool(kind, executable))

    add("unrar", shutil.which("unrar"))
    for kind, executable in _windows_install_candidates():
        if kind == "unrar":
            add(kind, executable)
    for name in ("7z", "7zz"):
        add("7z", shutil.which(name))
    for kind, executable in _windows_install_candidates():
        if kind == "7z":
            add(kind, executable)
    add("unar", shutil.which("unar"))
    add("bsdtar", shutil.which("bsdtar"))
    tar_candidates = [shutil.which("tar")]
    system_root = os.environ.get("SYSTEMROOT")
    if system_root:
        tar_candidates.append(str(Path(system_root) / "System32" / "tar.exe"))
    for tar in dict.fromkeys(t for t in tar_candidates if t):
        if tar not in seen and Path(tar).is_file() and _is_bsdtar(tar):
            add("bsdtar", tar)
    return tools


def ensure_rar_backend() -> list[RarTool]:
    """The available RAR tools; raises :class:`ArchiveToolMissingError` (with install advice) if none.

    ``cs2bot doctor`` can call this to tell the owner whether ``.rar`` demos can be unpacked.
    """
    tools = find_rar_tools()
    if not tools:
        raise ArchiveToolMissingError(_RAR_TOOL_HELP)
    return tools


# --------------------------------------------------------------------------------------- extraction


def _is_dem_member(name: str) -> bool:
    return name.lower().endswith(".dem")


def _unique_target(dest_dir: Path, name: str, taken: set[str]) -> Path:
    base = safe_filename(name, default="demo.dem")
    if not base.lower().endswith(".dem"):
        base += ".dem"
    candidate, counter = base, 1
    while candidate.lower() in taken:
        counter += 1
        candidate = f"{base[:-4]}-{counter}.dem"
    taken.add(candidate.lower())
    return dest_dir / candidate


def _copy_limited(src: IO[bytes] | Any, target: Path, max_bytes: int) -> int:
    """Stream ``src`` into ``target`` (via a temp file); abort above ``max_bytes`` (decompression bomb)."""
    part = target.with_name(target.name + ".part")
    written = 0
    try:
        with part.open("wb") as out:
            while True:
                chunk = src.read(_COPY_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise ArchiveError(f"{target.name} is larger than the {max_bytes}-byte limit")
                out.write(chunk)
        os.replace(part, target)
    finally:
        part.unlink(missing_ok=True)
    return written


def _single_file_target(path: Path, dest_dir: Path, strip: Sequence[str]) -> Path:
    name = path.name
    for suffix in strip:
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    return _unique_target(dest_dir, name, set())


def _extract_zip(path: Path, dest_dir: Path, max_bytes: int) -> list[Path]:
    out: list[Path] = []
    taken: set[str] = set()
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir() or not _is_dem_member(info.filename):
                    continue
                target = _unique_target(dest_dir, info.filename, taken)
                with archive.open(info) as src:
                    _copy_limited(src, target, max_bytes)
                out.append(target)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, NotImplementedError, OSError) as exc:
        _remove_all(out)
        raise ArchiveError(f"cannot extract zip archive {path.name}: {exc}") from exc
    except ArchiveError:
        _remove_all(out)
        raise
    return out


def _list_rar_dem_members(path: Path, max_bytes: int) -> list[str] | None:
    """Names of the ``.dem`` members according to the archive headers (``None`` if they are unreadable).

    Raises :class:`ArchiveError` when the listing works and shows no demo, or a demo above the limit.
    """
    try:
        with rarfile.RarFile(path) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
    except (rarfile.Error, OSError, ValueError) as exc:
        log.warning("cannot list %s with rarfile (%s); extracting everything", path.name, exc)
        return None
    members = [info for info in infos if _is_dem_member(info.filename or "")]
    if not members:
        raise ArchiveError(f"{path.name} contains no .dem file")
    for info in members:
        if (info.file_size or 0) > max_bytes:
            raise ArchiveError(f"{info.filename} in {path.name} is larger than the {max_bytes}-byte limit")
    return [str(info.filename) for info in members]


def _collect_dem_files(work_dir: Path, dest_dir: Path, max_bytes: int, taken: set[str]) -> list[Path]:
    """Move the demos a tool unpacked below ``work_dir`` into ``dest_dir`` (flat, sanitised names)."""
    found: list[Path] = []
    for root, _dirs, files in os.walk(work_dir, followlinks=False):
        for name in files:
            candidate = Path(root) / name
            if candidate.is_symlink() or not candidate.is_file() or not _is_dem_member(name):
                continue
            found.append(candidate)
    out: list[Path] = []
    for candidate in sorted(found, key=lambda p: str(p.relative_to(work_dir)).lower()):
        if candidate.stat().st_size > max_bytes:
            _remove_all(out)
            raise ArchiveError(f"{candidate.name} is larger than the {max_bytes}-byte limit")
        target = _unique_target(dest_dir, candidate.name, taken)
        os.replace(candidate, target)
        out.append(target)
    return out


def _extract_rar(path: Path, dest_dir: Path, max_bytes: int) -> list[Path]:
    """Unpack the demos of a RAR archive with the first installed tool that manages to do it."""
    members = _list_rar_dem_members(path, max_bytes)
    tools = ensure_rar_backend()
    # Names that a tool could read as an option / list file are not passed on: extract everything then.
    explicit = members if members and not any(m.startswith(("-", "@")) for m in members) else []
    failures: list[str] = []
    for tool in tools:
        work_dir = Path(tempfile.mkdtemp(prefix=".unrar-", dir=dest_dir))
        try:
            command = tool.command(path.resolve(), work_dir, explicit)
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=RAR_TOOL_TIMEOUT_S,
                    check=False,
                    stdin=subprocess.DEVNULL,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                failures.append(f"{tool.kind}: {type(exc).__name__}: {exc}")
                continue
            extracted = _collect_dem_files(work_dir, dest_dir, max_bytes, set())
            expected = len(members) if members is not None else 1
            if result.returncode == 0 and len(extracted) >= expected:
                return extracted
            _remove_all(extracted)
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            failures.append(
                f"{tool.kind}: exit code {result.returncode}, {len(extracted)} of {expected} demo files"
                + (f" ({detail[-1][:200]})" if detail else "")
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
    raise ArchiveError(f"cannot extract rar archive {path.name}: " + "; ".join(failures))


def _extract_stream(path: Path, dest_dir: Path, max_bytes: int, kind: ArchiveKind) -> list[Path]:
    target = _single_file_target(path, dest_dir, (".zst", ".zstd", ".gz", ".bz2", ".bz"))
    try:
        if kind == "zst":
            with path.open("rb") as raw, zstandard.ZstdDecompressor().stream_reader(raw) as src:
                _copy_limited(src, target, max_bytes)
        elif kind == "gz":
            with gzip.open(path, "rb") as src:
                _copy_limited(src, target, max_bytes)
        else:
            with bz2.open(path, "rb") as src:
                _copy_limited(src, target, max_bytes)
    except (zstandard.ZstdError, gzip.BadGzipFile, EOFError, OSError, ValueError) as exc:
        target.unlink(missing_ok=True)
        raise ArchiveError(f"cannot decompress {path.name}: {type(exc).__name__}: {exc}") from exc
    return [target]


def _remove_all(paths: Iterable[Path]) -> None:
    for p in paths:
        p.unlink(missing_ok=True)


def extract_archive(path: Path, dest_dir: Path, *, max_member_bytes: int = MAX_DEM_BYTES) -> list[Path]:
    """Extract every ``.dem`` of an archive into ``dest_dir``; returns the paths sorted by name.

    Supported: ``.rar`` (external tool, see :func:`find_rar_tools`), ``.zip``, single-file
    ``.zst`` / ``.gz`` / ``.bz2`` and a plain ``.dem``, which is returned as-is (not copied).
    Raises :class:`ArchiveToolMissingError` when a RAR tool is needed but missing, and
    :class:`ArchiveError` for corrupt / unknown archives or archives without a demo file.
    """
    path = Path(path)
    dest_dir = Path(dest_dir)
    if not path.is_file():
        raise ArchiveError(f"archive {path} does not exist")
    kind = detect_archive_kind(path)
    if kind == "dem":
        return [path]
    if kind == "unknown":
        raise ArchiveError(f"{path.name}: unknown archive type (expected rar/zip/zst/gz/bz2/dem)")
    dest_dir.mkdir(parents=True, exist_ok=True)
    if kind == "rar":
        extracted = _extract_rar(path, dest_dir, max_member_bytes)
    elif kind == "zip":
        extracted = _extract_zip(path, dest_dir, max_member_bytes)
    else:
        extracted = _extract_stream(path, dest_dir, max_member_bytes, kind)
    if not extracted:
        raise ArchiveError(f"{path.name} contains no .dem file")
    return sorted(extracted, key=lambda p: p.name.lower())


# ------------------------------------------------------------------------------------- map matching

_MAP_NUMBER_RE = re.compile(r"(?:^|[^a-z0-9])m(?:ap)?[-_ ]?([1-9])(?![a-z0-9])", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def read_demo_map_name(path: Path) -> str | None:
    """Normalised map name from the demo header (``"de_mirage"`` -> ``"mirage"``); ``None`` on failure."""
    from demoparser2 import DemoParser  # local import: keeps archive handling usable without the parser

    try:
        header = DemoParser(str(path)).parse_header()
        raw = header.get("map_name")
    except Exception as exc:  # demoparser2 raises its own Exception subclass
        log.warning("cannot read demo header of %s: %s", path.name, exc)
        return None
    return normalize_map_name(str(raw)) if raw else None


def filename_map_number(name: str) -> int | None:
    """Map number hint in a demo filename: ``...-m2-inferno.dem`` -> 2, ``..._map3.dem`` -> 3."""
    match = _MAP_NUMBER_RE.search(Path(name).stem)
    return int(match.group(1)) if match else None


def filename_map_name(name: str, candidates: Iterable[str]) -> str | None:
    """Which of ``candidates`` (normalised map names) the filename mentions; the last mention wins.

    HLTV puts the map at the end (``natus-vincere-vs-faze-m1-mirage``), so a team that happens to be
    called like a map earlier in the name does not confuse the hint.
    """
    wanted = set(candidates)
    tokens = _TOKEN_RE.findall(Path(name).stem.lower())
    found: str | None = None
    for i, token in enumerate(tokens):
        options = [token]
        if i + 1 < len(tokens):
            options.append(f"{token} {tokens[i + 1]}")  # "dust 2", "dust ii"
        for option in options:
            normalised = normalize_map_name(option)
            if normalised in wanted:
                found = normalised
    return found


def match_dem_files_to_maps(
    dem_paths: Sequence[Path],
    maps: Sequence[tuple[int, str]],
    header_reader: HeaderReader = read_demo_map_name,
) -> dict[int, Path]:
    """Assign demo files to the maps of a match: ``{map_number: dem_path}``.

    ``maps`` is ``[(map_number, map_name), ...]`` (the match's ``MapPlayed`` rows). The demo header's
    map name is authoritative; filename hints (``-m2-``, the map name) break ties and stand in when
    a header cannot be read. A file is never assigned on no evidence at all, except in the trivial
    case of one file for one map. Maps without a file are simply absent from the result, extra
    files are ignored; when several files fit one map (a demo split into parts after a server
    crash) the largest one is used.
    """
    wanted = [(number, normalize_map_name(name)) for number, name in maps]
    map_names = {name for _, name in wanted}
    candidates: list[tuple[int, int, str, int, Path]] = []  # (-score, -size, name, map_number, path)
    for path in dem_paths:
        try:
            header = header_reader(path)
        except Exception as exc:
            log.warning("header reader failed for %s: %s", path.name, exc)
            header = None
        header = normalize_map_name(header) if header else None
        name_hint = filename_map_name(path.name, map_names)
        number_hint = filename_map_number(path.name)
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        for number, map_name in wanted:
            if header is not None:
                if header != map_name:
                    continue
                score = 100
            else:
                if name_hint is not None and name_hint != map_name:
                    continue
                score = 0
            if name_hint == map_name:
                score += 10
            if number_hint is not None:
                if number_hint == number:
                    score += 5
                elif header is None and name_hint is None:
                    continue  # the only evidence we have points at another map
                else:
                    score -= 3
            if score <= 0 and not (len(dem_paths) == 1 and len(wanted) == 1):
                continue
            candidates.append((-score, -size, path.name.lower(), number, path))
    assigned: dict[int, Path] = {}
    used: set[Path] = set()
    for _neg_score, _neg_size, _name, number, path in sorted(candidates, key=lambda c: c[:4]):
        if number in assigned or path in used:
            continue
        assigned[number] = path
        used.add(path)
    return assigned
