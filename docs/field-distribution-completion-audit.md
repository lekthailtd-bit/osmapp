# Field Distribution — Completion Audit

_Date: 2026-09-26_

Canonical product decisions remain in [field-distribution-decisions.md](./field-distribution-decisions.md). This document records implementation and verification status only; implementation defaults do **not** settle open product decisions.

## Current assessment

- **Ledger implementation:** ~95% — all explicit SETTLED field-distribution behaviours inspected are implemented on the integration branch; most INFERRED V1 behaviours are also present.
- **Automated verification:** ~92% — 692/692 JavaScript tests and 125/125 non-E2E Python tests pass after the Cloudflare delivery fix; the critical multi-participant field workflow passes in Chromium.
- **Deployment readiness:** ~94% — the app is live over HTTPS on the integration server with persistent SQLite storage, verified online backups, and a successful restore drill. Remaining work is real-device field validation and routine capacity monitoring.
- **Overall V1 release readiness:** ~95% — the implementation and release record are consolidated; the remaining substantive validation gap is a real-device GPS/offline/reconnect soak.

## Verified against the ledger

Implemented and inspected:

- Campaign create/select and campaign-linked walks.
- Authenticated terminal operator kept separate from one-or-many walk participants.
- One shared GPS trace linked to all participants rather than duplicate traces.
- Start / pause / resume / finish session lifecycle.
- Local-first GPS capture, IndexedDB outbox and batched retryable sync.
- Operator-bound offline outbox so queued events cannot be attributed to the next logged-in user.
- Stable IDs/revisions and server-side ownership checks.
- SQLite canonical store behind Flask.
- Raw GPS retained separately from derived road coverage.
- Shared historical session/coverage view and filtering.
- Central project geometry/revision support.
- Session recovery after reload.
- CSV/GeoJSON reporting paths.
- Cautious coverage language that does not claim household delivery from GPS.

## Fresh verification evidence

On `review/field-distribution-qc`:

- `npm test`: **692 passed, 0 failed**.
- `pytest -q -m 'not e2e'`: **125 passed, 0 failed**.
- Critical Chromium E2E `test_field_walk_supports_one_operator_and_multiple_participants`: **passed**.
- Public HTTPS smoke: all application modules, including `field`, load at `osmapp.lekthai.co.uk`.
- Cloudflare/Rocket Loader load-order protection is committed with its static-delivery assertions.

A broad legacy Chromium E2E run exposed flaky pre-existing tests outside field distribution. The repeatedly investigated territory-list/gap test exercises files that are byte-identical on `main` and the QC branch; isolated reruns produced both pass and fail results. Do not classify that as a field-distribution regression.

## Deployment state

Current integration-server deployment:

- HTTPS hostname: `osmapp.lekthai.co.uk`.
- Canonical SQLite path: `/var/lib/osmapp/osmapp.sqlite3`.
- Service runs locally on port 5057 behind Cloudflare Tunnel.
- Web bootstrap is disabled in the production service.
- Daily SQLite backup at 03:15 UTC.
- Backup uses SQLite's online backup API, runs `PRAGMA integrity_check`, and retains 14 days.

During this audit the integration host root filesystem was approximately **93% full**. Disposable npm/uv/apt caches were cleared without touching application data or the Playwright browser cache, reducing usage to approximately **85%** (about 1.6 GB free). Continue monitoring capacity as this host carries canonical data.

## Remaining release work

### Must do before calling V1 finished

1. Real-device soak: start a walk, lose connectivity, keep walking, restore connectivity, verify ordered sync and session recovery.
2. Make the final release/merge decision after that soak.

Completed during this audit:
- QC/reliability commits consolidated into PR #1; duplicate PR #2 closed.
- Integration-host disk usage reduced from ~93% to ~85% by clearing disposable caches.
- Backup restore drill passed from a copied backup: SQLite integrity `ok` and expected field tables present.

### Open product decisions — do not silently settle from code defaults

The decision ledger still owns these: territory reuse, final auth UX/roles, GPS sampling thresholds, road-covered semantics, whether central project geometry is formally V1, and leaflet-count policy.

### Follow-on hardening

- Schema migration/version journal.
- User lifecycle/admin management polish.
- Audit stream for management changes.
- Coverage pagination/summary endpoints if history volume warrants it.
- Stabilise the legacy territory-list/gap E2E race so the whole upstream suite is reliably green.
