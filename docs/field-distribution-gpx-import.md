# GPX import scope

Status: **SETTLED** for the field-distribution V1 extension.

This note complements `field-distribution-decisions.md` and records the deliberately narrow historical-import path added after operators used consumer GPS apps as temporary recorders.

## Settled decisions

- Support **GPX file import only**. Do not add recorder-specific OAuth, API access, polling or webhooks.
- A GPX import creates one normal finished `distribution_session` and canonical `gps_points`; it does not create a parallel tracking model.
- Campaign assignment and at least one participant are required. Territory assignment is optional.
- One imported GPS trace may be linked to multiple participants, matching the existing multi-participant walk model.
- Preserve the original uploaded GPX checksum-addressed beside the SQLite data and store import provenance in `field_imports`.
- Reject duplicate imports using both the raw-file SHA-256 and a normalized trace SHA-256, so a re-export of the same coordinates/timestamps is still caught.
- Preserve the GPX file coordinates and timestamps. Set GPS `accuracy` to `NULL`; never invent an accuracy value that the file does not provide.
- Imported traces appear immediately in campaign history, distance totals and map overlays.
- Do **not** fabricate provisional road matches on the server. The current road derivation depends on the street dataset cached in the browser when a native walk finishes. Historical imports therefore keep raw GPS authoritative and may be reprocessed through a future explicit road-coverage recalculation path.

## Operator flow

1. Export the recorded activity as GPX (for example from Strava or Garmin Connect in Safari on iPhone).
2. Open `/field/import-gpx`.
3. Preview the trace.
4. Choose campaign, optional territory, walker(s), and optional leaflet count.
5. Import.
