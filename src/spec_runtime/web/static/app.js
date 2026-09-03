/* spec web — vanilla JS SPA: routing, fetch, SSE */
(function () {
  "use strict";

  var app = document.getElementById("app");
  var sseDot = document.getElementById("sse-dot");
  var sseLabel = document.getElementById("sse-label");
  var currentView = "dashboard";
  var currentSpecId = null;
  var latestSnapshot = null;
  var showMerged = false;
  var elapsedTickerId = null;
  var sseAbortController = null;
  var sseReconnectTimer = null;
  var csrfStorageKey = "spec-web-csrf";

  function authenticatedFetch(path, opts) {
    opts = opts || {};
    var headers = new Headers(opts.headers || {});
    if (String(path).indexOf("/api/") === 0) {
      var proof = window.localStorage.getItem(csrfStorageKey);
      if (proof) headers.set("X-Spec-CSRF", proof);
    }
    var requestOpts = Object.assign({}, opts, { headers: headers });
    return fetch(path, requestOpts).then(function (response) {
      if (response.status === 401 || response.status === 403) {
        window.localStorage.removeItem(csrfStorageKey);
      }
      return response;
    });
  }

  window.specFetch = authenticatedFetch;

  // ---- Helpers ----

  function escapeHtml(s) {
    var d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  // Text-node escaping does not necessarily encode quotes. Encode them when
  // interpolating untrusted data into a quoted HTML attribute.
  function escapeAttribute(s) {
    return escapeHtml(s).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function specRoute(specId) {
    return "#/specs/" + escapeAttribute(encodeURIComponent(specId));
  }

  function badgeClass(status) {
    var map = {
      running: "badge-running",
      passed: "badge-passed",
      merged: "badge-merged",
      failed: "badge-failed",
      blocked: "badge-blocked",
      waiting: "badge-waiting",
      stale: "badge-stale",
      queued: "badge-queued",
      "not-started": "badge-not-started",
      "in-progress": "badge-in-progress",
    };
    return map[status] || "badge-not-started";
  }

  function badge(status) {
    return '<span class="badge ' + badgeClass(status) + '">' + escapeHtml(status) + "</span>";
  }

  function api(path, opts) {
    return authenticatedFetch(path, opts).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) {
          throw new Error(data.error || ("Request failed with status " + response.status));
        }
        return data;
      });
    });
  }

  function showActionError(error) {
    window.alert(error && error.message ? error.message : "Action failed");
  }

  function incrementElapsed(text) {
    var m = text.match(/^(?:(\d+)h)?(\d+)m(\d{2})s$/);
    if (!m) return text;
    var h = parseInt(m[1] || "0", 10);
    var min = parseInt(m[2], 10);
    var sec = parseInt(m[3], 10);
    sec += 1;
    if (sec >= 60) { sec = 0; min += 1; }
    if (min >= 60) { min = 0; h += 1; }
    var ss = (sec < 10 ? "0" : "") + sec;
    var mm = h ? ((min < 10 ? "0" : "") + min) : String(min);
    return h ? (h + "h" + mm + "m" + ss + "s") : (mm + "m" + ss + "s");
  }

  function startElapsedTicker() {
    stopElapsedTicker();
    elapsedTickerId = setInterval(function () {
      var rows = document.querySelectorAll("#active-runs-body tr");
      for (var i = 0; i < rows.length; i++) {
        var statusCell = rows[i].querySelector('[data-label="Status"]');
        if (!statusCell) continue;
        var badgeEl = statusCell.querySelector(".badge");
        if (!badgeEl || badgeEl.textContent.trim() !== "running") continue;
        var elapsedCell = rows[i].querySelector('[data-label="Elapsed"]');
        if (!elapsedCell) continue;
        var updated = incrementElapsed(elapsedCell.textContent);
        elapsedCell.textContent = updated;
        // Keep snapshot in sync so re-renders (e.g. toggle-merged) use current values
        if (latestSnapshot && latestSnapshot.rows && latestSnapshot.rows[i]) {
          latestSnapshot.rows[i].elapsed = updated;
        }
      }
    }, 1000);
  }

  function stopElapsedTicker() {
    if (elapsedTickerId !== null) {
      clearInterval(elapsedTickerId);
      elapsedTickerId = null;
    }
  }

  // ---- SSE ----

  function connectSSE() {
    if (sseAbortController) sseAbortController.abort();
    if (sseReconnectTimer) window.clearTimeout(sseReconnectTimer);
    sseAbortController = new AbortController();
    authenticatedFetch("/api/v1/events", { signal: sseAbortController.signal })
      .then(function (response) {
        if (!response.ok || !response.body) throw new Error("event stream unavailable");
        return readDashboardStream(response.body.getReader());
      })
      .catch(function (error) {
        if (error.name === "AbortError") return;
        sseDot.className = "status-dot disconnected";
        sseLabel.textContent = "reconnecting";
        sseReconnectTimer = window.setTimeout(connectSSE, 3000);
      });
  }

  function readDashboardStream(reader) {
    var decoder = new TextDecoder();
    var buffer = "";
    sseDot.className = "status-dot connected";
    sseLabel.textContent = "live";

    function pump() {
      return reader.read().then(function (result) {
        if (result.done) throw new Error("event stream closed");
        buffer += decoder.decode(result.value, { stream: true });
        var boundary;
        while ((boundary = buffer.indexOf("\n\n")) !== -1) {
          var block = buffer.substring(0, boundary);
          buffer = buffer.substring(boundary + 2);
          var eventName = "message";
          var dataLines = [];
          block.split("\n").forEach(function (line) {
            if (line.indexOf("event:") === 0) eventName = line.substring(6).trim();
            if (line.indexOf("data:") === 0) dataLines.push(line.substring(5).trimStart());
          });
          if (eventName === "run_update" || eventName === "dashboard") {
            try {
              latestSnapshot = JSON.parse(dataLines.join("\n"));
              if (currentView === "dashboard") renderDashboard(latestSnapshot);
            } catch (err) { /* ignore malformed events */ }
          }
        }
        return pump();
      });
    }

    return pump();
  }

  // ---- Dashboard view ----

  function renderDashboard(data) {
    if (!data) { app.innerHTML = '<div class="loading">Loading...</div>'; return; }

    var html = "";
    // Summary cards
    html += '<div class="summary-row">';
    html += summaryCard(data.active_count, "Active");
    html += summaryCard(data.failed_count, "Failed");
    html += summaryCard(data.queued_count, "Queued");
    html += summaryCard(data.passed_count, "Passed");
    html += summaryCard(data.merged_count, "Merged");
    html += "</div>";

    // Full spec table with status badges
    if (data.specs && data.specs.length > 0) {
      // Build a lookup of active run info by spec_id
      var runBySpec = {};
      if (data.rows) {
        for (var r = 0; r < data.rows.length; r++) {
          runBySpec[data.rows[r].spec_id] = data.rows[r];
        }
      }

      // Count merged specs and filter if needed
      var mergedCount = 0;
      var visibleSpecs = [];
      for (var s = 0; s < data.specs.length; s++) {
        if (data.specs[s].status === "merged") {
          mergedCount++;
          if (showMerged) visibleSpecs.push(data.specs[s]);
        } else {
          visibleSpecs.push(data.specs[s]);
        }
      }

      html += '<div class="section-header"><span class="section-title" style="margin:0">Specs</span>';
      if (mergedCount > 0) {
        html += ' <button class="btn btn-sm btn-toggle-merged" data-action="toggle-merged">';
        html += showMerged ? "Hide merged" : "Show merged (" + mergedCount + ")";
        html += "</button>";
      }
      html += "</div>";
      html += '<div class="table-wrap"><table>';
      html += "<thead><tr><th>Spec</th><th>Status</th><th>Area</th><th>Priority</th><th>Description</th><th>Active Run</th></tr></thead>";
      html += "<tbody>";
      for (var sv = 0; sv < visibleSpecs.length; sv++) {
        var spec = visibleSpecs[sv];
        var activeRun = runBySpec[spec.spec_id];
        html += "<tr>";
        html += '<td data-label="Spec"><a href="' + specRoute(spec.spec_id) + '">' + escapeHtml(spec.spec_id) + "</a></td>";
        html += '<td data-label="Status">' + badge(spec.status) + "</td>";
        html += '<td data-label="Area">' + escapeHtml(spec.area || "-") + "</td>";
        html += '<td data-label="Priority">' + spec.priority + "</td>";
        html += '<td data-label="Description">' + escapeHtml(spec.description || "-") + "</td>";
        html += '<td data-label="Active Run">';
        if (activeRun) {
          html += badge(activeRun.status) + " " + escapeHtml(activeRun.phase || "");
        } else {
          html += '<span style="color:var(--text-muted)">-</span>';
        }
        html += "</td></tr>";
      }
      html += "</tbody></table></div>";
    }

    // Active runs table
    if (data.rows && data.rows.length > 0) {
      html += '<div class="section-title">Active Runs</div>';
      html += '<div class="table-wrap"><table>';
      html += "<thead><tr><th>Spec</th><th>Status</th><th>Phase</th><th>Agent</th><th>Retries</th><th>Elapsed</th><th>Actions</th></tr></thead>";
      html += '<tbody id="active-runs-body">';
      for (var i = 0; i < data.rows.length; i++) {
        var row = data.rows[i];
        html += "<tr>";
        html += '<td data-label="Spec"><a href="' + specRoute(row.spec_id) + '">' + escapeHtml(row.display_spec_id || row.spec_id) + "</a></td>";
        html += '<td data-label="Status">' + badge(row.status) + "</td>";
        html += '<td data-label="Phase">' + escapeHtml(row.phase) + "</td>";
        html += '<td data-label="Agent">' + escapeHtml(row.agent) + "</td>";
        html += '<td data-label="Retries">' + escapeHtml(row.retries) + "</td>";
        html += '<td data-label="Elapsed">' + escapeHtml(row.elapsed) + "</td>";
        html += '<td data-label="Actions">';
        if (row.status === "running") {
          html += '<button class="btn btn-danger btn-sm" data-action="stop" data-spec="' + escapeAttribute(row.spec_id) + '">Stop</button>';
        } else {
          html += '<button class="btn btn-primary btn-sm" data-action="implement" data-spec="' + escapeAttribute(row.spec_id) + '">Implement</button>';
        }
        html += "</td></tr>";
      }
      html += "</tbody></table></div>";
    }

    // Dispatch queue
    if (data.queue && data.queue.length > 0) {
      html += '<div class="section-title">Dispatch Queue</div>';
      html += '<div class="table-wrap"><table>';
      html += "<thead><tr><th>Spec</th><th>Agent</th><th>Area</th><th>Priority</th><th>Reason</th></tr></thead>";
      html += "<tbody>";
      for (var j = 0; j < data.queue.length; j++) {
        var q = data.queue[j];
        html += "<tr>";
        html += '<td data-label="Spec"><a href="' + specRoute(q.spec_id) + '">' + escapeHtml(q.spec_id) + "</a></td>";
        html += '<td data-label="Agent">' + escapeHtml(q.agent) + "</td>";
        html += '<td data-label="Area">' + escapeHtml(q.area) + "</td>";
        html += '<td data-label="Priority">' + q.priority + "</td>";
        html += '<td data-label="Reason">' + escapeHtml(q.reason) + "</td>";
        html += "</tr>";
      }
      html += "</tbody></table></div>";
    }

    // Chat sessions placeholder — loaded asynchronously
    html += '<div class="section-title">Chat Sessions</div>';
    html += '<div class="btn-group" style="margin-bottom:0.5rem">';
    html += '<a class="btn btn-primary btn-sm" href="#/chat/new/create">New Spec</a> ';
    html += '<a class="btn btn-primary btn-sm" href="#/chat/new/task">New Task</a>';
    html += "</div>";
    html += '<div id="chat-sessions-list"><span style="color:var(--text-muted)">Loading...</span></div>';

    // Dispatch controls
    html += '<div class="section-title">Dispatch</div>';
    html += '<div class="btn-group">';
    html += '<button class="btn btn-primary btn-sm" data-action="dispatch-start">Start Dispatch</button>';
    html += '<button class="btn btn-danger btn-sm" data-action="dispatch-stop">Stop Dispatch</button>';
    html += "</div>";

    app.innerHTML = html;
    startElapsedTicker();

    // Fetch chat sessions and render them
    loadChatSessions();
  }

  function summaryCard(value, label) {
    return '<div class="summary-card"><div class="value">' + value + '</div><div class="label">' + label + "</div></div>";
  }

  function loadChatSessions() {
    api("/api/v1/chat/sessions").then(function (sessions) {
      var container = document.getElementById("chat-sessions-list");
      if (!container) return;

      if (!sessions || sessions.length === 0) {
        container.innerHTML = '<span style="color:var(--text-muted)">No active sessions</span>';
        return;
      }

      var html = '<div class="table-wrap"><table>';
      html += "<thead><tr><th>Session</th><th>Mode</th><th>Agent</th><th>Status</th><th>Turn</th></tr></thead>";
      html += "<tbody>";
      for (var i = 0; i < sessions.length; i++) {
        var s = sessions[i];
        html += "<tr>";
        html += '<td data-label="Session"><a href="#/chat/' + escapeAttribute(encodeURIComponent(s.session_id)) + '">' + escapeHtml(s.session_id.substring(0, 8)) + "...</a></td>";
        html += '<td data-label="Mode">' + escapeHtml(s.mode) + "</td>";
        html += '<td data-label="Agent">' + escapeHtml(s.agent) + "</td>";
        html += '<td data-label="Status">' + badge(s.status) + "</td>";
        html += '<td data-label="Turn">' + (s.turn_active ? badge("running") : '<span style="color:var(--text-muted)">-</span>') + "</td>";
        html += "</tr>";
      }
      html += "</tbody></table></div>";

      container.innerHTML = html;
    }).catch(function () {
      var container = document.getElementById("chat-sessions-list");
      if (container) container.innerHTML = '<span style="color:var(--text-muted)">Failed to load sessions</span>';
    });
  }

  // ---- Simple markdown renderer ----

  function renderMarkdown(text) {
    if (!text) return "";
    var lines = text.split("\n");
    var html = "";
    var inCode = false;
    var inList = false;

    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];

      // Fenced code blocks
      if (line.match(/^```/)) {
        if (inCode) {
          html += "</code></pre>";
          inCode = false;
        } else {
          if (inList) { html += "</ul>"; inList = false; }
          html += "<pre><code>";
          inCode = true;
        }
        continue;
      }
      if (inCode) {
        html += escapeHtml(line) + "\n";
        continue;
      }

      // Close list if this line is not a list item
      if (inList && !line.match(/^\s*[-*]\s/)) {
        html += "</ul>";
        inList = false;
      }

      // Headers
      var hMatch = line.match(/^(#{1,6})\s+(.*)/);
      if (hMatch) {
        var level = hMatch[1].length;
        html += "<h" + level + ">" + renderInline(hMatch[2]) + "</h" + level + ">";
        continue;
      }

      // List items
      var liMatch = line.match(/^\s*[-*]\s+(.*)/);
      if (liMatch) {
        if (!inList) { html += "<ul>"; inList = true; }
        html += "<li>" + renderInline(liMatch[1]) + "</li>";
        continue;
      }

      // Empty line
      if (line.trim() === "") {
        continue;
      }

      // Paragraph
      html += "<p>" + renderInline(line) + "</p>";
    }

    if (inCode) html += "</code></pre>";
    if (inList) html += "</ul>";
    return html;
  }

  function renderInline(text) {
    // Escape HTML first to prevent XSS from raw HTML in spec content,
    // then apply markdown formatting on the safe string.
    text = escapeHtml(text);
    // Bold
    text = text.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    text = text.replace(/__(.+?)__/g, "<strong>$1</strong>");
    // Italic
    text = text.replace(/\*(.+?)\*/g, "<em>$1</em>");
    text = text.replace(/_(.+?)_/g, "<em>$1</em>");
    // Inline code
    text = text.replace(/`([^`]+)`/g, "<code>$1</code>");
    return text;
  }

  // ---- Phase progress display ----

  var PHASES = ["bootstrap", "scoping", "implement", "verify", "review", "merge", "cleanup"];

  function renderPhaseProgress(currentPhase) {
    var html = '<div class="phase-progress">';
    var reached = false;
    for (var i = 0; i < PHASES.length; i++) {
      var cls = "phase-step";
      if (PHASES[i] === currentPhase) {
        cls += " phase-current";
        reached = true;
      } else if (!reached) {
        cls += " phase-done";
      }
      html += '<span class="' + cls + '">' + escapeHtml(PHASES[i]) + "</span>";
    }
    html += "</div>";
    return html;
  }

  // ---- Spec detail view ----

  function showSpecDetail(specId) {
    stopElapsedTicker();
    currentView = "spec-detail";
    currentSpecId = specId;
    app.innerHTML = '<div class="loading">Loading spec ' + escapeHtml(specId) + "...</div>";

    api("/api/v1/specs/" + encodeURIComponent(specId)).then(function (spec) {
      if (spec.error) { app.innerHTML = "<p>" + escapeHtml(spec.error) + "</p>"; return; }

      var html = '<div class="spec-detail">';
      html += "<h2>" + escapeHtml(spec.spec_id) + "</h2>";

      // Meta grid
      html += '<div class="meta">';
      html += metaItem("Status", badge(spec.status));
      html += metaItem("Area", escapeHtml(spec.area || "-"));
      html += metaItem("Priority", spec.priority);
      html += metaItem("Depends On", spec.depends_on && spec.depends_on.length > 0 ? escapeHtml(spec.depends_on.join(", ")) : "-");
      html += "</div>";

      // Description
      html += "<p>" + escapeHtml(spec.description) + "</p>";

      // Actions
      html += '<div class="btn-group" style="margin:1rem 0">';
      html += '<button class="btn btn-primary btn-sm" data-action="implement" data-spec="' + escapeAttribute(specId) + '">Implement</button>';
      html += '<button class="btn btn-danger btn-sm" data-action="stop" data-spec="' + escapeAttribute(specId) + '">Stop</button>';
      html += "</div>";

      // Body — prefer server-rendered body_html, fall back to client-side
      if (spec.body_html || spec.body) {
        html += '<div class="section-title">Spec Content</div>';
        html += '<div class="spec-body rendered-md">' + (spec.body_html || renderMarkdown(spec.body)) + "</div>";
      }

      // Latest run with phase progress
      if (spec.latest_run) {
        var run = spec.latest_run;
        html += '<div class="section-title">Latest Run</div>';
        if (run.phase) {
          html += renderPhaseProgress(run.phase);
        }
        html += '<div class="meta">';
        html += metaItem("Run ID", escapeHtml(run.run_id || "-"));
        html += metaItem("Phase", escapeHtml(run.phase || "-"));
        html += metaItem("Status", badge(run.status || "-"));
        html += metaItem("Attempts", run.attempts || 0);
        html += "</div>";

        // Log viewer
        if (run.run_id) {
          html += '<div class="section-title">Log</div>';
          html += '<div class="log-viewer" id="log-viewer">Loading log...</div>';
        }
      }

      // Run history table
      if (spec.runs && spec.runs.length > 0) {
        html += '<div class="section-title">Run History</div>';
        html += '<div class="table-wrap"><table>';
        html += "<thead><tr><th>Run ID</th><th>Status</th><th>Phase</th><th>Agent</th><th>Attempts</th><th>Elapsed</th><th>Created</th></tr></thead>";
        html += "<tbody>";
        for (var ri = 0; ri < spec.runs.length; ri++) {
          var rh = spec.runs[ri];
          html += "<tr>";
          html += '<td data-label="Run ID">' + escapeHtml(rh.run_id || "-") + "</td>";
          html += '<td data-label="Status">' + badge(rh.status || "-") + "</td>";
          html += '<td data-label="Phase">' + escapeHtml(rh.phase || "-") + "</td>";
          html += '<td data-label="Agent">' + escapeHtml(rh.agent || "-") + "</td>";
          html += '<td data-label="Attempts">' + (rh.attempts || 0) + "</td>";
          html += '<td data-label="Elapsed">' + escapeHtml(rh.elapsed || "-") + "</td>";
          html += '<td data-label="Created">' + escapeHtml(rh.created_at || "-") + "</td>";
          html += "</tr>";
        }
        html += "</tbody></table></div>";
      }

      html += "</div>";
      app.innerHTML = html;

      // Load log if run exists
      if (spec.latest_run && spec.latest_run.run_id) {
        loadLog(spec.latest_run.run_id);
      }
    });
  }

  function loadLog(runId) {
    api("/api/v1/runs/" + encodeURIComponent(runId) + "/log?lines=200").then(function (data) {
      var viewer = document.getElementById("log-viewer");
      if (!viewer) return;
      if (data.lines && data.lines.length > 0) {
        viewer.textContent = data.lines.join("\n");
        viewer.scrollTop = viewer.scrollHeight;
      } else {
        viewer.textContent = "(no log output)";
      }
    });
  }

  function metaItem(label, value) {
    return '<div class="meta-item"><div class="meta-label">' + label + '</div><div class="meta-value">' + value + "</div></div>";
  }

  // ---- Actions ----

  function implementSpec(specId) {
    if (!window.confirm("Start or resume implementation for " + specId + "?")) return;
    api("/api/v1/specs/" + encodeURIComponent(specId) + "/implement", { method: "POST" })
      .then(function () { /* SSE will update */ })
      .catch(showActionError);
  }

  function stopSpec(specId) {
    if (!window.confirm("Stop the active run for " + specId + "?")) return;
    api("/api/v1/specs/" + encodeURIComponent(specId) + "/stop", { method: "POST" })
      .then(function () { /* SSE will update */ })
      .catch(showActionError);
  }

  function dispatchStart() {
    api("/api/v1/dispatch/start", { method: "POST" }).catch(showActionError);
  }

  function dispatchStop() {
    api("/api/v1/dispatch/stop", { method: "POST" }).catch(showActionError);
  }

  // ---- Routing ----

  function handleRoute() {
    var hash = window.location.hash || "#/";
    var specMatch = hash.match(/^#\/specs\/(.+)$/);
    var chatNewMatch = hash.match(/^#\/chat\/new\/(create|task)$/);
    var chatSessionMatch = hash.match(/^#\/chat\/([a-f0-9]+)$/);

    // Update nav active state
    var links = document.querySelectorAll("[data-route]");
    for (var i = 0; i < links.length; i++) {
      links[i].classList.remove("active");
    }

    if (chatNewMatch) {
      currentView = "chat";
      currentSpecId = null;
      var mode = chatNewMatch[1];
      var chatLink = document.querySelector('[data-route="chat' + (mode === "task" ? "-task" : "") + '"]');
      if (chatLink) chatLink.classList.add("active");
      if (window.specChat) {
        window.specChat.renderNewChat(app, mode);
      }
    } else if (chatSessionMatch) {
      currentView = "chat";
      currentSpecId = null;
      if (window.specChat) {
        window.specChat.renderChatSession(app, chatSessionMatch[1]);
      }
    } else if (specMatch) {
      showSpecDetail(decodeURIComponent(specMatch[1]));
    } else {
      currentView = "dashboard";
      currentSpecId = null;
      var dashLink = document.querySelector('[data-route="dashboard"]');
      if (dashLink) dashLink.classList.add("active");
      if (latestSnapshot) {
        renderDashboard(latestSnapshot);
      } else {
        api("/api/v1/dashboard").then(function (data) {
          latestSnapshot = data;
          renderDashboard(data);
        });
      }
    }
  }

  // ---- Init ----

  window.addEventListener("hashchange", handleRoute);

  // Delegated click handler — avoids inline onclick with dynamic values
  function toggleMerged() {
    showMerged = !showMerged;
    if (latestSnapshot) renderDashboard(latestSnapshot);
  }

  var actionMap = {
    implement: implementSpec,
    stop: stopSpec,
    "dispatch-start": dispatchStart,
    "dispatch-stop": dispatchStop,
    "toggle-merged": toggleMerged,
  };
  app.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-action]");
    if (!btn) return;
    var handler = actionMap[btn.getAttribute("data-action")];
    if (handler) handler(btn.getAttribute("data-spec") || undefined);
  });

  connectSSE();
  handleRoute();
})();
