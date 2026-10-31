"""Sumo backend: a shared live map for people who knowingly opt in.

Security model
- A device registers anonymously and receives a secret token (never shown to others).
- Other participants only ever see a *public id* (a one-way hash), never the device id/token.
- Re-registering an existing device requires presenting its current token.
- The map data (/api/people) requires a registered device token unless SUMO_PUBLIC_MAP=1.
"""
import hmac
import math
import os
import re
import secrets
import sqlite3
import time
import hashlib
from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, g, jsonify, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "static"), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024
app.json.sort_keys = False

DB_PATH = (
    os.environ.get("KINLY_DB_PATH")
    or os.environ.get("SUMO_DB_PATH")
    or os.path.join(BASE_DIR, "sumo.sqlite3")
)
PUBLIC_MAP = os.environ.get("SUMO_PUBLIC_MAP", "0") == "1"
HIDE_AFTER_HOURS = float(os.environ.get("SUMO_HIDE_AFTER_HOURS", "12"))
LIVE_SECONDS = 30
RECENT_SECONDS = 300
TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%f+00:00"


# ----------------------------------------------------------------- database
def connect_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with closing(connect_db()) as db:
        try:
            db.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            pass
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY,
                token TEXT NOT NULL UNIQUE,
                family_code TEXT NOT NULL DEFAULT 'PUBLIC',
                device_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS device_locations (
                device_id TEXT PRIMARY KEY,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                accuracy REAL,
                recorded_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
                FOREIGN KEY(device_id) REFERENCES devices(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_locations_received ON device_locations(received_at);
            """
        )
        cols = {r["name"] for r in db.execute("PRAGMA table_info(devices)")}
        if "stopped_ms" not in cols:
            db.execute("ALTER TABLE devices ADD COLUMN stopped_ms INTEGER NOT NULL DEFAULT 0")
        db.commit()


init_db()


# ------------------------------------------------------------------ helpers
def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime(TS_FORMAT)


def iso_from_ms(ms):
    return iso(datetime.fromtimestamp(ms / 1000.0, timezone.utc))


def parse_iso(text):
    try:
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def public_id(device_id):
    """Stable one-way id shown to other people. Reveals nothing usable for auth."""
    return hashlib.sha256(("sumo-public:" + device_id).encode()).hexdigest()[:12]


def clean_name(value):
    text = re.sub(r"[\x00-\x1f\x7f]", "", str(value or ""))
    return " ".join(text.split())


def bearer_token():
    value = request.headers.get("Authorization", "")
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return ""


def client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else request.remote_addr) or "unknown"


_hits = defaultdict(deque)
_last_prune = [time.monotonic()]


def rate_limited(key, limit, window_seconds):
    now = time.monotonic()
    q = _hits[key]
    while q and now - q[0] > window_seconds:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(now)
    if now - _last_prune[0] > 300:  # drop idle keys so memory cannot grow forever
        _last_prune[0] = now
        for k in [k for k, v in _hits.items() if not v or now - v[-1] > 3600]:
            _hits.pop(k, None)
    return False


def error(message, status, **extra):
    return jsonify({"ok": False, "error": message, **extra}), status


def current_device():
    token = bearer_token()
    if not token:
        return None
    with closing(connect_db()) as db:
        row = db.execute("SELECT * FROM devices WHERE token = ?", (token,)).fetchone()
    if row is not None and hmac.compare_digest(row["token"], token):
        return row
    return None


def device_auth_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        device = current_device()
        if device is None:
            return error("Device registration required", 401, code="invalid_token")
        g.device = device
        return fn(*args, **kwargs)

    return wrapper


_last_cleanup = [0.0]


def maybe_cleanup(db):
    if time.monotonic() - _last_cleanup[0] < 3600:
        return
    _last_cleanup[0] = time.monotonic()
    db.execute("DELETE FROM device_locations WHERE recorded_at < ?", (iso(utcnow() - timedelta(days=7)),))
    db.execute(
        "DELETE FROM devices WHERE created_at < ? AND id NOT IN (SELECT device_id FROM device_locations)",
        (iso(utcnow() - timedelta(days=30)),),
    )
    db.commit()


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["X-Frame-Options"] = "DENY"
    if resp.mimetype == "text/html":
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: https://tile.openstreetmap.org https://*.tile.openstreetmap.org; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
        )
        resp.headers["Cache-Control"] = "no-store"
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------------- API
@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "Sumo", "time": iso(utcnow())})


@app.post("/api/device/register")
def register_device():
    if rate_limited("reg:" + client_ip(), 30, 3600):
        return error("Too many sign-ups from this network. Try again later.", 429)
    data = request.get_json(silent=True) or {}
    name = clean_name(data.get("name"))
    if len(name) < 2 or len(name) > 40:
        return error("Enter a name between 2 and 40 characters.", 400)

    claimed_id = str(data.get("device_id", "")).strip()[:64]
    presented = bearer_token()
    with closing(connect_db()) as db:
        row = None
        if claimed_id and presented:
            candidate = db.execute("SELECT * FROM devices WHERE id = ?", (claimed_id,)).fetchone()
            # The id alone is NOT enough: the caller must also hold the matching secret token.
            if candidate is not None and hmac.compare_digest(candidate["token"], presented):
                row = candidate
        if row is not None:
            device_id, token = row["id"], row["token"]
            db.execute("UPDATE devices SET device_name = ? WHERE id = ?", (name, device_id))
        else:
            device_id = secrets.token_urlsafe(18)
            token = secrets.token_urlsafe(32)
            db.execute(
                "INSERT INTO devices(id, token, family_code, device_name, created_at) VALUES(?,?,?,?,?)",
                (device_id, token, "PUBLIC", name, iso(utcnow())),
            )
        maybe_cleanup(db)
        db.commit()
    return jsonify(
        {"ok": True, "device_id": device_id, "token": token, "public_id": public_id(device_id), "device_name": name}
    )


@app.post("/api/location")
@device_auth_required
def update_location():
    if rate_limited("loc:" + g.device["id"], 120, 60):
        return error("Sending locations too fast.", 429)
    data = request.get_json(silent=True) or {}
    now_ms = int(time.time() * 1000)
    try:
        lat = float(data.get("latitude"))
        lon = float(data.get("longitude"))
        accuracy = float(data.get("accuracy", 0) or 0)
        stamp = int(data.get("timestamp", now_ms))
    except (TypeError, ValueError):
        return error("Invalid location payload", 400)
    if not (math.isfinite(lat) and math.isfinite(lon) and math.isfinite(accuracy)):
        return error("Invalid location payload", 400)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return error("Coordinates out of range", 400)
    accuracy = min(max(accuracy, 0.0), 100000.0)
    if stamp > now_ms + 120_000:  # phone clock ahead: treat as "now"
        stamp = now_ms
    if stamp < now_ms - 30 * 86_400_000:
        return error("Location sample is too old", 400)

    # The user pressed Stop at stopped_ms: late/in-flight samples from before that moment are dropped.
    if stamp <= (g.device["stopped_ms"] or 0):
        return jsonify({"ok": True, "applied": False, "reason": "sharing_stopped"})

    with closing(connect_db()) as db:
        cur = db.execute(
            """INSERT INTO device_locations(device_id,latitude,longitude,accuracy,recorded_at,received_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(device_id) DO UPDATE SET latitude=excluded.latitude, longitude=excluded.longitude,
                   accuracy=excluded.accuracy, recorded_at=excluded.recorded_at, received_at=excluded.received_at
               WHERE excluded.recorded_at >= device_locations.recorded_at""",
            (g.device["id"], lat, lon, accuracy, iso_from_ms(stamp), iso(utcnow())),
        )
        applied = cur.rowcount > 0  # False when an older (out-of-order) sample arrived late
        db.commit()
    return jsonify({"ok": True, "applied": applied})


@app.post("/api/location/stop")
@device_auth_required
def stop_sharing():
    """Remove this device from the map immediately and ignore samples older than the stop moment."""
    if rate_limited("stop:" + g.device["id"], 30, 60):
        return error("Too many requests.", 429)
    data = request.get_json(silent=True) or {}
    now_ms = int(time.time() * 1000)
    try:
        stopped_ms = int(data.get("timestamp", now_ms))
    except (TypeError, ValueError):
        stopped_ms = now_ms
    stopped_ms = max(0, min(stopped_ms, now_ms + 86_400_000))
    with closing(connect_db()) as db:
        db.execute("UPDATE devices SET stopped_ms = MAX(stopped_ms, ?) WHERE id = ?", (stopped_ms, g.device["id"]))
        db.execute("DELETE FROM device_locations WHERE device_id = ?", (g.device["id"],))
        db.commit()
    return jsonify({"ok": True})


@app.get("/api/people")
def people():
    me = current_device()
    if me is None and not PUBLIC_MAP:
        return error("Open the map from the Sumo app.", 401, code="invalid_token")
    if rate_limited("people:" + client_ip(), 600, 60):
        return error("Too many requests.", 429)
    with closing(connect_db()) as db:
        rows = db.execute(
            """SELECT d.id, d.device_name, l.latitude, l.longitude, l.accuracy, l.recorded_at
               FROM devices d JOIN device_locations l ON l.device_id = d.id"""
        ).fetchall()
    now = utcnow()
    my_public = public_id(me["id"]) if me is not None else None
    members = []
    for r in rows:
        recorded = parse_iso(r["recorded_at"])
        if recorded is None:
            continue
        age = max(0.0, (now - recorded).total_seconds())
        if age > HIDE_AFTER_HOURS * 3600:
            continue
        pid = public_id(r["id"])
        members.append(
            {
                "id": pid,
                "name": r["device_name"],
                "latitude": r["latitude"],
                "longitude": r["longitude"],
                "accuracy": r["accuracy"],
                "recorded_at": r["recorded_at"],
                "age_seconds": int(age),
                "status": "live" if age < LIVE_SECONDS else "recent" if age < RECENT_SECONDS else "stale",
                "has_location": True,
                "is_me": pid == my_public,
            }
        )
    members.sort(key=lambda m: (not m["is_me"], m["name"].casefold()))
    return jsonify({"ok": True, "server_time_ms": int(time.time() * 1000), "members": members})


# ---------------------------------------------------------------- web page
@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.errorhandler(404)
def not_found(_e):
    return error("Not found", 404)


@app.errorhandler(405)
def bad_method(_e):
    return error("Method not allowed", 405)


@app.errorhandler(413)
def too_large(_e):
    return error("Request too large", 413)


@app.errorhandler(500)
def server_error(_e):
    return error("Server error", 500)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
