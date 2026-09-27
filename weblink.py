#!/usr/bin/env python3
"""Web Link GUI -- a small local web app around gamecom_link.py.

Run it with:  python app.py
It starts a local server and opens your browser to it automatically.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, request

# gamecom_link.py lives in static/ alongside the rest of the app's assets --
# add it to the import path so it can still be imported as a normal module.
sys.path.insert(0, str(Path(__file__).parent / "static"))
import gamecom_link as wl

try:
    from serial.tools import list_ports
except ImportError:
    list_ports = None

app = Flask(__name__, static_url_path="", static_folder="static")

HOST = "127.0.0.1"

# Where scores get sent. The app posts here directly from Python, so the
# website tab that's showing the one-time code can pick up the results.
UPLOAD_URL = "https://gamecom.dreampipe.net/upload.php"
API_KEY = "TiGeRgAmEcOm1995@" #API KEY IS OPTIONAL
PORT = 5151

DATA_DIR = Path(__file__).parent / "data"
SCORES_JSON = DATA_DIR / "scores.json"
CHEATS_JSON = DATA_DIR / "cheats.json"


def _norm_id(game_id: str) -> str:
    """Normalize a game id for matching: 'Fighters Megamix' / 'FMEGAMIX' /
    ' fmegamix ' should all compare equal. Same normalization used elsewhere
    for matching a game name off the cartridge."""
    return (game_id or "").upper().replace(" ", "")


def _load_json(path: Path, default):
    if not path.exists():
        print(f"  [gamecom-gui] warning: {path} not found -- using empty defaults")
        return default
    try:
        with path.open("r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  [gamecom-gui] warning: could not read {path}: {e}")
        return default


def load_scores_config() -> dict[str, dict]:
    """data/scores.json -> {normalized gameId: {gameName, exclude}}"""
    raw = _load_json(SCORES_JSON, [])
    if isinstance(raw, dict):
        raw = [raw]
    out = {}
    for entry in raw:
        gid = entry.get("gameId")
        if gid:
            out[_norm_id(gid)] = entry
    return out


def load_cheats_config() -> list[dict]:
    """data/cheats.json -- accepts either one cheat object or a list of them."""
    raw = _load_json(CHEATS_JSON, [])
    if isinstance(raw, dict):
        raw = [raw]
    return raw


def build_games_and_cheats(decoded_records: list[dict]) -> tuple[list[dict], list[dict]]:
    """Turn the cartridge's raw decoded records into what the UI shows:

    - games: renamed via scores.json's gameName, with exclude=true games
      dropped from the high-scores table entirely.
    - cheats: cheats.json entries, but ONLY for games actually present on
      this cartridge right now -- a cheat for a game you don't own never
      shows up.
    """
    scores_cfg = load_scores_config()
    cheats_cfg = load_cheats_config()

    present_ids = {_norm_id(d["game"]) for d in decoded_records if d.get("game")}

    print(f"  [gamecom-gui] games detected on cartridge: "
          f"{[d.get('game') for d in decoded_records]}")
    print(f"  [gamecom-gui] normalized ids: {sorted(present_ids)}")
    print(f"  [gamecom-gui] scores.json ids: {sorted(scores_cfg.keys())}")
    print(f"  [gamecom-gui] cheats.json ids: "
          f"{sorted(_norm_id(c.get('gameId')) for c in cheats_cfg)}")

    games = []
    for d in decoded_records:
        gid = _norm_id(d["game"])
        cfg = scores_cfg.get(gid)
        if cfg and cfg.get("exclude"):
            continue
        score_raw = d.get("score_raw") or ""
        if score_raw.isdigit() and int(score_raw) < 1:
            # purely-numeric score of 0 / 00 / 0000 / etc -- nothing to show.
            # Non-numeric scores (e.g. Indy 500's "P01 07:35") are untouched
            # even if they contain a zero-ish substring.
            continue
        display_name = cfg["gameName"] if cfg and cfg.get("gameName") else d["game"]
        # "cart" is the name the cartridge itself reports (e.g. INDY500);
        # the website uses it to match the score to the right leaderboard.
        games.append({"game": display_name, "cart": d["game"], "score": d["score_raw"]})

    cheats = []
    for c in cheats_cfg:
        gid = _norm_id(c.get("gameId"))
        if gid and gid in present_ids:
            cheats.append({
                "gameId": c.get("gameId"),
                "game": c.get("gameName", c.get("gameId")),
                "cheat": c.get("description", ""),
            })
        else:
            print(f"  [gamecom-gui] cheat for {c.get('gameId')!r} skipped -- "
                  f"not found on this cartridge")

    return games, cheats


class Connection:
    """The one cartridge link this app can hold open at a time, shared
    across requests until /api/disconnect explicitly tears it down."""
    def __init__(self):
        self.port: str | None = None
        self.link: "wl.Link | None" = None
        self.session: "wl.Session | None" = None
        self.games: list[dict] = []

    @property
    def active(self) -> bool:
        return self.session is not None

    def teardown(self) -> None:
        if self.session is not None:
            try:
                self.session.disconnect()
            except Exception:
                pass
        if self.link is not None:
            try:
                self.link.close()
            except Exception:
                pass
        self.port = None
        self.link = None
        self.session = None
        self.games = []


conn = Connection()
conn_lock = threading.Lock()


@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/api/ports")
def api_ports():
    """Return the COM/serial ports currently present on this machine."""
    if list_ports is None:
        return jsonify({"ports": []})
    ports = [
        {"device": p.device, "description": p.description}
        for p in list_ports.comports()
    ]
    ports.sort(key=lambda p: p["device"])
    return jsonify({"ports": ports})


@app.post("/api/connect")
def api_connect():
    """Open the cartridge link and keep it open. The link stays connected
    across requests -- nothing sends TERMINATION_REQUEST until /api/disconnect
    is called (or a new /api/connect replaces a stale one)."""
    data = request.get_json(silent=True) or {}
    port = data.get("port")
    if not port:
        return jsonify({"success": False, "error": "No port selected."}), 400

    with conn_lock:
        if conn.active:
            conn.teardown()

        link = None
        try:
            link = wl.Link(port)
            sess = wl.Session(link)
            if not sess.connect():
                link.close()
                return jsonify({"success": False,
                                 "error": "Failed to connect to game.com."})
            payload = sess.full_list()
            if payload is None:
                sess.disconnect()
                link.close()
                return jsonify({"success": False,
                                 "error": "Failed to connect to game.com."})
            decoded = wl.decode_full_list(payload)
            games, cheats = build_games_and_cheats(decoded)

            conn.port, conn.link, conn.session = port, link, sess
            conn.games = games
            return jsonify({"success": True, "games": games, "cheats": cheats})
        except Exception:
            if link is not None:
                try:
                    link.close()
                except Exception:
                    pass
            return jsonify({"success": False,
                             "error": "Failed to connect to game.com."})


@app.post("/api/disconnect")
def api_disconnect():
    """The only place that sends TERMINATION_REQUEST."""
    with conn_lock:
        conn.teardown()
    return jsonify({"success": True})


@app.post("/api/download-cheat")
def api_download_cheat():
    """Apply one cheat's poke command to its matching record on the
    cartridge, over the connection /api/connect already opened -- no
    reconnect, no disconnect afterward."""
    data = request.get_json(silent=True) or {}
    game_id = data.get("gameId")
    if not game_id:
        return jsonify({"success": False, "error": "No cheat selected."}), 400

    with conn_lock:
        if not conn.active:
            return jsonify({"success": False, "error": "Not connected."})

        cheat = next(
            (c for c in load_cheats_config() if _norm_id(c.get("gameId")) == _norm_id(game_id)),
            None,
        )
        if cheat is None:
            return jsonify({"success": False,
                             "error": f"No cheat found for {game_id}."})

        try:
            pokes = wl.parse_command_pokes(cheat.get("command", ""))
        except ValueError as e:
            return jsonify({"success": False, "error": f"Bad cheat command: {e}"})

        try:
            payload = conn.session.full_list()
            if payload is None:
                return jsonify({"success": False,
                                 "error": "Lost connection to game.com."})
            found = wl.find_record_in_list(payload, game_id)
            if found is None:
                return jsonify({
                    "success": False,
                    "error": f"{cheat.get('gameName', game_id)} not found on this cartridge.",
                })
            _offset, record = found
            try:
                patched = wl.apply_pokes(record, pokes)
            except (IndexError, ValueError) as e:
                return jsonify({"success": False, "error": f"Refusing to patch: {e}"})
            ok = conn.session.overwrite(patched)
            if not ok:
                return jsonify({
                    "success": False,
                    "error": "Cartridge did not acknowledge the write.",
                })
            return jsonify({"success": True})
        except Exception:
            return jsonify({"success": False, "error": "Lost connection to game.com."})


@app.post("/api/submit-scores")
def api_submit_scores():
    """Send the scores from the last connect to upload.php along with the
    one-time code. The website tab showing that code notices the upload and
    moves to the results page by itself."""
    data = request.get_json(silent=True) or {}
    code = "".join(ch for ch in str(data.get("code", "")) if ch.isdigit())
    if len(code) != 6:
        return jsonify({"success": False,
                         "error": "Enter the 6-digit code from the website."})

    with conn_lock:
        games = list(conn.games)
    if not games:
        return jsonify({"success": False, "error": "No scores to submit."})

    body = urllib.parse.urlencode({
        "api_key": API_KEY,
        "code": code,
        "scores": json.dumps(games),
    }).encode("utf-8")
    req = urllib.request.Request(
        UPLOAD_URL, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "GameComWebLink/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            reply = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError):
        return jsonify({"success": False,
                         "error": "Couldn't reach the website. Check your internet connection."})
    except ValueError:
        return jsonify({"success": False,
                         "error": "The website sent back an unexpected reply."})

    if reply.get("success"):
        return jsonify({"success": True})
    return jsonify({"success": False,
                     "error": reply.get("error", "The website rejected the upload.")})


@app.post("/api/shutdown")
def api_shutdown():
    """Close button: cleanly disconnect from the game.com (sends
    TERMINATION_REQUEST if linked), then stop the Python program shortly
    after this reply has been sent back to the page."""
    with conn_lock:
        conn.teardown()
    print("  [gamecom-gui] Close button pressed -- shutting down.")
    threading.Timer(0.5, lambda: os._exit(0)).start()
    return jsonify({"success": True})


def _open_browser() -> None:
    time.sleep(0.7)
    webbrowser.open(f"http://{HOST}:{PORT}/")


class _DropDevServerWarning(logging.Filter):
    """Werkzeug prints its 'do not use in production' warning bundled
    together with the useful 'Running on http://...' line as one log
    message. This drops just that one line and keeps the rest."""
    _NEEDLE = "WARNING: This is a development server."

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and self._NEEDLE in record.msg:
            record.msg = "\n".join(
                line for line in record.msg.split("\n") if self._NEEDLE not in line
            ).lstrip("\n")
        return True


if __name__ == "__main__":
    import atexit
    logging.getLogger("werkzeug").addFilter(_DropDevServerWarning())
    atexit.register(conn.teardown)  # send TERMINATION_REQUEST if still open on exit
    threading.Thread(target=_open_browser, daemon=True).start()
    app.run(host=HOST, port=PORT, debug=False, threaded=False)
