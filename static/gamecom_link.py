"""gamecom_link.py -- Game.com Web Link protocol, runtime-only.

This is the trimmed-down core extracted from the original reverse-engineering
toolkit (weblink_toolkit.py). It keeps only what the app needs to actually
talk to a cartridge and read/patch its records: frame encode/decode, record
decode, and the serial Link/Session objects. All of the CLI, hardware-probing
commands, the offline simulator, and the self-test harness were left behind
in the dev tool on purpose -- none of that is meant to ship to end users.

If you add support for a new game's cheat, that's the one thing you'll still
edit by hand here: add a `<game>_decode` / `<game>_pokes` pair, the way
`indy500_decode` / `indy500_pokes` already work below, and register it in
GAME_DECODERS.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from enum import IntEnum

try:
    import serial  # type: ignore
except ImportError:
    serial = None

# ============================================================================
# constants
# ============================================================================

BAUD = 9600
RECORD_LEN = 64
MAX_RETRIES = 3
RECORD_NAME_SEP = 0x01
DEFAULT_IDLE_GAP = 0.60
DEFAULT_TIMEOUT = 6.0


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
    Cmd.UNKNOWN_0E: "UNKNOWN 0E",
    Cmd.INVALID_COMMAND: "INVALID COMMAND",
}

#: `04 00` instead of `04 FB` on every real termination ack -- cosmetic
#: firmware quirk, the link still closes fine. See Session.disconnect().
QUIRK_FRAMES = {b"\x04\x00": Cmd.TERMINATION_ACKNOWLEDGE}


class ChecksumError(ValueError):
    def __init__(self, sent: int, calculated: int):
        self.sent = sent
        self.calculated = calculated
        super().__init__(f"CheckSum Sent: {sent}  CheckSum Calc'd: {calculated}")


# ============================================================================
# frame codec
# ============================================================================

def _checksum(data: bytes) -> int:
    return (255 - (sum(data) & 0xFF)) & 0xFF


def checksum(cmd: int, rest: bytes) -> int:
    return _checksum(bytes([cmd]) + rest)


def build(cmd: Cmd | int, payload: bytes = b"") -> bytes:
    cmd = int(cmd)
    complement = (255 - cmd) & 0xFF
    if not payload:
        return bytes([cmd, complement])
    rest = bytes([complement]) + payload
    return bytes([cmd]) + rest + bytes([checksum(cmd, rest)])


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
        return "".join(chr(b) if 32 <= b < 127 else "." for b in self.payload)

    def __str__(self) -> str:
        if not self.payload:
            return self.label
        if self.cmd == Cmd.SEND_TEXT_MESSAGE:
            return f"{self.label}  from game.com: {self.text!r}"
        return f"{self.label}  [{len(self.payload)} bytes] {self.payload.hex(' ')}"


def parse(raw: bytes) -> Frame:
    if len(raw) < 3:
        if len(raw) == 2 and raw[1] == (255 - raw[0]) & 0xFF:
            return Frame(cmd=raw[0], payload=b"")
        raise ValueError(f"frame too short: {raw.hex(' ')}")
    cmd, complement = raw[0], raw[1]
    if complement != (255 - cmd) & 0xFF:
        raise ValueError(f"byte 1 (0x{complement:02x}) is not 255-cmd")
    sent = raw[-1]
    calc = checksum(cmd, raw[1:-1])
    if sent != calc:
        raise ChecksumError(sent, calc)
    return Frame(cmd=cmd, payload=raw[2:-1])


def split_frames(buf: bytes) -> tuple[list[Frame], bytes]:
    """Carve as many complete frames as possible out of a byte stream.

    Never raises -- returns (frames, leftover_bytes). This is the tolerant
    path used everywhere a reply is read.
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
            break
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
# record decode / patch
# ============================================================================

def name_span(record: bytes) -> tuple[int, int] | None:
    """(start, end) of the ASCII name inside a record, anchored on the 0x01
    separator, or None."""
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
        "name_span": (start, sep),
        "score_raw": score_text,
        "score": int(score_text) if score_text.isdigit() else None,
        "raw_hex": record.hex(" "),
    }


def decode_full_list(payload: bytes) -> list[dict]:
    out = []
    for i in range(0, len(payload) - RECORD_LEN + 1, RECORD_LEN):
        d = decode_record(payload[i:i + RECORD_LEN])
        d["index"] = i // RECORD_LEN
        # Real game ids are always multiple characters (INDY500, FMEGAMIX,
        # ...). A single stray printable byte sitting next to the 0x01
        # separator in an otherwise-empty/garbage slot can decode as a
        # one-character "name" -- that's noise, not a game, so drop it.
        if d["game"] and len(d["game"]) >= 2:
            out.append(d)
    return out


def find_record_in_list(payload: bytes, game_id: str) -> tuple[int, bytes] | None:
    """Locate one game's 64-byte record inside a FULL_LIST_SEND payload by
    its game id (case/whitespace-insensitive). Returns (byte_offset, record)
    or None if that game isn't on this cartridge."""
    key = (game_id or "").upper().replace(" ", "")
    for i in range(0, len(payload) - RECORD_LEN + 1, RECORD_LEN):
        rec = payload[i:i + RECORD_LEN]
        d = decode_record(rec)
        if d["game"] and d["game"].upper().replace(" ", "") == key:
            return i, rec
    return None


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
            raise ValueError(f'expected off=val pairs, got {part!r}')
    if not out:
        raise ValueError("need at least one off=val pair")
    return out


def parse_command_pokes(command: str) -> dict[int, int]:
    """Parse a cheat's stored command string (e.g. --poke "25=0x0f") into
    an {offset: value} dict, the same syntax the CLI tool's --poke uses.
    Only --poke is supported right now -- that's the only command shape any
    cheat currently uses."""
    import shlex
    tokens = shlex.split(command or "")
    if len(tokens) >= 2 and tokens[0] == "--poke":
        return parse_pokes(tokens[1])
    raise ValueError(f"unsupported cheat command: {command!r}")


def apply_pokes(record: bytes, pokes: dict[int, int], force: bool = False) -> bytes:
    """Write arbitrary (offset -> byte) pairs into a record.

    Refuses to touch the name field (the cartridge identifies the record by
    it) unless force=True.
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
                f"offset {off} is inside this record's name field {guard}"
            )
        rec[off] = val
    return bytes(rec)


# ============================================================================
# frame constructors
# ============================================================================

def link_request() -> bytes:
    return build(Cmd.LINK_REQUEST)


def termination_request() -> bytes:
    return build(Cmd.TERMINATION_REQUEST)


def full_list_request() -> bytes:
    return build(Cmd.FULL_LIST_REQUEST)


def overwrite_game_score(record: bytes) -> bytes:
    if len(record) != RECORD_LEN:
        raise ValueError(f"record must be exactly {RECORD_LEN} bytes, got {len(record)}")
    return build(Cmd.OVERWRITE_GAME_SCORE, record)


# ============================================================================
# per-game record layouts, recovered by diffing real captures
# ============================================================================
# See weblink_toolkit.py (the dev tool) for how these were derived, and for
# the recipe to add a new one: dump a game's record, change its score on the
# device, dump again, diff the two, and turn what moved into a *_decode /
# *_pokes pair like this.

INDY_TICKS_PER_SECOND = 20


def indy500_decode(record: bytes) -> dict:
    pos = record[35]
    ticks = (record[36] << 8) | record[37]
    return {"position": pos, "ticks": ticks,
            "seconds": ticks / INDY_TICKS_PER_SECOND,
            "time": f"{int(ticks / INDY_TICKS_PER_SECOND) // 60}:"
                    f"{int(ticks / INDY_TICKS_PER_SECOND) % 60:02d}"}


def indy500_pokes(position: int, seconds: float) -> dict[int, int]:
    if not 0 <= position <= 255:
        raise ValueError("position must be 0..255")
    ticks = int(round(seconds * INDY_TICKS_PER_SECOND))
    if not 0 <= ticks <= 0xFFFF:
        raise ValueError(f"{seconds}s does not fit in 16 bits of ticks")
    text = f"P{position:02d} {int(seconds) // 60:02d}:{int(seconds) % 60:02d}"
    pokes = {35: position, 36: (ticks >> 8) & 0xFF, 37: ticks & 0xFF}
    for i, ch in enumerate(text[:9].ljust(9)):
        pokes[11 + i] = ord(ch)
    return pokes


GAME_DECODERS = {"INDY500": indy500_decode}


# ============================================================================
# transport
# ============================================================================

class Link:
    def __init__(self, port: str, timeout: float = 1.0,
                 idle_gap: float = DEFAULT_IDLE_GAP):
        if serial is None:
            raise RuntimeError("pyserial is required: pip install pyserial")
        self.ser = serial.Serial(
            port, baudrate=BAUD, bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
            timeout=timeout,
        )
        self.port = port
        self.idle_gap = idle_gap

    def close(self) -> None:
        self.ser.close()

    def __enter__(self) -> "Link":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def send(self, frame: bytes) -> None:
        self.ser.write(frame)
        self.ser.flush()

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
        return bytes(buf)

    def read_frame(self, timeout: float = DEFAULT_TIMEOUT,
                   idle_gap: float | None = None) -> Frame | None:
        raw = self.read_raw(timeout, idle_gap)
        if not raw:
            return None
        return parse(raw)

    def read_frames(self, timeout: float = DEFAULT_TIMEOUT,
                    idle_gap: float | None = None) -> list[Frame]:
        """Tolerant read: never raises, returns whatever frames are in there."""
        raw = self.read_raw(timeout, idle_gap)
        if not raw:
            return []
        frames, _leftover = split_frames(raw)
        return frames

    def expect(self, want: Cmd, timeout: float = DEFAULT_TIMEOUT) -> Frame | None:
        try:
            frame = self.read_frame(timeout)
        except (ValueError, ChecksumError):
            return None
        return frame if frame is not None and frame.cmd == want else None


class Session:
    def __init__(self, link: Link):
        self.link = link

    def connect(self, attempts: int = 3) -> bool:
        for _ in range(attempts):
            self.link.send(link_request())
            if self.link.expect(Cmd.LINK_ACKNOWLEDGE, timeout=3.0):
                return True
        return False

    def disconnect(self) -> None:
        self.link.send(termination_request())
        try:
            self.link.read_frames(timeout=2.0)
        except Exception:
            pass

    def full_list(self) -> bytes | None:
        for _attempt in range(MAX_RETRIES):
            self.link.send(full_list_request())
            try:
                f = self.link.expect(Cmd.FULL_LIST_SEND, timeout=15.0)
            except (ValueError, ChecksumError):
                continue
            if f is None:
                continue
            if len(f.payload) % RECORD_LEN:
                # a byte was dropped on the wire; retry
                continue
            return f.payload
        return None

    def overwrite(self, record: bytes) -> bool:
        """Send OVERWRITE_GAME_SCORE.

        The cartridge acknowledges a successful write with either
        COMMAND_ACKNOWLEDGEMENT (0x0B) or a bare `0C F3`
        (SEND_TEXT_MESSAGE header, empty payload) -- both confirmed on real
        hardware to mean the write went through.
        """
        self.link.send(overwrite_game_score(record))
        frames = self.link.read_frames(timeout=10.0)
        for f in frames:
            if f.cmd == Cmd.COMMAND_ACKNOWLEDGEMENT:
                return True
            if f.cmd == Cmd.SEND_TEXT_MESSAGE and not f.payload:
                return True
        return False
