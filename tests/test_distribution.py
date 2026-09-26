"""Field distribution API: authentication, central projects and offline-safe sync."""

from __future__ import annotations

from pathlib import Path

import pytest
from flask import Flask

from osmapp import create_app


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Flask:
    monkeypatch.setenv("OSMAPP_DB_PATH", str(tmp_path / "field.sqlite3"))
    monkeypatch.setenv("OSMAPP_SECRET_KEY", "test-only-secret")
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


def test_first_account_bootstraps_once_and_login_persists(client):
    user = bootstrap(client)
    assert user["name"] == "Tom"
    assert user["role"] == "admin"

    again = client.post(
        "/service/field/auth/bootstrap",
        json={"name": "Other", "username": "other", "password": "another-pass"},
    )
    assert again.status_code == 409

    client.post("/service/field/auth/logout")
    status = client.get("/service/field/auth/status").get_json()
    assert status["configured"] is True
    assert status["user"] is None
    assert status["people"] == []

    bad = client.post("/service/field/auth/login", json={"username": "tom", "password": "wrong"})
    assert bad.status_code == 401
    good = client.post(
        "/service/field/auth/login",
        json={"username": "TOM", "password": "correct-horse"},
    )
    assert good.status_code == 200
    assert good.get_json()["user"]["name"] == "Tom"


def test_people_can_exist_without_accounts_and_walk_together(client):
    user = bootstrap(client)
    helper = client.post("/service/field/people", json={"name": "Chloe"})
    assert helper.status_code == 201
    helper_id = helper.get_json()["person"]["id"]

    campaign = client.post(
        "/service/field/campaigns", json={"name": "Thai for £20", "status": "active"}
    ).get_json()["campaign"]

    walk = client.post(
        "/service/field/sessions",
        json={
            "id": "walk_offline_001",
            "campaign_id": campaign["id"],
            "device_id": "phone-a",
            "participant_ids": [user["person_id"], helper_id],
            "started_at": "2026-09-26T10:00:00+00:00",
        },
    )
    assert walk.status_code == 201

    listed = client.get(
        f"/service/field/sessions?campaign_id={campaign['id']}"
    ).get_json()["sessions"]
    assert len(listed) == 1
    assert {p["name"] for p in listed[0]["participants"]} == {"Tom", "Chloe"}


def test_project_writes_require_the_revision_the_editor_loaded(client):
    bootstrap(client)
    created = client.post(
        "/service/field/projects",
        json={"name": "Gorleston", "payload": {"version": 3, "clusters": []}},
    ).get_json()["project"]

    saved = client.put(
        f"/service/field/projects/{created['id']}",
        json={
            "expected_revision": 1,
            "name": "Gorleston rounds",
            "payload": {"version": 3, "clusters": [{"type": "Feature"}]},
        },
    )
    assert saved.status_code == 200
    assert saved.get_json()["project"]["revision"] == 2

    stale = client.put(
        f"/service/field/projects/{created['id']}",
        json={
            "expected_revision": 1,
            "payload": {"version": 3, "clusters": []},
        },
    )
    assert stale.status_code == 409
    assert stale.get_json()["current_revision"] == 2


def test_gps_retry_is_idempotent_and_coverage_is_shared(client):
    user = bootstrap(client)
    campaign = client.post(
        "/service/field/campaigns", json={"name": "Door drop", "status": "active"}
    ).get_json()["campaign"]
    client.post(
        "/service/field/sessions",
        json={
            "id": "walk_retry",
            "campaign_id": campaign["id"],
            "participant_ids": [user["person_id"]],
        },
    )
    points = [
        {
            "id": "p_1",
            "seq": 1,
            "lat": 52.607,
            "lon": 1.729,
            "accuracy": 7.5,
            "recorded_at": "2026-09-26T10:01:00+00:00",
        },
        {
            "id": "p_2",
            "seq": 2,
            "lat": 52.6071,
            "lon": 1.7291,
            "accuracy": 8.0,
            "recorded_at": "2026-09-26T10:01:10+00:00",
        },
    ]
    first = client.post("/service/field/sessions/walk_retry/points", json={"points": points})
    retry = client.post("/service/field/sessions/walk_retry/points", json={"points": points})
    assert first.get_json()["inserted"] == 2
    assert retry.get_json()["inserted"] == 0

    roads = client.put(
        "/service/field/sessions/walk_retry/roads",
        json={
            "roads": [
                {
                    "road_key": "way_123",
                    "road_name": "Victoria Road",
                    "covered_m": 95.0,
                    "total_m": 100.0,
                }
            ]
        },
    )
    assert roads.status_code == 200

    coverage = client.get(
        f"/service/field/coverage?campaign_id={campaign['id']}"
    ).get_json()["sessions"]
    assert [p["id"] for p in coverage[0]["points"]] == ["p_1", "p_2"]
    assert coverage[0]["roads"][0]["road_name"] == "Victoria Road"
    assert coverage[0]["participants"][0]["name"] == "Tom"


def test_walk_can_pause_resume_finish_and_store_leaflet_count(client):
    bootstrap(client)
    campaign = client.post(
        "/service/field/campaigns", json={"name": "Round", "status": "active"}
    ).get_json()["campaign"]
    walk = client.post(
        "/service/field/sessions", json={"campaign_id": campaign["id"]}
    ).get_json()["walk"]

    paused = client.patch(
        f"/service/field/sessions/{walk['id']}",
        json={"status": "paused", "expected_revision": 1},
    )
    assert paused.get_json()["walk"]["revision"] == 2

    resumed = client.patch(
        f"/service/field/sessions/{walk['id']}",
        json={"status": "active", "expected_revision": 2},
    )
    assert resumed.get_json()["walk"]["revision"] == 3

    finished = client.patch(
        f"/service/field/sessions/{walk['id']}",
        json={"status": "finished", "expected_revision": 3, "leaflet_count": 187},
    )
    body = finished.get_json()["walk"]
    assert body["revision"] == 4
    assert body["finished_at"]


def test_walk_writes_are_owned_by_operator_unless_admin(client):
    admin = bootstrap(client)
    helper = client.post(
        "/service/field/people",
        json={
            "name": "Chloe",
            "username": "chloe",
            "password": "leaflets-123",
            "role": "field",
        },
    ).get_json()["person"]
    campaign = client.post(
        "/service/field/campaigns", json={"name": "Ownership", "status": "active"}
    ).get_json()["campaign"]

    client.post("/service/field/auth/logout")
    assert client.post(
        "/service/field/auth/login",
        json={"username": "chloe", "password": "leaflets-123"},
    ).status_code == 200
    walk = client.post(
        "/service/field/sessions",
        json={
            "id": "walk_chloe",
            "campaign_id": campaign["id"],
            "participant_ids": [helper["id"]],
        },
    ).get_json()["walk"]
    assert walk["id"] == "walk_chloe"

    client.post("/service/field/auth/logout")
    client.post(
        "/service/field/auth/login",
        json={"username": "tom", "password": "correct-horse"},
    )
    # Admin may recover/complete another operator's walk.
    assert client.patch(
        "/service/field/sessions/walk_chloe",
        json={"status": "paused", "expected_revision": 1},
    ).status_code == 200

    # A second normal field account may not mutate Chloe's trace.
    other = client.post(
        "/service/field/people",
        json={
            "name": "Lizette",
            "username": "lizette",
            "password": "leaflets-456",
            "role": "field",
        },
    ).get_json()["person"]
    assert other["id"]
    client.post("/service/field/auth/logout")
    client.post(
        "/service/field/auth/login",
        json={"username": "lizette", "password": "leaflets-456"},
    )
    assert client.post(
        "/service/field/sessions/walk_chloe/points",
        json={
            "points": [
                {"id": "wrong_user", "seq": 1, "lat": 52.6, "lon": 1.7}
            ]
        },
    ).status_code == 403
    assert client.put(
        "/service/field/sessions/walk_chloe/roads",
        json={"roads": []},
    ).status_code == 403


def test_finished_walk_cannot_be_resumed(client):
    bootstrap(client)
    campaign = client.post(
        "/service/field/campaigns", json={"name": "State machine", "status": "active"}
    ).get_json()["campaign"]
    walk = client.post(
        "/service/field/sessions", json={"campaign_id": campaign["id"]}
    ).get_json()["walk"]
    finished = client.patch(
        f"/service/field/sessions/{walk['id']}",
        json={"status": "finished", "expected_revision": 1},
    )
    assert finished.status_code == 200
    resumed = client.patch(
        f"/service/field/sessions/{walk['id']}",
        json={"status": "active", "expected_revision": 2},
    )
    assert resumed.status_code == 409


def test_bad_json_shape_and_bad_participant_shape_are_400(client):
    bootstrap(client)
    campaign = client.post(
        "/service/field/campaigns", json={"name": "Validation", "status": "active"}
    ).get_json()["campaign"]
    assert client.post("/service/field/campaigns", json=[]).status_code == 400
    response = client.post(
        "/service/field/sessions",
        json={"campaign_id": campaign["id"], "participant_ids": "not-an-array"},
    )
    assert response.status_code == 400


def test_walk_status_retry_is_idempotent_after_lost_ack(client):
    bootstrap(client)
    campaign = client.post(
        "/service/field/campaigns", json={"name": "Retry status", "status": "active"}
    ).get_json()["campaign"]
    walk = client.post(
        "/service/field/sessions", json={"campaign_id": campaign["id"]}
    ).get_json()["walk"]
    payload = {
        "status": "paused",
        "expected_revision": 1,
        "event_id": "event_pause_001",
    }
    first = client.patch(f"/service/field/sessions/{walk['id']}", json=payload)
    retry = client.patch(f"/service/field/sessions/{walk['id']}", json=payload)
    assert first.status_code == 200
    assert retry.status_code == 200
    assert retry.get_json()["duplicate"] is True
    assert retry.get_json()["walk"]["revision"] == 2


def test_project_write_retry_is_idempotent(client):
    bootstrap(client)
    created = client.post(
        "/service/field/projects",
        json={
            "id": "project_retry",
            "write_id": "write_create",
            "name": "Retry project",
            "payload": {"version": 3, "clusters": []},
        },
    ).get_json()["project"]
    payload = {
        "expected_revision": created["revision"],
        "write_id": "write_save_001",
        "payload": {"version": 3, "clusters": [{"type": "Feature"}]},
    }
    first = client.put("/service/field/projects/project_retry", json=payload)
    retry = client.put("/service/field/projects/project_retry", json=payload)
    assert first.status_code == 200
    assert retry.status_code == 200
    assert retry.get_json()["duplicate"] is True
    assert retry.get_json()["project"]["revision"] == 2


def test_same_source_territory_can_belong_to_two_campaigns(client):
    bootstrap(client)
    project = client.post(
        "/service/field/projects",
        json={"name": "Shared map", "payload": {"version": 3}},
    ).get_json()["project"]
    one = client.post(
        "/service/field/campaigns", json={"name": "Campaign A", "status": "active"}
    ).get_json()["campaign"]
    two = client.post(
        "/service/field/campaigns", json={"name": "Campaign B", "status": "active"}
    ).get_json()["campaign"]
    geometry = {
        "type": "Polygon",
        "coordinates": [[[1.0, 52.0], [1.1, 52.0], [1.1, 52.1], [1.0, 52.0]]],
    }
    a = client.put(
        f"/service/field/campaigns/{one['id']}/territories/source_001",
        json={"project_id": project["id"], "label": "Round 1", "geometry": geometry},
    ).get_json()["territory"]
    b = client.put(
        f"/service/field/campaigns/{two['id']}/territories/source_001",
        json={"project_id": project["id"], "label": "Round 1", "geometry": geometry},
    ).get_json()["territory"]
    assert a["id"] != b["id"]
    assert a["source_territory_id"] == b["source_territory_id"] == "source_001"
    assert len(client.get(f"/service/field/territories?campaign_id={one['id']}").get_json()["territories"]) == 1
    assert len(client.get(f"/service/field/territories?campaign_id={two['id']}").get_json()["territories"]) == 1
