# Proposed field release policy

This is a proposal for the open role and setup decisions in `field-distribution-decisions.md`. It does not change their settled status.

- Administrators create and edit campaigns, central map projects, and campaign territory assignments. They also create participant and login records.
- Field accounts can read campaign/project/territory data, select participants, and create, sync, and finish their own walks. Administrators may recover another operator's walk.
- The first administrator is created on the server with `flask --app osmapp:create_app field-user NAME USERNAME --role admin`. Web bootstrap is disabled by default. To allow a one-time web setup in a controlled environment, set `OSMAPP_ALLOW_WEB_BOOTSTRAP=1`, create the first account, then remove the setting. The endpoint still refuses subsequent account creation.

This simple policy protects canonical map and campaign data from accidental edits on shared field devices. It gives field staff no management write path. It costs an administrator's time for new campaigns and map changes; if that becomes a bottleneck, add narrower permissions after the first field trial.

The first-account CLI and the SQLite database must run on a persistent filesystem shared with the web process. The current Heroku dyno filesystem does not meet that requirement.
