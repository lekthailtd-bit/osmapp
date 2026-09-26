"""Central field-distribution storage, authentication and sync.

The deployment is intentionally small: SQLite is the canonical store and each
request opens a short-lived connection. WAL mode lets GPS syncs and management
reads coexist without introducing a separate database service.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import uuid
from datetime import timedelta
from functools import wraps
from pathlib import Path
from typing import Any, Callable, TypeVar, cast

import click
from flask import Blueprint, Flask, g, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

bp = Blueprint("distribution", __name__, url_prefix="/service/field")
F = TypeVar("F", bound=Callable[..., Any])


@bp.errorhandler(ValueError)
def _validation_error(exc: ValueError):
    return jsonify(error=str(exc)), 400
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_STATUSES = {"planned", "active", "paused", "finished", "cancelled"}
_CAMPAIGN_STATUSES = {"draft", "active", "finished", "archived"}
_ALLOWED_WALK_TRANSITIONS = {
    "planned": {"planned", "active", "cancelled"},
    "active": {"active", "paused", "finished", "cancelled"},
    "paused": {"paused", "active", "finished", "cancelled"},
    "finished": {"finished"},
    "cancelled": {"cancelled"},
}

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS people (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL UNIQUE REFERENCES people(id),
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'field' CHECK(role IN ('admin', 'field')),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    starts_at TEXT,
    ends_at TEXT,
    notes TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    last_write_id TEXT,
    updated_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaign_territories (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    source_territory_id TEXT NOT NULL,
    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
    label TEXT NOT NULL,
    geometry_json TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    revision INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    UNIQUE(campaign_id, source_territory_id)
);
CREATE INDEX IF NOT EXISTS idx_campaign_territories_campaign
    ON campaign_territories(campaign_id);

CREATE TABLE IF NOT EXISTS distribution_sessions (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
    territory_id TEXT REFERENCES campaign_territories(id),
    project_id TEXT REFERENCES projects(id),
    started_by_user_id TEXT NOT NULL REFERENCES users(id),
    device_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    leaflet_count INTEGER,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_distribution_sessions_campaign
    ON distribution_sessions(campaign_id, started_at);

CREATE TABLE IF NOT EXISTS session_participants (
    session_id TEXT NOT NULL REFERENCES distribution_sessions(id) ON DELETE CASCADE,
    person_id TEXT NOT NULL REFERENCES people(id),
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(session_id, person_id)
);

CREATE TABLE IF NOT EXISTS session_events (
    session_id TEXT NOT NULL REFERENCES distribution_sessions(id) ON DELETE CASCADE,
    event_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN ('start', 'pause', 'resume', 'finish')),
    recorded_at TEXT NOT NULL,
    PRIMARY KEY(session_id, event_id)
);

CREATE TABLE IF NOT EXISTS gps_points (
    session_id TEXT NOT NULL REFERENCES distribution_sessions(id) ON DELETE CASCADE,
    point_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    lat REAL NOT NULL CHECK(lat BETWEEN -90 AND 90),
    lon REAL NOT NULL CHECK(lon BETWEEN -180 AND 180),
    accuracy REAL,
    recorded_at TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY(session_id, point_id)
);
CREATE INDEX IF NOT EXISTS idx_gps_points_session_seq
    ON gps_points(session_id, seq);

CREATE TABLE IF NOT EXISTS road_coverage (
    session_id TEXT NOT NULL REFERENCES distribution_sessions(id) ON DELETE CASCADE,
    road_key TEXT NOT NULL,
    road_name TEXT,
    covered_m REAL NOT NULL DEFAULT 0,
    total_m REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(session_id, road_key)
);
"""


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _id(value: Any, *, field: str = "id") -> str:
    value = str(value or "")
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"Invalid {field}.")
    return value


def _data_dir() -> Path:
    explicit = os.environ.get("OSMAPP_DB_PATH")
    if explicit:
        return Path(explicit).expanduser().resolve().parent
    return Path(os.environ.get("OSMAPP_DATA_DIR", ".osmapp-data")).expanduser().resolve()


def database_path() -> Path:
    explicit = os.environ.get("OSMAPP_DB_PATH")
    if explicit:
        return Path(explicit).expanduser().resolve()
    return _data_dir() / "osmapp.sqlite3"


def _secret_key() -> str:
    configured = os.environ.get("OSMAPP_SECRET_KEY")
    if configured:
        return configured
    path = _data_dir() / "session-secret"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    value = secrets.token_hex(32)
    path.write_text(value, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return value


def connect() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 10000")
    db.execute("PRAGMA journal_mode = WAL")
    return db


def init_database() -> None:
    with connect() as db:
        db.executescript(SCHEMA)
        columns = {row["name"] for row in db.execute("PRAGMA table_info(session_participants)")}
        if "position" not in columns:
            db.execute(
                "ALTER TABLE session_participants ADD COLUMN position INTEGER NOT NULL DEFAULT 0"
            )
            rows = list(db.execute(
                "SELECT rowid,session_id FROM session_participants ORDER BY session_id,rowid"
            ))
            positions: dict[str, int] = {}
            for row in rows:
                position = positions.get(row["session_id"], 0)
                db.execute(
                    "UPDATE session_participants SET position=? WHERE rowid=?",
                    (position, row["rowid"]),
                )
                positions[row["session_id"]] = position + 1


def _json() -> dict[str, Any]:
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object.")
    return value


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _editable_walk(
    db: sqlite3.Connection, session_id: str
) -> tuple[sqlite3.Row | None, tuple[Any, int] | None]:
    """Return a walk the current user may mutate, or an HTTP error tuple."""
    row = db.execute(
        "SELECT * FROM distribution_sessions WHERE id=?", (session_id,)
    ).fetchone()
    if row is None:
        return None, (jsonify(error="Walk session not found."), 404)
    user = g.field_user
    if user["role"] != "admin" and row["started_by_user_id"] != user["id"]:
        return None, (jsonify(error="This walk belongs to another operator."), 403)
    return row, None


def _current_user() -> dict[str, Any] | None:
    user_id = session.get("user_id")
    if not user_id:
        return None
    with connect() as db:
        row = db.execute(
            """SELECT u.id, u.username, u.role, u.person_id, p.name
               FROM users u JOIN people p ON p.id = u.person_id
               WHERE u.id = ? AND u.active = 1 AND p.active = 1""",
            (user_id,),
        ).fetchone()
    return _row(row)


def require_auth(fn: F) -> F:
    @wraps(fn)
    def wrapped(*args: Any, **kwargs: Any):
        user = _current_user()
        if not user:
            return jsonify(error="Authentication required."), 401
        g.field_user = user
        return fn(*args, **kwargs)

    return cast(F, wrapped)


def require_admin(fn: F) -> F:
    @wraps(fn)
    @require_auth
    def wrapped(*args: Any, **kwargs: Any):
        if g.field_user["role"] != "admin":
            return jsonify(error="Administrator access required."), 403
        return fn(*args, **kwargs)

    return cast(F, wrapped)


def _people(db: sqlite3.Connection, active_only: bool = True) -> list[dict[str, Any]]:
    sql = """SELECT p.id, p.name, p.active, u.id AS user_id, u.username, u.role
             FROM people p LEFT JOIN users u ON u.person_id = p.id"""
    if active_only:
        sql += " WHERE p.active = 1"
    sql += " ORDER BY p.name COLLATE NOCASE"
    return [dict(r) for r in db.execute(sql)]


@bp.get("/auth/status")
def auth_status():
    with connect() as db:
        configured = bool(db.execute("SELECT 1 FROM users LIMIT 1").fetchone())
        user = _current_user()
        people = _people(db) if user else []
    return jsonify(configured=configured, user=user, people=people)


@bp.post("/auth/bootstrap")
def auth_bootstrap():
    data = _json()
    username = str(data.get("username", "")).strip()
    name = str(data.get("name", "")).strip()
    password = str(data.get("password", ""))
    if len(username) < 2 or len(name) < 2 or len(password) < 8:
        return jsonify(error="Name/username must be at least 2 characters and password at least 8."), 400
    now = _now()
    person_id, user_id = _new_id("person"), _new_id("user")
    try:
        with connect() as db:
            if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                return jsonify(error="Initial account already exists."), 409
            db.execute("INSERT INTO people(id,name,created_at) VALUES(?,?,?)", (person_id, name, now))
            db.execute(
                """INSERT INTO users(id,person_id,username,password_hash,role,created_at)
                   VALUES(?,?,?,?, 'admin', ?)""",
                (user_id, person_id, username, generate_password_hash(password), now),
            )
    except sqlite3.IntegrityError:
        return jsonify(error="That username is already in use."), 409
    session.clear()
    session["user_id"] = user_id
    session.permanent = True
    return jsonify(user=_current_user()), 201


@bp.post("/auth/login")
def auth_login():
    data = _json()
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    with connect() as db:
        row = db.execute(
            """SELECT u.id, u.password_hash FROM users u JOIN people p ON p.id=u.person_id
               WHERE u.username=? COLLATE NOCASE AND u.active=1 AND p.active=1""",
            (username,),
        ).fetchone()
        if not row or not check_password_hash(row["password_hash"], password):
            return jsonify(error="Incorrect username or password."), 401
        db.execute("UPDATE users SET last_login_at=? WHERE id=?", (_now(), row["id"]))
    session.clear()
    session["user_id"] = row["id"]
    session.permanent = True
    return jsonify(user=_current_user())


@bp.post("/auth/logout")
def auth_logout():
    session.clear()
    return jsonify(ok=True)


@bp.route("/people", methods=["GET", "POST"])
@require_auth
def people():
    if request.method == "GET":
        with connect() as db:
            return jsonify(people=_people(db, active_only=request.args.get("all") != "1"))
    if g.field_user["role"] != "admin":
        return jsonify(error="Administrator access required."), 403
    data = _json()
    name = str(data.get("name", "")).strip()
    if len(name) < 2:
        return jsonify(error="Name is required."), 400
    now, person_id = _now(), _new_id("person")
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    role = str(data.get("role", "field"))
    if role not in {"field", "admin"}:
        return jsonify(error="Invalid role."), 400
    if username and len(password) < 8:
        return jsonify(error="Accounts require a password of at least 8 characters."), 400
    try:
        with connect() as db:
            db.execute("INSERT INTO people(id,name,created_at) VALUES(?,?,?)", (person_id, name, now))
            user_id = None
            if username:
                user_id = _new_id("user")
                db.execute(
                    """INSERT INTO users(id,person_id,username,password_hash,role,created_at)
                       VALUES(?,?,?,?,?,?)""",
                    (user_id, person_id, username, generate_password_hash(password), role, now),
                )
    except sqlite3.IntegrityError:
        return jsonify(error="That username is already in use."), 409
    return jsonify(person={"id": person_id, "name": name, "user_id": user_id}), 201


@bp.route("/campaigns", methods=["GET", "POST"])
@require_auth
def campaigns():
    with connect() as db:
        if request.method == "GET":
            rows = db.execute(
                """SELECT * FROM campaigns
                   WHERE (?='' OR status=?) ORDER BY created_at DESC""",
                (request.args.get("status", ""), request.args.get("status", "")),
            )
            return jsonify(campaigns=[dict(r) for r in rows])
        data = _json()
        name = str(data.get("name", "")).strip()
        status = str(data.get("status", "draft"))
        if not name or status not in _CAMPAIGN_STATUSES:
            return jsonify(error="Campaign name/status is invalid."), 400
        now, campaign_id = _now(), _new_id("campaign")
        db.execute(
            """INSERT INTO campaigns(id,name,status,starts_at,ends_at,notes,created_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                campaign_id, name, status, data.get("starts_at"), data.get("ends_at"),
                str(data.get("notes", "")), g.field_user["id"], now, now,
            ),
        )
    return jsonify(campaign={"id": campaign_id, "name": name, "status": status, "revision": 1}), 201


@bp.patch("/campaigns/<campaign_id>")
@require_auth
def update_campaign(campaign_id: str):
    try:
        campaign_id = _id(campaign_id)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    data = _json()
    with connect() as db:
        current = db.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not current:
            return jsonify(error="Campaign not found."), 404
        expected = data.get("expected_revision")
        if expected is not None and int(expected) != current["revision"]:
            return jsonify(error="Campaign changed on another device.", current=dict(current)), 409
        status = str(data.get("status", current["status"]))
        if status not in _CAMPAIGN_STATUSES:
            return jsonify(error="Invalid campaign status."), 400
        revision = current["revision"] + 1
        db.execute(
            """UPDATE campaigns SET name=?, status=?, starts_at=?, ends_at=?, notes=?,
               updated_at=?, revision=? WHERE id=?""",
            (
                str(data.get("name", current["name"])).strip() or current["name"],
                status, data.get("starts_at", current["starts_at"]),
                data.get("ends_at", current["ends_at"]), str(data.get("notes", current["notes"])),
                _now(), revision, campaign_id,
            ),
        )
        updated = db.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    return jsonify(campaign=dict(updated))


@bp.route("/projects", methods=["GET", "POST"])
@require_auth
def projects():
    with connect() as db:
        if request.method == "GET":
            rows = db.execute(
                "SELECT id,name,revision,updated_by,created_at,updated_at FROM projects ORDER BY updated_at DESC"
            )
            return jsonify(projects=[dict(r) for r in rows])
        data = _json()
        name = str(data.get("name", "")).strip()
        payload = data.get("payload")
        if not name or not isinstance(payload, dict):
            return jsonify(error="Project name and payload are required."), 400
        try:
            project_id = _id(data.get("id") or _new_id("project"), field="project id")
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        write_id = str(data.get("write_id") or "") or None
        existing = db.execute(
            "SELECT id,name,revision FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if existing:
            return jsonify(project=dict(existing), duplicate=True)
        now = _now()
        db.execute(
            """INSERT INTO projects(
               id,name,payload_json,revision,last_write_id,updated_by,created_at,updated_at)
               VALUES(?,?,?,1,?,?,?,?)""",
            (
                project_id, name, json.dumps(payload, separators=(",", ":")),
                write_id, g.field_user["id"], now, now,
            ),
        )
    return jsonify(project={"id": project_id, "name": name, "revision": 1}), 201


@bp.get("/projects/<project_id>")
@require_auth
def get_project(project_id: str):
    with connect() as db:
        row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row:
        return jsonify(error="Project not found."), 404
    out = dict(row)
    out["payload"] = json.loads(out.pop("payload_json"))
    return jsonify(project=out)


@bp.put("/projects/<project_id>")
@require_auth
def put_project(project_id: str):
    data = _json()
    payload = data.get("payload")
    if not isinstance(payload, dict):
        return jsonify(error="Project payload is required."), 400
    with connect() as db:
        current = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not current:
            return jsonify(error="Project not found."), 404
        write_id = str(data.get("write_id") or "")
        if write_id and current["last_write_id"] == write_id:
            return jsonify(
                project={"id": project_id, "revision": current["revision"]},
                duplicate=True,
            )
        expected = int(data.get("expected_revision", -1))
        if expected != current["revision"]:
            return jsonify(
                error="Project changed on another device.",
                current_revision=current["revision"],
            ), 409
        revision = current["revision"] + 1
        db.execute(
            """UPDATE projects SET name=?,payload_json=?,revision=?,last_write_id=?,
               updated_by=?,updated_at=? WHERE id=?""",
            (
                str(data.get("name", current["name"])).strip() or current["name"],
                json.dumps(payload, separators=(",", ":")), revision, write_id or None,
                g.field_user["id"], _now(), project_id,
            ),
        )
    return jsonify(project={"id": project_id, "revision": revision})


@bp.route("/territories", methods=["GET"])
@require_auth
def territories():
    campaign_id = request.args.get("campaign_id", "")
    with connect() as db:
        rows = db.execute(
            """SELECT id,campaign_id,source_territory_id,project_id,label,geometry_json,active,revision,updated_at
               FROM campaign_territories WHERE campaign_id=? AND active=1 ORDER BY label COLLATE NOCASE""",
            (campaign_id,),
        )
        items = []
        for row in rows:
            item = dict(row)
            item["geometry"] = json.loads(item.pop("geometry_json"))
            items.append(item)
    return jsonify(territories=items)


@bp.put("/campaigns/<campaign_id>/territories/<territory_id>")
@require_auth
def put_territory(campaign_id: str, territory_id: str):
    data = _json()
    try:
        campaign_id, territory_id = _id(campaign_id), _id(territory_id)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    geometry = data.get("geometry")
    label = str(data.get("label", territory_id)).strip()
    if not isinstance(geometry, dict) or not label:
        return jsonify(error="Territory label and geometry are required."), 400
    with connect() as db:
        if not db.execute("SELECT 1 FROM campaigns WHERE id=?", (campaign_id,)).fetchone():
            return jsonify(error="Campaign not found."), 404
        current = db.execute(
            """SELECT id,revision FROM campaign_territories
               WHERE campaign_id=? AND source_territory_id=?""",
            (campaign_id, territory_id),
        ).fetchone()
        association_id = current["id"] if current else _new_id("territory")
        revision = (current["revision"] + 1) if current else 1
        now = _now()
        if current:
            db.execute(
                """UPDATE campaign_territories SET project_id=?,label=?,geometry_json=?,
                   active=1,revision=?,updated_at=? WHERE id=?""",
                (
                    data.get("project_id"), label,
                    json.dumps(geometry, separators=(",", ":")),
                    revision, now, association_id,
                ),
            )
        else:
            db.execute(
                """INSERT INTO campaign_territories(
                   id,campaign_id,source_territory_id,project_id,label,geometry_json,
                   active,revision,updated_at) VALUES(?,?,?,?,?,?,1,?,?)""",
                (
                    association_id, campaign_id, territory_id, data.get("project_id"),
                    label, json.dumps(geometry, separators=(",", ":")), revision, now,
                ),
            )
    return jsonify(
        territory={
            "id": association_id,
            "source_territory_id": territory_id,
            "revision": revision,
        }
    )


def _participant_rows(db: sqlite3.Connection, session_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {sid: [] for sid in session_ids}
    if not session_ids:
        return result
    marks = ",".join("?" for _ in session_ids)
    rows = db.execute(
        f"""SELECT sp.session_id,p.id,p.name FROM session_participants sp
            JOIN people p ON p.id=sp.person_id WHERE sp.session_id IN ({marks})
            ORDER BY sp.session_id,sp.position,p.name COLLATE NOCASE""",
        session_ids,
    )
    for row in rows:
        result[row["session_id"]].append({"id": row["id"], "name": row["name"]})
    return result


@bp.route("/sessions", methods=["GET", "POST"])
@require_auth
def walk_sessions():
    if request.method == "POST":
        data = _json()
        try:
            session_id = _id(data.get("id") or _new_id("walk"), field="session id")
            campaign_id = _id(data.get("campaign_id"), field="campaign id")
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        raw_participants = data.get("participant_ids", [])
        if not isinstance(raw_participants, list):
            return jsonify(error="participant_ids must be an array."), 400
        participant_ids = list(dict.fromkeys(str(x) for x in raw_participants))
        if not participant_ids:
            participant_ids = [g.field_user["person_id"]]
        started_at, now = str(data.get("started_at") or _now()), _now()
        device_id = str(data.get("device_id", "browser"))[:128] or "browser"
        claimed_operator = data.get("started_by_user_id")
        if claimed_operator is not None and str(claimed_operator) != g.field_user["id"]:
            return jsonify(error="Offline walk belongs to another operator."), 403
        territory_id = data.get("territory_id")
        project_id = data.get("project_id")
        with connect() as db:
            existing = db.execute("SELECT * FROM distribution_sessions WHERE id=?", (session_id,)).fetchone()
            if existing:
                if (
                    g.field_user["role"] != "admin"
                    and existing["started_by_user_id"] != g.field_user["id"]
                ):
                    return jsonify(error="That offline walk id belongs to another operator."), 403
                return jsonify(walk=dict(existing), duplicate=True)
            if not db.execute("SELECT 1 FROM campaigns WHERE id=?", (campaign_id,)).fetchone():
                return jsonify(error="Campaign not found."), 404
            if territory_id:
                territory = db.execute(
                    "SELECT campaign_id FROM campaign_territories WHERE id=? AND active=1",
                    (territory_id,),
                ).fetchone()
                if territory is None or territory["campaign_id"] != campaign_id:
                    return jsonify(error="Territory is not part of this campaign."), 400
            if project_id and not db.execute(
                "SELECT 1 FROM projects WHERE id=?", (project_id,)
            ).fetchone():
                return jsonify(error="Project not found."), 400
            marks = ",".join("?" for _ in participant_ids)
            found = {r["id"] for r in db.execute(
                f"SELECT id FROM people WHERE active=1 AND id IN ({marks})", participant_ids
            )}
            if found != set(participant_ids):
                return jsonify(error="One or more participants are unavailable."), 400
            db.execute(
                """INSERT INTO distribution_sessions(
                   id,campaign_id,territory_id,project_id,started_by_user_id,device_id,status,
                   started_at,created_at,updated_at) VALUES(?,?,?,?,?,?, 'active', ?,?,?)""",
                (
                    session_id, campaign_id, territory_id, project_id,
                    g.field_user["id"], device_id, started_at, now, now,
                ),
            )
            db.executemany(
                "INSERT INTO session_participants(session_id,person_id,position) VALUES(?,?,?)",
                [(session_id, p, i) for i, p in enumerate(participant_ids)],
            )
            db.execute(
                "INSERT INTO session_events(session_id,event_id,event_type,recorded_at) VALUES(?,?, 'start', ?)",
                (session_id, _new_id("event"), started_at),
            )
        return jsonify(walk={"id": session_id, "status": "active", "started_at": started_at}), 201

    campaign_id = request.args.get("campaign_id", "")
    with connect() as db:
        sql = """SELECT ds.*, c.name AS campaign_name, ct.label AS territory_label,
                 (SELECT count(*) FROM gps_points gp WHERE gp.session_id=ds.id) AS point_count
                 FROM distribution_sessions ds
                 JOIN campaigns c ON c.id=ds.campaign_id
                 LEFT JOIN campaign_territories ct ON ct.id=ds.territory_id"""
        params: list[Any] = []
        if campaign_id:
            sql += " WHERE ds.campaign_id=?"
            params.append(campaign_id)
        sql += " ORDER BY ds.started_at DESC LIMIT 500"
        items = [dict(r) for r in db.execute(sql, params)]
        participants = _participant_rows(db, [x["id"] for x in items])
        for item in items:
            item["participants"] = participants.get(item["id"], [])
    return jsonify(sessions=items)


@bp.patch("/sessions/<session_id>")
@require_auth
def update_walk_session(session_id: str):
    data = _json()
    status = str(data.get("status", ""))
    if status not in _STATUSES:
        return jsonify(error="Invalid session status."), 400
    with connect() as db:
        current, denied = _editable_walk(db, session_id)
        if denied:
            return denied
        assert current is not None
        event_id = str(data.get("event_id") or "")
        if event_id:
            try:
                event_id = _id(event_id, field="event id")
            except ValueError as exc:
                return jsonify(error=str(exc)), 400
            if db.execute(
                "SELECT 1 FROM session_events WHERE session_id=? AND event_id=?",
                (session_id, event_id),
            ).fetchone():
                return jsonify(
                    walk={
                        "id": session_id,
                        "status": current["status"],
                        "revision": current["revision"],
                        "finished_at": current["finished_at"],
                    },
                    duplicate=True,
                )
        if status not in _ALLOWED_WALK_TRANSITIONS.get(current["status"], set()):
            return jsonify(
                error=f"Cannot move a {current['status']} walk to {status}."
            ), 409
        expected = data.get("expected_revision")
        if expected is not None and int(expected) != current["revision"]:
            return jsonify(error="Walk session changed on another device.", current=dict(current)), 409
        revision, now = current["revision"] + 1, _now()
        finished_at = data.get("finished_at", current["finished_at"])
        event_type = None
        if status != current["status"]:
            event_type = {"paused": "pause", "active": "resume", "finished": "finish"}.get(status)
        if status == "finished" and not finished_at:
            finished_at = now
        leaflet_count = data.get("leaflet_count", current["leaflet_count"])
        if leaflet_count is not None:
            leaflet_count = max(0, int(leaflet_count))
        db.execute(
            """UPDATE distribution_sessions SET status=?,finished_at=?,leaflet_count=?,
               revision=?,updated_at=? WHERE id=?""",
            (status, finished_at, leaflet_count, revision, now, session_id),
        )
        if event_type:
            db.execute(
                "INSERT INTO session_events(session_id,event_id,event_type,recorded_at) VALUES(?,?,?,?)",
                (
                    session_id, event_id or _new_id("event"), event_type,
                    str(data.get("recorded_at") or now),
                ),
            )
    return jsonify(walk={"id": session_id, "status": status, "revision": revision, "finished_at": finished_at})


@bp.post("/sessions/<session_id>/points")
@require_auth
def add_points(session_id: str):
    data = _json()
    points = data.get("points")
    if not isinstance(points, list) or len(points) > 1000:
        return jsonify(error="points must be an array of at most 1000 items."), 400
    inserted = 0
    now = _now()
    try:
        with connect() as db:
            current, denied = _editable_walk(db, session_id)
            if denied:
                return denied
            assert current is not None
            for point in points:
                if not isinstance(point, dict):
                    raise ValueError("Invalid GPS point.")
                point_id = _id(point.get("id"), field="point id")
                lat, lon = float(point["lat"]), float(point["lon"])
                accuracy = point.get("accuracy")
                accuracy = None if accuracy is None else max(0.0, float(accuracy))
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError("GPS coordinate outside valid range.")
                cursor = db.execute(
                    """INSERT OR IGNORE INTO gps_points(
                       session_id,point_id,seq,lat,lon,accuracy,recorded_at,synced_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        session_id, point_id, int(point.get("seq", 0)), lat, lon, accuracy,
                        str(point.get("recorded_at") or now), now,
                    ),
                )
                inserted += cursor.rowcount
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(inserted=inserted, received=len(points))


@bp.get("/coverage")
@require_auth
def coverage():
    campaign_id = request.args.get("campaign_id", "")
    if not campaign_id:
        return jsonify(error="campaign_id is required."), 400
    with connect() as db:
        sessions = [dict(r) for r in db.execute(
            """SELECT ds.id,ds.campaign_id,ds.territory_id,ds.project_id,ds.status,ds.started_at,
               ds.finished_at,ds.leaflet_count,ds.device_id,ct.label AS territory_label
               FROM distribution_sessions ds
               LEFT JOIN campaign_territories ct ON ct.id=ds.territory_id
               WHERE ds.campaign_id=? ORDER BY ds.started_at""",
            (campaign_id,),
        )]
        participants = _participant_rows(db, [x["id"] for x in sessions])
        for item in sessions:
            item["participants"] = participants.get(item["id"], [])
            item["points"] = [
                dict(r) for r in db.execute(
                    """SELECT point_id AS id,seq,lat,lon,accuracy,recorded_at
                       FROM gps_points WHERE session_id=? ORDER BY seq,recorded_at""",
                    (item["id"],),
                )
            ]
            item["roads"] = [
                dict(r) for r in db.execute(
                    """SELECT road_key,road_name,covered_m,total_m FROM road_coverage
                       WHERE session_id=? ORDER BY road_name,road_key""",
                    (item["id"],),
                )
            ]
    return jsonify(sessions=sessions)


@bp.put("/sessions/<session_id>/roads")
@require_auth
def put_road_coverage(session_id: str):
    data = _json()
    roads = data.get("roads")
    if not isinstance(roads, list) or len(roads) > 10000:
        return jsonify(error="roads must be an array of at most 10000 items."), 400
    now = _now()
    with connect() as db:
        current, denied = _editable_walk(db, session_id)
        if denied:
            return denied
        assert current is not None
        for road in roads:
            if not isinstance(road, dict):
                continue
            key = _id(road.get("road_key"), field="road key")
            db.execute(
                """INSERT INTO road_coverage(session_id,road_key,road_name,covered_m,total_m,updated_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(session_id,road_key) DO UPDATE SET
                     road_name=excluded.road_name,covered_m=excluded.covered_m,
                     total_m=excluded.total_m,updated_at=excluded.updated_at""",
                (
                    session_id, key, str(road.get("road_name", ""))[:256],
                    max(0.0, float(road.get("covered_m", 0))),
                    max(0.0, float(road.get("total_m", 0))), now,
                ),
            )
    return jsonify(ok=True, count=len(roads))


def init_app(app: Flask) -> None:
    init_database()
    app.secret_key = _secret_key()
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = os.environ.get("OSMAPP_COOKIE_SECURE") == "1"
    app.register_blueprint(bp)

    @app.cli.command("field-user")
    @click.argument("name")
    @click.argument("username")
    @click.password_option()
    @click.option("--role", type=click.Choice(["admin", "field"]), default="field")
    def field_user(name: str, username: str, password: str, role: str):
        """Create a selectable person with a login account."""
        now, person_id, user_id = _now(), _new_id("person"), _new_id("user")
        try:
            with connect() as db:
                db.execute("INSERT INTO people(id,name,created_at) VALUES(?,?,?)", (person_id, name, now))
                db.execute(
                    """INSERT INTO users(id,person_id,username,password_hash,role,created_at)
                       VALUES(?,?,?,?,?,?)""",
                    (user_id, person_id, username, generate_password_hash(password), role, now),
                )
        except sqlite3.IntegrityError as exc:
            raise click.ClickException("Username already exists.") from exc
        click.echo(f"Created {username} ({role})")
