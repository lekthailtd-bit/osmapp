"""Historical GPX import into canonical field-distribution sessions."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from flask import Flask

from osmapp import create_app
from osmapp.internal.distribution import connect


GPX = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="Strava" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>Leaflet walk</name><trkseg>
    <trkpt lat="52.6070000" lon="1.7290000"><time>2026-10-01T17:00:00Z</time></trkpt>
    <trkpt lat="52.6072000" lon="1.7293000"><time>2026-10-01T17:02:00Z</time></trkpt>
    <trkpt lat="52.6075000" lon="1.7297000"><time>2026-10-01T17:05:00Z</time></trkpt>
  </trkseg></trk>
</gpx>
"""
GARMIN_GPX = GPX.replace(b'creator="Strava"', b'creator="Garmin Connect"')


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Flask:
    monkeypatch.setenv("OSMAPP_DB_PATH", str(tmp_path / "field.sqlite3"))
    monkeypatch.setenv("OSMAPP_SECRET_KEY", "test-only-secret")
    monkeypatch.setenv("OSMAPP_ALLOW_WEB_BOOTSTRAP", "1")
    app = create_app()
    app.config.update(TESTING=True)
    return app


@pytest.fixture
def client(app: Flask):
    return app.test_client()


def bootstrap(client):
    response = client.post(
        "/service/field/auth/bootstrap",
        json={"name": "Tom", "username": "tom", "password": "correct-horse"},
    )
    assert response.status_code == 201
    return response.get_json()["user"]


def upload_data(
    campaign_id: str | None = None,
    participants: list[str] | None = None,
    raw: bytes = GPX,
    filename: str = "previous-walk.gpx",
):
    data: dict[str, object] = {"file": (BytesIO(raw), filename)}
    if campaign_id is not None:
        data["campaign_id"] = campaign_id
    if participants is not None:
        data["participant_id"] = participants
    return data


def test_import_page_is_available_without_creating_a_second_app(client):
    response = client.get("/field/import-gpx")
    assert response.status_code == 200
    assert b"Import previous walk" in response.data
    assert b"Garmin Connect" in response.data
    assert b"GPX only" in response.data


def test_garmin_connect_gpx_uses_the_same_generic_preview_path(client):
    bootstrap(client)
    response = client.post(
        "/service/field/imports/gpx/preview",
        data=upload_data(raw=GARMIN_GPX, filename="garmin-walk.gpx"),
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert response.get_json()["point_count"] == 3


def test_preview_requires_authentication_and_summarises_valid_gpx(client):
    denied = client.post(
        "/service/field/imports/gpx/preview",
        data=upload_data(),
        content_type="multipart/form-data",
    )
    assert denied.status_code == 401

    bootstrap(client)
    response = client.post(
        "/service/field/imports/gpx/preview",
        data=upload_data(),
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["point_count"] == 3
    assert body["distance_m"] > 0
    assert body["started_at"] == "2026-10-01T17:00:00+00:00"
    assert body["finished_at"] == "2026-10-01T17:05:00+00:00"
    assert body["duplicate"] is False
    assert len(body["preview_points"]) == 3


def test_import_creates_finished_canonical_walk_with_provenance_and_original_file(
    client, tmp_path: Path
):
    user = bootstrap(client)
    helper = client.post("/service/field/people", json={"name": "Lauren"}).get_json()[
        "person"
    ]
    campaign = client.post(
        "/service/field/campaigns",
        json={"name": "October leaflets", "status": "active"},
    ).get_json()["campaign"]

    response = client.post(
        "/service/field/imports/gpx",
        data=upload_data(campaign["id"], [user["person_id"], helper["id"]])
        | {"leaflet_count": "240"},
        content_type="multipart/form-data",
    )
    assert response.status_code == 201
    imported = response.get_json()["walk"]
    assert imported["source"] == "gpx_file"
    assert imported["status"] == "finished"
    assert imported["point_count"] == 3
    assert imported["leaflet_count"] == 240

    coverage = client.get(
        f"/service/field/coverage?campaign_id={campaign['id']}"
    ).get_json()["sessions"]
    assert len(coverage) == 1
    walk = coverage[0]
    assert walk["id"] == imported["id"]
    assert walk["device_id"] == "import:gpx"
    assert walk["status"] == "finished"
    assert walk["leaflet_count"] == 240
    assert [person["name"] for person in walk["participants"]] == ["Tom", "Lauren"]
    assert len(walk["points"]) == 3
    assert all(point["accuracy"] is None for point in walk["points"])
    assert walk["roads"] == []

    with connect() as db:
        provenance = db.execute(
            "SELECT * FROM field_imports WHERE session_id=?", (imported["id"],)
        ).fetchone()
        events = list(
            db.execute(
                """SELECT event_type,recorded_at FROM session_events
                   WHERE session_id=? ORDER BY recorded_at""",
                (imported["id"],),
            )
        )
    assert provenance is not None
    assert provenance["source"] == "gpx_file"
    assert provenance["original_filename"] == "previous-walk.gpx"
    assert provenance["point_count"] == 3
    assert [event["event_type"] for event in events] == ["start", "finish"]
    assert (tmp_path / provenance["stored_path"]).read_bytes() == GPX


def test_legacy_strava_source_constraint_migrates_to_generic_gpx(client):
    from osmapp.internal.gpx_import import init_import_database

    with connect() as db:
        db.execute("DROP TABLE field_imports")
        db.execute(
            """CREATE TABLE field_imports (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL UNIQUE REFERENCES distribution_sessions(id) ON DELETE CASCADE,
                source TEXT NOT NULL CHECK(source IN ('strava_gpx')),
                original_filename TEXT NOT NULL,
                file_sha256 TEXT NOT NULL UNIQUE,
                trace_sha256 TEXT NOT NULL UNIQUE,
                stored_path TEXT NOT NULL,
                imported_by_user_id TEXT NOT NULL REFERENCES users(id),
                imported_at TEXT NOT NULL,
                point_count INTEGER NOT NULL
            )"""
        )
    init_import_database()
    with connect() as db:
        table_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='field_imports'"
        ).fetchone()["sql"]
    assert "gpx_file" in table_sql
    assert "strava_gpx" not in table_sql


def test_duplicate_trace_is_rejected_even_if_reexported_file_bytes_differ(client):
    user = bootstrap(client)
    campaign = client.post(
        "/service/field/campaigns", json={"name": "Door drop", "status": "active"}
    ).get_json()["campaign"]

    first = client.post(
        "/service/field/imports/gpx",
        data=upload_data(campaign["id"], [user["person_id"]]),
        content_type="multipart/form-data",
    )
    assert first.status_code == 201
    first_id = first.get_json()["walk"]["id"]

    same_trace = GPX.replace(b"<trk><name>", b"<trk>\n    <name>")
    second = client.post(
        "/service/field/imports/gpx",
        data={
            "file": (BytesIO(same_trace), "another-export.gpx"),
            "campaign_id": campaign["id"],
            "participant_id": [user["person_id"]],
        },
        content_type="multipart/form-data",
    )
    assert second.status_code == 409
    assert second.get_json()["duplicate"] is True
    assert second.get_json()["session_id"] == first_id


@pytest.mark.parametrize(
    ("filename", "payload", "message"),
    [
        ("walk.txt", GPX, "Only .gpx files are supported."),
        ("broken.gpx", b"<gpx><trk>", "not valid GPX"),
        (
            "entity.gpx",
            b'<!DOCTYPE gpx [<!ENTITY x "bad">]><gpx version="1.1"></gpx>',
            "DTD or entity",
        ),
        (
            "notime.gpx",
            b'<gpx><trk><trkseg><trkpt lat="52" lon="1"></trkpt>'
            b'<trkpt lat="52.1" lon="1.1"></trkpt></trkseg></trk></gpx>',
            "missing its timestamp",
        ),
    ],
)
def test_invalid_import_files_fail_closed(client, filename, payload, message):
    bootstrap(client)
    response = client.post(
        "/service/field/imports/gpx/preview",
        data={"file": (BytesIO(payload), filename)},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert message in response.get_json()["error"]
