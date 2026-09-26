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

test("territory evidence comes from GPS positions, not a session assignment", () => {
  const turf = {
    point: (coordinates) => ({ coordinates }),
    booleanPointInPolygon: (point) =>
      point.coordinates[0] >= 1 && point.coordinates[0] <= 2,
  };
  const { field } = loadApp(["field.js"], {
    window: { App: {}, turf },
    turf,
    navigator: { onLine: true },
  });
  const territory = { id: "territory_1", geometry: { type: "Polygon" } };
  assert.equal(field._test.territoryHasTrace(territory, [{
    territory_id: territory.id,
    points: [{ lon: 3, lat: 52 }],
  }]), false);
  assert.equal(field._test.territoryHasTrace(territory, [{
    territory_id: "territory_other",
    points: [{ lon: 1.5, lat: 52 }],
  }]), true);
  assert.equal(field._test.territoryHasTrace(territory, []), false);
});

test("coverage UI does not declare a whole territory or road complete", () => {
  const source = readFileSync(
    new URL("../../src/osmapp/static/js/field.js", import.meta.url),
    "utf8",
  );
  assert.doesNotMatch(source, /· (?:not )?walked["']/);
  assert.match(source, /provisional road matches/);
  assert.match(source, /no GPS recorded inside/);
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
  assert.match(startWalk, /close\(\);[\s\S]*_persist\(\)\.then[\s\S]*_startWatch\(\)/);
});

test("a GPS sample arriving during point upload remains queued", async () => {
  const originalFetch = globalThis.fetch;
  const first = { id: "point_1", seq: 0, lat: 52.6, lon: 1.7 };
  const second = { id: "point_2", seq: 1, lat: 52.61, lon: 1.7 };
  const walk = { id: "walk_1", status: "active", serverCreated: true,
    points: [first], syncedCount: 0, pendingStatuses: [], roads: null };
  const writes = [];
  // The server receives the first point; GPS appends the second while the
  // upload is in flight. A live-array-length acknowledgement would lose it.
  globalThis.fetch = async (_url, options) => {
    const sent = JSON.parse(options.body).points;
    assert.deepEqual(sent.map((p) => p.id), ["point_1"]);
    walk.points.push(second);
    return { ok: true, json: async () => ({ received: 1 }) };
  };
  try {
    const loaded = loadApp(["field.js"], { window: { App: { store: {
      set: async (_key, value) => { writes.push(structuredClone(value)); },
    } } }, navigator: { onLine: true } });
    loaded.field._test.state.queue = [walk];
    await loaded.field._test.syncWalk(walk);
    assert.equal(walk.syncedCount, 1);
    assert.equal(walk.points.length, 2);
    assert.deepEqual(writes.at(-1).queue[0].points.map((p) => p.id), ["point_1", "point_2"]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("field snapshots commit in order even if IndexedDB writes are delayed", async () => {
  const commits = [];
  let release;
  const blocked = new Promise((resolve) => { release = resolve; });
  let calls = 0;
  const loaded = loadApp(["field.js"], { window: { App: { store: {
    set: async (_key, value) => {
      if (++calls === 1) await blocked;
      commits.push(structuredClone(value));
    },
  } } }, navigator: { onLine: false } });
  const f = loaded.field._test;
  f.state.selectedCampaignId = "first";
  const first = f.persist();
  f.state.selectedCampaignId = "second";
  const second = f.persist();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(calls, 1, "the second write must wait for the first transaction");
  release();
  await Promise.all([first, second]);
  assert.deepEqual(commits.map((v) => v.selectedCampaignId), ["first", "second"]);
});

test("required field writes reject if IndexedDB cannot open", async () => {
  const app = loadApp(["store.js"], { window: { App: {} }, console: { warn() {} } });
  await assert.rejects(app.store.set("field:client", {}, { required: true }));
});
