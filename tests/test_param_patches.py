"""soulstruct param fixes: UTF-16 row names and duplicate row IDs survive a round trip."""
from soulstruct.base.params.param import ParamFlags1, ParamFlags2, TypedParam
from soulstruct.eldenring.params.paramdef import WORLD_MAP_LEGACY_CONV_PARAM_ST as ROW

import soulstruct_patches as sp

Param = TypedParam(ROW)


def row(name: str, fill: int) -> ROW:
    r = ROW.from_bytes(bytes([fill]) * 48)
    r.Name = name
    return r


def make_param(rows: dict) -> Param:
    # Header values of Elden Ring's real WorldMapLegacyConvParam (UTF-16 row names).
    return Param(param_type="WORLD_MAP_LEGACY_CONV_PARAM_ST", flags1=ParamFlags1(0x85),
                 flags2=ParamFlags2(0x07), unknown=2, rows=rows)


def test_utf16_row_names_round_trip_whole():
    p = make_param({5: row("Roundtable Hold -> Unnamed Tile", 0), 10: row("Tile", 1)})
    data = p.to_bytes()
    # Each name keeps its last character and a full 2-byte terminator.
    assert "Tile".encode("utf-16-le") + b"\0\0" in data
    q = Param.from_bytes(data)
    assert {k: r.Name for k, r in q.rows.items()} == {
        5: "Roundtable Hold -> Unnamed Tile", 10: "Tile"}


def test_duplicate_row_ids_are_kept_after_their_first_copy():
    p = make_param({5: row("a", 0), 10: row("b", 1)})
    sp._PARAM_DUPLICATE_ROWS[id(p)] = (p, [(10, row("second copy of 10", 2))])
    data = p.to_bytes()

    q = Param.from_bytes(data)
    dups = sp._PARAM_DUPLICATE_ROWS[id(q)][1]
    assert [(i, r.Name) for i, r in dups] == [(10, "second copy of 10")]
    assert q.to_bytes() == data          # read + write gives the same bytes
