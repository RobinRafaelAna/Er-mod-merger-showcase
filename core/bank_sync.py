"""
bank_sync.py — make the merged package's banks agree with its own loose WEMs.

A voice line lives in two places: the loose WEM under sd/enus/wem, and a copy inside every
bank that references it. Dubbing tools write the banks when the audio is generated, so a
take generated LATER is only in the loose file and the banks still hold the original. That
is how Margit's second take stayed in English inside s10_00_0010.bnk and s11_00_0040.bnk
while its loose WEM was already dubbed (2026-09-23) - and merging cannot see it, because
each mod's files are internally inconsistent before the merge even starts.

The rule is deliberately narrow, so nothing a mod meant to do is undone:

    replace a bank's embedded sound ONLY IF
      the bank still holds exactly vanilla's bytes for it, AND
      the package ships a loose WEM with that id whose bytes differ from vanilla

Whole file goes in, matching voice_packer: writing only a prefetch-sized fragment is what
the format implies, but in game it silenced the line.
"""
from __future__ import annotations
import struct
from pathlib import Path

_ALIGN = 16


def _sections(data: bytes):
    off = 0
    while off + 8 <= len(data):
        tag = data[off:off + 4]
        size = struct.unpack_from("<I", data, off + 4)[0]
        yield tag, off + 8, size
        off += 8 + size


def _sounds(data: bytes) -> dict[int, bytes]:
    didx = datao = None
    for tag, start, size in _sections(data):
        if tag == b"DIDX":
            didx = (start, size)
        elif tag == b"DATA":
            datao = start
    if didx is None or datao is None:
        return {}
    out = {}
    for i in range(didx[1] // 12):
        sid, off, size = struct.unpack_from("<III", data, didx[0] + i * 12)
        out[sid] = data[datao + off:datao + off + size]
    return out


def _rebuild(data: bytes, new: dict[int, bytes]) -> bytes:
    didx = bytearray()
    blob = bytearray()
    for sid, payload in new.items():
        didx += struct.pack("<III", sid, len(blob), len(payload))
        blob += payload
        blob += b"\0" * ((-len(payload)) % _ALIGN)
    out = bytearray()
    for tag, start, size in _sections(data):
        body = (bytes(didx) if tag == b"DIDX" else
                bytes(blob) if tag == b"DATA" else data[start:start + size])
        out += tag + struct.pack("<I", len(body)) + body
    return bytes(out)


def sync(package: Path, vanilla: Path, log=lambda m: None) -> tuple[int, int]:
    """Returns (banks changed, sounds replaced)."""
    sd = package / "sd"
    van_sd = vanilla / "sd"
    if not sd.is_dir() or not van_sd.is_dir():
        return 0, 0
    loose = {int(p.stem): p for p in sd.rglob("wem/**/*.wem") if p.stem.isdigit()}
    if not loose:
        return 0, 0
    banks = sounds = 0
    for bnk in sorted(sd.rglob("*.bnk")):
        van = van_sd / bnk.relative_to(sd)
        if not van.exists():
            continue
        try:
            data = bnk.read_bytes()
            cur, vsnd = _sounds(data), _sounds(van.read_bytes())
        except Exception:
            continue
        stale = {}
        for sid, blob in cur.items():
            src = loose.get(sid)
            if src is None or sid not in vsnd or blob != vsnd[sid]:
                continue                      # not ours, or the bank already differs
            payload = src.read_bytes()
            if payload != vsnd[sid]:
                stale[sid] = payload
        if not stale:
            continue
        try:
            bnk.write_bytes(_rebuild(data, {sid: stale.get(sid, b) for sid, b in cur.items()}))
        except Exception as exc:
            log(f"Banks: could not rewrite {bnk.name} ({exc})")
            continue
        banks += 1
        sounds += len(stale)
        log(f"Banks: {bnk.relative_to(package).as_posix()} — {len(stale)} sound(s) taken "
            f"from the package's own loose WEMs")
    return banks, sounds
