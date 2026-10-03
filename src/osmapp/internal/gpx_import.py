"""GPX file import for historical field-distribution walks.

The importer deliberately does not integrate with recorder-specific APIs. Operators
export one activity as GPX from Strava, Garmin Connect, or another GPS app, preview
it, assign the existing campaign/participants, then the server stores the trace in
the same canonical tables used by native field recording.

The original GPX is retained checksum-addressed beside the SQLite database. Imported
sessions are therefore distinguishable and auditable without creating a second walk
model.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from flask import Blueprint, Flask, g, jsonify, render_template, request
from werkzeug.utils import secure_filename

from .distribution import _data_dir, _new_id, _now, connect, require_auth

bp = Blueprint("gpx_import", __name__)

MAX_GPX_BYTES = 8 * 1024 * 1024
MAX_GPX_POINTS = 200_000
PREVIEW_POINTS = 500

IMPORT_SOURCE = "gpx_file"
IMPORT_DEVICE_ID = "import:gpx"
LEGACY_IMPORT_SOURCE = "strava_gpx"

IMPORT_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS field_imports (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL UNIQUE
        REFERENCES distribution_sessions(id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK(source IN ('{IMPORT_SOURCE}')),
    original_filename TEXT NOT NULL,
    file_sha256 TEXT NOT NULL UNIQUE,
    trace_sha256 TEXT NOT NULL UNIQUE,
    stored_path TEXT NOT NULL,
    imported_by_user_id TEXT NOT NULL REFERENCES users(id),
    imported_at TEXT NOT NULL,
    point_count INTEGER NOT NULL
);
"""
IMPORT_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_field_imports_imported_at
    ON field_imports(imported_at);
"""


@bp.errorhandler(ValueError)
def _validation_error(exc: ValueError):
    return jsonify(error=str(exc)), 400


def init_import_database() -> None:
    with connect() as db:
        row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='field_imports'"
        ).fetchone()
        if row is not None:
            table_sql = row["sql"] or ""
            if LEGACY_IMPORT_SOURCE in table_sql and IMPORT_SOURCE not in table_sql:
                db.execute("ALTER TABLE field_imports RENAME TO field_imports_legacy_source")
                db.execute(IMPORT_TABLE_SQL)
                db.execute(
                    """INSERT INTO field_imports(
                       id,session_id,source,original_filename,file_sha256,trace_sha256,
                       stored_path,imported_by_user_id,imported_at,point_count
                       )
                       SELECT id,session_id,?,original_filename,file_sha256,trace_sha256,
                              stored_path,imported_by_user_id,imported_at,point_count
                         FROM field_imports_legacy_source""",
                    (IMPORT_SOURCE,),
                )
                db.execute("DROP TABLE field_imports_legacy_source")
        db.execute(IMPORT_TABLE_SQL)
        db.execute(IMPORT_INDEX_SQL)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_timestamp(value: str) -> tuple[datetime, str]:
    text = value.strip()
    if not text:
        raise ValueError("A GPX track point is missing its timestamp.")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("The GPX contains an invalid timestamp.") from exc
    if parsed.tzinfo is None:
        raise ValueError("GPX timestamps must include a timezone.")
    utc = parsed.astimezone(timezone.utc)
    return utc, utc.isoformat(timespec="seconds")


def _haversine_m(a: dict[str, Any], b: dict[str, Any]) -> float:
    rad = math.pi / 180
    p1, p2 = a["lat"] * rad, b["lat"] * rad
    dp = (b["lat"] - a["lat"]) * rad
    dl = (b["lon"] - a["lon"]) * rad
    h = (
        math.sin(dp / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    )
    return 6371000 * 2 * math.atan2(math.sqrt(h), math.sqrt(max(0.0, 1 - h)))


def _parse_gpx(raw: bytes) -> dict[str, Any]:
    if not raw:
        raise ValueError("Choose a GPX file to import.")
    if len(raw) > MAX_GPX_BYTES:
        raise ValueError("GPX files must be 8 MB or smaller.")

    upper = raw.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("GPX files containing DTD or entity declarations are not accepted.")

    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise ValueError("That file is not valid GPX XML.") from exc
    if _local_name(root.tag).lower() != "gpx":
        raise ValueError("That file is not a GPX document.")

    points: list[dict[str, Any]] = []
    previous_time: datetime | None = None
    trace_hash = hashlib.sha256()

    for node in root.iter():
        if _local_name(node.tag).lower() != "trkpt":
            continue
        if len(points) >= MAX_GPX_POINTS:
            raise ValueError("GPX contains too many track points.")
        try:
            lat = float(node.attrib["lat"])
            lon = float(node.attrib["lon"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("A GPX track point has invalid coordinates.") from exc
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("A GPX track point is outside the valid coordinate range.")

        time_text = ""
        for child in node:
            if _local_name(child.tag).lower() == "time":
                time_text = child.text or ""
                break
        parsed_time, timestamp = _parse_timestamp(time_text)
        if previous_time is not None and parsed_time < previous_time:
            raise ValueError("GPX track timestamps are not in chronological order.")
        previous_time = parsed_time

        point = {"lat": lat, "lon": lon, "recorded_at": timestamp}
        points.append(point)
        trace_hash.update(f"{lat:.7f},{lon:.7f},{timestamp}\n".encode("ascii"))

    if len(points) < 2:
        raise ValueError("The GPX must contain at least two timestamped track points.")

    distance_m = sum(_haversine_m(points[i - 1], points[i]) for i in range(1, len(points)))
    bounds = {
        "south": min(p["lat"] for p in points),
        "west": min(p["lon"] for p in points),
        "north": max(p["lat"] for p in points),
        "east": max(p["lon"] for p in points),
    }
    step = max(1, math.ceil(len(points) / PREVIEW_POINTS))
    preview = [
        {"lat": points[i]["lat"], "lon": points[i]["lon"]}
        for i in range(0, len(points), step)
    ]
    final_preview_point = {"lat": points[-1]["lat"], "lon": points[-1]["lon"]}
    if preview[-1] != final_preview_point:
        preview.append(final_preview_point)

    return {
        "points": points,
        "trace_sha256": trace_hash.hexdigest(),
        "started_at": points[0]["recorded_at"],
        "finished_at": points[-1]["recorded_at"],
        "point_count": len(points),
        "distance_m": distance_m,
        "bounds": bounds,
        "preview_points": preview,
    }


def _uploaded_gpx() -> tuple[str, bytes, dict[str, Any], str]:
    upload = request.files.get("file")
    if upload is None:
        raise ValueError("Choose a GPX file to import.")
    filename = secure_filename(upload.filename or "") or "strava.gpx"
    if not filename.lower().endswith(".gpx"):
        raise ValueError("Only .gpx files are supported.")
    raw = upload.read(MAX_GPX_BYTES + 1)
    parsed = _parse_gpx(raw)
    return filename[:255], raw, parsed, hashlib.sha256(raw).hexdigest()


def _duplicate(db: sqlite3.Connection, file_sha256: str, trace_sha256: str):
    return db.execute(
        """SELECT fi.session_id, fi.original_filename, fi.imported_at,
                  c.name AS campaign_name
           FROM field_imports fi
           JOIN distribution_sessions ds ON ds.id=fi.session_id
           JOIN campaigns c ON c.id=ds.campaign_id
           WHERE fi.file_sha256=? OR fi.trace_sha256=?
           LIMIT 1""",
        (file_sha256, trace_sha256),
    ).fetchone()


def _store_original(file_sha256: str, raw: bytes) -> str:
    relative = Path("imports") / "gpx" / f"{file_sha256}.gpx"
    destination = _data_dir() / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return relative.as_posix()

    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return relative.as_posix()


@bp.get("/field/import-gpx")
def import_page():
    return render_template("gpx_import.html.j2")


@bp.post("/service/field/imports/gpx/preview")
@require_auth
def preview_gpx():
    filename, _raw, parsed, file_sha256 = _uploaded_gpx()
    with connect() as db:
        duplicate = _duplicate(db, file_sha256, parsed["trace_sha256"])
    return jsonify(
        filename=filename,
        point_count=parsed["point_count"],
        started_at=parsed["started_at"],
        finished_at=parsed["finished_at"],
        distance_m=round(parsed["distance_m"], 1),
        bounds=parsed["bounds"],
        preview_points=parsed["preview_points"],
        duplicate=bool(duplicate),
        duplicate_session_id=duplicate["session_id"] if duplicate else None,
    )


@bp.post("/service/field/imports/gpx")
@require_auth
def import_gpx():
    filename, raw, parsed, file_sha256 = _uploaded_gpx()
    campaign_id = str(request.form.get("campaign_id", "")).strip()
    territory_id = str(request.form.get("territory_id", "")).strip() or None
    participant_ids = list(
        dict.fromkeys(x for x in request.form.getlist("participant_id") if x)
    )
    if not participant_ids:
        encoded = request.form.get("participant_ids", "")
        if encoded:
            try:
                decoded = json.loads(encoded)
            except json.JSONDecodeError as exc:
                raise ValueError("participant_ids must be a JSON array.") from exc
            if not isinstance(decoded, list):
                raise ValueError("participant_ids must be a JSON array.")
            participant_ids = list(dict.fromkeys(str(x) for x in decoded if x))

    if not campaign_id:
        raise ValueError("Choose a campaign.")
    if not participant_ids:
        raise ValueError("Choose at least one walker.")

    raw_leaflets = str(request.form.get("leaflet_count", "")).strip()
    leaflet_count = None
    if raw_leaflets:
        try:
            leaflet_count = int(raw_leaflets)
        except ValueError as exc:
            raise ValueError("Leaflet count must be a whole number.") from exc
        if leaflet_count < 0:
            raise ValueError("Leaflet count cannot be negative.")

    now = _now()
    session_id = _new_id("walk")
    import_id = _new_id("import")
    trace_sha256 = parsed["trace_sha256"]

    try:
        with connect() as db:
            duplicate = _duplicate(db, file_sha256, trace_sha256)
            if duplicate:
                return (
                    jsonify(
                        error="This walk has already been imported.",
                        duplicate=True,
                        session_id=duplicate["session_id"],
                        campaign_name=duplicate["campaign_name"],
                        imported_at=duplicate["imported_at"],
                    ),
                    409,
                )

            if not db.execute(
                "SELECT 1 FROM campaigns WHERE id=?", (campaign_id,)
            ).fetchone():
                return jsonify(error="Campaign not found."), 404

            project_id = None
            if territory_id:
                territory = db.execute(
                    """SELECT campaign_id, project_id
                       FROM campaign_territories
                       WHERE id=? AND active=1""",
                    (territory_id,),
                ).fetchone()
                if territory is None or territory["campaign_id"] != campaign_id:
                    return jsonify(error="Territory is not part of this campaign."), 400
                project_id = territory["project_id"]

            marks = ",".join("?" for _ in participant_ids)
            found = {
                row["id"]
                for row in db.execute(
                    f"SELECT id FROM people WHERE active=1 AND id IN ({marks})",
                    participant_ids,
                )
            }
            if found != set(participant_ids):
                return jsonify(error="One or more participants are unavailable."), 400

            stored_path = _store_original(file_sha256, raw)
            db.execute(
                """INSERT INTO distribution_sessions(
                   id,campaign_id,territory_id,project_id,started_by_user_id,device_id,
                   status,started_at,finished_at,leaflet_count,revision,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,'finished',?,?,?,1,?,?)""",
                (
                    session_id,
                    campaign_id,
                    territory_id,
                    project_id,
                    g.field_user["id"],
                    IMPORT_DEVICE_ID,
                    parsed["started_at"],
                    parsed["finished_at"],
                    leaflet_count,
                    now,
                    now,
                ),
            )
            db.executemany(
                """INSERT INTO session_participants(session_id,person_id,position)
                   VALUES(?,?,?)""",
                [(session_id, person_id, index) for index, person_id in enumerate(participant_ids)],
            )
            db.executemany(
                """INSERT INTO session_events(session_id,event_id,event_type,recorded_at)
                   VALUES(?,?,?,?)""",
                [
                    (session_id, _new_id("event"), "start", parsed["started_at"]),
                    (session_id, _new_id("event"), "finish", parsed["finished_at"]),
                ],
            )
            point_prefix = file_sha256[:16]
            db.executemany(
                """INSERT INTO gps_points(
                   session_id,point_id,seq,lat,lon,accuracy,recorded_at,synced_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                [
                    (
                        session_id,
                        f"gpx_{point_prefix}_{index}",
                        index,
                        point["lat"],
                        point["lon"],
                        None,
                        point["recorded_at"],
                        now,
                    )
                    for index, point in enumerate(parsed["points"])
                ],
            )
            db.execute(
                """INSERT INTO field_imports(
                   id,session_id,source,original_filename,file_sha256,trace_sha256,
                   stored_path,imported_by_user_id,imported_at,point_count
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    import_id,
                    session_id,
                    IMPORT_SOURCE,
                    filename,
                    file_sha256,
                    trace_sha256,
                    stored_path,
                    g.field_user["id"],
                    now,
                    parsed["point_count"],
                ),
            )
    except sqlite3.IntegrityError:
        with connect() as db:
            duplicate = _duplicate(db, file_sha256, trace_sha256)
        if duplicate:
            return (
                jsonify(
                    error="This walk has already been imported.",
                    duplicate=True,
                    session_id=duplicate["session_id"],
                ),
                409,
            )
        raise

    return (
        jsonify(
            walk={
                "id": session_id,
                "status": "finished",
                "started_at": parsed["started_at"],
                "finished_at": parsed["finished_at"],
                "point_count": parsed["point_count"],
                "distance_m": round(parsed["distance_m"], 1),
                "leaflet_count": leaflet_count,
                "source": IMPORT_SOURCE,
            },
            import_id=import_id,
        ),
        201,
    )


def init_app(app: Flask) -> None:
    init_import_database()
    app.register_blueprint(bp)
