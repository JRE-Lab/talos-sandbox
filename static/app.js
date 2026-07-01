/* ============================================================================
   TALOS Sandbox — front-end controller (vanilla JS, no build step)

   Architecture:
     - ONE renderer (renderEvent) drives the feed + fleet tiles from §3 events.
     - An EventSource ABSTRACTION feeds that renderer in two modes:
         REPLAY: fetch full transcript, pace client-side from event `t` deltas.
         LIVE:   open SSE on /api/live/{run_id}/stream, feed the same renderer.
     - Every fetch is guarded; the replay path works with the model/network down.

   Contract refs: §3 event envelope, §4 transcript schema, §5 HTTP API,
   §6 audit chain, §10 front-end behavior.
   ============================================================================ */
(function () {
  "use strict";

  // --------------------------------------------------------------------------
  // Constants
  // --------------------------------------------------------------------------
  var FALLBACK_SCENARIOS = [
    { id: "s1",  name: "Clean rollout",       moment: "the happy path — every gate passes" },
    { id: "s2",  name: "Block the bad plan",  moment: "the Critic rejects a reckless plan" },
    { id: "s3",  name: "Catch the regression",moment: "a bad build is rolled back at canary" },
    { id: "s3b", name: "Escape to ring",      moment: "regression escapes canary → fleet rollback" },
    { id: "s4",  name: "Verify the audit",    moment: "the audit chain proves itself" }
  ];

  var FLEET_LAYOUT = {
    canary: ["TALOS-CANARY"],
    ring1: ["TALOS-R1A", "TALOS-R1B"]
  };

  // pacing: max wall-clock seconds we allow a single inter-event gap to consume
  var MAX_GAP_S = 3.0;          // clamp long soaks in normal Play
  var FF_GAP_S = 0.12;          // fast-forward collapses gaps

  var ACTORS = ["system", "planner", "critic", "monitor", "executor", "fleet", "diagnostician"];

  // --------------------------------------------------------------------------
  // DOM handles
  // --------------------------------------------------------------------------
  var $ = function (id) { return document.getElementById(id); };
  var el = {
    videoLink:    $("video-link"),
    scenarioList: $("scenario-list"),
    coach:        $("coach-text"),
    fleetRings:   $("fleet-rings"),
    feed:         $("event-feed"),
    modeLabel:    $("mode-label"),
    soakTimer:    $("soak-timer"),
    soakRing:     $("soak-ring"),
    soakRemaining:$("soak-remaining"),
    // controls
    ctlRestart:   $("ctl-restart"),
    ctlStep:      $("ctl-step"),
    ctlPlay:      $("ctl-play"),
    ctlPause:     $("ctl-pause"),
    ctlFf:        $("ctl-ff"),
    ctlApprove:   $("ctl-approve"),
    // live
    livePanel:    $("panel-live"),
    liveInput:    $("live-directive"),
    liveRunBtn:   $("live-run-btn"),
    liveStatus:   $("live-status-line"),
    liveRefusal:  $("live-refusal"),
    // audit
    auditVerify:  $("audit-verify"),
    tamperSelect: $("tamper-select"),
    auditTamper:  $("audit-tamper"),
    auditVerdict: $("audit-verdict"),
    auditRecords: $("audit-records")
  };

  // --------------------------------------------------------------------------
  // Application state
  // --------------------------------------------------------------------------
  var state = {
    mode: "replay",            // "replay" | "live"
    scenarioId: null,          // current scenario id (for audit)
    events: [],                // loaded transcript events
    cursor: 0,                 // index of next event to render
    playing: false,
    fastForward: false,
    timer: null,               // setTimeout handle for paced play
    soakInterval: null,        // setInterval handle for the soak countdown
    awaitingApproval: false,   // true while paused on approval_required
    eventSource: null,         // live SSE handle
    hosts: {}                  // host -> last-known {version, health}
  };

  // --------------------------------------------------------------------------
  // Tiny helpers
  // --------------------------------------------------------------------------
  function esc(s) {
    if (s === null || s === undefined) { return ""; }
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function clearChildren(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }
  function safeActor(a) { return ACTORS.indexOf(a) >= 0 ? a : "system"; }

  async function getJSON(url) {
    var res = await fetch(url, { headers: { "Accept": "application/json" } });
    if (!res.ok) { throw new Error("HTTP " + res.status); }
    return res.json();
  }

  // --------------------------------------------------------------------------
  // Header: video link
  // --------------------------------------------------------------------------
  function initVideoLink() {
    try {
      var url = (typeof window !== "undefined" && window.VIDEO_URL) || "REPLACE_WITH_VIDEO_URL";
      if (el.videoLink) { el.videoLink.setAttribute("href", url); }
    } catch (e) { /* non-fatal */ }
  }

  // --------------------------------------------------------------------------
  // Fleet tiles
  // --------------------------------------------------------------------------
  function renderFleetSkeleton() {
    clearChildren(el.fleetRings);
    // canary ring
    el.fleetRings.appendChild(buildRingGroup("canary", FLEET_LAYOUT.canary, false));
    // ring1 with HA pair
    el.fleetRings.appendChild(buildRingGroup("ring1", FLEET_LAYOUT.ring1, true));
  }

  function buildRingGroup(ring, hosts, paired) {
    var group = document.createElement("div");
    group.className = "ring-group";
    group.dataset.ring = ring;

    var label = document.createElement("div");
    label.className = "ring-label";
    label.innerHTML = '<span class="ring-tag">' + esc(ring) + "</span>" +
      "<span>" + (ring === "canary" ? "canary" : "production") + "</span>";
    group.appendChild(label);

    var row = document.createElement("div");
    row.className = "tile-row" + (paired ? " paired ha-pair" : "");
    hosts.forEach(function (h) { row.appendChild(buildTile(h, ring, paired)); });
    group.appendChild(row);
    return group;
  }

  function buildTile(host, ring, paired) {
    var tile = document.createElement("div");
    tile.className = "host-tile";
    tile.id = "tile-" + host;
    tile.dataset.host = host;
    var role = ring === "canary" ? "canary" : (host === "TALOS-R1A" ? "prod · HA-A" : "prod · HA-B");
    tile.innerHTML =
      '<span class="dot"></span>' +
      '<div class="tile-body">' +
        '<span class="tile-host">' + esc(host) + "</span>" +
        '<span class="tile-meta">' +
          '<span class="tile-version" data-v>—</span>' +
          '<span class="tile-role">' + esc(role) + "</span>" +
        "</span>" +
      "</div>";
    return tile;
  }

  function setTileState(host, opts) {
    var tile = $("tile-" + host);
    if (!tile) { return; }
    var rec = state.hosts[host] || (state.hosts[host] = {});
    if (opts.version !== undefined && opts.version !== null) {
      var vEl = tile.querySelector("[data-v]");
      if (vEl && vEl.textContent !== opts.version) {
        vEl.textContent = opts.version;
        vEl.classList.remove("changed");
        // restart the flash
        void vEl.offsetWidth;
        vEl.classList.add("changed");
      }
      rec.version = opts.version;
    }
    if (opts.health) {
      tile.classList.remove("health-green", "health-amber", "health-red");
      tile.classList.add("health-" + opts.health);
      rec.health = opts.health;
    }
  }

  // --------------------------------------------------------------------------
  // THE RENDERER — one entry point for both replay and live
  // Applies an event to the feed and (where relevant) the fleet tiles.
  // Never throws on an unknown type.
  // --------------------------------------------------------------------------
  function renderEvent(ev) {
    if (!ev || typeof ev !== "object") { return; }
    var type = ev.type || "narration";
    var actor = safeActor(ev.actor);
    var data = ev.data || {};

    // tile-state side effects (topology / health / deploy / rollback)
    try {
      applyTileSideEffects(type, data);
    } catch (e) { /* never let a tile update break the feed */ }

    // soak countdown side effect
    if (type === "soak") {
      try { startSoakCountdown(data); } catch (e) { /* ignore */ }
    }

    // approval gating
    if (type === "approval_required") {
      state.awaitingApproval = true;
      enableApprove(true);
      pause(); // hold here until the judge clicks Approve (replay) or live advances
    }

    // build and append the feed line
    var line = buildFeedLine(ev, type, actor, data);
    appendFeedLine(line);

    if (type === "done") {
      onDone(data);
    }
  }

  function applyTileSideEffects(type, data) {
    if (type === "topology" && Array.isArray(data.hosts)) {
      data.hosts.forEach(function (h) {
        if (h && h.host) { setTileState(h.host, { version: h.version, health: h.health }); }
      });
    } else if (type === "health" && data.host) {
      setTileState(data.host, { health: data.health });
    } else if (type === "deploy" && data.host) {
      setTileState(data.host, { version: data.to_version });
    } else if (type === "rollback" && Array.isArray(data.hosts)) {
      data.hosts.forEach(function (h) { setTileState(h, { version: data.to_version }); });
    }
  }

  // --------------------------------------------------------------------------
  // Feed line construction (type-specific bodies)
  // --------------------------------------------------------------------------
  function buildFeedLine(ev, type, actor, data) {
    var div = document.createElement("div");
    div.className = "ev actor-" + actor + " type-" + type;

    // emphasis classes
    if (type === "verdict" && data.decision === "reject") { div.classList.add("emph-reject"); }
    if (type === "verdict" && data.decision === "approve") { div.classList.add("emph-approve"); }
    if (type === "rollback") { div.classList.add("emph-rollback"); }
    if (type === "breach") { div.classList.add("emph-breach"); }

    var tLabel = (typeof ev.t === "number") ? ("t+" + ev.t.toFixed(1) + "s") : "";
    var title = ev.title || defaultTitle(type, data);
    var body = ev.body || "";

    var html =
      '<div class="ev-time">' + esc(tLabel) + "</div>" +
      '<div class="ev-actor">' + esc(actor) + "</div>" +
      '<div class="ev-main">' +
        '<div class="ev-title">' + esc(title) + "</div>" +
        (body ? '<div class="ev-body">' + esc(body) + "</div>" : "") +
        '<div class="ev-data">' + renderData(type, data) + "</div>" +
      "</div>";
    div.innerHTML = html;
    return div;
  }

  function defaultTitle(type, data) {
    switch (type) {
      case "directive": return "Directive received";
      case "topology": return "Fleet topology";
      case "plan": return "Plan authored";
      case "critique": return "Critique";
      case "verdict": return data.decision === "reject" ? "Plan REJECTED" : "Plan approved";
      case "revision": return "Plan revised";
      case "deploy": return "Deploy";
      case "gate_eval": return data.result === "fail" ? "Gate FAILED" : "Gate passed";
      case "health": return "Health update";
      case "soak": return "Soak";
      case "approval_required": return "Approval required";
      case "approved": return "Approved";
      case "breach": return "BREACH detected";
      case "rollback": return "ROLLBACK";
      case "narration": return "";
      case "done": return "Run complete";
      default: return type;
    }
  }

  // type-specific data payload renderers; default is a generic line
  function renderData(type, data) {
    try {
      switch (type) {
        case "directive": return renderDirective(data);
        case "plan": return renderPlan(data, "plan");
        case "revision": return renderPlan(data, "revision");
        case "critique": return renderCritique(data);
        case "verdict": return renderVerdict(data);
        case "deploy": return renderDeploy(data);
        case "gate_eval": return renderGateEval(data);
        case "health": return renderHealth(data);
        case "soak": return renderSoak(data);
        case "approval_required": return renderApprovalReq(data);
        case "approved": return chip("approved " + esc(data.ring || ""), "res-pass");
        case "breach": return renderBreach(data);
        case "rollback": return renderRollback(data);
        case "topology": return renderTopology(data);
        case "narration": return ""; // body carries the text
        case "done": return renderDone(data);
        default: return renderGeneric(type, data);
      }
    } catch (e) {
      return renderGeneric(type, data);
    }
  }

  function chip(text, cls) { return '<span class="chip ' + cls + '">' + text + "</span>"; }

  function renderGeneric(type, data) {
    var keys = data && typeof data === "object" ? Object.keys(data) : [];
    if (keys.length === 0) { return ""; }
    var parts = keys.slice(0, 6).map(function (k) {
      var v = data[k];
      if (v && typeof v === "object") { v = JSON.stringify(v); }
      return '<span class="m"><b>' + esc(k) + "</b> " + esc(v) + "</span>";
    });
    return '<div class="metrics">' + parts.join("") + "</div>";
  }

  function renderDirective(d) {
    var u = d.urgency || "routine";
    return chip(esc(u), "urgency-" + esc(u)) + (d.text ? esc(d.text) : "");
  }

  function renderTopology(d) {
    var n = Array.isArray(d.hosts) ? d.hosts.length : 0;
    return '<span class="m">' + n + " hosts initialized</span>";
  }

  function renderPlan(d, kind) {
    var html = "";
    if (kind === "plan" && d.risk) { html += chip("risk " + esc(d.risk), "risk-" + esc(d.risk)); }
    if (d.rationale) { html += '<div class="ev-body">' + esc(d.rationale) + "</div>"; }
    if (kind === "revision" && d.note) { html += '<div class="ev-body">' + esc(d.note) + "</div>"; }
    if (Array.isArray(d.steps) && d.steps.length) {
      html += '<ul class="steps">';
      d.steps.forEach(function (s) {
        var hosts = Array.isArray(s.hosts) ? s.hosts.join(", ") : "";
        var soak = (s.soak_s !== undefined && s.soak_s !== null) ? " · soak " + esc(s.soak_s) + "s" : "";
        html += "<li><span class=\"step-action\">" + esc(s.action || "step") + "</span> " +
          esc(s.ring || "") + (hosts ? " → " + esc(hosts) : "") + soak + "</li>";
      });
      html += "</ul>";
    }
    return html;
  }

  function renderCritique(d) {
    var sev = d.severity || "info";
    var html = chip(esc(sev), "sev-" + esc(sev));
    if (d.concern) { html += '<div class="ev-body">' + esc(d.concern) + "</div>"; }
    if (d.blast_radius) {
      html += '<div class="ev-body"><b>Blast radius:</b> ' + esc(d.blast_radius) + "</div>";
    }
    return html;
  }

  function renderVerdict(d) {
    var dec = d.decision || "approve";
    var html = chip(esc(dec).toUpperCase(), dec === "reject" ? "res-fail" : "res-pass");
    if (Array.isArray(d.reasons) && d.reasons.length) {
      html += '<ul class="violations">';
      d.reasons.forEach(function (r) { html += "<li>" + esc(r) + "</li>"; });
      html += "</ul>";
    }
    return html;
  }

  function renderDeploy(d) {
    return '<span class="m"><b>' + esc(d.host) + "</b> " +
      esc(d.from_version || "?") + " → " + esc(d.to_version || "?") + "</span>";
  }

  function metricCell(label, value, bad) {
    return '<span class="m' + (bad ? " bad" : "") + '"><b>' + esc(label) + "</b> " + esc(value) + "</span>";
  }

  function renderMetrics(m) {
    if (!m || typeof m !== "object") { return ""; }
    var bad = {};
    // mark obviously-bad cells for color (best effort, gate is authoritative)
    if (m.http_health !== undefined && Number(m.http_health) !== 200) { bad.http_health = true; }
    if (m.memory_mb !== undefined && Number(m.memory_mb) > 1500) { bad.memory_mb = true; }
    if (m.eventlog_errors !== undefined && Number(m.eventlog_errors) > 5) { bad.eventlog_errors = true; }
    if (m.service_state !== undefined && m.service_state !== "Running") { bad.service_state = true; }
    var cells = [];
    if (m.service_state !== undefined) { cells.push(metricCell("svc", m.service_state, bad.service_state)); }
    if (m.http_health !== undefined) { cells.push(metricCell("http", m.http_health, bad.http_health)); }
    if (m.eventlog_errors !== undefined) { cells.push(metricCell("errs", m.eventlog_errors, bad.eventlog_errors)); }
    if (m.memory_mb !== undefined) { cells.push(metricCell("mem", m.memory_mb + "MB", bad.memory_mb)); }
    return '<div class="metrics">' + cells.join("") + "</div>";
  }

  function renderGateEval(d) {
    var res = d.result || "pass";
    var html = chip(esc(res).toUpperCase(), res === "fail" ? "res-fail" : "res-pass");
    html += '<span class="m"><b>' + esc(d.host || "") + "</b> " + esc(d.gate || "") + "</span>";
    html += renderMetrics(d.metrics);
    if (Array.isArray(d.violations) && d.violations.length) {
      html += '<ul class="violations">';
      d.violations.forEach(function (v) { html += "<li>" + esc(v) + "</li>"; });
      html += "</ul>";
    }
    return html;
  }

  function renderHealth(d) {
    var color = d.health || "green";
    var dot = chip(esc(color), color === "red" ? "res-fail" : (color === "amber" ? "sev-warning" : "res-pass"));
    var m = {
      service_state: d.service_state,
      http_health: d.http_health,
      eventlog_errors: d.eventlog_errors,
      memory_mb: d.memory_mb
    };
    return '<span class="m"><b>' + esc(d.host || "") + "</b></span>" + dot + renderMetrics(m);
  }

  function renderSoak(d) {
    var ring = d.ring || "";
    var dur = (d.duration_s !== undefined) ? d.duration_s : "?";
    var rem = (d.remaining_s !== undefined) ? d.remaining_s : dur;
    return '<span class="m"><b>' + esc(ring) + "</b> soaking " + esc(rem) + "s / " + esc(dur) + "s</span>";
  }

  function renderApprovalReq(d) {
    var html = chip("gate · " + esc(d.ring || ""), "sev-warning");
    if (d.prompt) { html += '<div class="ev-body">' + esc(d.prompt) + "</div>"; }
    return html;
  }

  function renderBreach(d) {
    return '<span class="m bad"><b>' + esc(d.host || "") + "</b> " +
      esc(d.metric || "") + " = " + esc(d.value) + " (threshold " + esc(d.threshold) + ")</span>";
  }

  function renderRollback(d) {
    var scope = d.scope || "host";
    var hosts = Array.isArray(d.hosts) ? d.hosts.join(", ") : "";
    var html = chip("scope " + esc(scope), "scope-" + esc(scope));
    html += '<span class="m"><b>' + esc(hosts) + "</b> → " + esc(d.to_version || "?") + "</span>";
    if (d.reason) { html += '<div class="ev-body">' + esc(d.reason) + "</div>"; }
    return html;
  }

  function renderDone(d) {
    var html = "";
    if (d.outcome) { html += chip(esc(d.outcome), "res-pass"); }
    if (d.summary) { html += '<div class="ev-body">' + esc(d.summary) + "</div>"; }
    return html;
  }

  function appendFeedLine(node) {
    if (el.feed.querySelector(".feed-empty")) { clearChildren(el.feed); }
    el.feed.appendChild(node);
    el.feed.scrollTop = el.feed.scrollHeight;
  }

  // --------------------------------------------------------------------------
  // Soak countdown timer
  // --------------------------------------------------------------------------
  function startSoakCountdown(data) {
    stopSoakCountdown();
    var ring = data.ring || "";
    var remaining = Number(data.remaining_s);
    if (!isFinite(remaining)) { remaining = Number(data.duration_s) || 0; }
    el.soakRing.textContent = ring;
    el.soakRemaining.textContent = Math.max(0, Math.round(remaining)) + "s";
    el.soakTimer.hidden = false;
    // tick down on wall clock; this is purely visual flavor for the feed
    state.soakInterval = setInterval(function () {
      remaining -= 1;
      if (remaining <= 0 || !state.playing) {
        el.soakRemaining.textContent = "0s";
        if (remaining <= 0) { stopSoakCountdown(); }
        return;
      }
      el.soakRemaining.textContent = Math.round(remaining) + "s";
    }, 1000);
  }
  function stopSoakCountdown() {
    if (state.soakInterval) { clearInterval(state.soakInterval); state.soakInterval = null; }
  }
  function hideSoakTimer() {
    stopSoakCountdown();
    el.soakTimer.hidden = true;
  }

  function onDone() {
    stopPlay();
    hideSoakTimer();
    state.awaitingApproval = false;
    enableApprove(false);
  }

  // --------------------------------------------------------------------------
  // Mode label
  // --------------------------------------------------------------------------
  function setMode(mode) {
    state.mode = mode;
    if (mode === "live") {
      el.modeLabel.textContent = "Live — real model against the simulated fleet";
      el.modeLabel.className = "mode-label mode-live";
    } else {
      el.modeLabel.textContent = "Replaying a recorded run";
      el.modeLabel.className = "mode-label mode-replay";
    }
  }

  // --------------------------------------------------------------------------
  // REPLAY engine — client-side paced from event `t` deltas
  // --------------------------------------------------------------------------
  function resetFeed() {
    clearChildren(el.feed);
    var empty = document.createElement("div");
    empty.className = "feed-empty";
    empty.textContent = "Pick a scenario to begin, or run a live directive.";
    el.feed.appendChild(empty);
  }

  function loadTranscriptForReplay(transcript) {
    stopLive();
    stopPlay();
    hideSoakTimer();
    setMode("replay");
    state.events = Array.isArray(transcript.events) ? transcript.events.slice() : [];
    // ensure ordering by seq if present
    state.events.sort(function (a, b) { return (a.seq || 0) - (b.seq || 0); });
    state.cursor = 0;
    state.awaitingApproval = false;
    state.hosts = {};
    enableApprove(false);
    clearChildren(el.feed);
    if (state.events.length === 0) {
      resetFeed();
    }
    renderFleetSkeleton();
  }

  function stepReplay() {
    if (state.cursor >= state.events.length) { stopPlay(); return false; }
    var ev = state.events[state.cursor];
    state.cursor += 1;
    renderEvent(ev);
    // approval_required pauses inside renderEvent; caller checks awaitingApproval
    return true;
  }

  function gapToNext() {
    // wall-clock seconds to wait before rendering the next event
    if (state.cursor <= 0 || state.cursor >= state.events.length) { return 0; }
    var prev = state.events[state.cursor - 1];
    var next = state.events[state.cursor];
    var dt = (Number(next.t) || 0) - (Number(prev.t) || 0);
    if (!isFinite(dt) || dt < 0) { dt = 0; }
    var cap = state.fastForward ? FF_GAP_S : MAX_GAP_S;
    // compress: real sim-seconds map down; clamp to cap
    var wall = Math.min(dt, cap);
    if (state.fastForward) { wall = Math.min(wall, FF_GAP_S); }
    return wall * 1000;
  }

  function scheduleNext() {
    clearTimer();
    if (!state.playing) { return; }
    if (state.awaitingApproval) { return; }       // gated
    if (state.cursor >= state.events.length) { stopPlay(); return; }
    var delay = gapToNext();
    state.timer = setTimeout(function () {
      if (!state.playing || state.awaitingApproval) { return; }
      var advanced = stepReplay();
      if (advanced && state.playing && !state.awaitingApproval) {
        scheduleNext();
      } else if (!advanced) {
        stopPlay();
      }
    }, delay);
  }

  function clearTimer() {
    if (state.timer) { clearTimeout(state.timer); state.timer = null; }
  }

  function play() {
    if (state.mode !== "replay") { return; }
    if (state.cursor >= state.events.length) { return; }
    if (state.awaitingApproval) { return; }       // must Approve first
    state.playing = true;
    updateControlStates();
    // render the first event immediately if we're at the start
    if (state.cursor === 0) {
      stepReplay();
    }
    scheduleNext();
  }

  function pause() {
    state.playing = false;
    clearTimer();
    updateControlStates();
  }

  function stopPlay() {
    state.playing = false;
    clearTimer();
    updateControlStates();
  }

  function restart() {
    if (state.mode === "live") {
      // restarting a live run just clears the feed; can't replay a live stream
      stopLive();
    }
    stopPlay();
    hideSoakTimer();
    state.cursor = 0;
    state.awaitingApproval = false;
    state.hosts = {};
    enableApprove(false);
    clearChildren(el.feed);
    renderFleetSkeleton();
    if (state.events.length === 0) { resetFeed(); }
  }

  function stepOnce() {
    if (state.mode !== "replay") { return; }
    if (state.awaitingApproval) { return; }       // gated until Approve
    pause();
    stepReplay();
  }

  function fastForward() {
    if (state.mode !== "replay") { return; }
    state.fastForward = true;
    hideSoakTimer();
    if (!state.playing && !state.awaitingApproval) {
      play();
    } else {
      // already playing: re-schedule with the collapsed gap immediately
      clearTimer();
      scheduleNext();
    }
    // fast-forward is a momentary mode; revert after a short window so a later
    // Play paces normally again.
    setTimeout(function () { state.fastForward = false; }, 50);
  }

  function approve() {
    if (!state.awaitingApproval) { return; }
    state.awaitingApproval = false;
    enableApprove(false);
    hideSoakTimer();
    // resume play from where we paused
    if (state.mode === "replay") {
      state.playing = true;
      updateControlStates();
      scheduleNext();
    }
  }

  function enableApprove(on) {
    el.ctlApprove.disabled = !on;
  }

  function updateControlStates() {
    var hasEvents = state.events.length > 0 || state.mode === "live";
    var atEnd = state.mode === "replay" && state.cursor >= state.events.length;
    el.ctlPlay.disabled = state.mode === "live" || !hasEvents || atEnd || state.awaitingApproval || state.playing;
    el.ctlPause.disabled = !state.playing;
    el.ctlStep.disabled = state.mode === "live" || !hasEvents || atEnd || state.awaitingApproval;
    el.ctlFf.disabled = state.mode === "live" || !hasEvents || atEnd;
    el.ctlRestart.disabled = !hasEvents;
  }

  // --------------------------------------------------------------------------
  // Scenario picker
  // --------------------------------------------------------------------------
  async function loadScenarios() {
    var list = FALLBACK_SCENARIOS;
    try {
      var data = await getJSON("/api/scenarios");
      if (Array.isArray(data) && data.length) { list = data; }
    } catch (e) {
      // network/model down: keep the hardcoded list so the picker still works
      console.warn("scenarios fetch failed; using fallback list", e);
    }
    renderScenarioButtons(list);
  }

  function renderScenarioButtons(list) {
    clearChildren(el.scenarioList);
    list.forEach(function (s) {
      var b = document.createElement("button");
      b.className = "scenario-btn";
      b.dataset.id = s.id;
      var star = (s.id === "s2") ? '<span class="scn-star">start here</span>' : "";
      b.innerHTML =
        '<span class="scn-id">' + esc(s.id) + "</span>" +
        '<span class="scn-name">' + esc(s.name || s.id) + star + "</span>" +
        '<span class="scn-moment">' + esc(s.moment || s.summary || "") + "</span>";
      b.addEventListener("click", function () { selectScenario(s.id, b); });
      el.scenarioList.appendChild(b);
    });
  }

  function markActiveScenario(btn) {
    var all = el.scenarioList.querySelectorAll(".scenario-btn");
    for (var i = 0; i < all.length; i++) { all[i].classList.remove("active"); }
    if (btn) { btn.classList.add("active"); }
  }

  var SCENARIO_TIPS = {
    s1: 'Clean rollout &mdash; press <b>Play</b> and watch every gate pass: canary &rarr; ring1, fleet ends green.',
    s2: 'The Critic in action &mdash; watch it <b>REJECT</b> the reckless plan (both HA-pair hosts at once), then approve the safe revision. Press <b>Step</b> to read each turn.',
    s3: 'Watch the <b>soak timer</b> &mdash; the new build breaches mid-soak and TALOS <b>auto-rolls-back</b> the canary. Caught before the fleet.',
    s3c: 'The <b>headline</b> &mdash; caught &amp; rolled back, then a <b>Diagnostician</b> diagnoses the regression and TALOS <b>auto-heals</b> forward to the fixed 2.1.6. No human in the loop.',
    s3b: 'The regression clears the canary but breaches <b>after promotion</b> &rarr; a fleet-scale rollback. The gates still catch it.',
    s4: 'Audit demo &mdash; after it plays, click <b>Verify</b> (chain OK), then <b>Tamper</b> a record and Verify again &mdash; it <b>FAILS</b> at that link.'
  };

  async function selectScenario(id, btn) {
    markActiveScenario(btn);
    if (el.coach && SCENARIO_TIPS[id]) { el.coach.innerHTML = SCENARIO_TIPS[id]; }
    state.scenarioId = id;
    el.liveRefusal.hidden = true;
    // load transcript (replay path — must work with model/network down beyond this fetch)
    try {
      var transcript = await getJSON("/api/scenarios/" + encodeURIComponent(id));
      loadTranscriptForReplay(transcript);
      updateControlStates();
      // auto-start play for a smooth demo
      play();
    } catch (e) {
      console.warn("transcript fetch failed", e);
      showFeedError("Could not load scenario " + id + " (" + e.message + ").");
    }
    // load audit panel (independent fetch; failure is non-fatal)
    loadAudit(id);
  }

  function showFeedError(msg) {
    clearChildren(el.feed);
    var d = document.createElement("div");
    d.className = "feed-empty";
    d.textContent = msg;
    el.feed.appendChild(d);
  }

  // --------------------------------------------------------------------------
  // Audit panel
  // --------------------------------------------------------------------------
  async function loadAudit(id) {
    el.auditVerdict.hidden = true;
    el.auditVerdict.className = "audit-verdict";
    try {
      var data = await getJSON("/api/audit/" + encodeURIComponent(id));
      var records = (data && Array.isArray(data.records)) ? data.records : [];
      renderAuditRecords(records);
      populateTamperSelect(records);
      el.auditVerify.disabled = records.length === 0;
      el.tamperSelect.disabled = records.length === 0;
      el.auditTamper.disabled = records.length === 0;
    } catch (e) {
      console.warn("audit fetch failed", e);
      clearChildren(el.auditRecords);
      var li = document.createElement("li");
      li.className = "audit-empty";
      li.textContent = "Audit records unavailable.";
      el.auditRecords.appendChild(li);
      el.auditVerify.disabled = true;
      el.tamperSelect.disabled = true;
      el.auditTamper.disabled = true;
    }
  }

  function renderAuditRecords(records, brokenAt) {
    clearChildren(el.auditRecords);
    if (!records.length) {
      var empty = document.createElement("li");
      empty.className = "audit-empty";
      empty.textContent = "No audit records for this scenario.";
      el.auditRecords.appendChild(empty);
      return;
    }
    var brokenReached = false;
    records.forEach(function (r) {
      var li = document.createElement("li");
      li.className = "audit-rec";
      var isBroken = (brokenAt !== undefined && brokenAt !== null && r.id === brokenAt);
      if (isBroken) { li.classList.add("broken"); brokenReached = true; }
      else if (brokenReached) { li.classList.add("broken-after"); }

      var hashShort = r.hash ? String(r.hash).slice(0, 16) : "—";
      var prevShort = r.prev_hash ? String(r.prev_hash).slice(0, 12) : "—";
      li.innerHTML =
        '<div class="rec-top">' +
          '<span class="rec-id">#' + esc(r.id) + "</span>" +
          '<span class="rec-actor">' + esc(r.actor || "") + "</span>" +
          (isBroken ? '<span class="broken-flag">CHAIN BROKEN</span>'
                    : '<span class="rec-ts">' + esc(r.ts || "") + "</span>") +
        "</div>" +
        '<div class="rec-action">' + esc(r.action || "") + "</div>" +
        '<div class="rec-hash"><b>prev</b> ' + esc(prevShort) + "… · <b>hash</b> " + esc(hashShort) + "…</div>";
      el.auditRecords.appendChild(li);
    });
  }

  function populateTamperSelect(records) {
    clearChildren(el.tamperSelect);
    var ph = document.createElement("option");
    ph.value = "";
    ph.textContent = "record #…";
    el.tamperSelect.appendChild(ph);
    records.forEach(function (r) {
      var o = document.createElement("option");
      o.value = String(r.id);
      o.textContent = "#" + r.id + " — " + (r.action ? String(r.action).slice(0, 28) : "");
      el.tamperSelect.appendChild(o);
    });
  }

  async function verifyAudit() {
    if (!state.scenarioId) { return; }
    try {
      var data = await getJSON("/api/audit/" + encodeURIComponent(state.scenarioId) + "/verify");
      if (data && data.ok) {
        showAuditVerdict(true, "Chain OK — all " + countRecords() + " records verify against the hash chain.");
        // re-render clean (remove any prior broken highlight)
        reloadAuditClean();
      } else {
        var k = (data && data.broken_at !== undefined) ? data.broken_at : null;
        showAuditVerdict(false, "Chain FAILED at record #" + k + ".");
        highlightBroken(k);
      }
    } catch (e) {
      console.warn("verify failed", e);
      showAuditVerdict(false, "Verify request failed (" + e.message + ").");
    }
  }

  async function tamperAudit() {
    if (!state.scenarioId) { return; }
    var k = el.tamperSelect.value;
    if (!k) { showAuditVerdict(false, "Pick a record to tamper first."); return; }
    try {
      var url = "/api/audit/" + encodeURIComponent(state.scenarioId) +
                "/verify?tamper=" + encodeURIComponent(k);
      var data = await getJSON(url);
      var brokenAt = (data && data.broken_at !== undefined && data.broken_at !== null)
        ? data.broken_at : Number(k);
      showAuditVerdict(false,
        "Tampered record #" + k + " — chain FAILS at #" + brokenAt + ". Every link after it is invalidated.");
      highlightBroken(brokenAt);
    } catch (e) {
      console.warn("tamper failed", e);
      showAuditVerdict(false, "Tamper request failed (" + e.message + ").");
    }
  }

  function highlightBroken(brokenAt) {
    // re-fetch the clean records and re-render with the broken marker
    fetchAuditRecords().then(function (records) {
      renderAuditRecords(records, normalizeId(brokenAt, records));
    }).catch(function () { /* leave current view */ });
  }

  function reloadAuditClean() {
    fetchAuditRecords().then(function (records) {
      renderAuditRecords(records, null);
    }).catch(function () { /* leave current view */ });
  }

  function normalizeId(brokenAt, records) {
    // broken_at may be a number; records ids are numbers too. Coerce.
    var n = Number(brokenAt);
    if (isFinite(n)) { return n; }
    return brokenAt;
  }

  async function fetchAuditRecords() {
    var data = await getJSON("/api/audit/" + encodeURIComponent(state.scenarioId));
    return (data && Array.isArray(data.records)) ? data.records : [];
  }

  function countRecords() {
    return el.auditRecords.querySelectorAll(".audit-rec").length;
  }

  function showAuditVerdict(ok, msg) {
    el.auditVerdict.hidden = false;
    el.auditVerdict.className = "audit-verdict " + (ok ? "ok" : "fail");
    el.auditVerdict.textContent = (ok ? "✔ " : "✖ ") + msg;
  }

  // --------------------------------------------------------------------------
  // LIVE mode — status gate + run + SSE into the same renderer
  // --------------------------------------------------------------------------
  async function initLive() {
    try {
      var status = await getJSON("/api/live/status");
      if (!status || status.enabled === false) {
        hideLiveBox();
        return;
      }
      showLiveBox();
      updateLiveStatusLine(status);
    } catch (e) {
      // status unreachable: hide live box, replay still fully works
      console.warn("live status failed; hiding live box", e);
      hideLiveBox();
    }
  }

  function hideLiveBox() {
    if (el.livePanel) { el.livePanel.classList.add("hidden"); }
  }
  function showLiveBox() {
    if (el.livePanel) { el.livePanel.classList.remove("hidden"); }
  }

  function updateLiveStatusLine(status) {
    if (!status) { return; }
    var parts = [];
    if (status.remaining_runs_session !== undefined) {
      parts.push(status.remaining_runs_session + " run(s) left this session");
    }
    if (status.spend !== undefined && status.cap !== undefined) {
      parts.push("$" + Number(status.spend).toFixed(2) + " / $" + Number(status.cap).toFixed(2));
    }
    if (status.busy) { parts.push("busy"); }
    el.liveStatus.textContent = parts.join(" · ");
    el.liveRunBtn.disabled = !!status.busy;
  }

  async function runLive() {
    var directive = (el.liveInput.value || "").trim();
    el.liveRefusal.hidden = true;
    if (!directive) {
      el.liveStatus.textContent = "Type a directive first.";
      return;
    }
    el.liveRunBtn.disabled = true;
    el.liveStatus.textContent = "Requesting live run…";
    var res;
    try {
      res = await fetch("/api/live/run", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        body: JSON.stringify({ directive: directive })
      });
    } catch (e) {
      el.liveStatus.textContent = "Live request failed (network). Replay still works.";
      el.liveRunBtn.disabled = false;
      return;
    }

    if (res.status === 429 || res.status === 403) {
      // guardrail refusal — show reason + offer the fallback scenario
      var refusal = {};
      try { refusal = await res.json(); } catch (e) { /* ignore */ }
      showRefusal(refusal);
      el.liveRunBtn.disabled = false;
      return;
    }

    if (!res.ok) {
      el.liveStatus.textContent = "Live run rejected (HTTP " + res.status + "). Replay still works.";
      el.liveRunBtn.disabled = false;
      return;
    }

    var payload = {};
    try { payload = await res.json(); } catch (e) { /* ignore */ }
    var runId = payload && payload.run_id;
    if (!runId) {
      el.liveStatus.textContent = "Live run accepted but no run_id returned.";
      el.liveRunBtn.disabled = false;
      return;
    }
    el.liveStatus.textContent = "Live run accepted — streaming…";
    startLiveStream(runId);
  }

  function showRefusal(refusal) {
    var reason = (refusal && refusal.reason) || "Live run refused by the guardrail.";
    var fallback = refusal && refusal.fallback_scenario;
    clearChildren(el.liveRefusal);
    var msg = document.createElement("div");
    msg.textContent = "Live refused: " + reason;
    el.liveRefusal.appendChild(msg);
    if (fallback) {
      var btn = document.createElement("button");
      btn.className = "btn btn-primary fallback-btn";
      btn.textContent = "Play fallback scenario (" + fallback + ")";
      btn.addEventListener("click", function () {
        el.liveRefusal.hidden = true;
        var fb = el.scenarioList.querySelector('.scenario-btn[data-id="' + fallback + '"]');
        selectScenario(fallback, fb);
      });
      el.liveRefusal.appendChild(btn);
    }
    el.liveRefusal.hidden = false;
    el.liveStatus.textContent = "";
  }

  function startLiveStream(runId) {
    stopLive();
    stopPlay();
    hideSoakTimer();
    setMode("live");
    // fresh feed + fleet for the live run
    state.events = [];
    state.cursor = 0;
    state.awaitingApproval = false;
    state.hosts = {};
    enableApprove(false);
    clearChildren(el.feed);
    renderFleetSkeleton();
    updateControlStates();

    var url = "/api/live/" + encodeURIComponent(runId) + "/stream";
    var source;
    try {
      source = new EventSource(url);
    } catch (e) {
      el.liveStatus.textContent = "Could not open live stream. Falling back to replay.";
      el.liveRunBtn.disabled = false;
      setMode("replay");
      return;
    }
    state.eventSource = source;

    source.onmessage = function (msgEv) {
      var data;
      try { data = JSON.parse(msgEv.data); } catch (e) { return; }
      try { renderEvent(data); } catch (e) { /* never crash on a bad frame */ }
      if (data && data.type === "done") {
        stopLive();
        el.liveStatus.textContent = "Live run complete.";
        el.liveRunBtn.disabled = false;
        refreshLiveStatus();
      }
    };
    source.onerror = function () {
      // SSE error: close, surface, keep replay usable
      stopLive();
      el.liveStatus.textContent = "Live stream ended (or errored). Replay still works.";
      el.liveRunBtn.disabled = false;
      refreshLiveStatus();
    };
  }

  function stopLive() {
    if (state.eventSource) {
      try { state.eventSource.close(); } catch (e) { /* ignore */ }
      state.eventSource = null;
    }
  }

  async function refreshLiveStatus() {
    try {
      var status = await getJSON("/api/live/status");
      if (status && status.enabled === false) { hideLiveBox(); return; }
      updateLiveStatusLine(status);
    } catch (e) { /* non-fatal */ }
  }

  // --------------------------------------------------------------------------
  // Wire up controls
  // --------------------------------------------------------------------------
  function wireControls() {
    el.ctlRestart.addEventListener("click", restart);
    el.ctlStep.addEventListener("click", stepOnce);
    el.ctlPlay.addEventListener("click", play);
    el.ctlPause.addEventListener("click", pause);
    el.ctlFf.addEventListener("click", fastForward);
    el.ctlApprove.addEventListener("click", approve);

    el.auditVerify.addEventListener("click", verifyAudit);
    el.auditTamper.addEventListener("click", tamperAudit);

    el.liveRunBtn.addEventListener("click", runLive);
    el.liveInput.addEventListener("keydown", function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { runLive(); }
    });
  }

  // --------------------------------------------------------------------------
  // Boot
  // --------------------------------------------------------------------------
  function boot() {
    initVideoLink();
    renderFleetSkeleton();
    wireControls();
    updateControlStates();
    loadScenarios();
    initLive();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
