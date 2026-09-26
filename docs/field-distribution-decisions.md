# Field Distribution — Decision Ledger

Canonical decisions for extending **osmapp** into a centrally-synchronised field-distribution tracker.

_Last updated: 2026-09-26_

## Status key

- **SETTLED** — agreed in conversation; do not contradict without proposing a change.
- **INFERRED** — derived from current goals/context; confirm or change before implementation.
- **DEFERRED** — deliberately outside the first release.

## Product goal

- **SETTLED:** Track leaflet-distribution walking activity on the existing OSM/Leaflet map.
- **SETTLED:** Associate recorded activity with a campaign.
- **SETTLED:** Move canonical project/campaign data from browser-only storage to central storage.
- **SETTLED:** Keep local storage for offline resilience rather than replacing it outright.

## V1 desired feature set

### Campaigns
- **SETTLED:** Create/select a campaign before field work.
- **INFERRED:** Campaigns have name, status, start/end dates, notes, and optional leaflet/offer metadata.
- **INFERRED:** Historical campaigns remain viewable after completion.

### Areas / territories
- **INFERRED:** Existing osmapp polygons/territories can be assigned to a campaign.
- **INFERRED:** A campaign can contain multiple rounds/areas.
- **INFERRED:** Field users can see assigned/current areas on the map.

### Distribution sessions
- **SETTLED:** A field user starts/stops a distribution session.
- **SETTLED:** Session stores GPS trace, timestamps, and campaign association.
- **SETTLED:** A session records one or more human participants separately from the authenticated terminal user.
- **SETTLED:** One terminal can record a walk for 2–3 (or more) people walking together without creating duplicate GPS traces.
- **INFERRED:** Session also stores authenticated user/device, distance, duration, and leaflet count.
- **INFERRED:** Pause/resume is supported.

### Live GPS tracking
- **SETTLED:** Use device geolocation to show the current position and live breadcrumb/polyline.
- **SETTLED:** GPS samples are written locally first.
- **SETTLED:** GPS samples sync to the central server in batches when connectivity exists.
- **SETTLED:** Loss of connectivity must not stop recording.
- **INFERRED:** Store accuracy with each point and reject/flag obviously poor samples.

### Shared map / coverage
- **SETTLED:** Central map can overlay multiple distribution sessions.
- **SETTLED:** Users can see which roads/areas have and have not been covered.
- **INFERRED:** Filtering by campaign, date, user/device, territory, and session.
- **INFERRED:** Different visual treatment for current vs historical activity.

### Road coverage derivation
- **SETTLED:** Raw GPS trace is the source-of-truth evidence.
- **SETTLED:** “Roads covered” is derived later by map-matching GPS against the OSM road network.
- **INFERRED:** Store derived road coverage separately so it can be recalculated if matching logic changes.
- **DEFERRED:** Perfect house-by-house delivery proof.

### Central storage and sync
- **SETTLED:** Server/database is canonical; IndexedDB is offline cache/outbox.
- **SETTLED:** GPS/session events should be append-friendly and resumable.
- **INFERRED:** PostgreSQL behind the existing Flask application.
- **INFERRED:** Stable IDs and revision/version fields prevent accidental overwrite/conflicts.
- **INFERRED:** Existing browser projects can eventually sync centrally as well as campaign data.

### Users / devices
- **SETTLED:** User authentication is required: the system must know who is operating the terminal.
- **SETTLED:** Authentication identity and walk participants are separate concepts.
- **SETTLED:** Before starting a walk, the operator can select multiple participants who are physically walking together on that terminal/session.
- **SETTLED:** The GPS trace belongs to the distribution session and is linked to all selected participants; do not duplicate the trace per person.
- **INFERRED:** Remember the most recently selected participant set on a device, but require an explicit confirmation at Start so yesterday's walkers are not silently attributed.
- **INFERRED:** Field UI should not require repeated full login during a round.
- **INFERRED:** Users should have individual accounts; shared terminal credentials are not the preferred model.
- **INFERRED:** Participant records may include active/inactive status so former staff remain attributable in historical sessions.
- **DEFERRED:** Complex HR/permission system unless deployment needs it.

### Field usability
- **INFERRED:** Mobile/PWA-first UI with very large Start / Pause / Finish controls.
- **INFERRED:** Clear offline/sync state.
- **INFERRED:** Battery-conscious GPS sampling rather than maximum-frequency recording.
- **INFERRED:** Accidental tab close/app restart should allow session recovery.

### Management / reporting
- **INFERRED:** Campaign summary: distance walked, time, sessions, areas/roads covered, remaining coverage.
- **INFERRED:** Ability to inspect an individual session trace.
- **INFERRED:** Export of campaign/session data as GeoJSON/CSV where useful.

## Explicit non-goals for first release

- **DEFERRED:** Continuous live staff surveillance outside an active user-started distribution session.
- **DEFERRED:** Exact “leaflet delivered to this household” claims inferred purely from GPS.
- **DEFERRED:** Automated route optimisation/navigation.
- **DEFERRED:** Payroll/timekeeping.
- **DEFERRED:** Gamification/leaderboards.
- **DEFERRED:** Replacing OSM as the geographic source.

## Architecture rules

1. **SETTLED:** Preserve raw GPS; never store only the derived road result.
2. **SETTLED:** Local-first recording; network failure must not lose field work.
3. **SETTLED:** Central server becomes canonical once records have synced.
4. **INFERRED:** Sync endpoints should be idempotent so retries cannot duplicate points/sessions.
5. **INFERRED:** Derived coverage must be reproducible from raw data.
6. **INFERRED:** Avoid centralising data that is unnecessary for the distribution purpose.

## Open decisions

- Campaign/territory relationship and whether territories can be reused across campaigns.
- Exact authentication mechanism (e.g. password/passkey/PIN/session duration) and role model.
- GPS sampling policy (time/distance thresholds).
- Definition of “road covered” and handling of walking one side of a road.
- Whether central storage of existing osmapp project geometry is part of V1 or a follow-on.
- Whether leaflet counts are entered manually, estimated, or omitted.
