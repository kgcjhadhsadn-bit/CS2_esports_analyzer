"""Archive extraction (zip / zst / gz / bz2 / rar / plain dem) and demo-file <-> map matching."""

from __future__ import annotations

import bz2
import gzip
import os
import struct
import subprocess
import zipfile
import zlib
from pathlib import Path
from typing import Any

import pytest
import rarfile
import zstandard

from cs2bot.ingest.demos import archive as archive_module
from cs2bot.ingest.demos.archive import (
    RarTool,
    detect_archive_kind,
    ensure_rar_backend,
    extract_archive,
    filename_map_name,
    filename_map_number,
    find_rar_tools,
    match_dem_files_to_maps,
)
from cs2bot.ingest.demos.errors import ArchiveError, ArchiveToolMissingError

DEMO_MAGIC = b"PBDEMS2\x00"


def demo_bytes(tag: str = "", size: int = 256) -> bytes:
    """Bytes that start like a CS2 demo file."""
    return (DEMO_MAGIC + tag.encode() + b"\x00" * size)[: max(size, len(DEMO_MAGIC) + len(tag))]


# ------------------------------------------------------------------------------------- handmade rar


def _rar_block(head_type: int, flags: int, body: bytes, data: bytes = b"") -> bytes:
    header = struct.pack("<BHH", head_type, flags, 7 + len(body)) + body
    return struct.pack("<H", zlib.crc32(header) & 0xFFFF) + header + data


def make_stored_rar(files: dict[str, bytes]) -> bytes:
    """A valid RAR 2.9 archive with *stored* (uncompressed) members, built by hand.

    No free tool can create RAR archives, but the stored format is simple: marker, archive header,
    one file header + raw data per member, end block. ``rarfile``, ``unar`` and ``bsdtar`` read it.
    """
    out = b"Rar!\x1a\x07\x00" + _rar_block(0x73, 0x0000, b"\x00" * 6)
    for name, data in files.items():
        raw = name.encode("utf-8")
        body = struct.pack(
            "<IIBIIBBHI",
            len(data),
            len(data),
            3,
            zlib.crc32(data) & 0xFFFFFFFF,
            0x5A6F0000,
            20,
            0x30,
            len(raw),
            0x20,
        )
        out += _rar_block(0x74, 0x8000, body + raw, data)
    return out + _rar_block(0x7B, 0x4000, b"")


# ----------------------------------------------------------------------------------------- kinds


def test_detect_archive_kind_by_magic_bytes(tmp_path: Path) -> None:
    samples: dict[str, bytes] = {
        "rar": make_stored_rar({"a.dem": demo_bytes()}),
        "zst": zstandard.ZstdCompressor().compress(demo_bytes()),
        "gz": gzip.compress(demo_bytes()),
        "dem": demo_bytes(),
    }
    zip_path = tmp_path / "download"  # a browser download without an extension
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("a.dem", demo_bytes())
    assert detect_archive_kind(zip_path) == "zip"
    for kind, data in samples.items():
        path = tmp_path / f"noext-{kind}"
        path.write_bytes(data)
        assert detect_archive_kind(path) == kind
    # a wrong extension does not fool the detection
    wrong = tmp_path / "really-a-zip.rar"
    wrong.write_bytes(zip_path.read_bytes())
    assert detect_archive_kind(wrong) == "zip"


def test_detect_archive_kind_falls_back_to_the_extension(tmp_path: Path) -> None:
    for suffix, kind in ((".rar", "rar"), (".zip", "zip"), (".zst", "zst"), (".gz", "gz"), (".dem", "dem")):
        path = tmp_path / f"file{suffix}"
        path.write_bytes(b"no magic here")
        assert detect_archive_kind(path) == kind
    bz = tmp_path / "match.dem.bz2"
    bz.write_bytes(bz2.compress(demo_bytes()))
    assert detect_archive_kind(bz) == "bz2"
    other = tmp_path / "notes.txt"
    other.write_bytes(b"hello")
    assert detect_archive_kind(other) == "unknown"
    with pytest.raises(ArchiveError):
        detect_archive_kind(tmp_path / "missing.rar")


# ------------------------------------------------------------------------------------------- zip


def test_extract_zip_flattens_and_keeps_only_demos(tmp_path: Path) -> None:
    archive = tmp_path / "match.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("alpha-vs-bravo-m1-mirage.dem", demo_bytes("m1"))
        zf.writestr("nested/dir/alpha-vs-bravo-m2-inferno.DEM", demo_bytes("m2"))
        zf.writestr("readme.txt", "not a demo")
        zf.writestr("empty-dir/", "")
    dest = tmp_path / "out"
    paths = extract_archive(archive, dest)

    assert [p.name for p in paths] == ["alpha-vs-bravo-m1-mirage.dem", "alpha-vs-bravo-m2-inferno.DEM"]
    assert all(p.parent == dest for p in paths)
    assert paths[0].read_bytes() == demo_bytes("m1")
    assert sorted(p.name for p in dest.iterdir()) == sorted(p.name for p in paths)  # no .part / txt leftovers
    assert archive.exists()  # deleting the archive is the pipeline's decision


def test_extract_zip_cannot_escape_the_destination(tmp_path: Path) -> None:
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../../escape.dem", demo_bytes("x"))
        zf.writestr("/abs/olute.dem", demo_bytes("y"))
        zf.writestr("C:\\Windows\\system.dem", demo_bytes("z"))
    dest = tmp_path / "deep" / "out"
    paths = extract_archive(archive, dest)

    assert len(paths) == 3
    assert all(p.parent == dest for p in paths)
    assert sorted(p.name for p in paths) == ["escape.dem", "olute.dem", "system.dem"]
    assert not (tmp_path / "escape.dem").exists() and not (tmp_path / "deep" / "escape.dem").exists()


def test_extract_zip_duplicate_basenames_get_unique_names(tmp_path: Path) -> None:
    archive = tmp_path / "dups.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a/demo.dem", demo_bytes("first"))
        zf.writestr("b/demo.dem", demo_bytes("second"))
    paths = extract_archive(archive, tmp_path / "out")
    assert sorted(p.name for p in paths) == ["demo-2.dem", "demo.dem"]
    assert {p.read_bytes() for p in paths} == {demo_bytes("first"), demo_bytes("second")}


def test_zip_without_demo_or_corrupt_zip_is_an_error(tmp_path: Path) -> None:
    empty = tmp_path / "nodemo.zip"
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("readme.txt", "x")
    with pytest.raises(ArchiveError, match=r"no \.dem file"):
        extract_archive(empty, tmp_path / "out")
    corrupt = tmp_path / "corrupt.zip"
    corrupt.write_bytes(b"PK\x03\x04" + b"garbage" * 10)
    with pytest.raises(ArchiveError, match="cannot extract zip"):
        extract_archive(corrupt, tmp_path / "out")


def test_member_size_limit_stops_decompression_bombs(tmp_path: Path) -> None:
    archive = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("small.dem", demo_bytes("ok", 100))
        zf.writestr("huge.dem", b"\x00" * 200_000)
    dest = tmp_path / "out"
    with pytest.raises(ArchiveError, match="larger than"):
        extract_archive(archive, dest, max_member_bytes=50_000)
    assert list(dest.iterdir()) == []  # nothing half-written is left behind

    stream = tmp_path / "bomb.dem.zst"
    stream.write_bytes(zstandard.ZstdCompressor().compress(b"\x00" * 200_000))
    with pytest.raises(ArchiveError, match="larger than"):
        extract_archive(stream, dest, max_member_bytes=50_000)
    assert list(dest.iterdir()) == []


# --------------------------------------------------------------------------- single-file streams


@pytest.mark.parametrize("kind", ["zst", "gz", "bz2"])
def test_extract_single_compressed_demo(tmp_path: Path, kind: str) -> None:
    data = demo_bytes("stream", 4096)
    packed = {
        "zst": zstandard.ZstdCompressor().compress(data),
        "gz": gzip.compress(data),
        "bz2": bz2.compress(data),
    }[kind]
    source = tmp_path / f"003712345678901234567_0123456789.dem.{kind}"
    source.write_bytes(packed)
    (path,) = extract_archive(source, tmp_path / "out")
    assert path.name == "003712345678901234567_0123456789.dem"
    assert path.read_bytes() == data


def test_zst_stream_without_dem_in_its_name_gets_the_extension(tmp_path: Path) -> None:
    source = tmp_path / "download.zst"
    source.write_bytes(zstandard.ZstdCompressor().compress(demo_bytes()))
    (path,) = extract_archive(source, tmp_path / "out")
    assert path.name == "download.dem"


def test_corrupt_stream_is_an_archive_error(tmp_path: Path) -> None:
    source = tmp_path / "broken.dem.gz"
    source.write_bytes(gzip.compress(demo_bytes("x", 5000))[:40])
    with pytest.raises(ArchiveError, match="cannot decompress"):
        extract_archive(source, tmp_path / "out")
    assert not any((tmp_path / "out").glob("*"))


# ----------------------------------------------------------------------------------- plain / misc


def test_plain_demo_is_passed_through(tmp_path: Path) -> None:
    source = tmp_path / "alpha-vs-bravo-mirage.dem"
    source.write_bytes(demo_bytes("plain"))
    assert extract_archive(source, tmp_path / "out") == [source]
    assert not (tmp_path / "out").exists()  # nothing copied, nothing created


def test_unknown_and_missing_files(tmp_path: Path) -> None:
    html = tmp_path / "challenge.html"
    html.write_text("<html>Just a moment...</html>")
    with pytest.raises(ArchiveError, match="unknown archive type"):
        extract_archive(html, tmp_path / "out")
    with pytest.raises(ArchiveError, match="does not exist"):
        extract_archive(tmp_path / "nope.rar", tmp_path / "out")


# ------------------------------------------------------------------------------------------ rar


def test_extract_rar_with_a_real_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end with a real RAR tool, if this machine has one of those the hand-made archive was
    checked with (unar 1.10 and bsdtar 3.7; real RAR5 / solid archives were tried by hand as well)."""
    tools = [tool for tool in find_rar_tools() if tool.kind in ("unar", "bsdtar")]
    if not tools:
        pytest.skip("neither unar nor bsdtar is installed")
    monkeypatch.setattr(archive_module, "find_rar_tools", lambda: tools)
    archive = tmp_path / "alpha-vs-bravo.rar"
    archive.write_bytes(
        make_stored_rar(
            {
                "alpha-vs-bravo-m1-mirage.dem": demo_bytes("m1", 5000),
                "sub/alpha-vs-bravo-m2-nuke.dem": demo_bytes("m2", 7000),
                "info.txt": b"hello",
            }
        )
    )
    dest = tmp_path / "out"
    paths = extract_archive(archive, dest)
    assert [p.name for p in paths] == ["alpha-vs-bravo-m1-mirage.dem", "alpha-vs-bravo-m2-nuke.dem"]
    assert paths[0].read_bytes() == demo_bytes("m1", 5000)
    assert paths[1].read_bytes() == demo_bytes("m2", 7000)
    assert sorted(p.name for p in dest.iterdir()) == [p.name for p in paths]  # temp dir and txt are gone


class FakeRarInfo:
    def __init__(self, filename: str, file_size: int = 1000, directory: bool = False) -> None:
        self.filename = filename
        self.file_size = file_size
        self._directory = directory

    def is_dir(self) -> bool:
        return self._directory


def fake_rarfile(monkeypatch: pytest.MonkeyPatch, infos: list[FakeRarInfo] | Exception) -> None:
    """Replace ``rarfile.RarFile`` (member listing) inside the archive module."""

    class FakeRarFile:
        def __init__(self, _path: Any) -> None:
            if isinstance(infos, Exception):
                raise infos

        def __enter__(self) -> FakeRarFile:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def infolist(self) -> list[FakeRarInfo]:
            assert not isinstance(infos, Exception)
            return infos

    monkeypatch.setattr(archive_module.rarfile, "RarFile", FakeRarFile)


class FakeTools:
    """Replace tool discovery and ``subprocess.run``: each tool 'extracts' the files it is told to."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tools: list[RarTool],
        behaviour: dict[str, dict[str, bytes] | int | Exception],
    ) -> None:
        self.commands: list[list[str]] = []
        self.behaviour = behaviour
        monkeypatch.setattr(archive_module, "find_rar_tools", lambda: list(tools))
        monkeypatch.setattr(archive_module.subprocess, "run", self.run)

    @staticmethod
    def _dest(command: list[str]) -> Path:
        exe = command[0]
        if exe.endswith("unrar"):
            return Path(command[-1])
        if exe.endswith("7z"):
            return Path(next(arg[2:] for arg in command if arg.startswith("-o")))
        if exe.endswith("unar"):
            return Path(command[command.index("-o") + 1])
        return Path(command[command.index("-C") + 1])

    def run(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        assert kwargs.get("stdin") is subprocess.DEVNULL  # a password prompt must never hang the job
        outcome = self.behaviour[Path(command[0]).name]
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, int):
            return subprocess.CompletedProcess(command, outcome, "", "unsupported compression method")
        dest = self._dest(command)
        for name, data in outcome.items():
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return subprocess.CompletedProcess(command, 0, "", "")


def rar_file(tmp_path: Path, name: str = "match.rar") -> Path:
    path = tmp_path / name
    path.write_bytes(b"Rar!\x1a\x07\x01\x00" + b"\x00" * 64)  # RAR5 signature; the content is never read
    return path


def test_extract_rar_with_mocked_rarfile_and_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_rarfile(
        monkeypatch,
        [
            FakeRarInfo("demos/", directory=True),
            FakeRarInfo("demos/alpha-vs-bravo-m1-mirage.dem"),
            FakeRarInfo("demos/alpha-vs-bravo-m2-inferno.dem"),
            FakeRarInfo("demos/readme.nfo"),
        ],
    )
    tools = FakeTools(
        monkeypatch,
        [RarTool("7z", "/opt/7z")],
        {
            "7z": {
                "demos/alpha-vs-bravo-m1-mirage.dem": demo_bytes("m1"),
                "demos/alpha-vs-bravo-m2-inferno.dem": demo_bytes("m2"),
                "demos/readme.nfo": b"x",
            }
        },
    )
    archive = rar_file(tmp_path)
    dest = tmp_path / "out"
    paths = extract_archive(archive, dest)

    assert [p.name for p in paths] == ["alpha-vs-bravo-m1-mirage.dem", "alpha-vs-bravo-m2-inferno.dem"]
    assert paths[1].read_bytes() == demo_bytes("m2")
    assert sorted(p.name for p in dest.iterdir()) == [p.name for p in paths]
    (command,) = tools.commands
    assert command[:5] == ["/opt/7z", "x", "-y", "-bd", "-p"]
    assert str(archive.resolve()) in command
    # only the demos are requested from the tool
    assert command[-2:] == ["demos/alpha-vs-bravo-m1-mirage.dem", "demos/alpha-vs-bravo-m2-inferno.dem"]


def test_rar_tool_command_lines() -> None:
    archive, dest = Path("/data/a.rar"), Path("/data/out")
    a, d = str(archive), str(dest)
    members = ["m1.dem", "m2.dem"]
    assert RarTool("unrar", "unrar").command(archive, dest, members) == [
        "unrar", "x", "-inul", "-y", "-o+", "-p-", a, "m1.dem", "m2.dem", f"{d}{os.sep}",
    ]  # fmt: skip
    assert RarTool("unar", "unar").command(archive, dest, members) == [
        "unar", "-q", "-f", "-D", "-o", d, a, "m1.dem", "m2.dem",
    ]  # fmt: skip
    assert RarTool("bsdtar", "tar.exe").command(archive, dest) == ["tar.exe", "-x", "-f", a, "-C", d]
    assert RarTool("7z", "7z").command(archive, dest) == ["7z", "x", "-y", "-bd", "-p", f"-o{d}", a]


def test_rar_falls_back_to_the_next_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """unar cannot read solid RAR5, bsdtar cannot read solid RAR3: whichever works is used."""
    fake_rarfile(monkeypatch, [FakeRarInfo("m1-mirage.dem")])
    tools = FakeTools(
        monkeypatch,
        [RarTool("unrar", "/x/unrar"), RarTool("unar", "/x/unar"), RarTool("bsdtar", "/x/bsdtar")],
        {"unrar": OSError("exec format error"), "unar": 1, "bsdtar": {"m1-mirage.dem": demo_bytes("ok")}},
    )
    dest = tmp_path / "out"
    (path,) = extract_archive(rar_file(tmp_path), dest)
    assert path.read_bytes() == demo_bytes("ok")
    assert [Path(c[0]).name for c in tools.commands] == ["unrar", "unar", "bsdtar"]
    assert [p.name for p in dest.iterdir()] == ["m1-mirage.dem"]


def test_rar_tool_that_reports_success_but_extracts_too_little_is_not_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_rarfile(monkeypatch, [FakeRarInfo("m1.dem"), FakeRarInfo("m2.dem")])
    FakeTools(monkeypatch, [RarTool("unar", "/x/unar")], {"unar": {"m1.dem": demo_bytes()}})
    dest = tmp_path / "out"
    with pytest.raises(ArchiveError, match="1 of 2 demo files"):
        extract_archive(rar_file(tmp_path), dest)
    assert list(dest.iterdir()) == []


def test_rar_error_lists_every_tool_that_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_rarfile(monkeypatch, [FakeRarInfo("m1.dem")])
    FakeTools(
        monkeypatch,
        [RarTool("unar", "/x/unar"), RarTool("bsdtar", "/x/bsdtar")],
        {"unar": 2, "bsdtar": subprocess.TimeoutExpired("bsdtar", 5)},
    )
    with pytest.raises(ArchiveError) as info:
        extract_archive(rar_file(tmp_path), tmp_path / "out")
    message = str(info.value)
    assert "unar: exit code 2" in message and "unsupported compression method" in message
    assert "bsdtar: TimeoutExpired" in message
    assert not isinstance(info.value, ArchiveToolMissingError)


def test_rar_without_any_tool_raises_a_helpful_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_rarfile(monkeypatch, [FakeRarInfo("m1.dem")])
    monkeypatch.setattr(archive_module, "find_rar_tools", list)
    with pytest.raises(ArchiveToolMissingError) as info:
        extract_archive(rar_file(tmp_path), tmp_path / "out")
    assert "7-Zip" in str(info.value) and "unar" in str(info.value)
    assert isinstance(info.value, ArchiveError)
    with pytest.raises(ArchiveToolMissingError):
        ensure_rar_backend()


def test_rar_listing_without_demo_or_with_an_oversized_demo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(archive_module, "find_rar_tools", lambda: pytest.fail("no tool is needed to say no"))
    fake_rarfile(monkeypatch, [FakeRarInfo("readme.txt")])
    with pytest.raises(ArchiveError, match=r"no \.dem file"):
        extract_archive(rar_file(tmp_path), tmp_path / "out")
    fake_rarfile(monkeypatch, [FakeRarInfo("m1.dem", file_size=10**12)])
    with pytest.raises(ArchiveError, match="larger than"):
        extract_archive(rar_file(tmp_path), tmp_path / "out")


def test_rar_with_unreadable_listing_extracts_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_rarfile(monkeypatch, rarfile.BadRarFile("unknown header"))
    tools = FakeTools(
        monkeypatch,
        [RarTool("bsdtar", "/x/bsdtar")],
        {"bsdtar": {"a/m1.dem": demo_bytes("1"), "notes.txt": b"x"}},
    )
    (path,) = extract_archive(rar_file(tmp_path), tmp_path / "out")
    assert path.name == "m1.dem"
    assert tools.commands[0][-2] == "-C"  # no member names were passed


def test_rar_member_names_that_look_like_options_are_not_passed_to_the_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_rarfile(monkeypatch, [FakeRarInfo("-o/etc/evil.dem")])
    tools = FakeTools(monkeypatch, [RarTool("unar", "/x/unar")], {"unar": {"evil.dem": demo_bytes()}})
    (path,) = extract_archive(rar_file(tmp_path), tmp_path / "out")
    assert path.parent == tmp_path / "out"
    assert not any(arg.startswith("-o/") for arg in tools.commands[0])


def test_rar_symlinks_left_by_a_tool_are_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = tmp_path / "secret.dem"
    secret.write_bytes(b"do not touch")
    fake_rarfile(monkeypatch, [FakeRarInfo("m1.dem")])
    tools = FakeTools(monkeypatch, [RarTool("unar", "/x/unar")], {"unar": {"m1.dem": demo_bytes("real")}})
    original_run = tools.run

    def run_and_plant_symlink(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        result = original_run(command, **kwargs)
        try:
            (FakeTools._dest(command) / "link.dem").symlink_to(secret)
        except OSError:
            pytest.skip("symlinks cannot be created here")
        return result

    monkeypatch.setattr(archive_module.subprocess, "run", run_and_plant_symlink)
    paths = extract_archive(rar_file(tmp_path), tmp_path / "out")
    assert [p.name for p in paths] == ["m1.dem"]
    assert secret.read_bytes() == b"do not touch"


def test_find_rar_tools_prefers_unrar_and_detects_windows_tar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program_files = tmp_path / "Program Files"
    (program_files / "7-Zip").mkdir(parents=True)
    (program_files / "7-Zip" / "7z.exe").write_bytes(b"MZ")
    system_root = tmp_path / "Windows"
    (system_root / "System32").mkdir(parents=True)
    tar_exe = system_root / "System32" / "tar.exe"
    tar_exe.write_bytes(b"MZ")
    for var in ("ProgramW6432", "ProgramFiles(x86)"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ProgramFiles", str(program_files))
    monkeypatch.setenv("SYSTEMROOT", str(system_root))
    on_path = {"unar": "/usr/bin/unar", "tar": "/usr/bin/tar"}
    monkeypatch.setattr(archive_module.shutil, "which", lambda name: on_path.get(name))
    # /usr/bin/tar is GNU tar (cannot read rar); the Windows one is bsdtar
    monkeypatch.setattr(archive_module, "_is_bsdtar", lambda exe: exe == str(tar_exe))
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda self: str(self) in (str(tar_exe), str(program_files / "7-Zip" / "7z.exe"), "/usr/bin/tar"),
    )

    tools = find_rar_tools()
    assert tools == [
        RarTool("7z", str(program_files / "7-Zip" / "7z.exe")),
        RarTool("unar", "/usr/bin/unar"),
        RarTool("bsdtar", str(tar_exe)),
    ]
    assert ensure_rar_backend() == tools


# ---------------------------------------------------------------------------------- map matching


def dem_files(tmp_path: Path, *names: str, sizes: dict[str, int] | None = None) -> list[Path]:
    paths = []
    for name in names:
        path = tmp_path / name
        path.write_bytes(b"\x00" * (sizes or {}).get(name, 10))
        paths.append(path)
    return paths


def test_match_by_header_map_name(tmp_path: Path) -> None:
    files = dem_files(tmp_path, "x1.dem", "x2.dem", "x3.dem")
    headers = {"x1.dem": "de_inferno", "x2.dem": "de_mirage", "x3.dem": "de_nuke"}
    result = match_dem_files_to_maps(
        files, [(1, "mirage"), (2, "nuke"), (3, "inferno")], header_reader=lambda p: headers[p.name]
    )
    assert result == {1: files[1], 2: files[2], 3: files[0]}


def test_match_handles_missing_and_extra_files(tmp_path: Path) -> None:
    files = dem_files(tmp_path, "a-m1-mirage.dem", "a-m3-ancient.dem", "warmup-dust2.dem")
    headers = {"a-m1-mirage.dem": "mirage", "a-m3-ancient.dem": "ancient", "warmup-dust2.dem": "dust2"}
    result = match_dem_files_to_maps(
        files, [(1, "mirage"), (2, "inferno"), (3, "ancient")], header_reader=lambda p: headers[p.name]
    )
    assert result == {1: files[0], 3: files[1]}  # map 2 has no demo, the dust2 file matches nothing
    assert match_dem_files_to_maps([], [(1, "mirage")], header_reader=lambda p: None) == {}
    assert match_dem_files_to_maps(files, [], header_reader=lambda p: "mirage") == {}


def test_match_uses_filename_hints_when_the_header_is_unreadable(tmp_path: Path) -> None:
    files = dem_files(
        tmp_path, "natus-vincere-vs-faze-m1-mirage.dem", "natus-vincere-vs-faze-m2-dust2.dem", "random.dem"
    )

    def broken_reader(path: Path) -> str | None:
        if path.name == "random.dem":
            raise RuntimeError("truncated demo")
        return None

    result = match_dem_files_to_maps(
        files, [(1, "mirage"), (2, "dust2"), (3, "nuke")], header_reader=broken_reader
    )
    assert result == {1: files[0], 2: files[1]}  # random.dem is not assigned on no evidence


def test_match_by_map_number_hint_only(tmp_path: Path) -> None:
    files = dem_files(tmp_path, "match-m2.dem", "match-map1.dem")
    result = match_dem_files_to_maps(files, [(1, "mirage"), (2, "nuke")], header_reader=lambda p: None)
    assert result == {1: files[1], 2: files[0]}


def test_header_wins_over_a_misleading_filename(tmp_path: Path) -> None:
    files = dem_files(tmp_path, "team-mirage-vs-nuke-m1-inferno.dem")
    result = match_dem_files_to_maps(
        files, [(1, "inferno"), (2, "mirage")], header_reader=lambda p: "de_mirage"
    )
    assert result == {2: files[0]}


def test_single_file_single_map_is_trusted_without_evidence(tmp_path: Path) -> None:
    files = dem_files(tmp_path, "0037.dem")
    assert match_dem_files_to_maps(files, [(1, "mirage")], header_reader=lambda p: None) == {1: files[0]}
    # ... but not when the header names another map
    assert match_dem_files_to_maps(files, [(1, "mirage")], header_reader=lambda p: "de_nuke") == {}
    # ... and not when there are several maps to choose from
    assert match_dem_files_to_maps(files, [(1, "mirage"), (2, "nuke")], header_reader=lambda p: None) == {}


def test_demo_split_into_parts_uses_the_largest_file(tmp_path: Path) -> None:
    files = dem_files(
        tmp_path,
        "a-vs-b-m1-mirage-p1.dem",
        "a-vs-b-m1-mirage-p2.dem",
        "a-vs-b-m2-nuke.dem",
        sizes={"a-vs-b-m1-mirage-p1.dem": 100, "a-vs-b-m1-mirage-p2.dem": 900},
    )
    headers = {files[0].name: "mirage", files[1].name: "mirage", files[2].name: "nuke"}
    result = match_dem_files_to_maps(
        files, [(1, "mirage"), (2, "nuke")], header_reader=lambda p: headers[p.name]
    )
    assert result == {1: files[1], 2: files[2]}


def test_same_map_played_twice_is_told_apart_by_the_number_hint(tmp_path: Path) -> None:
    """Rare (show matches, some BO2s), but the header alone cannot decide."""
    files = dem_files(tmp_path, "a-vs-b-m2-mirage.dem", "a-vs-b-m1-mirage.dem")
    result = match_dem_files_to_maps(
        files, [(1, "mirage"), (2, "mirage")], header_reader=lambda p: "de_mirage"
    )
    assert result == {1: files[1], 2: files[0]}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("natus-vincere-vs-faze-m1-mirage.dem", 1),
        ("navi-vs-faze-m3-dust2.dem", 3),
        ("navi_vs_faze_map2_nuke.dem", 2),
        ("navi-vs-faze-map-2.dem", 2),
        ("m2-inferno.dem", 2),
        ("navi-vs-faze-mirage.dem", None),
        ("team-m80-vs-faze-nuke.dem", None),  # "m80" is a team, not map 80
        ("vitality-vs-mouz-m1.dem", 1),
        ("3dmax-vs-fnatic.dem", None),
    ],
)
def test_filename_map_number(name: str, expected: int | None) -> None:
    assert filename_map_number(name) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("natus-vincere-vs-faze-m1-mirage.dem", "mirage"),
        ("navi-vs-faze-m2-dust2.dem", "dust2"),
        ("navi-vs-faze-m2-dust-2.dem", "dust2"),
        ("navi-vs-faze-de_inferno.dem", "inferno"),
        ("NAVI-vs-FaZe-M3-ANCIENT.dem", "ancient"),
        ("navi-vs-faze-m1.dem", None),
        ("nuke-esports-vs-faze-m1-mirage.dem", "mirage"),  # the last mention wins
        ("navi-vs-faze-m1-overpass.dem", None),  # not one of this match's maps
    ],
)
def test_filename_map_name(name: str, expected: str | None) -> None:
    assert filename_map_name(name, ["mirage", "dust2", "inferno", "ancient", "nuke"]) == expected
