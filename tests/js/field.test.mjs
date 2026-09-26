import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { loadApp } from "./helpers/load.mjs";

function module() {
  return loadApp(["field.js"], {
    window: { App: {} },
    navigator: { onLine: true },
  }).field._test;
}

test("field distance is metres and zero for the same point", () => {
  const f = module();
  assert.equal(f.distanceM({ lat: 52.6, lon: 1.7 }, { lat: 52.6, lon: 1.7 }), 0);
  const north = f.distanceM({ lat: 52.6, lon: 1.7 }, { lat: 52.6009, lon: 1.7 });
  assert.ok(north > 95 && north < 105, north);
});

test("trace distance adds consecutive legs", () => {
  const f = module();
  const points = [
    { lat: 52.6, lon: 1.7 },
    { lat: 52.6009, lon: 1.7 },
    { lat: 52.6018, lon: 1.7 },
  ];
  const total = f.traceDistance(points);
  assert.ok(total > 190 && total < 210, total);
});

test("an OSM id makes the road key stable", () => {
  const f = module();
  const road = {
    id: "way/123",
    properties: { name: "Victoria Road" },
    geometry: { type: "LineString", coordinates: [[1.7, 52.6], [1.71, 52.61]] },
  };
  assert.equal(f.roadKey(road), "way_way_123");
  assert.equal(f.roadKey(structuredClone(road)), f.roadKey(road));
});

test("fallback road key depends on geometry and name, not response order", () => {
  const f = module();
  const road = {
    properties: { name: "Albion Road" },
    geometry: { type: "LineString", coordinates: [[1.7, 52.6], [1.71, 52.61]] },
  };
  assert.equal(f.roadKey(road), f.roadKey(structuredClone(road)));
  assert.notEqual(
    f.roadKey(road),
    f.roadKey({ ...road, properties: { name: "Nelson Road" } }),
  );
});

test("central project replacement suspends ordinary session autosave", () => {
  const source = readFileSync(
    new URL("../../src/osmapp/static/js/field.js", import.meta.url),
    "utf8",
  );
  const openProject = source.slice(
    source.indexOf("function _openProject"),
    source.indexOf("function _syncTerritories"),
  );
  assert.match(openProject, /App\.session\.setSuspended\(true\)/);
  assert.match(openProject, /App\.data\.applyPayload/);
  assert.match(openProject, /finally[\s\S]*App\.session\.setSuspended\(false\)/);
});


test("offline walk status queue preserves every transition in order", () => {
  const f = module();
  const walk = {
    pendingStatuses: [
      { status: "paused", event_id: "pause_1" },
      { status: "active", event_id: "resume_1" },
      { status: "finished", event_id: "finish_1" },
    ],
  };
  assert.deepEqual(
    f.statusQueue(walk).map((event) => event.status),
    ["paused", "active", "finished"],
  );
});

test("legacy single pending status migrates into the ordered queue once", () => {
  const f = module();
  const legacy = {
    pendingStatus: { status: "paused", event_id: "pause_old" },
  };
  assert.equal(f.statusQueue(legacy).length, 1);
  assert.equal(f.statusQueue(legacy).length, 1);
  assert.equal(legacy.pendingStatus, null);
  assert.equal(legacy.pendingStatuses[0].event_id, "pause_old");
});


test("offline queues sync only under the operator who recorded them", () => {
  const f = module();
  const walk = { operator: { id: "user_tom" } };
  assert.equal(f.walkOwnedByUser(walk, { id: "user_tom" }), true);
  assert.equal(f.walkOwnedByUser(walk, { id: "user_chloe" }), false);
  assert.equal(f.walkOwnedByUser({}, { id: "user_tom" }), false);
});


test("starting a walk hands active controls from the drawer to the live bar", () => {
  const source = readFileSync(
    new URL("../../src/osmapp/static/js/field.js", import.meta.url),
    "utf8",
  );
  const renderLive = source.slice(
    source.indexOf("function _renderLive()"),
    source.indexOf("function _val("),
  );
  const startWalk = source.slice(
    source.indexOf("function _startWalk()"),
    source.indexOf("function _statusQueue("),
  );
  assert.match(renderLive, /if \(!walk \|\| _open\)/);
  assert.match(startWalk, /_requestWakeLock\(\);[\s\S]*close\(\);/);
});
