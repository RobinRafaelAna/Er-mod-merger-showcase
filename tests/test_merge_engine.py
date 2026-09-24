"""MergeEngine behaviour on small synthetic mods (no game files needed)."""
from pathlib import Path

import pytest

from core.conflict import ConflictType, Severity
from core.merge_engine import MergeEngine
from core.mod_manager import ModManager


def make_mod(root: Path, name: str, files: dict[str, bytes]) -> Path:
    mod = root / name
    for rel, data in files.items():
        p = mod / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return mod


def manager_for(tmp_path: Path, *mods: Path) -> ModManager:
    m = ModManager()
    for mod in mods:  # first added = highest priority
        m.add_mod(mod)
    m.output_path = tmp_path / "out"
    return m


def test_highest_priority_file_wins_and_conflict_is_reported(tmp_path):
    a = make_mod(tmp_path, "A", {"parts/x.dcx": b"from A", "only_a.txt": b"a"})
    b = make_mod(tmp_path, "B", {"parts/x.dcx": b"from B", "only_b.txt": b"b"})
    conflicts = MergeEngine().run(manager_for(tmp_path, a, b))

    out = tmp_path / "out"
    assert (out / "parts/x.dcx").read_bytes() == b"from A"
    assert (out / "only_a.txt").exists() and (out / "only_b.txt").exists()
    replaced = [c for c in conflicts if c.type == ConflictType.FILE_REPLACE]
    assert [c.file for c in replaced] == ["parts/x.dcx"]
    assert replaced[0].resolved_by == "A"


def test_refuses_a_mod_that_is_the_output_folder(tmp_path):
    a = make_mod(tmp_path, "A", {"f.txt": b"1"})
    m = manager_for(tmp_path, a)
    m.output_path = a
    with pytest.raises(ValueError, match="output folder"):
        MergeEngine().run(m)


def test_build_leftovers_are_not_merged(tmp_path):
    a = make_mod(tmp_path, "A", {"sd/x.bnk": b"real", "_tyo/sd/x.bnk": b"backup"})
    MergeEngine().run(manager_for(tmp_path, a))
    assert (tmp_path / "out/sd/x.bnk").exists()
    assert not (tmp_path / "out/_tyo").exists()


def test_verify_output_reports_a_missing_file(tmp_path):
    a = make_mod(tmp_path, "A", {"chr/c0000.anibnd.dcx": b"x" * 100})
    m = manager_for(tmp_path, a)
    m.output_path.mkdir()
    errors = MergeEngine()._verify_output(m.mods, m.output_path)
    assert len(errors) == 1 and errors[0].severity == Severity.ERROR


def _dcx_header(version: bytes, fmt: bytes) -> bytes:
    head = bytearray(0x4C)
    head[0:4] = b"DCX\0"
    head[4:8] = version
    head[0x28:0x2C] = fmt
    return bytes(head) + b"payload"


def test_krak_header_version_is_corrected_and_dflt_left_alone(tmp_path):
    bad_krak = _dcx_header(b"\x00\x00\x10\x00", b"KRAK")
    dflt = _dcx_header(b"\x00\x00\x10\x00", b"DFLT")
    a = make_mod(tmp_path, "A", {"event/m.emevd.dcx": bad_krak, "script/s.luabnd.dcx": dflt})
    MergeEngine().run(manager_for(tmp_path, a))

    fixed = (tmp_path / "out/event/m.emevd.dcx").read_bytes()
    assert fixed[4:8] == b"\x00\x01\x10\x00"
    assert fixed[:4] + fixed[8:] == bad_krak[:4] + bad_krak[8:]      # nothing else changed
    assert (tmp_path / "out/script/s.luabnd.dcx").read_bytes() == dflt
    assert (a / "event/m.emevd.dcx").read_bytes() == bad_krak       # source mod untouched
