import test from "node:test";
import assert from "node:assert/strict";
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
