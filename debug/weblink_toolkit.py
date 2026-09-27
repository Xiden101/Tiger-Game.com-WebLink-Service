#!/usr/bin/env python3
"""
Tiger Web Link — toolkit.

Single-file replacement for Tiger's dead 1997 high-score service for the
Game.com Web Link cartridge. Serial protocol, cheat engine, cartridge
simulator, and diagnostic probes for the parts that are still unknown.

    python3 weblink_toolkit.py --selftest        # no hardware needed
    python3 weblink_toolkit.py --examples        # every example payload
    python3 weblink_toolkit.py --port COM3 --full-list


WHAT THE HARDWARE ACTUALLY SAID
-------------------------------
A --probe-cmds sweep on a real cartridge produced a very clean split:

    0x05 FULL LIST REQUEST        -> works
    0x07 GAME SCORE REQUEST       -> INVALID COMMAND
    0x09 OVERWRITE GAME SCORE     -> INVALID COMMAND
    0x0B COMMAND ACKNOWLEDGEMENT  -> INVALID COMMAND
    everything else               -> total silence

Silence is not a failure mode -- it is a *direction*. The cartridge only ever
parses commands the PC is allowed to send; the rest it ignores because it only
ever sends them. So:

    PC   -> cart :  01, 03, 05, 07, 09, 0B
    cart -> PC   :  02, 04, 06, 08, 0A, 0C, 0D, 0E, 0F

**SEND_TEXT_MESSAGE (0x0C) and SEND_SCREEN_GRAPHIC (0x0D) are cart->PC.**
That is why every framing and encoding variant got silence: the PC was never
meant to send them. It corroborates two things we already had and misread --
the `from game.com: ` string in the PC app, and the "Write text message."
status line in the cartridge ROM. The *user composes the message on the
handheld*.

Which also explains 0x07 and 0x09. The PC does not pull a single game; the
user picks it on the Game.com and the cartridge pushes GAME_SCORE_SEND (0x08)
across. OVERWRITE (0x09) is the reply to that push, not an unprompted write --
so it is only likely to be accepted in the window right after the cartridge has
handed us a record. `--follow` implements exactly that flow.

    python3 weblink_toolkit.py --port COM3 --follow

The earlier `0C F3` reply to an overwrite was not truncation (a later probe got
a clean `0F F0` instead) and not an ack. Under the direction model it reads as
the cartridge starting to send a text message -- i.e. it had something to say.

STILL OPEN
----------
*   Whether 0x07/0x09 are rejected because of cartridge *state* or because this
    firmware revision simply does not implement them. `--follow` distinguishes
    these: if a pushed record can be patched and accepted, it is state.
*   The 64-byte record's internal layout. A real Indy 500 record is
    `11 52 00 | "INDY500" | 01 | "P31 07:53" | zeros | 1f 24 f5 | zeros`.
    The name and score text are understood; `11 52 00` and `1f 24 f5` are not.
    `decode_record()` now surfaces them as `prefix` and `unknown`.
*   `04 00` instead of `04 FB` on every termination. Best explanation: the
    cartridge runs its checksum routine over the finished 2-byte frame --
    255 - (0x04 + 0xFB) = 0 -- and writes the result over the complement byte.
    A quirk on the termination path only. Handled, cosmetic.
*   The link occasionally drops a byte: one FULL_LIST_SEND arrived 639 bytes
    instead of 640. Because the dropped byte came out of the zero padding the
    checksum still validated -- so the length is now checked explicitly and a
    short read is retried.

CONFIRMED AGAINST REAL HARDWARE
-------------------------------
    bare command :  [CMD][255-CMD]                          2 bytes, no checksum
    data frame   :  [CMD][255-CMD][payload...][CHECKSUM]
    record frame :  [CMD][255-CMD][64-byte record][CHECKSUM]      67 bytes
    full list    :  [06][F9][10 x 64 bytes][CHECKSUM]            643 bytes

LINK_REQUEST/ACKNOWLEDGE, TERMINATION_REQUEST and FULL_LIST_REQUEST/SEND all
work. A real capture: `06 F9 11 52 00 49 4E 44 59 35 30 30 01 50 33 31 ...`

CONFIRMED FROM THE BINARY (instruction level, GameCom.exe @ 0x40ac50)
---------------------------------------------------------------------
GAME_SCORE_REQUEST framing is *not* the problem:

    0040ac90  mov ecx, 7        ; buf(0) = 0x07
    0040acd0  mov byte [ecx], al
    0040acd2  mov ecx, 0xf8     ; buf(1) = 0xF8 = 255-7
    0040acdc  mov byte [edx+1], al
    ...                         ; buf(2..) = Asc(Mid$(name$, i, 1)), i = 1..Len
    0040af8x  cmp ..., 0xe      ; bounds check: max index 14 -> buffer is 15 bytes

15 = 2 header + 12 name + 1 checksum. So `[07][F8][12-byte name][ck]` is
exactly right, and the name is written *verbatim* from the string — the app
applies no padding of its own. Which means if the cartridge is rejecting it,
the problem is the name *content* or the cartridge's state, not the framing.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import IntEnum

try:
    import serial  # type: ignore
    from serial.tools import list_ports  # type: ignore
except ImportError:  # pragma: no cover
    serial = None
    list_ports = None


# ============================================================================
# protocol constants
# ============================================================================

BAUD = 9600
SERIAL_SETTINGS = "9600,N,8,1"
RECORD_LEN = 64          # confirmed: 640 / 64 == 10 records, no remainder
FRAME_HEADER_LEN = 2     # [CMD][255-CMD], confirmed on every frame type
MAX_RETRIES = 3

#: Request-side name field: 11 chars + '|' terminator = 12 bytes. Confirmed by
#: the 15-byte buffer bound in GameListRequest. Separate from whatever the name
#: looks like INSIDE a 64-byte record.
GAME_NAME_LEN = 11
GAME_NAME_TERMINATOR = b"|"

#: Separator between name and score text inside a real record, from a captured
#: Indy 500 record ("...INDY 500" 0x01 "P31 07:53").
RECORD_NAME_SEP = 0x01

#: How long the line must stay quiet before a frame is considered complete.
#: 0.25 was too aggressive and truncated real replies -- see module docstring.
DEFAULT_IDLE_GAP = 0.60
DEFAULT_TIMEOUT = 6.0

#: Game.com LCD: 200x160, 4 grey levels = 2 bits per pixel.
SCREEN_W, SCREEN_H, SCREEN_BPP = 200, 160, 2


class Cmd(IntEnum):
    LINK_REQUEST = 0x01
    LINK_ACKNOWLEDGE = 0x02
    TERMINATION_REQUEST = 0x03
    TERMINATION_ACKNOWLEDGE = 0x04
    FULL_LIST_REQUEST = 0x05
    FULL_LIST_SEND = 0x06
    GAME_SCORE_REQUEST = 0x07
    GAME_SCORE_SEND = 0x08
    OVERWRITE_GAME_SCORE = 0x09
    GAME_NOT_FOUND_ACK = 0x0A
    COMMAND_ACKNOWLEDGEMENT = 0x0B
    SEND_TEXT_MESSAGE = 0x0C
    SEND_SCREEN_GRAPHIC = 0x0D
    UNKNOWN_0E = 0x0E
    INVALID_COMMAND = 0x0F


LABELS = {
    Cmd.LINK_REQUEST: "LINK REQUEST",
    Cmd.LINK_ACKNOWLEDGE: "LINK ACKNOWLEDGE",
    Cmd.TERMINATION_REQUEST: "TERMINATION REQUEST",
    Cmd.TERMINATION_ACKNOWLEDGE: "TERMINATION ACKNOWLEDGE",
    Cmd.FULL_LIST_REQUEST: "FULL LIST REQUEST",
    Cmd.FULL_LIST_SEND: "FULL LIST SEND",
    Cmd.GAME_SCORE_REQUEST: "GAME SCORE REQUEST",
    Cmd.GAME_SCORE_SEND: "GAME SCORE SEND",
    Cmd.OVERWRITE_GAME_SCORE: "OVERWRITE GAME SCORE",
    Cmd.GAME_NOT_FOUND_ACK: "GAME NOT FOUND ACK",
    Cmd.COMMAND_ACKNOWLEDGEMENT: "COMMAND ACKNOWLEDGEMENT",
    Cmd.SEND_TEXT_MESSAGE: "SEND TEXT MESSAGE",
    Cmd.SEND_SCREEN_GRAPHIC: "SEND SCREEN GRAPHIC",
    Cmd.UNKNOWN_0E: "XXXXXXXXXXXXXXXXX",
    Cmd.INVALID_COMMAND: "INVALID COMMAND",
}


#: Which side may *send* each command. Derived from a --probe-cmds sweep on
#: real hardware: the cartridge answers only 0x05 (works), 0x07/0x09/0x0B
#: (INVALID COMMAND) and ignores everything else in total silence. That split is
#: exactly PC->cart vs cart->PC. Commands the cartridge never accepts as input
#: are, by definition, ones it only ever *sends*.
DIRECTION = {
    Cmd.LINK_REQUEST: "pc",
    Cmd.LINK_ACKNOWLEDGE: "cart",
    Cmd.TERMINATION_REQUEST: "pc",
    Cmd.TERMINATION_ACKNOWLEDGE: "cart",
    Cmd.FULL_LIST_REQUEST: "pc",
    Cmd.FULL_LIST_SEND: "cart",
    Cmd.GAME_SCORE_REQUEST: "pc",
    Cmd.GAME_SCORE_SEND: "cart",
    Cmd.OVERWRITE_GAME_SCORE: "pc",
    Cmd.GAME_NOT_FOUND_ACK: "cart",
    Cmd.COMMAND_ACKNOWLEDGEMENT: "pc",
    Cmd.SEND_TEXT_MESSAGE: "cart",     # <- the cartridge sends these TO us
    Cmd.SEND_SCREEN_GRAPHIC: "cart",   # <- likewise
    Cmd.UNKNOWN_0E: "cart",
    Cmd.INVALID_COMMAND: "cart",
}

#: Frames real hardware emits that break the [CMD][255-CMD] rule.
#: `04 00` instead of `04 FB` on every single termination, without exception.
#: Best explanation: the cartridge runs its checksum routine over the finished
#: two-byte frame -- 255 - (0x04 + 0xFB) = 255 - 255 = 0 -- and writes the
#: result over the complement byte. A firmware quirk on the termination path
#: only; the link does close correctly, so it is cosmetic.
QUIRK_FRAMES = {
    b"\x04\x00": Cmd.TERMINATION_ACKNOWLEDGE,
}


class ChecksumError(ValueError):
    def __init__(self, sent: int, calculated: int):
        self.sent = sent
        self.calculated = calculated
        super().__init__(f"CheckSum Sent: {sent}  CheckSum Calc'd: {calculated}")


# ============================================================================
# codec
# ============================================================================

CHECKSUM_INCLUDE_CMD = True


def _checksum(data: bytes) -> int:
    return (255 - (sum(data) & 0xFF)) & 0xFF


def checksum(cmd: int, rest: bytes, include_cmd: bool | None = None) -> int:
    """rest = complement byte + payload (everything between cmd and the ck slot)."""
    inc = CHECKSUM_INCLUDE_CMD if include_cmd is None else include_cmd
    return _checksum((bytes([cmd]) + rest) if inc else rest)


def build(cmd: Cmd | int, payload: bytes = b"", include_cmd: bool | None = None) -> bytes:
    cmd = int(cmd)
    complement = (255 - cmd) & 0xFF
    if not payload:
        return bytes([cmd, complement])
    rest = bytes([complement]) + payload
    return bytes([cmd]) + rest + bytes([checksum(cmd, rest, include_cmd)])


@dataclass(frozen=True)
class Frame:
    cmd: int
    payload: bytes

    @property
    def label(self) -> str:
        try:
            return LABELS[Cmd(self.cmd)]
        except ValueError:
            return f"UNKNOWN(0x{self.cmd:02X})"

    @property
    def text(self) -> str:
        """Payload as printable text -- for SEND TEXT MESSAGE frames."""
        return "".join(chr(b) if 32 <= b < 127 else "." for b in self.payload)

    def __str__(self) -> str:
        if not self.payload:
            return self.label
        if self.cmd == Cmd.SEND_TEXT_MESSAGE:
            return f"{self.label}  from game.com: {self.text!r}"
        return f"{self.label}  [{len(self.payload)} bytes] {self.payload.hex(' ')}"


def parse(raw: bytes, include_cmd: bool | None = None) -> Frame:
    if len(raw) < 3:
        if len(raw) == 2 and raw[1] == (255 - raw[0]) & 0xFF:
            return Frame(cmd=raw[0], payload=b"")
        raise ValueError(f"frame too short: {raw.hex(' ')}")
    cmd, complement = raw[0], raw[1]
    if complement != (255 - cmd) & 0xFF:
        raise ValueError(
            f"byte 1 (0x{complement:02x}) is not 255-cmd (expected "
            f"0x{(255 - cmd) & 0xff:02x}) -- not a valid frame header"
        )
    sent = raw[-1]
    calc = checksum(cmd, raw[1:-1], include_cmd)
    if sent != calc:
        raise ChecksumError(sent, calc)
    return Frame(cmd=cmd, payload=raw[2:-1])


def detect_checksum_variant(raw: bytes) -> bool | None:
    for inc in (True, False):
        try:
            parse(raw, include_cmd=inc)
            return inc
        except (ValueError, ChecksumError):
            continue
    return None


def split_frames(buf: bytes) -> tuple[list[Frame], bytes]:
    """Carve as many complete frames as possible out of a byte stream.

    A reply can legitimately contain more than one frame, and a truncated read
    can leave a partial one. Returns (frames, leftover_bytes). Never raises --
    this is the tolerant path used by diagnostics.
    """
    frames: list[Frame] = []
    i = 0
    while i + 2 <= len(buf):
        quirk = QUIRK_FRAMES.get(bytes(buf[i:i + 2]))
        if quirk is not None:
            frames.append(Frame(cmd=int(quirk), payload=b""))
            i += 2
            continue
        cmd, comp = buf[i], buf[i + 1]
        if comp != (255 - cmd) & 0xFF:
            break                       # not a frame header; stop cleanly
        # Try the longest valid data frame starting here, else a bare frame.
        best = None
        for end in range(len(buf), i + 2, -1):
            try:
                f = parse(buf[i:end])
            except (ValueError, ChecksumError):
                continue
            best = (f, end)
            break
        if best:
            frames.append(best[0])
            i = best[1]
        else:
            frames.append(Frame(cmd=cmd, payload=b""))
            i += 2
    return frames, buf[i:]


# ============================================================================
# name / record helpers
# ============================================================================

def encode_game_name(name: str, pad: str = "_", terminator: bool = True,
                     upper: bool = False) -> bytes:
    """Request-side search key: 11 chars + '|' = 12 bytes.

    NOTE: no uppercasing by default. The one real name constant recoverable
    from the original software is `Mort.Kombat|` -- mixed case. Forcing upper
    case guarantees a mismatch on exactly the game we have ground truth for.
    """
    s = name.upper() if upper else name
    s = s.replace(" ", pad)[:GAME_NAME_LEN].ljust(GAME_NAME_LEN, pad)
    out = s.encode("ascii", "replace")
    return out + GAME_NAME_TERMINATOR if terminator else out


def name_span(record: bytes) -> tuple[int, int] | None:
    """(start, end) of the ASCII name inside a real record, or None.

    Anchors on the 0x01 separator seen in a real capture, walking backwards
    over printable characters. Used to derive the cheat guard range instead of
    assuming the name sits at offsets 0..11 -- a real Indy 500 record has
    several bytes before the name text, so that assumption was wrong.
    """
    sep = record.find(bytes([RECORD_NAME_SEP]))
    if sep <= 0:
        return None
    start = sep
    while start > 0 and 32 <= record[start - 1] < 127:
        start -= 1
    return (start, sep) if start < sep else None


def decode_record(record: bytes) -> dict:
    """Best-effort decode of one 64-byte record (name 0x01 score-text)."""
    span = name_span(record)
    if span is None:
        return {"game": None, "score_raw": None, "score": None,
                "name_span": None, "raw_hex": record.hex(" ")}
    start, sep = span
    name = record[start:sep].decode("ascii", "replace").strip()
    end = sep + 1
    while end < len(record) and 32 <= record[end] < 127:
        end += 1
    score_text = record[sep + 1:end].decode("ascii", "replace").strip()
    # Everything outside the name and the score text is unexplained. In a real
    # Indy 500 record that is `11 52 00` at the front and `1f 24 f5` further in
    # -- prime candidates for whatever the game actually reads back.
    unknown = {i: record[i] for i in range(len(record))
               if record[i] and not (start <= i < end)}
    fields = None
    dec = GAME_DECODERS.get(name.upper().replace(" ", ""))
    if dec:
        try:
            fields = dec(record)
        except Exception:
            fields = None
    return {
        "game": name,
        "fields": fields,
        "name_bytes": record[start:sep],
        "name_span": (start, sep),
        "score_raw": score_text,
        "score": int(score_text) if score_text.isdigit() else None,
        "prefix": record[:start],
        "unknown": unknown,
        "raw_hex": record.hex(" "),
    }


def decode_full_list(payload: bytes) -> list[dict]:
    out = []
    for i in range(0, len(payload) - RECORD_LEN + 1, RECORD_LEN):
        d = decode_record(payload[i:i + RECORD_LEN])
        d["index"] = i // RECORD_LEN
        if d["game"]:
            out.append(d)
    return out


def find_diff(before: bytes, after: bytes) -> list[tuple[int, int, int]]:
    n = max(len(before), len(after))
    a, b = before.ljust(n, b"\x00"), after.ljust(n, b"\x00")
    return [(i, a[i], b[i]) for i in range(n) if a[i] != b[i]]


# ============================================================================
# frame constructors
# ============================================================================

def link_request() -> bytes:
    return build(Cmd.LINK_REQUEST)


def termination_request() -> bytes:
    return build(Cmd.TERMINATION_REQUEST)


def full_list_request() -> bytes:
    return build(Cmd.FULL_LIST_REQUEST)


def command_acknowledgement() -> bytes:
    return build(Cmd.COMMAND_ACKNOWLEDGEMENT)


def game_score_request(name: str, **kw) -> bytes:
    return build(Cmd.GAME_SCORE_REQUEST, encode_game_name(name, **kw))


def game_score_request_raw(key: bytes) -> bytes:
    """GAME_SCORE_REQUEST with the name bytes supplied verbatim.

    Use this with bytes lifted straight out of a record the cartridge itself
    sent -- it removes every encoding assumption at once.
    """
    if len(key) > 13:
        raise ValueError(f"key {len(key)} bytes; the app's buffer holds 12 max")
    return build(Cmd.GAME_SCORE_REQUEST, key)


def overwrite_game_score(record: bytes) -> bytes:
    if len(record) != RECORD_LEN:
        raise ValueError(f"record must be exactly {RECORD_LEN} bytes, got {len(record)}")
    return build(Cmd.OVERWRITE_GAME_SCORE, record)


def send_text_message(text: str) -> bytes:
    return build(Cmd.SEND_TEXT_MESSAGE, text.encode("ascii", "replace"))


def pack_screen(pixels: bytes, w: int = SCREEN_W, h: int = SCREEN_H,
                bpp: int = SCREEN_BPP) -> bytes:
    """Pack one byte per pixel (grey level 0..3) into `bpp` bits per pixel.

    *** ENTIRELY SPECULATIVE. *** Nothing in the cartridge ROM strings
    corroborates SEND_SCREEN_GRAPHIC the way "Write text message." corroborates
    0x0C. The Game.com LCD is 200x160 with 4 grey levels, so 2bpp MSB-first is
    the obvious guess and that is all it is. A full screen is 8000 bytes, which
    is ~8.3 seconds at 9600 baud -- expect to need a long --timeout.
    """
    if bpp not in (1, 2, 4, 8):
        raise ValueError("bpp must be 1, 2, 4 or 8")
    want = w * h
    px = pixels[:want].ljust(want, b"\x00")
    per_byte = 8 // bpp
    mask = (1 << bpp) - 1
    out = bytearray()
    for i in range(0, want, per_byte):
        acc = 0
        for j in range(per_byte):
            acc = (acc << bpp) | (px[i + j] & mask)
        out.append(acc)
    return bytes(out)


def send_screen_graphic(pixels: bytes, **kw) -> bytes:
    return build(Cmd.SEND_SCREEN_GRAPHIC, pack_screen(pixels, **kw))


def screen_test_pattern(w: int = SCREEN_W, h: int = SCREEN_H) -> bytes:
    """Vertical bars cycling through all 4 grey levels -- easy to spot on the LCD."""
    return bytes(((x // 8) + (y // 8)) % 4 for y in range(h) for x in range(w))


# ============================================================================
# cheat engine  (mechanism traced from OverwriteGameScore, FUN_0040b150)
# ============================================================================

MORTAL_KOMBAT = "Mort.Kombat|"
DUKE_NUKEM = "DUKE_NUKEM_|"


def apply_cheat(record: bytes, flag_offset: int, value: int, value_offset: int,
                mode: int = 1, game: str = "", force: bool = False) -> bytes:
    """
        record[value_offset]      = value
        record[value_offset + 1]  = value          # Mortal Kombat only
        record[flag_offset]      |= mode           # OR, never overwritten
        check_idx = min(flag_offset, value_offset) + 1
        record[check_idx] = (value_byte + flag_byte) & 0xFF
                            complemented unless Duke Nukem; skipped for MK
    """
    if len(record) != RECORD_LEN:
        raise ValueError(f"record must be exactly {RECORD_LEN} bytes, got {len(record)}")
    rec = bytearray(record)

    guard = name_span(record)
    def put(i: int, v: int) -> None:
        if not 0 <= i < RECORD_LEN:
            raise IndexError(f"record offset {i} outside 0..{RECORD_LEN - 1}")
        if guard and guard[0] <= i < guard[1] and not force:
            raise IndexError(
                f"record offset {i} is inside this record's name field "
                f"{guard} -- patching it would corrupt the record's identity. "
                "Pass force=True / --force to override."
            )
        rec[i] = v & 0xFF

    put(value_offset, value)
    if game == MORTAL_KOMBAT:
        put(value_offset + 1, value)
    put(flag_offset, rec[flag_offset] | mode)
    if game != MORTAL_KOMBAT:
        total = (rec[value_offset] + rec[flag_offset]) & 0xFF
        # 0x40b4cf: complemented only when the game is NEITHER Duke Nukem NOR
        # Mortal Kombat (`or edx, eax; jne` skips the subtract if either matched).
        if game not in (DUKE_NUKEM, MORTAL_KOMBAT):
            total = (255 - total) & 0xFF
        put(min(flag_offset, value_offset) + 1, total)
    return bytes(rec)


def apply_pokes(record: bytes, pokes: dict[int, int], force: bool = False) -> bytes:
    """Write arbitrary (offset -> byte) pairs. No check byte, no per-game quirks.

    The 1997 cheat shape writes one value byte, ORs a flag byte and repairs one
    check byte. Real records need more than that -- Indy 500's finishing
    position and race time are three bytes plus nine of ASCII -- so this is the
    general path. OVERWRITE GAME SCORE carries the whole 64-byte record; nothing
    in the protocol restricts you to the cheat shape.
    """
    if len(record) != RECORD_LEN:
        raise ValueError(f"record must be exactly {RECORD_LEN} bytes, got {len(record)}")
    rec = bytearray(record)
    guard = name_span(record)
    for off, val in sorted(pokes.items()):
        if not 0 <= off < RECORD_LEN:
            raise IndexError(f"record offset {off} outside 0..{RECORD_LEN - 1}")
        if not 0 <= val <= 255:
            raise ValueError(f"value {val} at offset {off} is not a byte")
        if guard and guard[0] <= off < guard[1] and not force:
            raise IndexError(
                f"offset {off} is inside this record's name field {guard}; "
                "the cartridge identifies the record by that name. Use --force "
                "if you really mean it."
            )
        rec[off] = val
    return bytes(rec)


# ---------------------------------------------------------------------------
# per-game record layouts, recovered by diffing real captures
# ---------------------------------------------------------------------------
#
# INDY 500 -- confirmed across two independent captures of the same cartridge:
#
#   capture A   text "P31 07:53"   [35]=0x1f=31   [36:38]=0x24f5=9461
#   capture B   text "P23 07:35"   [35]=0x17=23   [36:38]=0x238c=9100
#
#   [35]      finishing position, plain binary        31 -> "P31", 23 -> "P23"
#   [36:38]   race time, BIG-ENDIAN, 1/20 s ticks     9461/20 = 473.05s = 7:53
#                                                     9100/20 = 455.00s = 7:35
#   [11:20]   the same thing again as ASCII text, which is what gets displayed
#
# Bytes [0:2] are a per-game constant -- `11 52` for Indy 500, `04 26` for
# Fighters Megamix -- and did NOT change when the score did, across two
# captures. So they are an identifier, NOT a checksum. There is no whole-record
# integrity byte to repair.

INDY_TICKS_PER_SECOND = 20


def indy500_decode(record: bytes) -> dict:
    pos = record[35]
    ticks = (record[36] << 8) | record[37]
    return {"position": pos, "ticks": ticks,
            "seconds": ticks / INDY_TICKS_PER_SECOND,
            "time": f"{int(ticks / INDY_TICKS_PER_SECOND) // 60}:"
                    f"{int(ticks / INDY_TICKS_PER_SECOND) % 60:02d}"}


def indy500_pokes(position: int, seconds: float) -> dict[int, int]:
    """Bytes needed to set Indy 500's finishing position and race time.

    Writes the binary fields *and* the ASCII display text, so the two agree --
    the cartridge shows the text, and the game very likely reads the binary.
    """
    if not 0 <= position <= 255:
        raise ValueError("position must be 0..255")
    ticks = int(round(seconds * INDY_TICKS_PER_SECOND))
    if not 0 <= ticks <= 0xFFFF:
        raise ValueError(f"{seconds}s is {ticks} ticks; must fit in 16 bits "
                         f"(max {0xFFFF / INDY_TICKS_PER_SECOND:.1f}s)")
    text = f"P{position:02d} {int(seconds) // 60:02d}:{int(seconds) % 60:02d}"
    pokes = {35: position, 36: (ticks >> 8) & 0xFF, 37: ticks & 0xFF}
    for i, ch in enumerate(text[:9].ljust(9)):
        pokes[11 + i] = ord(ch)
    return pokes


GAME_DECODERS = {"INDY500": indy500_decode}


# ============================================================================
# transport: real serial link
# ============================================================================

class Link:
    TX_BYTE_DELAY = 0.0  # tested: pacing did not fix anything

    def __init__(self, port: str, timeout: float = 1.0, verbose: bool = True,
                 idle_gap: float = DEFAULT_IDLE_GAP):
        if serial is None:
            sys.exit("pyserial is required:  pip install pyserial")
        self.ser = serial.Serial(
            port, baudrate=BAUD, bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
            timeout=timeout,
        )
        self.verbose = verbose
        self.port = port
        self.idle_gap = idle_gap
        self.last_raw = b""      # exact bytes of the last read, for diagnostics

    def close(self) -> None:
        self.ser.close()

    def __enter__(self) -> "Link":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def send(self, frame: bytes) -> None:
        if self.TX_BYTE_DELAY:
            for b in frame:
                self.ser.write(bytes([b]))
                time.sleep(self.TX_BYTE_DELAY)
        else:
            self.ser.write(frame)
        self.ser.flush()
        if self.verbose:
            try:
                print(f"  TX  {_hx(frame):<30} {parse(frame)}")
            except (ValueError, ChecksumError):
                print(f"  TX  {_hx(frame)}")

    # -- raw --------------------------------------------------------------

    def read_raw(self, timeout: float = DEFAULT_TIMEOUT,
                 idle_gap: float | None = None) -> bytes:
        """Accumulate bytes until the line stays quiet for idle_gap."""
        gap = self.idle_gap if idle_gap is None else idle_gap
        deadline = time.monotonic() + timeout
        buf = bytearray()
        while time.monotonic() < deadline:
            chunk = self.ser.read(256)
            if chunk:
                buf += chunk
                quiet = time.monotonic() + gap
                while time.monotonic() < quiet and time.monotonic() < deadline:
                    more = self.ser.read(self.ser.in_waiting or 1)
                    if more:
                        buf += more
                        quiet = time.monotonic() + gap
                break
        self.last_raw = bytes(buf)
        return self.last_raw

    def read_timed(self, seconds: float) -> list[tuple[float, int]]:
        """(timestamp_delta, byte) for everything heard in `seconds`."""
        out: list[tuple[float, int]] = []
        t0 = last = time.monotonic()
        end = t0 + seconds
        while time.monotonic() < end:
            b = self.ser.read(1)
            if b:
                now = time.monotonic()
                out.append((now - last, b[0]))
                last = now
        return out

    # -- frames -----------------------------------------------------------

    def read_frame(self, timeout: float = DEFAULT_TIMEOUT,
                   idle_gap: float | None = None) -> Frame | None:
        raw = self.read_raw(timeout, idle_gap)
        if not raw:
            return None
        if self.verbose:
            print(f"  RX  {_hx(raw):<30}", end="")
        try:
            frame = parse(raw)
        except (ValueError, ChecksumError) as e:
            if self.verbose:
                print(f"!! {e}")
            _diagnose(raw, e)
            raise
        if self.verbose:
            print(frame)
        return frame

    def read_frames(self, timeout: float = DEFAULT_TIMEOUT,
                    idle_gap: float | None = None) -> list[Frame]:
        """Tolerant read: never raises, returns whatever frames are in there."""
        raw = self.read_raw(timeout, idle_gap)
        if not raw:
            return []
        frames, leftover = split_frames(raw)
        if self.verbose:
            print(f"  RX  {_hx(raw)}")
            for f in frames:
                print(f"      -> {f}")
            if leftover:
                print(f"      !! {len(leftover)} trailing byte(s): {leftover.hex(' ')}")
        return frames

    def expect(self, want: Cmd, timeout: float = DEFAULT_TIMEOUT) -> Frame | None:
        try:
            frame = self.read_frame(timeout)
        except (ValueError, ChecksumError):
            return None
        return frame if frame is not None and frame.cmd == want else None


def _hx(b: bytes, limit: int = 10) -> str:
    return b.hex(" ") if len(b) <= limit else f"{b[:limit].hex(' ')} +{len(b)-limit}"


_diagnosed = False


def _diagnose(raw: bytes, err: Exception) -> None:
    """Say something useful the first time a frame fails to parse."""
    global _diagnosed
    if _diagnosed:
        return
    _diagnosed = True
    if isinstance(err, ChecksumError):
        other = detect_checksum_variant(raw)
        if other is not None and other != CHECKSUM_INCLUDE_CMD:
            print(f"\n  ** This frame validates with CHECKSUM_INCLUDE_CMD = {other}.")
            print("  ** Change it at the top of this file -- it settles an open question.\n")
        return
    if len(raw) == 2:
        print("\n  ** Only 2 bytes arrived. If this was supposed to be a data frame,")
        print("  ** the read may have stopped in a mid-frame pause. Retry with")
        print("  ** --idle-gap 1.5 before concluding the cartridge sent a bare frame.\n")


# ============================================================================
# transport: offline fake cartridge
# ============================================================================

class OfflineCart:
    def __init__(self):
        self.records: dict[bytes, bytearray] = {}
        self._seed()

    def _seed(self) -> None:
        for name, score in ((b"INDY 500", b"P31 07:53"),
                            (b"Mort.Kombat", b"128400"),
                            (b"DUKE_NUKEM_", b"65200")):
            rec = bytearray(RECORD_LEN)
            rec[0:3] = b"\x00\x00\x00"          # the unexplained 3-byte prefix
            rec[3:3 + len(name)] = name
            rec[3 + len(name)] = RECORD_NAME_SEP
            rec[4 + len(name):4 + len(name) + len(score)] = score
            self.records[name] = rec

    def full_list_payload(self) -> bytes:
        out = b"".join(bytes(r) for r in self.records.values())
        return out.ljust(10 * RECORD_LEN, b"\x00")

    @staticmethod
    def _norm(b: bytes) -> bytes:
        """Fold the request-key conventions (space<->'_', padding, '|') away.

        The real cartridge is evidently stricter than this -- that is the whole
        point of --probe-from-list -- but the simulator should not fail for a
        reason we already know is an open question.
        """
        return b.replace(b"_", b" ").rstrip(b"| \x00").upper()

    def find(self, key: bytes) -> bytes | None:
        probe = self._norm(key)
        for name, rec in self.records.items():
            if self._norm(name) == probe:
                return bytes(rec)
        return None

    def overwrite(self, record: bytes) -> bool:
        d = decode_record(record)
        if not d["game"]:
            return False
        for name in self.records:
            if name.upper() == d["game"].encode().upper():
                self.records[name] = bytearray(record)
                return True
        return False

    def handle(self, frame: Frame) -> bytes | None:
        if frame.cmd == Cmd.LINK_REQUEST:
            return build(Cmd.LINK_ACKNOWLEDGE)
        if frame.cmd == Cmd.TERMINATION_REQUEST:
            return build(Cmd.TERMINATION_ACKNOWLEDGE)
        if frame.cmd == Cmd.FULL_LIST_REQUEST:
            return build(Cmd.FULL_LIST_SEND, self.full_list_payload())
        if frame.cmd == Cmd.GAME_SCORE_REQUEST:
            rec = self.find(frame.payload)
            return build(Cmd.GAME_SCORE_SEND, rec) if rec else build(Cmd.GAME_NOT_FOUND_ACK)
        if frame.cmd == Cmd.OVERWRITE_GAME_SCORE:
            ok = self.overwrite(frame.payload)
            return build(Cmd.COMMAND_ACKNOWLEDGEMENT) if ok else build(Cmd.GAME_NOT_FOUND_ACK)
        if frame.cmd in (Cmd.SEND_TEXT_MESSAGE, Cmd.SEND_SCREEN_GRAPHIC):
            return build(Cmd.COMMAND_ACKNOWLEDGEMENT)
        return build(Cmd.INVALID_COMMAND)


class OfflineLink:
    """Duck-typed stand-in for Link, backed by OfflineCart."""
    port = "<offline>"
    idle_gap = 0.0

    def __init__(self, cart: OfflineCart | None = None, verbose: bool = True,
                 pushes: list[bytes] | None = None):
        self.cart = cart or OfflineCart()
        self.verbose = verbose
        self._pending: bytes | None = None
        self.last_raw = b""
        #: Frames the fake cartridge will volunteer, so --follow can be
        #: exercised without hardware.
        self.pushes = list(pushes) if pushes is not None else [
            build(Cmd.GAME_SCORE_SEND, bytes(self.cart.records[b"INDY 500"])),
            build(Cmd.SEND_TEXT_MESSAGE, b"HI FROM GAME.COM"),
        ]

    def __enter__(self): return self
    def __exit__(self, *exc): pass
    def close(self): pass

    def send(self, frame: bytes) -> None:
        if self.verbose:
            print(f"  TX  {_hx(frame):<30} {parse(frame)}")
        self._pending = self.cart.handle(parse(frame))

    def read_raw(self, timeout: float = 0, idle_gap: float | None = None) -> bytes:
        raw, self._pending = self._pending or b"", None
        if not raw and self.pushes:
            raw = self.pushes.pop(0)
        elif not raw:
            raise KeyboardInterrupt        # nothing left to volunteer
        self.last_raw = raw
        return raw

    def read_frame(self, timeout: float = 0, idle_gap: float | None = None) -> Frame | None:
        raw = self.read_raw()
        if not raw:
            return None
        f = parse(raw)
        if self.verbose:
            print(f"  RX  {_hx(raw):<30} {f}")
        return f

    def read_frames(self, timeout: float = 0, idle_gap: float | None = None) -> list[Frame]:
        f = self.read_frame()
        return [f] if f else []

    def expect(self, want: Cmd, timeout: float = 0) -> Frame | None:
        f = self.read_frame()
        return f if f is not None and f.cmd == want else None


# ============================================================================
# session
# ============================================================================

class Session:
    def __init__(self, link):
        self.link = link

    def connect(self, attempts: int = 3) -> bool:
        for _ in range(attempts):
            self.link.send(link_request())
            if self.link.expect(Cmd.LINK_ACKNOWLEDGE, timeout=3.0):
                print(f"Connected on {self.link.port}")
                return True
        print("Unable to connect.")
        return False

    def disconnect(self) -> None:
        self.link.send(termination_request())
        # The termination ack comes back as `04 00` on real hardware instead of
        # `04 FB`, every run. Cosmetic -- the link does close -- so read it
        # tolerantly rather than raising on the way out the door.
        try:
            self.link.read_frames(timeout=2.0)
        except Exception:
            pass
        print("Link Terminated.")

    def full_list(self) -> bytes | None:
        for attempt in range(1, MAX_RETRIES + 1):
            self.link.send(full_list_request())
            try:
                f = self.link.expect(Cmd.FULL_LIST_SEND, timeout=15.0)
            except (ValueError, ChecksumError):
                print(f"  bad frame, retry {attempt}/{MAX_RETRIES}")
                continue
            if f is None:
                continue
            n = len(f.payload)
            if n % RECORD_LEN:
                # Seen on real hardware: a 639-byte payload instead of 640.
                # A dropped 0x00 out of the zero padding leaves the checksum
                # valid, so the checksum cannot catch this -- the length can.
                print(f"  short read: {n} bytes is not a multiple of "
                      f"{RECORD_LEN} ({n // RECORD_LEN} records + "
                      f"{n % RECORD_LEN} spare) -- a byte was dropped on the "
                      f"wire, retry {attempt}/{MAX_RETRIES}")
                continue
            return f.payload
        print("Trouble retrieving data.")
        return None

    def game_score(self, name: str, raw_key: bytes | None = None) -> bytes | None:
        frame_out = (game_score_request_raw(raw_key) if raw_key is not None
                     else game_score_request(name))
        for attempt in range(1, MAX_RETRIES + 1):
            self.link.send(frame_out)
            try:
                f = self.link.read_frame(timeout=10.0)
            except (ValueError, ChecksumError):
                print(f"  bad frame, retry {attempt}/{MAX_RETRIES}")
                continue
            if f is None:
                continue
            if f.cmd == Cmd.GAME_NOT_FOUND_ACK:
                print("You don't have a high score for that game, try again.")
                return None
            if f.cmd == Cmd.INVALID_COMMAND:
                print("Cartridge rejected GAME SCORE REQUEST (INVALID COMMAND).")
                return None
            if f.cmd == Cmd.GAME_SCORE_SEND:
                return f.payload
        return None

    def overwrite(self, record: bytes) -> bool:
        self.link.send(overwrite_game_score(record))
        frames = self.link.read_frames(timeout=10.0)
        for f in frames:
            if f.cmd == Cmd.COMMAND_ACKNOWLEDGEMENT:
                print("Record written and acknowledged.")
                return True
            if f.cmd == Cmd.SEND_TEXT_MESSAGE:
                print(f"  cartridge says: {f.text!r}")
        if frames:
            print(f"  unexpected reply: {', '.join(f.label for f in frames)}")
        else:
            print("  no reply")
        return False


# ============================================================================
# diagnostics -- for the four commands that are still unexplained
# ============================================================================

def probe_commands(sess: Session, link) -> None:
    """Send every bare command; tabulate what comes back.

    Answers the open question directly: which commands does this cartridge's
    firmware implement at all? A reply of INVALID COMMAND means "parsed, not
    supported"; silence means "not parsed, or waiting for a payload".
    """
    print("\n  cmd  name                       reply")
    print("  ---  -------------------------  " + "-" * 40)
    for cmd in Cmd:
        if cmd in (Cmd.LINK_REQUEST, Cmd.TERMINATION_REQUEST):
            continue                       # would tear the session down
        link.send(build(cmd))
        frames = link.read_frames(timeout=3.0)
        raw = link.last_raw
        if not frames:
            reply = "(silence)"
        else:
            reply = ", ".join(str(f) for f in frames)
        print(f"  {int(cmd):02X}   {LABELS[cmd]:<25}  {reply}")
        if raw:
            print(f"       raw: {raw.hex(' ')}")
        time.sleep(0.3)


#: Encoding variants to try for the GAME_SCORE_REQUEST search key. The framing
#: is confirmed correct, so if the cartridge rejects it, the key content is the
#: remaining variable.
NAME_VARIANTS = [
    ("as-typed + '|'",        dict(pad="_", terminator=True,  upper=False)),
    ("as-typed, no '|'",      dict(pad="_", terminator=False, upper=False)),
    ("UPPER + '|'",           dict(pad="_", terminator=True,  upper=True)),
    ("UPPER, no '|'",         dict(pad="_", terminator=False, upper=True)),
    ("space-padded + '|'",    dict(pad=" ", terminator=True,  upper=False)),
    ("space-padded, no '|'",  dict(pad=" ", terminator=False, upper=False)),
    ("NUL-padded + '|'",      dict(pad="\x00", terminator=True,  upper=False)),
]


def probe_names(link, name: str) -> None:
    """Sweep name encodings for one game and report each reply."""
    print(f"\n  Probing GAME SCORE REQUEST key encodings for {name!r}")
    print("  (framing is confirmed correct -- this varies only the key bytes)\n")
    for label, kw in NAME_VARIANTS:
        key = encode_game_name(name, **kw)
        link.send(build(Cmd.GAME_SCORE_REQUEST, key))
        frames = link.read_frames(timeout=4.0)
        reply = ", ".join(f.label for f in frames) if frames else "(silence)"
        print(f"    {label:<24} {key!r:<20} -> {reply}")
        time.sleep(0.3)


def probe_from_list(sess: Session, link) -> None:
    """Replay the exact name bytes the cartridge itself sent us.

    This removes every encoding assumption at once: whatever the cartridge put
    in its own records is, by definition, the encoding it uses.
    """
    payload = sess.full_list()
    if not payload:
        print("  couldn't read the full list; nothing to derive keys from")
        return
    records = decode_full_list(payload)
    if not records:
        print("  full list decoded to zero records")
        return
    print(f"\n  {len(records)} record(s) on the cartridge. Replaying their exact "
          f"name bytes as GAME SCORE REQUEST keys:\n")
    for d in records:
        nb = d["name_bytes"]
        for label, key in (
            ("verbatim",        nb),
            ("verbatim + '|'",  nb + b"|"),
            ("padded to 11+'|'", nb[:11].ljust(11, b"_") + b"|"),
        ):
            if len(key) > 13:
                continue
            link.send(build(Cmd.GAME_SCORE_REQUEST, key))
            frames = link.read_frames(timeout=4.0)
            reply = ", ".join(f.label for f in frames) if frames else "(silence)"
            print(f"    {d['game']!r:<16} {label:<18} {key!r:<18} -> {reply}")
            time.sleep(0.3)


def listen(link, seconds: float) -> None:
    """Passive capture with inter-byte timing.

    This is the measurement that settles whether `0C F3` and `04 00` are real
    two-byte frames or truncated reads: if the gap before the next byte is
    longer than the old 0.25s idle window, the reader was cutting frames in
    half and the cartridge was answering properly all along.
    """
    if not hasattr(link, "read_timed"):
        print("  --listen needs real hardware (--port); there is nothing to")
        print("  measure on the offline link, which replies instantly.")
        return
    print(f"\n  Listening for {seconds}s. Press buttons on the Game.com, or run a")
    print("  command from another process. Ctrl-C to stop early.\n")
    try:
        events = link.read_timed(seconds)
    except KeyboardInterrupt:
        events = []
    if not events:
        print("  (nothing heard)")
        return
    raw = bytes(b for _, b in events)
    print(f"  {len(events)} byte(s):  {raw.hex(' ')}\n")
    print("     gap(ms)  byte  note")
    biggest = 0.0
    for dt, b in events:
        note = ""
        if dt > 0.25:
            note = "  <-- longer than the OLD 0.25s idle window"
            biggest = max(biggest, dt)
        print(f"     {dt * 1000:7.0f}  0x{b:02x}{note}")
    frames, leftover = split_frames(raw)
    print()
    for f in frames:
        print(f"  parsed: {f}")
    if leftover:
        print(f"  leftover: {leftover.hex(' ')}")
    if biggest:
        print(f"\n  ** Largest mid-stream gap was {biggest*1000:.0f} ms.")
        print(f"  ** Use --idle-gap {max(1.0, biggest * 2):.1f} or higher.")


def find_record_in_list(payload: bytes, game: str) -> tuple[int, bytes] | None:
    """Locate one game's 64-byte record inside a FULL_LIST_SEND payload.

    Returns (byte_offset_into_payload, record_bytes) or None if not present.
    Matching is case/whitespace-insensitive against the decoded name, since
    that's how the list is already displayed (e.g. "INDY500").
    """
    key = game.upper().replace(" ", "")
    for i in range(0, len(payload) - RECORD_LEN + 1, RECORD_LEN):
        rec = payload[i:i + RECORD_LEN]
        d = decode_record(rec)
        if d["game"] and d["game"].upper().replace(" ", "") == key:
            return i, rec
    return None


def auto_overwrite_via_full_list(sess: "Session", args) -> None:
    """Untested-path experiment: fetch the full list (confirmed to work with
    no per-game interaction at all), patch the target record in memory, and
    immediately send OVERWRITE_GAME_SCORE back in the same session -- testing
    whether the cartridge accepts it right after a FULL_LIST_SEND, rather than
    only after a single-game GAME_SCORE_SEND push (which this cart may never
    produce on its own). See the module docstring's "STILL OPEN" state
    question -- this is the direct test of it.
    """
    payload = sess.full_list()
    if payload is None:
        return
    found = find_record_in_list(payload, args.game)
    if found is None:
        names = ", ".join(d["game"] for d in decode_full_list(payload))
        sys.exit(f"'{args.game}' not found in full list. Games present: {names}")
    _offset, rec = found
    pokes = pokes_from_args(args)
    if not pokes:
        sys.exit("need --poke or --set-indy to know what to change")
    try:
        patched = apply_pokes(rec, pokes, force=args.force)
    except (IndexError, ValueError) as e:
        sys.exit(f"refusing to patch: {e}")
    show_record(rec, "before: ")
    for i, b, a in find_diff(rec, patched):
        print(f"  offset {i:3d}: {b:02x} -> {a:02x}")
    show_record(patched, "after:  ")
    if args.dry_run:
        print("  (--dry-run: nothing sent)")
        return
    print("  attempting OVERWRITE_GAME_SCORE right after the full-list fetch...")
    ok = sess.overwrite(patched)
    if not ok:
        print("  Cartridge did not acknowledge. This may mean OVERWRITE truly")
        print("  needs a single-game GAME_SCORE_SEND push (which this cart may")
        print("  not have a menu for), not just a preceding FULL_LIST_SEND.")


def follow(sess: "Session", link, args) -> None:
    """Connect, then let the CARTRIDGE drive. This is the normal flow.

    A --probe-cmds sweep shows the cartridge accepts only 0x01/0x03/0x05 as
    inputs and answers 0x07/0x09/0x0B with INVALID COMMAND. Everything else it
    ignores completely. So the PC cannot ask for a single game -- the *user*
    picks it on the handheld and the cartridge pushes it over.

    That also matches the ROM's own status strings ("One game data sent.",
    "Write text message.") and the `from game.com: ` string in the PC app.

    Drive the menu on the Game.com while this is running.
    """
    print("\n  Connected and listening. Now use the cartridge's own menu on the")
    print("  Game.com -- send one game, or write a text message. Ctrl-C to stop.\n")
    seen = 0
    try:
        while True:
            frames = link.read_frames(timeout=args.timeout)
            if not frames:
                continue
            for f in frames:
                seen += 1
                if f.cmd == Cmd.GAME_SCORE_SEND:
                    _handle_pushed_record(sess, link, f.payload, args)
                elif f.cmd == Cmd.FULL_LIST_SEND:
                    for d in decode_full_list(f.payload):
                        print(f"    {d['game']:<14} {d['score_raw']}")
                elif f.cmd == Cmd.SEND_TEXT_MESSAGE:
                    print(f"  from game.com: {f.text!r}")
                    if args.ack:
                        link.send(command_acknowledgement())
                elif f.cmd == Cmd.SEND_SCREEN_GRAPHIC:
                    print(f"  screen graphic: {len(f.payload)} bytes")
                    if args.dump:
                        open(args.dump, "wb").write(f.payload)
                        print(f"  saved to {args.dump}")
                    if args.ack:
                        link.send(command_acknowledgement())
                else:
                    print(f"  <- {f}")
    except KeyboardInterrupt:
        print(f"\n  stopped after {seen} frame(s)")


def _handle_pushed_record(sess: "Session", link, record: bytes, args) -> None:
    """The cartridge just pushed us a game record."""
    d = decode_record(record)
    print(f"  <- GAME SCORE SEND  {d['game']!r}  {d['score_raw']!r}")
    print(f"     name at {d['name_span']}, prefix {d['prefix'].hex(' ')}")
    if d["unknown"]:
        interesting = {k: v for k, v in d["unknown"].items()}
        print(f"     other non-zero bytes: "
              f"{', '.join(f'{k}=0x{v:02x}' for k, v in interesting.items())}")
    if args.dump:
        open(args.dump, "wb").write(record)
        print(f"     saved to {args.dump}")
    if args.upload:
        upload(args.endpoint, args.user, [d])

    # If cheat parameters were supplied, patch it and hand it straight back --
    # this is the one window in which OVERWRITE GAME SCORE is likely to be
    # accepted, since the cartridge just told us it is holding this record.
    pokes = pokes_from_args(args)
    if pokes:
        try:
            patched = apply_pokes(record, pokes, force=args.force)
        except (IndexError, ValueError) as e:
            print(f"     refusing to patch: {e}")
            return
        for i, b, a in find_diff(record, patched):
            print(f"       offset {i:3d}: {b:02x} -> {a:02x}")
        show_record(patched, "     would become: ")
        if args.dry_run:
            print("     (--dry-run: not sent)")
            return
        print("     sending OVERWRITE GAME SCORE straight back")
        sess.overwrite(patched)
        return

    if args.cheat and None not in (args.flag_offset, args.value, args.value_offset):
        try:
            patched = apply_cheat(record, args.flag_offset, args.value,
                                  args.value_offset, args.mode,
                                  game=args.game or "", force=args.force)
        except (IndexError, ValueError) as e:
            print(f"     refusing to patch: {e}")
            return
        for i, b, a in find_diff(record, patched):
            print(f"       offset {i:3d}: {b:02x} -> {a:02x}")
        if args.dry_run:
            print("     (--dry-run: not sent)")
            return
        print("     sending OVERWRITE GAME SCORE straight back")
        sess.overwrite(patched)


def probe_text_handshake(link, text: str) -> None:
    """Test the 'the cartridge is announcing, not rejecting' theory.

    If `0C F3` is the cartridge *offering* to send a text message rather than
    a rejection, the right move is to acknowledge it and keep listening.
    """
    print("\n  1. sending SEND TEXT MESSAGE")
    link.send(send_text_message(text))
    frames = link.read_frames(timeout=5.0)
    print(f"     -> {', '.join(f.label for f in frames) if frames else '(silence)'}")

    print("  2. sending bare SEND TEXT MESSAGE header only (no payload)")
    link.send(build(Cmd.SEND_TEXT_MESSAGE))
    frames = link.read_frames(timeout=5.0)
    print(f"     -> {', '.join(f.label for f in frames) if frames else '(silence)'}")

    print("  3. acknowledging, then listening (does the cart then talk?)")
    link.send(command_acknowledgement())
    frames = link.read_frames(timeout=6.0)
    for f in frames:
        print(f"     -> {f}")
    if not frames:
        print("     -> (silence)")


# ============================================================================
# leaderboard upload
# ============================================================================

def upload(endpoint: str, user: str, records: list[dict]) -> None:
    clean = [{k: v for k, v in r.items() if k != "name_bytes"} for r in records]
    body = json.dumps({"user": user, "records": clean}).encode()
    req = urllib.request.Request(endpoint, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"{user}, your Upload was successful! ({resp.status})")
    except (urllib.error.URLError, OSError) as e:
        print(f"Sorry, {user}, your Upload was not successful: {e}")


# ============================================================================
# examples
# ============================================================================

EXAMPLES = r"""
EXAMPLE PAYLOADS -- exact bytes for every command
=================================================
(complement byte is always 255-CMD; checksum = 255 - (sum of all preceding
bytes & 0xFF), including the command byte -- flip CHECKSUM_INCLUDE_CMD if a
real capture ever says otherwise)

  PC -> cart (the cartridge parses these):
  LINK REQUEST                 01 FE
  TERMINATION REQUEST          03 FC
  FULL LIST REQUEST            05 FA
  COMMAND ACKNOWLEDGEMENT      0B F4     (rejected unless it answers a push)

  cart -> PC (the cartridge sends these; it ignores them as input):
  LINK ACKNOWLEDGE             02 FD
  TERMINATION ACKNOWLEDGE      04 FB     (real hardware sends 04 00 -- quirk)
  GAME NOT FOUND ACK           0A F5
  INVALID COMMAND              0F F0

  GAME SCORE REQUEST  (15 bytes: 2 header + 12 key + 1 checksum)
      07 F8 | 4D 6F 72 74 2E 4B 6F 6D 62 61 74 7C | ck
             M  o  r  t  .  K  o  m  b  a  t  |
      python3 weblink_toolkit.py --emit game-score-request --game "Mort.Kombat"

  OVERWRITE GAME SCORE  (67 bytes: 2 header + 64 record + 1 checksum)
      09 F6 | <64-byte record, usually one you just read back> | ck
      python3 weblink_toolkit.py --emit overwrite --record-hex "00 00 00 49 ..."

  SEND TEXT MESSAGE  (2 header + N text + 1 checksum)  *** cart -> PC ***
      0C F3 | 48 45 4C 4C 4F | ck
              H  E  L  L  O
      The PC does not send this. Real hardware ignores it in every framing and
      encoding variant, because the cartridge is the sender: the user composes
      the message on the Game.com. Receive it with --follow.
      python3 weblink_toolkit.py --emit text --text "HELLO"   # builder only

  SEND SCREEN GRAPHIC  (2 header + 8000 bytes + 1 checksum)  *** cart -> PC ***
      0D F2 | <200x160 @ 2bpp = 8000 bytes> | ck
      Same direction, and the payload format is still a guess. --follow will
      capture whatever the cartridge actually sends; --dump saves it.
      python3 weblink_toolkit.py --emit screen --screen-test  # builder only


THE NORMAL FLOW (cartridge-driven)
==================================
The PC cannot pull a single game or push text. Connect, then drive the menu on
the Game.com:

    python3 weblink_toolkit.py --port COM3 --follow
    python3 weblink_toolkit.py --port COM3 --follow --dump record.bin --upload

    # patch and hand back whatever the cartridge pushes, in the one window
    # where OVERWRITE is likely to be accepted:
    python3 weblink_toolkit.py --port COM3 --follow \
        --cheat --flag-offset 32 --value 200 --value-offset 40 --dry-run

WHAT THE PC CAN STILL INITIATE
==============================
    python3 weblink_toolkit.py --port COM3 --full-list        # works
    python3 weblink_toolkit.py --port COM3 --full-list --dump list.bin --upload

POKING NEW DATA INTO A GAME
===========================
Record fields recovered by diffing two real captures of the same cartridge:

  INDY 500   (id bytes 11 52)
      [11:20]  ASCII display text, e.g. "P23 07:35"
      [35]     finishing position, plain binary   (0x17=23 -> "P23")
      [36:38]  race time, BIG-ENDIAN, 1/20 s      (0x238C=9100 -> 455.0s = 7:35)
                                                  (0x24F5=9461 -> 473.05s = 7:53)
  There is NO record checksum: bytes [0:2] are a per-game constant and stayed
  `11 52` across both captures while everything else changed.

Patch it, offline, and get the exact frame bytes:

    python3 weblink_toolkit.py --record-file indy.bin --set-indy "1:30"

      offset  35: 17 -> 01        finish 1st
      offset  36: 23 -> 02        600 ticks = 30.0 s
      offset  37: 8c -> 58
      offset  12..19               ASCII text follows along
      OVERWRITE frame (67 bytes):
      09 f6 11 52 00 49 4e 44 59 35 30 30 01 50 30 31 20 30 30 3a 33 30 00 ...
      ... 00 01 02 58 00 ... 00 aa

Arbitrary bytes, for games whose layout you have not cracked yet:

    python3 weblink_toolkit.py --record-file fmegamix.bin --poke "25=9,26=9,27=9"

Then send it. OVERWRITE is only likely to be accepted right after the cartridge
has pushed you that record, so do it inside --follow:

    python3 weblink_toolkit.py --port COM3 --follow --set-indy "1:30" --dry-run
    python3 weblink_toolkit.py --port COM3 --follow --set-indy "1:30"

The 1997 cheat shape (--cheat --flag-offset/--value/--value-offset) writes one
value byte, ORs a flag byte and repairs one check byte. Indy 500 needs three
binary bytes plus nine of ASCII, so it does not fit -- use --poke/--set-indy.
Nothing in the protocol restricts you to the cheat shape; OVERWRITE carries the
whole 64-byte record.

HUNTING RECORD OFFSETS
======================
    python3 weblink_toolkit.py --port COM3 --follow --dump before.bin
    #  ... play the game, make progress, save ...
    python3 weblink_toolkit.py --port COM3 --follow --dump after.bin
    python3 weblink_toolkit.py --diff before.bin after.bin

That is exactly how Indy 500 fell out: two captures, three bytes moved, and the
ratio 9461/473.05 = 9100/455.00 = 20.0 gave up the tick rate.

Fighters Megamix (id bytes 04 26) is still open. Its record carries
`[18]=0x5a`, `[25:30]=01 02 03 04 03`, `[33:35]=03 06`, `[37]=09` next to the
display text "02" -- structured state rather than a single score.

DIAGNOSTICS
===========
    --probe-cmds        which commands this firmware parses at all
    --probe-from-list   replay the cartridge's own name bytes as request keys
    --probe-names       sweep case / padding / '|' terminator
    --listen 30         passive capture with inter-byte timing (no handshake,
                        so the cartridge may stay quiet -- prefer --follow)
    --idle-gap 1.5      raise if any reply ever looks truncated
"""


# ============================================================================
# self-test
# ============================================================================

def _selftest() -> None:
    # -- frames, against the constants in the binary --
    for cmd, ck in ((0x02, 0xFD), (0x03, 0xFC), (0x04, 0xFB), (0x05, 0xFA),
                    (0x09, 0xF6), (0x0A, 0xF5), (0x0B, 0xF4), (0x0C, 0xF3),
                    (0x0D, 0xF2), (0x0F, 0xF0)):
        assert build(cmd) == bytes([cmd, ck]), f"cmd {cmd:#04x}"
        assert parse(bytes([cmd, ck])).cmd == cmd

    # -- GAME SCORE REQUEST is 15 bytes, matching the buffer bound at 0x40ac50 --
    f = game_score_request("Mort.Kombat")
    assert len(f) == 15, len(f)
    assert f[:2] == b"\x07\xf8"
    assert parse(f).payload == b"Mort.Kombat|"
    assert b"MORT" not in f, "name must not be uppercased"

    # -- record frames --
    rec = bytes(range(RECORD_LEN))
    fr = overwrite_game_score(rec)
    assert len(fr) == FRAME_HEADER_LEN + RECORD_LEN + 1 == 67
    assert parse(fr).payload == rec

    # -- full list shape matches the real 643-byte capture --
    full = build(Cmd.FULL_LIST_SEND, bytes(10 * RECORD_LEN))
    assert len(full) == 643, len(full)
    assert len(parse(full).payload) == 640

    # -- corruption is caught --
    for i, exc in ((1, ValueError), (len(fr) - 1, ChecksumError)):
        bad = bytearray(fr); bad[i] ^= 0xFF
        try:
            parse(bytes(bad))
        except exc:
            pass
        else:
            raise AssertionError(f"byte {i} corruption not caught")

    # -- record decode, using the real Indy 500 shape --
    r = bytearray(RECORD_LEN)
    r[3:11] = b"INDY 500"
    r[11] = RECORD_NAME_SEP
    r[12:21] = b"P31 07:53"
    d = decode_record(bytes(r))
    assert d["game"] == "INDY 500", d
    assert d["score_raw"] == "P31 07:53", d
    assert d["name_span"] == (3, 11), d["name_span"]
    assert name_span(bytes(r)) == (3, 11)

    # -- the cheat guard follows the real name span, not a fixed 0..11 --
    try:
        apply_cheat(bytes(r), flag_offset=5, value=1, value_offset=40)
    except IndexError as e:
        assert "name field" in str(e), e
    else:
        raise AssertionError("patch inside the derived name span was allowed")
    # forced: the flag byte is ORed into whatever was there, never replaced
    assert apply_cheat(bytes(r), 5, 1, 40, force=True)[5] == (r[5] | 1)
    out = apply_cheat(bytes(r), flag_offset=32, value=0x7F, value_offset=40)
    assert out[40] == 0x7F and out[32] == 1
    assert out[33] == (255 - ((0x7F + 1) & 0xFF)) & 0xFF
    assert apply_cheat(bytes(r), 32, 0x7F, 40, game=DUKE_NUKEM)[33] == (0x7F + 1) & 0xFF
    mk = apply_cheat(bytes(r), 32, 0x42, 40, game=MORTAL_KOMBAT)
    assert mk[40] == 0x42 and mk[41] == 0x42 and mk[33] == 0

    # -- split_frames handles concatenated and truncated streams --
    stream = build(Cmd.LINK_ACKNOWLEDGE) + build(Cmd.SEND_TEXT_MESSAGE, b"HI")
    frames, leftover = split_frames(stream)
    assert [f.cmd for f in frames] == [0x02, 0x0C], [f.cmd for f in frames]
    assert frames[1].payload == b"HI" and not leftover
    frames, leftover = split_frames(b"\x0c\xf3")
    assert len(frames) == 1 and frames[0].payload == b""

    # -- name variants all fit the 15-byte buffer --
    for _, kw in NAME_VARIANTS:
        assert len(build(Cmd.GAME_SCORE_REQUEST, encode_game_name("Indy 500", **kw))) <= 15

    # -- screen packing --
    packed = pack_screen(bytes([0, 1, 2, 3] * (SCREEN_W * SCREEN_H // 4)))
    assert len(packed) == SCREEN_W * SCREEN_H * SCREEN_BPP // 8 == 8000
    assert packed[0] == 0b00011011
    assert len(send_screen_graphic(screen_test_pattern())) == 8000 + 3

    # -- offline cartridge round-trip --
    cart = OfflineCart()
    link = OfflineLink(cart, verbose=False)
    sess = Session(link)
    assert sess.connect()
    payload = sess.full_list()
    assert payload is not None and len(payload) == 640
    recs = decode_full_list(payload)
    assert any(x["game"] == "INDY 500" for x in recs), recs
    got = sess.game_score("INDY 500")
    assert got is not None and decode_record(got)["game"] == "INDY 500"
    patched = apply_cheat(got, flag_offset=32, value=9, value_offset=40)
    assert sess.overwrite(patched)
    again = sess.game_score("INDY 500")
    assert again[40] == 9 and again[32] == 1, "overwrite did not persist"
    assert find_diff(got, again) == [(32, 0, 1), (33, 0, 245), (40, 0, 9)]

    print("self-test: 40+ assertions passed "
          "(frames, records, cheats, screen packing, offline round-trip)")


# ============================================================================
# cli
# ============================================================================

def scan() -> None:
    if list_ports is None:
        sys.exit("pyserial is required:  pip install pyserial")
    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports found.")
        return
    for p in ports:
        print(f"\nTrying {p.device} ... ({p.description})")
        try:
            with Link(p.device, timeout=1.0, verbose=False) as link:
                if Session(link).connect(attempts=2):
                    print(f"  --> cartridge found on {p.device}")
                    return
        except Exception as e:
            print(f"  {p.device}: {e}")
    print("\nUnable to connect.")


def parse_pokes(spec: str) -> dict[int, int]:
    """Parse  off=val,off=val  with 0x / 0b prefixes allowed."""
    out: dict[int, int] = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            off, val = part.split("=", 1)
            out[int(off, 0)] = int(val, 0)
        except ValueError:
            raise SystemExit(f'--poke wants off=val pairs, got {part!r}\n'
                             '  e.g.  --poke "35=1,36=0x02,37=0x58"')
    if not out:
        raise SystemExit("--poke needs at least one off=val pair")
    return out


def pokes_from_args(args) -> dict[int, int] | None:
    if args.set_indy:
        try:
            pos, secs = args.set_indy.split(":", 1)
            return indy500_pokes(int(pos), float(secs))
        except ValueError as e:
            raise SystemExit(f'--set-indy wants POS:SECONDS, e.g. "1:30"  ({e})')
    return parse_pokes(args.poke) if args.poke else None


def show_record(record: bytes, label: str = "") -> None:
    d = decode_record(record)
    print(f"  {label}{d['game']!r}  text {d['score_raw']!r}")
    if d.get("fields"):
        print(f"     decoded: {d['fields']}")
    print(f"     id bytes {record[:2].hex(' ')}  name at {d['name_span']}")
    if d["name_span"] is None:
        print("     WARNING: name field is gone after this patch -- the "
              "separator byte was likely overwritten. Refusing to trust "
              "this record; check your offsets.")


def _emit(args) -> None:
    """Print the exact bytes of a frame without touching hardware."""
    kind = args.emit
    if kind == "game-score-request":
        frame = game_score_request(args.game or "Mort.Kombat")
    elif kind == "overwrite":
        if args.record_file:
            rec = open(args.record_file, "rb").read()
        elif args.record_hex:
            rec = bytes.fromhex(args.record_hex.replace(" ", ""))
        else:
            sys.exit("--emit overwrite needs --record-hex or --record-file")
        rec = rec.ljust(RECORD_LEN, b"\x00")[:RECORD_LEN]
        pokes = pokes_from_args(args)
        if pokes:
            show_record(rec, "before: ")
            patched = apply_pokes(rec, pokes, force=args.force)
            for i, b, a in find_diff(rec, patched):
                print(f"    offset {i:3d}: {b:02x} -> {a:02x}")
            show_record(patched, "after:  ")
            rec = patched
        frame = overwrite_game_score(rec)
    elif kind == "text":
        frame = send_text_message(args.text or "HELLO")
    elif kind == "screen":
        px = screen_test_pattern() if args.screen_test else open(args.screen, "rb").read()
        frame = send_screen_graphic(px)
    else:
        frame = build(int(kind, 0))
    print(f"{len(frame)} bytes")
    print(frame.hex(" "))


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port, e.g. COM3 or /dev/ttyUSB0")
    ap.add_argument("--offline", action="store_true",
                    help="in-process fake cartridge, no serial port at all")
    ap.add_argument("--scan", action="store_true", help="probe every serial port")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--examples", action="store_true",
                    help="print every example payload and the diagnostic recipes")
    ap.add_argument("--user", default="player")
    ap.add_argument("--idle-gap", type=float, default=DEFAULT_IDLE_GAP,
                    help=f"seconds of silence that ends a frame (default "
                         f"{DEFAULT_IDLE_GAP}; raise it if replies look truncated)")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)

    g = ap.add_argument_group("normal operation")
    g.add_argument("--full-list", action="store_true")
    g.add_argument("--game", help="fetch (or target, with --cheat) one game by name")
    g.add_argument("--raw", action="store_true", help="print raw payload hex")
    g.add_argument("--dump", metavar="FILE", help="save the fetched payload to FILE")
    g.add_argument("--upload", action="store_true")
    g.add_argument("--endpoint", default="http://127.0.0.1:8080/api/scores")

    c = ap.add_argument_group("cheats")
    c.add_argument("--cheat", action="store_true")
    c.add_argument("--flag-offset", type=int)
    c.add_argument("--value", type=int)
    c.add_argument("--value-offset", type=int)
    c.add_argument("--mode", type=int, default=1)
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--force", action="store_true", help="override the name-field guard")
    c.add_argument("--poke", metavar="SPEC",
                   help='raw record bytes: "35=1,36=0x02,37=0x58" (no cheat shape)')
    c.add_argument("--set-indy", metavar="POS:SECONDS",
                   help='Indy 500 helper, e.g. "1:30" = finish P01 in 00:30 '
                        '(writes the binary fields and the ASCII text together)')
    c.add_argument("--record-file", metavar="FILE",
                   help="apply --poke/--set-indy to a dumped record instead of "
                        "reading one off the cartridge")
    c.add_argument("--diff", nargs=2, metavar=("BEFORE", "AFTER"),
                   help="diff two record dumps (hex strings or files)")

    w = ap.add_argument_group("writes")
    w.add_argument("--text", help="send a text message to the cartridge (0x0C)")
    w.add_argument("--screen", metavar="FILE",
                   help="send a screen graphic (0x0D): one byte per pixel, 0..3")
    w.add_argument("--screen-test", action="store_true",
                   help="send a generated 4-level test pattern instead of a file")

    d = ap.add_argument_group("diagnostics")
    d.add_argument("--listen", type=float, metavar="SECONDS",
                   help="passive capture with inter-byte timing")
    d.add_argument("--probe-cmds", action="store_true",
                   help="send all 15 bare commands, tabulate replies")
    d.add_argument("--probe-names", metavar="GAME",
                   help="sweep GAME_SCORE_REQUEST key encodings")
    d.add_argument("--probe-from-list", action="store_true",
                   help="replay exact name bytes from the cartridge's own records")
    d.add_argument("--follow", action="store_true",
                   help="connect, then listen and let the CARTRIDGE drive "
                        "(the normal flow -- use the Game.com's own menu)")
    d.add_argument("--ack", action="store_true",
                   help="acknowledge cart-initiated frames in --follow")
    d.add_argument("--probe-text", action="store_true",
                   help="test the 'cartridge is announcing' theory for 0x0C")
    d.add_argument("--emit", metavar="KIND",
                   help="print frame bytes without hardware: game-score-request, "
                        "overwrite, text, screen, or a numeric command byte")
    d.add_argument("--record-hex", help="record bytes for --emit overwrite")
    d.add_argument("--raw-frame", metavar="HEX", action="append",
                   help="send exactly these bytes, no interpretation (repeatable)")

    args = ap.parse_args()

    if args.examples:
        print(EXAMPLES)
        return
    if args.selftest:
        return _selftest()
    if args.emit:
        return _emit(args)
    if args.diff:
        def _load(x):
            try:
                return open(x, "rb").read()
            except OSError:
                return bytes.fromhex(x.replace(" ", ""))
        before, after = _load(args.diff[0]), _load(args.diff[1])
        changes = find_diff(before, after)
        if not changes:
            print("Records are identical.")
            return
        span = name_span(before)
        print(f"{len(changes)} byte(s) differ -- candidate cheat offsets:")
        for i, b, a in changes:
            note = "  <-- inside the name field" if span and span[0] <= i < span[1] else ""
            print(f"  offset {i:3d} (0x{i:02x}): {b:02x} -> {a:02x}  ({b:3d} -> {a:3d}){note}")
        return
    if args.record_file and not (args.port or args.offline):
        rec = open(args.record_file, "rb").read().ljust(RECORD_LEN, b"\x00")[:RECORD_LEN]
        show_record(rec, "before: ")
        pokes = pokes_from_args(args)
        if not pokes:
            return
        patched = apply_pokes(rec, pokes, force=args.force)
        for i, b, a in find_diff(rec, patched):
            print(f"  offset {i:3d}: {b:02x} -> {a:02x}")
        show_record(patched, "after:  ")
        out = (args.dump or args.record_file + ".patched")
        open(out, "wb").write(patched)
        print(f"  wrote {out}")
        frame = overwrite_game_score(patched)
        print(f"  OVERWRITE frame ({len(frame)} bytes):\n  {frame.hex(' ')}")
        return

    if args.scan:
        return scan()

    if not args.port and not args.offline:
        ap.error("--port is required (or --offline / --scan / --selftest / --examples)")

    open_link = ((lambda: OfflineLink()) if args.offline
                 else (lambda: Link(args.port, idle_gap=args.idle_gap)))

    with open_link() as link:
        # --listen is passive: no handshake, just watch the wire.
        if args.listen:
            return listen(link, args.listen)

        sess = Session(link)
        if not sess.connect():
            return
        try:
            if args.follow:
                return follow(sess, link, args)
            if args.probe_cmds:
                return probe_commands(sess, link)
            if args.probe_names:
                return probe_names(link, args.probe_names)
            if args.probe_from_list:
                return probe_from_list(sess, link)
            if args.probe_text:
                return probe_text_handshake(link, args.text or "HELLO GAME.COM")
            if args.raw_frame:
                for hx in args.raw_frame:
                    link.send(bytes.fromhex(hx.replace(" ", "")))
                    for f in link.read_frames(timeout=args.timeout):
                        print(f"      -> {f}")
                return
            if args.text:
                print("  NOTE: 0x0C is a cart->PC command. Real hardware ignores")
                print("  it completely when the PC sends it. Use --follow and")
                print("  write the message on the Game.com instead.")
                link.send(send_text_message(args.text))
                frames = link.read_frames(timeout=args.timeout)
                print("  acknowledged." if any(f.cmd == Cmd.COMMAND_ACKNOWLEDGEMENT
                                               for f in frames) else "  no acknowledgement.")
                return
            if args.screen or args.screen_test:
                px = (screen_test_pattern() if args.screen_test
                      else open(args.screen, "rb").read())
                print(f"  sending {SCREEN_W}x{SCREEN_H} @ {SCREEN_BPP}bpp "
                      f"= {SCREEN_W*SCREEN_H*SCREEN_BPP//8} bytes "
                      f"(~{SCREEN_W*SCREEN_H*SCREEN_BPP/8*10/BAUD:.1f}s on the wire)")
                link.send(send_screen_graphic(px))
                frames = link.read_frames(timeout=max(args.timeout, 20.0))
                print("  acknowledged." if frames else "  no reply.")
                return

            # -- patch a game found via the full list, then overwrite
            #    immediately in the same session (no per-game push needed;
            #    testing the "just handed a record" acceptance window) --
            if args.game and (args.poke or args.set_indy) and not args.cheat:
                return auto_overwrite_via_full_list(sess, args)

            # -- cheat --
            if args.cheat:
                if not args.game:
                    sys.exit("--cheat needs --game")
                if None in (args.flag_offset, args.value, args.value_offset):
                    sys.exit("--cheat needs --flag-offset, --value and --value-offset")
                rec = sess.game_score(args.game)
                if rec is None:
                    return
                try:
                    patched = apply_cheat(rec, args.flag_offset, args.value,
                                          args.value_offset, args.mode,
                                          game=args.game, force=args.force)
                except (IndexError, ValueError) as e:
                    sys.exit(f"refusing to patch: {e}")
                for i, b, a in find_diff(rec, patched):
                    print(f"    offset {i:3d}: {b:02x} -> {a:02x}")
                if args.dry_run:
                    print("  (--dry-run: nothing written)")
                    return
                sess.overwrite(patched)
                return

            # -- reads --
            payload = (sess.game_score(args.game) if args.game else sess.full_list())
            if payload is None:
                return
            if args.dump:
                open(args.dump, "wb").write(payload)
                print(f"Wrote {len(payload)} bytes to {args.dump}")
            if args.raw:
                print(payload.hex(" "))
                return
            records = (decode_full_list(payload) if len(payload) > RECORD_LEN
                       else [decode_record(payload)])
            for r in records:
                if not r["game"]:
                    continue
                print(f"  Game: {r['game']:<14} Score: {r['score_raw']}"
                      f"   (name at {r['name_span']})")
            if args.upload:
                upload(args.endpoint, args.user, [r for r in records if r["game"]])
        finally:
            sess.disconnect()


if __name__ == "__main__":
    main()
