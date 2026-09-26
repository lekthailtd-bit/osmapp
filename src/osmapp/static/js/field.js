/**
 * field.js - campaign-based leaflet distribution tracking.
 *
 * SQLite on the Flask server is canonical. This module is deliberately
 * local-first: a walk is created in IndexedDB before geolocation starts, GPS
 * points are appended there before any network request, and the server is
 * caught up opportunistically. Losing signal therefore changes only the sync
 * badge, not whether the walk is recorded.
 */
var App = window.App || {};
App._loaded = App._loaded || [];

App.field = (function () {
  "use strict";

  var s = null;
  var _map = null;
  var _root = null;
  var _live = null;
  var _open = false;
  var _watchId = null;
  var _wakeLock = null;
  var _syncTimer = null;
  var _projectTimer = null;
  var _syncing = false;
  var _coverageLayers = null;
  var _liveLayer = null;
  var _liveMarker = null;

  var STORE_KEY = "field:client";
  var MAX_ACCURACY_M = 75;
  var MIN_POINT_DISTANCE_M = 6;
  var MAX_POINT_GAP_MS = 30000;
  var ROAD_SAMPLE_M = 20;
  var ROAD_MATCH_M = 25;

  var c = {
    configured: null,
    user: null,
    people: [],
    campaigns: [],
    projects: [],
    territories: [],
    coverage: [],
    selectedCampaignId: "",
    selectedTerritoryId: "",
    selectedParticipants: [],
    currentProject: null,
    projectDirty: false,
    activeWalkId: null,
    queue: [],
    online: navigator.onLine,
    lastError: "",
    filters: { person: "", territory: "", date: "", session: "" },
    roadStats: { covered: 0, total: 0 },
  };

  function _uuid(prefix) {
    var id =
      window.crypto && typeof window.crypto.randomUUID === "function"
        ? window.crypto.randomUUID().replace(/-/g, "")
        : Date.now().toString(36) + Math.random().toString(36).slice(2);
    return prefix + "_" + id;
  }

  function _deviceId() {
    var key = "osmapp.field.deviceId";
    var value = App.util.readJson(key, null);
    if (typeof value === "string" && value) return value;
    value = _uuid("device");
    App.util.writeJson(key, value);
    return value;
  }

  function _esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function _fmtTime(value) {
    if (!value) return "";
    try {
      return new Date(value).toLocaleString([], {
        day: "2-digit",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch (_) {
      return value;
    }
  }

  function _distanceM(a, b) {
    var rad = Math.PI / 180;
    var p1 = a.lat * rad;
    var p2 = b.lat * rad;
    var dp = (b.lat - a.lat) * rad;
    var dl = (b.lon - a.lon) * rad;
    var h =
      Math.sin(dp / 2) * Math.sin(dp / 2) +
      Math.cos(p1) *
        Math.cos(p2) *
        Math.sin(dl / 2) *
        Math.sin(dl / 2);
    return 6371000 * 2 * Math.atan2(Math.sqrt(h), Math.sqrt(1 - h));
  }

  function _traceDistance(points) {
    var total = 0;
    for (var i = 1; i < points.length; i++) total += _distanceM(points[i - 1], points[i]);
    return total;
  }

  function _persist() {
    return App.store.set(STORE_KEY, {
      configured: c.configured,
      user: c.user,
      people: c.people,
      campaigns: c.campaigns,
      projects: c.projects,
      territories: c.territories,
      selectedCampaignId: c.selectedCampaignId,
      selectedTerritoryId: c.selectedTerritoryId,
      selectedParticipants: c.selectedParticipants,
      currentProject: c.currentProject,
      projectDirty: c.projectDirty,
      activeWalkId: c.activeWalkId,
      queue: c.queue,
      filters: c.filters,
    });
  }

  function _restore() {
    return App.store.get(STORE_KEY).then(function (saved) {
      if (!saved || typeof saved !== "object") return;
      Object.keys(saved).forEach(function (key) {
        if (Object.prototype.hasOwnProperty.call(c, key)) c[key] = saved[key];
      });
      c.online = navigator.onLine;
    });
  }

  function _api(path, opts) {
    opts = opts || {};
    var headers = Object.assign({}, opts.headers || {});
    if (opts.body && typeof opts.body !== "string") {
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    opts.headers = headers;
    opts.credentials = "same-origin";
    return fetch("/service/field" + path, opts)
      .then(function (res) {
        return res
          .json()
          .catch(function () {
            return {};
          })
          .then(function (body) {
            if (!res.ok) {
              var err = new Error(body.error || "Request failed (" + res.status + ")");
              err.status = res.status;
              err.body = body;
              if (res.status === 401) {
                c.user = null;
                c.lastError = "Sign in required to sync";
                _persist();
              }
              throw err;
            }
            c.online = true;
            c.lastError = "";
            return body;
          });
      })
      .catch(function (err) {
        if (!err.status) {
          c.online = false;
          c.lastError = "Offline";
        }
        throw err;
      });
  }

  function init(map) {
    s = App.state;
    _map = map || s.leafletMap;
    if (_map) {
      _coverageLayers = L.featureGroup().addTo(_map);
      _liveLayer = L.polyline([], {
        pane: "fieldPane",
        color: "#1565c0",
        weight: 6,
        opacity: 0.9,
      }).addTo(_map);
    }
    _mount();
    window.addEventListener("online", function () {
      c.online = true;
      _syncAll().then(_refreshCatalog).catch(function () {});
      _render();
    });
    window.addEventListener("offline", function () {
      c.online = false;
      _render();
    });
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && _activeWalk() && _activeWalk().status === "active") {
        _requestWakeLock();
      }
    });
    _restore()
      .then(function () {
        _renderLiveTrace();
        var walk = _activeWalk();
        if (walk && walk.status === "active") _startWatch();
        return _refreshCatalog();
      })
      .catch(function (err) {
        console.warn(">>> Field restore:", err && err.message);
      })
      .then(function () {
        _render();
        _syncAll().catch(function () {});
      });
    App._loaded.push("field");
  }

  function isOpen() {
    return _open;
  }

  function open() {
    _open = true;
    _root.hidden = false;
    _render();
    if (App.controls) App.controls.refresh();
  }

  function close() {
    _open = false;
    _root.hidden = true;
    if (App.controls) App.controls.refresh();
  }

  function _mount() {
    _root = document.createElement("div");
    _root.className = "field-drawer";
    _root.hidden = true;
    _root.setAttribute("role", "region");
    _root.setAttribute("aria-label", "Field distribution");
    _root.addEventListener("click", _click);
    _root.addEventListener("change", _change);
    document.body.appendChild(_root);

    _live = App.dom.mountOnMap("tpl-field-live", _map);
    _live.hidden = true;
    _live.addEventListener("click", _click);
  }

  function _refreshCatalog() {
    return _api("/auth/status")
      .then(function (status) {
        c.configured = status.configured;
        c.user = status.user;
        if (status.user) c.people = status.people || [];
        if (!status.user) return null;
        return Promise.all([_api("/campaigns"), _api("/projects")]).then(function (parts) {
          c.campaigns = parts[0].campaigns || [];
          c.projects = parts[1].projects || [];
          if (!c.selectedCampaignId && c.campaigns.length) {
            var active = c.campaigns.find(function (x) {
              return x.status === "active";
            });
            c.selectedCampaignId = (active || c.campaigns[0]).id;
          }
          if (c.selectedCampaignId) return _refreshCampaignData();
          return null;
        });
      })
      .then(_persist)
      .then(_render)
      .catch(function (err) {
        if (err.status === 401) {
          c.user = null;
          _persist();
        }
        _render();
      });
  }

  function _refreshCampaignData() {
    if (!c.selectedCampaignId || !c.user) return Promise.resolve();
    return Promise.all([
      _api("/territories?campaign_id=" + encodeURIComponent(c.selectedCampaignId)),
      _api("/coverage?campaign_id=" + encodeURIComponent(c.selectedCampaignId)),
    ]).then(function (parts) {
      c.territories = parts[0].territories || [];
      c.coverage = parts[1].sessions || [];
      if (
        c.selectedTerritoryId &&
        !c.territories.some(function (x) {
          return x.id === c.selectedTerritoryId;
        })
      ) {
        c.selectedTerritoryId = "";
      }
      _drawCoverage();
      return _persist();
    });
  }

  function _campaign() {
    return c.campaigns.find(function (x) {
      return x.id === c.selectedCampaignId;
    });
  }

  function _activeWalk() {
    if (!c.activeWalkId) return null;
    return (
      c.queue.find(function (x) {
        return x.id === c.activeWalkId;
      }) || null
    );
  }

  function _personName(id) {
    var p = c.people.find(function (x) {
      return x.id === id;
    });
    return p ? p.name : id;
  }

  function _selectedNames() {
    return c.selectedParticipants.map(_personName);
  }

  function _render() {
    if (!_root) return;
    _renderLive();
    if (!_open) return;

    var online = c.online ? "Online" : "Offline";
    var netClass = c.online ? "is-online" : "is-offline";
    var head =
      '<div class="field-drawer__head"><div><strong>Field distribution</strong>' +
      '<span class="field-net ' +
      netClass +
      '">' +
      online +
      "</span></div>" +
      '<button type="button" class="field-x" data-action="close" aria-label="Close">×</button></div>';

    if (!c.user) {
      _root.innerHTML = head + _renderAuth();
      return;
    }

    var campaignOptions =
      '<option value="">Choose campaign…</option>' +
      c.campaigns
        .map(function (x) {
          return (
            '<option value="' +
            _esc(x.id) +
            '"' +
            (x.id === c.selectedCampaignId ? " selected" : "") +
            ">" +
            _esc(x.name) +
            " · " +
            _esc(x.status) +
            "</option>"
          );
        })
        .join("");

    var territoryOptions =
      '<option value="">Whole round / no territory</option>' +
      c.territories
        .map(function (x) {
          return (
            '<option value="' +
            _esc(x.id) +
            '"' +
            (x.id === c.selectedTerritoryId ? " selected" : "") +
            ">" +
            _esc(x.label) +
            "</option>"
          );
        })
        .join("");

    var projectOptions =
      '<option value="">Choose central project…</option>' +
      c.projects
        .map(function (x) {
          var current = c.currentProject && c.currentProject.id === x.id;
          return (
            '<option value="' +
            _esc(x.id) +
            '"' +
            (current ? " selected" : "") +
            ">" +
            _esc(x.name) +
            " · r" +
            x.revision +
            "</option>"
          );
        })
        .join("");

    var people = c.people
      .map(function (p) {
        var checked = c.selectedParticipants.indexOf(p.id) >= 0;
        return (
          '<label class="field-check"><input type="checkbox" data-role="participant" value="' +
          _esc(p.id) +
          '"' +
          (checked ? " checked" : "") +
          "> <span>" +
          _esc(p.name) +
          "</span></label>"
        );
      })
      .join("");

    var body =
      '<div class="field-drawer__body">' +
      '<div class="field-user"><span>Signed in as <strong>' +
      _esc(c.user.name) +
      "</strong></span>" +
      '<button type="button" class="btn btn--ghost" data-action="logout"' +
      (_activeWalk() ? " disabled" : "") +
      ">Sign out</button></div>" +
      '<section class="field-card"><h3>Campaign</h3>' +
      '<select data-role="campaign">' +
      campaignOptions +
      "</select>" +
      '<div class="field-row"><input data-role="new-campaign" placeholder="New campaign name">' +
      '<button type="button" class="btn btn--ghost" data-action="new-campaign">Create</button></div></section>' +
      '<section class="field-card"><h3>Central map project</h3>' +
      '<select data-role="project">' +
      projectOptions +
      "</select>" +
      '<div class="field-row"><button type="button" class="btn btn--ghost" data-action="open-project">Open selected</button>' +
      '<button type="button" class="btn btn--ghost" data-action="save-project">' +
      (c.currentProject ? "Save current" : "Save as new") +
      "</button></div>" +
      (c.currentProject
        ? '<p class="field-hint">Current: ' +
          _esc(c.currentProject.name) +
          " · r" +
          c.currentProject.revision +
          (c.projectDirty ? " · unsynced changes" : "") +
          "</p>"
        : '<p class="field-hint">The browser copy remains the offline cache until this map is saved centrally.</p>') +
      "</section>" +
      '<section class="field-card"><h3>Who is walking?</h3><div class="field-people">' +
      people +
      "</div>" +
      (c.user.role === "admin"
        ? '<details><summary>Add person</summary><div class="field-stack">' +
          '<input data-role="person-name" placeholder="Name">' +
          '<label class="field-check"><input type="checkbox" data-role="person-login"> Give them a login</label>' +
          '<input data-role="person-username" placeholder="Username" hidden>' +
          '<input data-role="person-password" type="password" placeholder="Password (8+ characters)" hidden>' +
          '<button type="button" class="btn btn--ghost" data-action="add-person">Add person</button></div></details>'
        : "") +
      "</section>" +
      '<section class="field-card"><h3>Round / territory</h3>' +
      '<select data-role="territory">' +
      territoryOptions +
      "</select>" +
      '<button type="button" class="btn btn--ghost field-wide" data-action="sync-territories"' +
      (!c.selectedCampaignId || !s.clusters || !s.clusters.length ? " disabled" : "") +
      ">Assign current map territories to campaign</button></section>" +
      _renderWalkCard() +
      _renderCoverageCard() +
      "</div>";

    _root.innerHTML = head + body;
  }

  function _renderAuth() {
    if (c.configured === false) {
      return (
        '<div class="field-drawer__body"><section class="field-card"><h3>Set up the first account</h3>' +
        '<p class="field-hint">This first account becomes the administrator.</p>' +
        '<div class="field-stack"><input data-role="auth-name" autocomplete="name" placeholder="Your name">' +
        '<input data-role="auth-user" autocomplete="username" placeholder="Username">' +
        '<input data-role="auth-pass" type="password" autocomplete="new-password" placeholder="Password (8+ characters)">' +
        '<button type="button" class="btn btn--primary" data-action="bootstrap">Create administrator</button></div></section></div>'
      );
    }
    return (
      '<div class="field-drawer__body"><section class="field-card"><h3>Sign in</h3>' +
      (c.online
        ? ""
        : '<p class="field-warning">You are offline. A new sign-in needs the server.</p>') +
      '<div class="field-stack"><input data-role="auth-user" autocomplete="username" placeholder="Username">' +
      '<input data-role="auth-pass" type="password" autocomplete="current-password" placeholder="Password">' +
      '<button type="button" class="btn btn--primary" data-action="login">Sign in</button></div></section></div>'
    );
  }

  function _renderWalkCard() {
    var walk = _activeWalk();
    if (walk) {
      return (
        '<section class="field-card field-card--live"><h3>Walk in progress</h3>' +
        "<p><strong>" +
        _esc(walk.participant_names.join(" + ")) +
        "</strong><br>" +
        _esc(walk.campaign_name || "Campaign") +
        (walk.territory_label ? " · " + _esc(walk.territory_label) : "") +
        "</p><p class=\"field-hint\">" +
        walk.points.length +
        " GPS points · " +
        (_traceDistance(walk.points) / 1000).toFixed(2) +
        " km · " +
        _esc(walk.status) +
        "</p>" +
        '<div class="field-row">' +
        (walk.status === "paused"
          ? '<button type="button" class="btn btn--primary" data-action="resume">Resume</button>'
          : '<button type="button" class="btn btn--ghost" data-action="pause">Pause</button>') +
        '<button type="button" class="btn btn--primary" data-action="finish">Finish</button></div></section>'
      );
    }

    var disabled =
      !c.user || !c.selectedCampaignId || !c.selectedParticipants.length;
    return (
      '<section class="field-card"><h3>Start distribution</h3>' +
      '<p class="field-hint">Confirm the people above, choose the campaign/territory, then start. One phone records one shared trace for the whole group.</p>' +
      '<button type="button" class="btn btn--primary field-wide" data-action="start"' +
      (disabled ? " disabled" : "") +
      ">Start walk</button></section>"
    );
  }

  function _filteredCoverage() {
    return c.coverage.filter(function (walk) {
      if (c.filters.person) {
        var has = (walk.participants || []).some(function (p) {
          return p.id === c.filters.person;
        });
        if (!has) return false;
      }
      if (c.filters.territory && walk.territory_id !== c.filters.territory) return false;
      if (c.filters.date && String(walk.started_at || "").slice(0, 10) !== c.filters.date)
        return false;
      if (c.filters.session && walk.id !== c.filters.session) return false;
      return true;
    });
  }

  function _renderCoverageCard() {
    var filtered = _filteredCoverage();
    var distance = filtered.reduce(function (sum, x) {
      return sum + _traceDistance(x.points || []);
    }, 0);
    var road = {};
    filtered.forEach(function (x) {
      (x.roads || []).forEach(function (r) {
        var old = road[r.road_key];
        if (!old || Number(r.covered_m) > Number(old.covered_m)) road[r.road_key] = r;
      });
    });
    var covered = Object.keys(road).filter(function (key) {
      return Number(road[key].covered_m) > 0;
    }).length;
    if (c.roadStats.total) covered = c.roadStats.covered;

    var sessions =
      '<option value="">All sessions</option>' +
      c.coverage
        .map(function (x) {
          return (
            '<option value="' +
            _esc(x.id) +
            '"' +
            (c.filters.session === x.id ? " selected" : "") +
            ">" +
            _esc(_fmtTime(x.started_at)) +
            " · " +
            _esc((x.participants || []).map(function (p) { return p.name; }).join(" + ")) +
            "</option>"
          );
        })
        .join("");
    var people =
      '<option value="">All people</option>' +
      c.people
        .map(function (p) {
          return '<option value="' + _esc(p.id) + '"' +
            (c.filters.person === p.id ? " selected" : "") + ">" + _esc(p.name) + "</option>";
        })
        .join("");
    var territories =
      '<option value="">All territories</option>' +
      c.territories
        .map(function (t) {
          return '<option value="' + _esc(t.id) + '"' +
            (c.filters.territory === t.id ? " selected" : "") + ">" + _esc(t.label) + "</option>";
        })
        .join("");

    var rows = filtered
      .slice()
      .reverse()
      .slice(0, 20)
      .map(function (x) {
        return (
          '<button type="button" class="field-history" data-action="focus-session" data-id="' +
          _esc(x.id) +
          '"><span><strong>' +
          _esc((x.participants || []).map(function (p) { return p.name; }).join(" + ") || "Unknown") +
          "</strong><br><small>" +
          _esc(_fmtTime(x.started_at)) +
          (x.territory_label ? " · " + _esc(x.territory_label) : "") +
          "</small></span><span>" +
          (_traceDistance(x.points || []) / 1000).toFixed(2) +
          " km</span></button>"
        );
      })
      .join("");

    return (
      '<section class="field-card"><h3>Campaign coverage</h3>' +
      '<div class="field-metrics"><div><strong>' +
      filtered.length +
      "</strong><span>sessions</span></div><div><strong>" +
      (distance / 1000).toFixed(1) +
      "</strong><span>km walked</span></div><div><strong>" +
      covered +
      (c.roadStats.total ? "/" + c.roadStats.total : "") +
      "</strong><span>roads covered</span></div></div>" +
      '<div class="field-grid"><select data-role="filter-person">' +
      people +
      '</select><select data-role="filter-territory">' +
      territories +
      '</select><input type="date" data-role="filter-date" value="' +
      _esc(c.filters.date) +
      '"><select data-role="filter-session">' +
      sessions +
      "</select></div>" +
      '<div class="field-row"><button type="button" class="btn btn--ghost" data-action="refresh-coverage">Refresh</button>' +
      '<button type="button" class="btn btn--ghost" data-action="export-geojson">GeoJSON</button>' +
      '<button type="button" class="btn btn--ghost" data-action="export-csv">CSV</button></div>' +
      '<div class="field-history-list">' +
      (rows || '<p class="field-hint">No recorded walks for this filter.</p>') +
      "</div></section>"
    );
  }

  function _setLiveVisible(visible) {
    _live.hidden = !visible;
    if (App.dom && App.dom.syncBottomBars) App.dom.syncBottomBars();
  }

  function _renderLive() {
    if (!_live) return;
    var walk = _activeWalk();
    if (!walk) {
      _setLiveVisible(false);
      return;
    }
    var pending = Math.max(0, walk.points.length - (walk.syncedCount || 0));
    var campaign = walk.campaign_name || "Campaign";
    var who = (walk.participant_names || []).join(" + ");
    _setLiveVisible(true);
    _live.innerHTML =
      '<div class="field-live__main"><strong>Walking: ' +
      _esc(who) +
      "</strong><span>" +
      _esc(campaign) +
      (walk.territory_label ? " · " + _esc(walk.territory_label) : "") +
      "</span><small>" +
      walk.points.length +
      " points · " +
      (_traceDistance(walk.points) / 1000).toFixed(2) +
      " km · " +
      (c.online ? (pending ? pending + " waiting to sync" : "synced") : pending + " waiting · offline") +
      "</small></div><div class=\"field-live__actions\">" +
      (walk.status === "paused"
        ? '<button type="button" data-action="resume">Resume</button>'
        : '<button type="button" data-action="pause">Pause</button>') +
      '<button type="button" data-action="finish">Finish</button></div>';
  }

  function _val(role) {
    var node = _root && _root.querySelector('[data-role="' + role + '"]');
    return node ? node.value : "";
  }

  function _click(e) {
    var node = e.target.closest("[data-action]");
    if (!node || node.disabled) return;
    var action = node.dataset.action;
    if (action === "close") return close();
    if (action === "bootstrap") return _bootstrap();
    if (action === "login") return _login();
    if (action === "logout") return _logout();
    if (action === "new-campaign") return _newCampaign();
    if (action === "add-person") return _addPerson();
    if (action === "save-project") return _saveProject(true);
    if (action === "open-project") return _openProject();
    if (action === "sync-territories") return _syncTerritories();
    if (action === "start") return _startWalk();
    if (action === "pause") return _setWalkStatus("paused");
    if (action === "resume") return _setWalkStatus("active");
    if (action === "finish") return _finishWalk();
    if (action === "refresh-coverage") return _refreshCampaignData().then(_render);
    if (action === "focus-session") return _focusSession(node.dataset.id);
    if (action === "export-geojson") return _exportGeoJSON();
    if (action === "export-csv") return _exportCSV();
  }

  function _change(e) {
    var role = e.target.dataset.role;
    if (role === "campaign") {
      c.selectedCampaignId = e.target.value;
      c.selectedTerritoryId = "";
      c.filters.territory = "";
      _persist();
      if (c.online) _refreshCampaignData().then(_render);
      else _render();
    } else if (role === "territory") {
      c.selectedTerritoryId = e.target.value;
      _persist();
    } else if (role === "participant") {
      var participantId = e.target.value;
      var participantIndex = c.selectedParticipants.indexOf(participantId);
      if (e.target.checked && participantIndex < 0) {
        c.selectedParticipants.push(participantId);
      } else if (!e.target.checked && participantIndex >= 0) {
        c.selectedParticipants.splice(participantIndex, 1);
      }
      _persist();
      _render();
    } else if (role === "person-login") {
      var show = !!e.target.checked;
      ["person-username", "person-password"].forEach(function (r) {
        var n = _root.querySelector('[data-role="' + r + '"]');
        if (n) n.hidden = !show;
      });
    } else if (role && role.indexOf("filter-") === 0) {
      c.filters[role.slice(7)] = e.target.value;
      _persist();
      _drawCoverage();
      _render();
    }
  }

  function _bootstrap() {
    return _api("/auth/bootstrap", {
      method: "POST",
      body: { name: _val("auth-name"), username: _val("auth-user"), password: _val("auth-pass") },
    })
      .then(function (body) {
        c.user = body.user;
        c.configured = true;
        c.selectedParticipants = [body.user.person_id];
        return _refreshCatalog();
      })
      .catch(_showError);
  }

  function _login() {
    return _api("/auth/login", {
      method: "POST",
      body: { username: _val("auth-user"), password: _val("auth-pass") },
    })
      .then(function (body) {
        c.user = body.user;
        if (!c.selectedParticipants.length) c.selectedParticipants = [body.user.person_id];
        return _refreshCatalog().then(function () {
          return _syncAll();
        });
      })
      .catch(_showError);
  }

  function _logout() {
    if (_activeWalk()) return;
    return _api("/auth/logout", { method: "POST" })
      .then(function () {
        c.user = null;
        return _persist();
      })
      .then(_render)
      .catch(_showError);
  }

  function _newCampaign() {
    var name = _val("new-campaign").trim();
    if (!name) return;
    return _api("/campaigns", { method: "POST", body: { name: name, status: "active" } })
      .then(function (body) {
        c.selectedCampaignId = body.campaign.id;
        return _refreshCatalog();
      })
      .catch(_showError);
  }

  function _addPerson() {
    var name = _val("person-name").trim();
    if (!name) return;
    var login = !!(_root.querySelector('[data-role="person-login"]') || {}).checked;
    var body = { name: name };
    if (login) {
      body.username = _val("person-username");
      body.password = _val("person-password");
    }
    return _api("/people", { method: "POST", body: body })
      .then(_refreshCatalog)
      .catch(_showError);
  }

  function _ensureFieldIds() {
    (s.clusters || []).forEach(function (entry) {
      entry.feature.properties = entry.feature.properties || {};
      if (!entry.feature.properties.fieldId) entry.feature.properties.fieldId = _uuid("territory");
    });
  }

  function projectDirty() {
    if (!c.currentProject) return;
    c.projectDirty = true;
    c.currentProject.pendingWriteId = _uuid("write");
    _persist();
    clearTimeout(_projectTimer);
    _projectTimer = setTimeout(function () {
      if (c.online) _saveProject(false);
    }, 2500);
  }

  function _saveProject(interactive) {
    if (!c.user) return Promise.resolve();
    _ensureFieldIds();
    var payload = App.data.buildPayload();
    if (!c.currentProject) {
      var suggested = "Field map " + new Date().toISOString().slice(0, 10);
      var name = interactive ? window.prompt("Central project name", suggested) : suggested;
      if (!name) return Promise.resolve();
      var newProjectId = _uuid("project");
      var newWriteId = _uuid("write");
      return _api("/projects", {
        method: "POST",
        body: {
          id: newProjectId,
          write_id: newWriteId,
          name: name,
          payload: payload,
        },
      })
        .then(function (body) {
          c.currentProject = {
            id: body.project.id,
            name: body.project.name,
            revision: body.project.revision,
            pendingWriteId: null,
          };
          c.projectDirty = false;
          return _refreshCatalog();
        })
        .catch(_showError);
    }

    var writeId = c.currentProject.pendingWriteId || _uuid("write");
    c.currentProject.pendingWriteId = writeId;
    return _api("/projects/" + encodeURIComponent(c.currentProject.id), {
      method: "PUT",
      body: {
        expected_revision: c.currentProject.revision,
        write_id: writeId,
        name: c.currentProject.name,
        payload: payload,
      },
    })
      .then(function (body) {
        c.currentProject.revision = body.project.revision;
        if (c.currentProject.pendingWriteId === writeId) {
          c.currentProject.pendingWriteId = null;
          c.projectDirty = false;
        }
        return _persist();
      })
      .then(_render)
      .catch(function (err) {
        if (err.status === 409) {
          c.projectDirty = true;
          _persist();
          alert(
            "This central project changed on another device. Nothing was overwritten. Open the central copy or save your browser copy as a new project.",
          );
          return;
        }
        _showError(err);
      });
  }

  function _openProject() {
    var select = _root.querySelector('[data-role="project"]');
    var id = select ? select.value : "";
    if (!id) return;
    if ((s.outerPolygonLayer || (s.clusters && s.clusters.length)) &&
        !window.confirm("Replace the map currently on screen with the selected central project?")) return;
    var saveCurrent =
      c.currentProject && c.projectDirty ? _saveProject(false) : Promise.resolve();
    return saveCurrent
      .then(function () {
        if (c.projectDirty)
          throw new Error("Save or resolve the current project before opening another one.");
        return _api("/projects/" + encodeURIComponent(id));
      })
      .then(function (body) {
        App.session.setSuspended(true);
        try {
          App.data.applyPayload(body.project.payload);
        } finally {
          App.session.setSuspended(false);
        }
        c.currentProject = {
          id: body.project.id,
          name: body.project.name,
          revision: body.project.revision,
          pendingWriteId: null,
        };
        c.projectDirty = false;
        return _persist();
      })
      .then(function () {
        _render();
        _drawCoverage();
      })
      .catch(_showError);
  }

  function _syncTerritories() {
    if (!c.selectedCampaignId || !s.clusters || !s.clusters.length) return Promise.resolve();
    var ensure = c.currentProject ? Promise.resolve() : _saveProject(true);
    return ensure.then(function () {
      if (!c.currentProject) return;
      _ensureFieldIds();
      var jobs = s.clusters.map(function (entry, i) {
        var id = entry.feature.properties.fieldId;
        return _api(
          "/campaigns/" +
            encodeURIComponent(c.selectedCampaignId) +
            "/territories/" +
            encodeURIComponent(id),
          {
            method: "PUT",
            body: {
              project_id: c.currentProject.id,
              label: entry.feature.properties.label || "Territory " + (i + 1),
              geometry: entry.feature.geometry,
            },
          },
        );
      });
      return Promise.all(jobs).then(_refreshCampaignData).then(_render);
    }).catch(_showError);
  }

  function _startWalk() {
    if (!c.user || !c.selectedCampaignId || !c.selectedParticipants.length) return;
    var names = _selectedNames();
    if (!window.confirm("Start this walk for " + names.join(" + ") + "?")) return;
    var camp = _campaign();
    var territory = c.territories.find(function (x) {
      return x.id === c.selectedTerritoryId;
    });
    var walk = {
      id: _uuid("walk"),
      campaign_id: c.selectedCampaignId,
      campaign_name: camp ? camp.name : "Campaign",
      territory_id: c.selectedTerritoryId || null,
      territory_label: territory ? territory.label : "",
      territory_geometry: territory ? territory.geometry : null,
      project_id: c.currentProject ? c.currentProject.id : null,
      participant_ids: c.selectedParticipants.slice(),
      participant_names: names,
      operator: c.user,
      device_id: _deviceId(),
      started_at: new Date().toISOString(),
      finished_at: null,
      leaflet_count: null,
      status: "active",
      revision: null,
      serverCreated: false,
      points: [],
      syncedCount: 0,
      pendingStatuses: [],
      roads: null,
      roadsSynced: false,
    };
    c.queue.push(walk);
    c.activeWalkId = walk.id;
    _persist();
    _renderLiveTrace();
    _startWatch();
    _requestWakeLock();
    _render();
    _syncAll().catch(function () {});
  }

  function _statusQueue(walk) {
    if (!Array.isArray(walk.pendingStatuses)) walk.pendingStatuses = [];
    // Migrate a walk saved by the first field build without discarding its
    // unsynced transition.
    if (walk.pendingStatus) {
      walk.pendingStatuses.push(walk.pendingStatus);
      walk.pendingStatus = null;
    }
    return walk.pendingStatuses;
  }

  function _setWalkStatus(status) {
    var walk = _activeWalk();
    if (!walk) return;
    walk.status = status;
    _statusQueue(walk).push({
      status: status,
      event_id: _uuid("event"),
      recorded_at: new Date().toISOString(),
    });
    if (status === "paused") {
      _stopWatch();
      _releaseWakeLock();
    } else if (status === "active") {
      _startWatch();
      _requestWakeLock();
    }
    _persist();
    _render();
    _syncAll().catch(function () {});
  }

  function _finishWalk() {
    var walk = _activeWalk();
    if (!walk) return;
    var raw = window.prompt("How many leaflets were distributed? Leave blank if unknown.", "");
    if (raw === null) return;
    var count = raw.trim() === "" ? null : Math.max(0, parseInt(raw, 10) || 0);
    walk.status = "finished";
    walk.finished_at = new Date().toISOString();
    walk.leaflet_count = count;
    _statusQueue(walk).push({
      status: "finished",
      event_id: _uuid("event"),
      recorded_at: walk.finished_at,
      finished_at: walk.finished_at,
      leaflet_count: count,
    });
    _stopWatch();
    _releaseWakeLock();
    _calculateRoadCoverage(walk);
    c.activeWalkId = null;
    _persist();
    _render();
    _syncAll().then(_refreshCampaignData).catch(function () {});
  }

  function _startWatch() {
    if (_watchId !== null || !navigator.geolocation) return;
    _watchId = navigator.geolocation.watchPosition(
      _onPosition,
      function (err) {
        c.lastError = "GPS: " + err.message;
        _renderLive();
      },
      { enableHighAccuracy: true, maximumAge: 5000, timeout: 15000 },
    );
  }

  function _stopWatch() {
    if (_watchId === null) return;
    navigator.geolocation.clearWatch(_watchId);
    _watchId = null;
  }

  function _onPosition(position) {
    var walk = _activeWalk();
    if (!walk || walk.status !== "active") return;
    var accuracy = Number(position.coords.accuracy || 0);
    if (accuracy && accuracy > MAX_ACCURACY_M) {
      c.lastError = "GPS accuracy " + Math.round(accuracy) + " m — point ignored";
      _renderLive();
      return;
    }
    var point = {
      id: _uuid("point"),
      seq: walk.points.length,
      lat: Number(position.coords.latitude),
      lon: Number(position.coords.longitude),
      accuracy: accuracy || null,
      recorded_at: new Date(position.timestamp || Date.now()).toISOString(),
    };
    var last = walk.points[walk.points.length - 1];
    if (last) {
      var age = new Date(point.recorded_at).getTime() - new Date(last.recorded_at).getTime();
      if (_distanceM(last, point) < MIN_POINT_DISTANCE_M && age < MAX_POINT_GAP_MS) return;
    }
    walk.points.push(point);
    c.lastError = "";
    _persist();
    _appendLivePoint(point);
    _renderLive();
    clearTimeout(_syncTimer);
    _syncTimer = setTimeout(function () {
      _syncAll().catch(function () {});
    }, 4000);
    if (walk.points.length - (walk.syncedCount || 0) >= 20) _syncAll().catch(function () {});
  }

  function _renderLiveTrace() {
    if (!_liveLayer) return;
    var walk = _activeWalk();
    var points = walk ? walk.points : [];
    _liveLayer.setLatLngs(
      points.map(function (p) {
        return [p.lat, p.lon];
      }),
    );
    if (_liveMarker) {
      _map.removeLayer(_liveMarker);
      _liveMarker = null;
    }
    if (points.length) _appendLivePoint(points[points.length - 1], true);
  }

  function _appendLivePoint(point, redrawOnly) {
    if (!_liveLayer || !point) return;
    if (!redrawOnly) _liveLayer.addLatLng([point.lat, point.lon]);
    if (_liveMarker) _map.removeLayer(_liveMarker);
    _liveMarker = L.circleMarker([point.lat, point.lon], {
      pane: "fieldPane",
      radius: 7,
      color: "#fff",
      fillColor: "#1565c0",
      fillOpacity: 1,
      weight: 3,
    }).addTo(_map);
  }

  function _requestWakeLock() {
    if (!navigator.wakeLock || _wakeLock) return;
    navigator.wakeLock
      .request("screen")
      .then(function (lock) {
        _wakeLock = lock;
        lock.addEventListener("release", function () {
          _wakeLock = null;
        });
      })
      .catch(function () {});
  }

  function _releaseWakeLock() {
    if (!_wakeLock) return;
    _wakeLock.release().catch(function () {});
    _wakeLock = null;
  }

  function _walkOwnedByUser(walk, user) {
    return !!(walk && walk.operator && user && walk.operator.id === user.id);
  }

  function _syncAll() {
    if (_syncing || !c.user || !navigator.onLine) return Promise.resolve();
    _syncing = true;
    var chain = Promise.resolve();
    c.queue
      .slice()
      .filter(function (walk) {
        return _walkOwnedByUser(walk, c.user);
      })
      .forEach(function (walk) {
        chain = chain.then(function () {
          return _syncWalk(walk);
        });
      });
    if (c.projectDirty && c.currentProject) {
      chain = chain.then(function () {
        return _saveProject(false);
      });
    }
    return chain
      .then(function () {
        c.online = true;
        return _persist();
      })
      .then(
        function (value) {
          _syncing = false;
          _render();
          return value;
        },
        function (err) {
          _syncing = false;
          _render();
          throw err;
        },
      );
  }

  function _syncWalk(walk) {
    var chain = Promise.resolve();
    if (!walk.serverCreated) {
      chain = chain.then(function () {
        return _api("/sessions", {
          method: "POST",
          body: {
            id: walk.id,
            campaign_id: walk.campaign_id,
            territory_id: walk.territory_id,
            project_id: walk.project_id,
            device_id: walk.device_id,
            participant_ids: walk.participant_ids,
            started_by_user_id: walk.operator && walk.operator.id,
            started_at: walk.started_at,
          },
        }).then(function (body) {
          walk.serverCreated = true;
          walk.revision = body.walk.revision || 1;
        });
      });
    }
    chain = chain.then(function () {
      var pending = walk.points.slice(walk.syncedCount || 0);
      if (!pending.length) return;
      return _api("/sessions/" + encodeURIComponent(walk.id) + "/points", {
        method: "POST",
        body: { points: pending },
      }).then(function () {
        walk.syncedCount = walk.points.length;
      });
    });
    chain = chain.then(function _syncNextStatus() {
      var statuses = _statusQueue(walk);
      if (!statuses.length) return;
      var body = Object.assign({}, statuses[0]);
      if (walk.revision != null) body.expected_revision = walk.revision;
      return _api("/sessions/" + encodeURIComponent(walk.id), {
        method: "PATCH",
        body: body,
      }).then(function (res) {
        walk.revision = res.walk.revision;
        statuses.shift();
        return _persist().then(_syncNextStatus);
      });
    });
    chain = chain.then(function () {
      if (!walk.roads || walk.roadsSynced === true) return;
      return _api("/sessions/" + encodeURIComponent(walk.id) + "/roads", {
        method: "PUT",
        body: { roads: walk.roads },
      }).then(function () {
        walk.roadsSynced = true;
      });
    });
    return chain.then(function () {
      if (
        walk.status === "finished" &&
        walk.serverCreated &&
        walk.syncedCount === walk.points.length &&
        !_statusQueue(walk).length &&
        (walk.roadsSynced || !walk.roads)
      ) {
        c.queue = c.queue.filter(function (x) {
          return x.id !== walk.id;
        });
      }
      return _persist();
    });
  }

  function _roadKey(feature) {
    var p = (feature && feature.properties) || {};
    var raw = feature.id || p.osmid || p.osm_id || "";
    if (Array.isArray(raw)) raw = raw.join("-");
    if (raw !== "") return "way_" + String(raw).replace(/[^A-Za-z0-9._:-]/g, "_");
    var text = JSON.stringify(feature.geometry || {}) + "|" + (p.name || "");
    var hash = 5381;
    for (var i = 0; i < text.length; i++) hash = ((hash << 5) + hash) ^ text.charCodeAt(i);
    return "road_h" + (hash >>> 0).toString(16);
  }

  function _calculateRoadCoverage(walk) {
    if (!window.turf || walk.points.length < 2 || !s.cachedStreets) {
      walk.roads = [];
      return;
    }
    var trace = turf.lineString(
      walk.points.map(function (p) {
        return [p.lon, p.lat];
      }),
    );
    var territory = walk.territory_geometry;
    var results = [];
    (s.cachedStreets.features || []).forEach(function (feature) {
      var geom = feature.geometry || {};
      var lines =
        geom.type === "LineString"
          ? [geom.coordinates]
          : geom.type === "MultiLineString"
            ? geom.coordinates
            : [];
      var approxTotal = 0;
      var approxCovered = 0;
      lines.forEach(function (coords) {
        if (!coords || coords.length < 2) return;
        var line = turf.lineString(coords);
        var lengthM = turf.length(line, { units: "kilometers" }) * 1000;
        if (!(lengthM > 0)) return;
        var samples = Math.max(1, Math.ceil(lengthM / ROAD_SAMPLE_M));
        var sampleLengthM = lengthM / samples;
        // Midpoints avoid counting both endpoints as a full sample. Each
        // retained point represents exactly one slice of this road.
        for (var i = 0; i < samples; i++) {
          var point = turf.along(
            line,
            ((i + 0.5) * sampleLengthM) / 1000,
            { units: "kilometers" },
          );
          if (territory && !turf.booleanPointInPolygon(point, territory)) continue;
          approxTotal += sampleLengthM;
          if (
            turf.pointToLineDistance(point, trace, { units: "meters" }) <= ROAD_MATCH_M
          )
            approxCovered += sampleLengthM;
        }
      });
      if (!(approxTotal > 0) || !(approxCovered > 0)) return;
      results.push({
        road_key: _roadKey(feature),
        road_name: (feature.properties || {}).name || "",
        covered_m: Math.min(approxTotal, approxCovered),
        total_m: approxTotal,
      });
    });
    walk.roads = results;
    walk.roadsSynced = false;
  }

  function _drawCoverage() {
    if (!_coverageLayers || !_map) return;
    _coverageLayers.clearLayers();
    var sessions = _filteredCoverage();
    var roadKeys = {};
    var usedTerritories = {};
    sessions.forEach(function (walk) {
      if (walk.territory_id) usedTerritories[walk.territory_id] = true;
      var pts = walk.points || [];
      if (pts.length > 1) {
        L.polyline(
          pts.map(function (p) {
            return [p.lat, p.lon];
          }),
          {
            pane: "fieldPane",
            color: walk.status === "finished" ? "#00897b" : "#f9a825",
            weight: 4,
            opacity: 0.68,
          },
        )
          .bindTooltip(
            (walk.participants || [])
              .map(function (p) {
                return p.name;
              })
              .join(" + ") +
              " · " +
              _fmtTime(walk.started_at),
          )
          .addTo(_coverageLayers);
      }
      (walk.roads || []).forEach(function (r) {
        if (Number(r.covered_m) > 0) roadKeys[r.road_key] = true;
      });
    });

    c.territories.forEach(function (t) {
      if (
        c.filters.territory &&
        c.filters.territory !== t.id
      )
        return;
      var done = !!usedTerritories[t.id];
      L.geoJSON({ type: "Feature", geometry: t.geometry, properties: {} }, {
        pane: "fieldPane",
        style: {
          color: done ? "#2e7d32" : "#c62828",
          weight: 2,
          opacity: 0.65,
          fillOpacity: 0.025,
          dashArray: done ? null : "7 7",
        },
      })
        .bindTooltip(t.label + (done ? " · walked" : " · not walked"))
        .addTo(_coverageLayers);
    });

    c.roadStats = { covered: 0, total: 0 };
    if (s.cachedStreets && c.territories.length) {
      var visibleTerritories = c.territories.filter(function (t) {
        return !c.filters.territory || c.filters.territory === t.id;
      });
      var territoryFeatures = visibleTerritories.map(function (t) {
        return { type: "Feature", properties: {}, geometry: t.geometry };
      });
      var relevantRoads = (s.cachedStreets.features || []).filter(function (f) {
        return territoryFeatures.some(function (territory) {
          try {
            return turf.booleanIntersects(f, territory);
          } catch (_) {
            return false;
          }
        });
      });
      c.roadStats.total = relevantRoads.length;
      c.roadStats.covered = relevantRoads.filter(function (f) {
        return !!roadKeys[_roadKey(f)];
      }).length;
      if (relevantRoads.length) {
        L.geoJSON(
          { type: "FeatureCollection", features: relevantRoads },
          {
            pane: "fieldPane",
            style: function (feature) {
              var covered = !!roadKeys[_roadKey(feature)];
              return covered
                ? { color: "#1b5e20", weight: 7, opacity: 0.72 }
                : { color: "#c62828", weight: 3, opacity: 0.42, dashArray: "5 7" };
            },
          },
        ).addTo(_coverageLayers);
      }
    }
  }

  function _focusSession(id) {
    var walk = c.coverage.find(function (x) {
      return x.id === id;
    });
    if (!walk || !walk.points || !walk.points.length) return;
    var bounds = L.latLngBounds(
      walk.points.map(function (p) {
        return [p.lat, p.lon];
      }),
    );
    if (bounds.isValid()) {
      _map.fitBounds(bounds.pad(0.15));
      close();
    }
  }

  function _download(name, type, text) {
    var url = URL.createObjectURL(new Blob([text], { type: type }));
    var a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () {
      URL.revokeObjectURL(url);
    }, 1000);
  }

  function _exportGeoJSON() {
    var features = _filteredCoverage()
      .filter(function (x) {
        return x.points && x.points.length > 1;
      })
      .map(function (x) {
        return {
          type: "Feature",
          properties: {
            session_id: x.id,
            started_at: x.started_at,
            finished_at: x.finished_at,
            territory: x.territory_label,
            participants: (x.participants || []).map(function (p) { return p.name; }).join(", "),
            leaflet_count: x.leaflet_count,
          },
          geometry: {
            type: "LineString",
            coordinates: x.points.map(function (p) {
              return [p.lon, p.lat];
            }),
          },
        };
      });
    _download(
      "field-coverage-" + new Date().toISOString().slice(0, 10) + ".geojson",
      "application/geo+json",
      JSON.stringify({ type: "FeatureCollection", features: features }, null, 2),
    );
  }

  function _exportCSV() {
    var rows = [
      ["session_id", "started_at", "finished_at", "participants", "territory", "distance_km", "leaflets", "gps_points"],
    ];
    _filteredCoverage().forEach(function (x) {
      rows.push([
        x.id,
        x.started_at || "",
        x.finished_at || "",
        (x.participants || []).map(function (p) { return p.name; }).join(" + "),
        x.territory_label || "",
        (_traceDistance(x.points || []) / 1000).toFixed(3),
        x.leaflet_count == null ? "" : x.leaflet_count,
        (x.points || []).length,
      ]);
    });
    var csv = rows
      .map(function (row) {
        return row
          .map(function (v) {
            return '"' + String(v).replace(/"/g, '""') + '"';
          })
          .join(",");
      })
      .join("\r\n");
    _download(
      "field-sessions-" + new Date().toISOString().slice(0, 10) + ".csv",
      "text/csv;charset=utf-8",
      csv,
    );
  }

  function _showError(err) {
    console.warn(">>> Field:", err);
    c.lastError = (err && err.message) || "Something went wrong.";
    alert(c.lastError);
    _render();
  }

  return {
    init: init,
    open: open,
    close: close,
    isOpen: isOpen,
    projectDirty: projectDirty,
    refreshCoverage: _refreshCampaignData,
    _test: {
      distanceM: _distanceM,
      traceDistance: _traceDistance,
      roadKey: _roadKey,
      statusQueue: _statusQueue,
      walkOwnedByUser: _walkOwnedByUser,
    },
  };
})();

window.App = App;
