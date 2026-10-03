(function () {
  "use strict";

  var authCard = document.getElementById("gpx-auth");
  var importer = document.getElementById("gpx-importer");
  var loginForm = document.getElementById("gpx-login");
  var fileInput = document.getElementById("gpx-file");
  var previewButton = document.getElementById("gpx-preview-button");
  var importButton = document.getElementById("gpx-import-button");
  var campaignSelect = document.getElementById("gpx-campaign");
  var territorySelect = document.getElementById("gpx-territory");
  var walkers = document.getElementById("gpx-walkers");
  var fileStatus = document.getElementById("gpx-file-status");
  var importStatus = document.getElementById("gpx-import-status");
  var previewCard = document.getElementById("gpx-preview-card");

  var state = { user: null, people: [], campaigns: [], preview: null };

  function setStatus(node, text, kind) {
    node.textContent = text || "";
    node.classList.toggle("is-error", kind === "error");
    node.classList.toggle("is-success", kind === "success");
  }

  function requestJson(url, options) {
    options = options || {};
    options.credentials = "same-origin";
    return fetch(url, options).then(function (response) {
      return response
        .json()
        .catch(function () {
          return {};
        })
        .then(function (body) {
          if (!response.ok) {
            var error = new Error(body.error || "Request failed (" + response.status + ")");
            error.status = response.status;
            error.body = body;
            throw error;
          }
          return body;
        });
    });
  }

  function selectedFile() {
    return fileInput.files && fileInput.files[0];
  }

  function checkedParticipants() {
    return Array.prototype.slice
      .call(walkers.querySelectorAll('input[type="checkbox"]:checked'))
      .map(function (input) {
        return input.value;
      });
  }

  function renderPeople() {
    walkers.innerHTML = "";
    state.people.forEach(function (person) {
      var label = document.createElement("label");
      label.className = "gpx-walker";
      var input = document.createElement("input");
      input.type = "checkbox";
      input.value = person.id;
      if (state.user && person.id === state.user.person_id) input.checked = true;
      var span = document.createElement("span");
      span.textContent = person.name;
      label.appendChild(input);
      label.appendChild(span);
      walkers.appendChild(label);
    });
  }

  function renderCampaigns() {
    campaignSelect.innerHTML = '<option value="">Choose campaign…</option>';
    state.campaigns.forEach(function (campaign) {
      var option = document.createElement("option");
      option.value = campaign.id;
      option.textContent = campaign.name + " · " + campaign.status;
      campaignSelect.appendChild(option);
    });
    var active = state.campaigns.find(function (campaign) {
      return campaign.status === "active";
    });
    if (active) campaignSelect.value = active.id;
    loadTerritories();
  }

  function loadTerritories() {
    territorySelect.innerHTML =
      '<option value="">Whole round / no territory</option>';
    if (!campaignSelect.value) return Promise.resolve();
    return requestJson(
      "/service/field/territories?campaign_id=" +
        encodeURIComponent(campaignSelect.value),
    )
      .then(function (body) {
        (body.territories || []).forEach(function (territory) {
          var option = document.createElement("option");
          option.value = territory.id;
          option.textContent = territory.label;
          territorySelect.appendChild(option);
        });
      })
      .catch(function (error) {
        setStatus(importStatus, error.message, "error");
      });
  }

  function loadCatalog() {
    return requestJson("/service/field/auth/status")
      .then(function (status) {
        state.user = status.user;
        state.people = status.people || [];
        if (!status.user) {
          authCard.hidden = false;
          importer.hidden = true;
          return null;
        }
        authCard.hidden = true;
        importer.hidden = false;
        renderPeople();
        return requestJson("/service/field/campaigns");
      })
      .then(function (body) {
        if (!body) return;
        state.campaigns = body.campaigns || [];
        renderCampaigns();
      })
      .catch(function (error) {
        setStatus(importStatus, error.message, "error");
      });
  }

  function formatDistance(metres) {
    if (!isFinite(metres)) return "—";
    return metres >= 1000
      ? (metres / 1000).toFixed(2) + " km"
      : Math.round(metres) + " m";
  }

  function formatDuration(start, end) {
    var ms = new Date(end).getTime() - new Date(start).getTime();
    if (!isFinite(ms) || ms < 0) return "—";
    var minutes = Math.round(ms / 60000);
    var hours = Math.floor(minutes / 60);
    var remainder = minutes % 60;
    return hours ? hours + "h " + remainder + "m" : minutes + "m";
  }

  function renderTrace(points) {
    var path = document.getElementById("gpx-trace-path");
    var start = document.getElementById("gpx-trace-start");
    var finish = document.getElementById("gpx-trace-finish");
    if (!points || points.length < 2) {
      path.setAttribute("d", "");
      return;
    }
    var minLat = Math.min.apply(
      null,
      points.map(function (p) {
        return p.lat;
      }),
    );
    var maxLat = Math.max.apply(
      null,
      points.map(function (p) {
        return p.lat;
      }),
    );
    var minLon = Math.min.apply(
      null,
      points.map(function (p) {
        return p.lon;
      }),
    );
    var maxLon = Math.max.apply(
      null,
      points.map(function (p) {
        return p.lon;
      }),
    );
    var width = 700;
    var height = 220;
    var pad = 12;
    var lonSpan = Math.max(maxLon - minLon, 0.000001);
    var latSpan = Math.max(maxLat - minLat, 0.000001);

    function xy(point) {
      return {
        x: pad + ((point.lon - minLon) / lonSpan) * (width - pad * 2),
        y: pad + ((maxLat - point.lat) / latSpan) * (height - pad * 2),
      };
    }

    var coords = points.map(xy);
    path.setAttribute(
      "d",
      coords
        .map(function (p, index) {
          return (index ? "L" : "M") + p.x.toFixed(1) + " " + p.y.toFixed(1);
        })
        .join(" "),
    );
    start.setAttribute("cx", coords[0].x);
    start.setAttribute("cy", coords[0].y);
    finish.setAttribute("cx", coords[coords.length - 1].x);
    finish.setAttribute("cy", coords[coords.length - 1].y);
  }

  function renderPreview(body) {
    state.preview = body;
    previewCard.hidden = false;
    document.getElementById("gpx-distance").textContent = formatDistance(
      body.distance_m,
    );
    document.getElementById("gpx-points").textContent = body.point_count;
    document.getElementById("gpx-duration").textContent = formatDuration(
      body.started_at,
      body.finished_at,
    );
    document.getElementById("gpx-time-range").textContent =
      new Date(body.started_at).toLocaleString() +
      " → " +
      new Date(body.finished_at).toLocaleString();
    renderTrace(body.preview_points || []);
    if (body.duplicate) {
      setStatus(
        fileStatus,
        "Already imported as " + body.duplicate_session_id,
        "error",
      );
    } else {
      setStatus(fileStatus, "Trace looks valid.", "success");
    }
  }

  loginForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var values = new FormData(loginForm);
    setStatus(importStatus, "Signing in…");
    requestJson("/service/field/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: values.get("username"),
        password: values.get("password"),
      }),
    })
      .then(loadCatalog)
      .catch(function (error) {
        setStatus(importStatus, error.message, "error");
      });
  });

  campaignSelect.addEventListener("change", loadTerritories);

  fileInput.addEventListener("change", function () {
    state.preview = null;
    previewCard.hidden = true;
    setStatus(fileStatus, selectedFile() ? selectedFile().name : "");
  });

  previewButton.addEventListener("click", function () {
    if (!selectedFile()) {
      setStatus(fileStatus, "Choose a GPX file first.", "error");
      return;
    }
    var form = new FormData();
    form.append("file", selectedFile());
    previewButton.disabled = true;
    setStatus(fileStatus, "Checking GPX…");
    requestJson("/service/field/imports/gpx/preview", {
      method: "POST",
      body: form,
    })
      .then(renderPreview)
      .catch(function (error) {
        state.preview = null;
        previewCard.hidden = true;
        setStatus(fileStatus, error.message, "error");
      })
      .finally(function () {
        previewButton.disabled = false;
      });
  });

  importButton.addEventListener("click", function () {
    if (!selectedFile() || !state.preview) {
      setStatus(importStatus, "Preview the GPX before importing it.", "error");
      return;
    }
    if (state.preview.duplicate) {
      setStatus(importStatus, "That walk is already imported.", "error");
      return;
    }
    if (!campaignSelect.value) {
      setStatus(importStatus, "Choose a campaign.", "error");
      return;
    }
    var participants = checkedParticipants();
    if (!participants.length) {
      setStatus(importStatus, "Choose at least one walker.", "error");
      return;
    }

    var form = new FormData();
    form.append("file", selectedFile());
    form.append("campaign_id", campaignSelect.value);
    if (territorySelect.value)
      form.append("territory_id", territorySelect.value);
    participants.forEach(function (id) {
      form.append("participant_id", id);
    });
    var leaflets = document.getElementById("gpx-leaflets").value.trim();
    if (leaflets) form.append("leaflet_count", leaflets);

    importButton.disabled = true;
    setStatus(importStatus, "Importing walk…");
    requestJson("/service/field/imports/gpx", {
      method: "POST",
      body: form,
    })
      .then(function (body) {
        state.preview.duplicate = true;
        state.preview.duplicate_session_id = body.walk.id;
        setStatus(
          importStatus,
          "Imported " +
            formatDistance(body.walk.distance_m) +
            " as " +
            body.walk.id +
            ". It is now in campaign history.",
          "success",
        );
        setStatus(fileStatus, "Imported successfully.", "success");
      })
      .catch(function (error) {
        if (error.body && error.body.duplicate) {
          state.preview.duplicate = true;
          state.preview.duplicate_session_id = error.body.session_id;
        }
        setStatus(importStatus, error.message, "error");
      })
      .finally(function () {
        importButton.disabled = false;
      });
  });

  loadCatalog();
})();
