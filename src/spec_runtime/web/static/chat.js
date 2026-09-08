/* spec web — Chat UI module
 *
 * Manages the chat interface: message thread, SSE streaming of agent
 * responses, and tool action card rendering.  Uses fetch() with
 * ReadableStream for the streaming POST response (EventSource only
 * supports GET, so manual SSE parsing is required for message sends).
 */
(function () {
  "use strict";

  // Exposed on window so app.js can call into chat module
  window.specChat = {
    renderNewChat: renderNewChat,
    renderChatSession: renderChatSession,
  };

  // ---- Helpers ----

  function escapeHtml(s) {
    var d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  function escapeAttribute(s) {
    return escapeHtml(s).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // ---- State ----

  var currentSessionId = null;
  var messages = []; // {role: "user"|"assistant"|"card", content: ..., event?: AgentEvent}
  var isStreaming = false;
  var inReviewMode = false; // true while spec review UI is showing
  var isSessionCompleted = false; // true once session.status becomes "completed"
  var availableBackends = null; // {claude: bool, codex: bool}
  var selectedAgent = null;
  var activeAbortController = null; // cancels in-flight SSE readers on route change

  function cancelActiveStream() {
    if (activeAbortController) {
      activeAbortController.abort();
      activeAbortController = null;
    }
  }

  // Fetch available backends on load
  function fetchBackends(callback) {
    if (availableBackends !== null) {
      if (callback) callback();
      return;
    }
    window.specFetch("/api/v1/chat/backends")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        availableBackends = data.backends || {};
        // Prefer the repository's configured default when it is available.
        if (data.default_agent && availableBackends[data.default_agent]) selectedAgent = data.default_agent;
        else if (availableBackends.claude) selectedAgent = "claude";
        else if (availableBackends.codex) selectedAgent = "codex";
        else selectedAgent = null;
        if (callback) callback();
      })
      .catch(function () {
        availableBackends = {};
        selectedAgent = null;
        if (callback) callback();
      });
  }

  function getSelectedAgent() {
    return selectedAgent || "claude";
  }

  // ---- New Chat View ----

  function renderNewChat(container, mode) {
    mode = mode || "create";
    cancelActiveStream();
    currentSessionId = null;
    messages = [];
    isStreaming = false;
    inReviewMode = false;
    isSessionCompleted = false;

    // Fetch backends then render
    fetchBackends(function () {
      _renderNewChatUI(container, mode);
    });
  }

  function _renderNewChatUI(container, mode) {
    var html = '<div class="chat-view">';
    html += '<div class="chat-header">';
    html += "<h2>" + (mode === "create" ? "New Spec" : "New Task") + "</h2>";

    // Agent picker — show when at least one backend is available
    if (availableBackends) {
      var claudeAvail = availableBackends.claude;
      var codexAvail = availableBackends.codex;
      html += ' <select id="agent-select" class="agent-select">';
      if (claudeAvail) html += '<option value="claude"' + (selectedAgent === "claude" ? " selected" : "") + '>Claude</option>';
      if (codexAvail) html += '<option value="codex"' + (selectedAgent === "codex" ? " selected" : "") + '>Codex</option>';
      if (!claudeAvail && !codexAvail) html += '<option value="" disabled selected>No backends available</option>';
      html += "</select>";
    }

    html += "</div>";
    html += '<div class="chat-messages" id="chat-messages">';
    if (selectedAgent) {
      html += '<div class="chat-empty">Start a conversation to ' +
        (mode === "create" ? "create a new spec" : "describe a task") + ".</div>";
    } else {
      html += '<div class="chat-empty">No agent backends are available. Install claude-agent-sdk or codex to use chat.</div>';
    }
    html += "</div>";
    html += '<div class="chat-input-bar" id="chat-input-bar">';
    html += '<textarea id="chat-input" class="chat-input" rows="1" placeholder="Type a message..." autocomplete="off"' +
      (selectedAgent ? "" : " disabled") + '></textarea>';
    html += '<button id="chat-send" class="btn btn-primary chat-send-btn"' +
      (selectedAgent ? "" : " disabled") + '>Send</button>';
    html += "</div>";
    html += "</div>";

    container.innerHTML = html;

    var input = document.getElementById("chat-input");
    var sendBtn = document.getElementById("chat-send");
    var agentSelect = document.getElementById("agent-select");

    if (agentSelect) {
      agentSelect.addEventListener("change", function () {
        selectedAgent = this.value;
      });
    }

    // Auto-grow textarea
    input.addEventListener("input", function () {
      this.style.height = "auto";
      this.style.height = Math.min(this.scrollHeight, 150) + "px";
    });

    // Enter to send, Shift+Enter for newline
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage(mode);
      }
    });

    sendBtn.addEventListener("click", function () {
      sendMessage(mode);
    });
  }

  // ---- Chat Session View (reconnect to existing) ----

  function renderChatSession(container, sessionId) {
    cancelActiveStream();
    currentSessionId = sessionId;
    messages = [];
    isStreaming = false;
    inReviewMode = false;
    isSessionCompleted = false;

    var html = '<div class="chat-view">';
    html += '<div class="chat-header">';
    html += "<h2>Chat Session</h2>";
    html += '<button class="btn btn-danger btn-sm" id="chat-stop">Stop</button>';
    html += "</div>";
    html += '<div class="chat-messages" id="chat-messages">';
    html += '<div class="chat-empty">Loading session...</div>';
    html += "</div>";
    html += '<div class="chat-input-bar" id="chat-input-bar">';
    html += '<textarea id="chat-input" class="chat-input" rows="1" placeholder="Type a message..." autocomplete="off"></textarea>';
    html += '<button id="chat-send" class="btn btn-primary chat-send-btn">Send</button>';
    html += "</div>";
    html += "</div>";

    container.innerHTML = html;

    var input = document.getElementById("chat-input");
    var sendBtn = document.getElementById("chat-send");
    var stopBtn = document.getElementById("chat-stop");

    input.addEventListener("input", function () {
      this.style.height = "auto";
      this.style.height = Math.min(this.scrollHeight, 150) + "px";
    });

    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendFollowUp();
      }
    });

    sendBtn.addEventListener("click", function () {
      sendFollowUp();
    });

    stopBtn.addEventListener("click", function () {
      stopSession();
    });

    // Load session metadata and history
    window.specFetch("/api/v1/chat/sessions/" + encodeURIComponent(sessionId))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.error) {
          document.getElementById("chat-messages").innerHTML =
            '<div class="chat-empty">' + escapeHtml(data.error) + "</div>";
          return;
        }
        var sessionMeta = data;
        if (data.status === "completed") {
          isSessionCompleted = true;
          updateSendButton();
        }
        var header = document.querySelector(".chat-header h2");
        if (header) {
          header.textContent = (data.mode === "create" ? "Spec Chat" : "Task Chat") +
            " — " + data.agent;
        }

        // Fetch and replay conversation history
        return window.specFetch("/api/v1/chat/sessions/" + encodeURIComponent(sessionId) + "/history")
          .then(function (r) { return r.json(); })
          .then(function (histData) {
            if (histData.history && histData.history.length > 0) {
              replayHistory(histData.history);
            } else {
              document.getElementById("chat-messages").innerHTML =
                '<div class="chat-empty">Session active. Send a message to continue.</div>';
            }

            // If a turn is still running server-side, reattach to the stream
            if (histData.turn_active) {
              // The server owns the live-turn cursor.  Inferring it from the
              // last assistant history entry can accidentally use the prior
              // turn and skip the first events of the current one.
              reattachToStream(sessionId, histData.live_event_count || 0);
            }
          });
      })
      .catch(function () {
        document.getElementById("chat-messages").innerHTML =
          '<div class="chat-empty">Failed to load session.</div>';
      });
  }

  function replayHistory(history) {
    messages = [];
    inReviewMode = false;
    for (var i = 0; i < history.length; i++) {
      var entry = history[i];
      if (entry.role === "user") {
        messages.push({ role: "user", content: entry.content });
      } else if (entry.role === "spec_review") {
        messages.push({ role: "spec_review", specId: entry.spec_id, specContent: entry.spec_content });
        inReviewMode = true;
      } else if (entry.role === "assistant" && entry.events) {
        var hadCard = false;
        for (var j = 0; j < entry.events.length; j++) {
          var evt = entry.events[j];
          if (evt.kind === "text") {
            // After a tool card, start a new assistant bubble to
            // preserve the real interleaved order.
            var lastMsg = messages.length > 0 ? messages[messages.length - 1] : null;
            if (lastMsg && lastMsg.role === "assistant" && !hadCard) {
              lastMsg.content += evt.text;
            } else {
              messages.push({ role: "assistant", content: evt.text });
              hadCard = false;
            }
          } else if (evt.kind === "error") {
            messages.push({ role: "error", content: evt.text });
          } else if (evt.kind === "task_spec_ready") {
            messages.push({ role: "spec_review", specId: evt.spec_id, specContent: evt.spec_content });
          } else if (evt.kind === "tool_call" || evt.kind === "tool_result" ||
                     evt.kind === "file_change" || evt.kind === "command") {
            messages.push({ role: "card", cardType: evt.kind, event: evt });
            hadCard = true;
          }
        }
      }
    }
    renderMessages();
    scrollToBottom();
    updateSendButton();
  }

  // ---- Send first message (creates session) ----

  function sendMessage(mode) {
    // After the first message creates a session, subsequent messages
    // from the same view must go to the existing session as follow-ups
    // rather than creating new sessions each time.
    if (currentSessionId) {
      sendFollowUp();
      return;
    }

    var input = document.getElementById("chat-input");
    var text = input.value.trim();
    if (!text || isStreaming) return;

    input.value = "";
    input.style.height = "auto";
    addUserMessage(text);

    isStreaming = true;
    updateSendButton();

    window.specFetch("/api/v1/chat/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: mode, agent: getSelectedAgent(), prompt: text }),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.error) {
          addErrorMessage(data.error);
          isStreaming = false;
          updateSendButton();
          return;
        }
        currentSessionId = data.session_id;

        // Update URL hash without triggering full re-render
        history.replaceState(null, "", "#/chat/" + currentSessionId);

        // The create endpoint already forwarded the initial prompt to the
        // bridge, so send the same text to /messages to get the stream.
        streamMessage(text);
      })
      .catch(function (err) {
        addErrorMessage("Failed to create session: " + err.message);
        isStreaming = false;
        updateSendButton();
      });
  }

  // ---- Send follow-up message ----

  function sendFollowUp() {
    var input = document.getElementById("chat-input");
    var text = input.value.trim();
    if (!text || isStreaming || !currentSessionId) return;

    input.value = "";
    input.style.height = "auto";
    addUserMessage(text);
    streamMessage(text);
  }

  // ---- Stream agent response via SSE ----

  function streamMessage(text) {
    isStreaming = true;
    updateSendButton();

    var assistantIdx = addAssistantPlaceholder();
    var controller = new AbortController();
    activeAbortController = controller;
    var boundSessionId = currentSessionId;

    window.specFetch("/api/v1/chat/sessions/" + encodeURIComponent(currentSessionId) + "/messages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text }),
      signal: controller.signal,
    })
      .then(function (response) {
        if (!response.ok) {
          return response.json().then(function (data) {
            throw new Error(data.error || "Request failed");
          });
        }
        return readSSEStream(response.body.getReader(), assistantIdx, boundSessionId);
      })
      .catch(function (err) {
        if (err.name === "AbortError") return; // cancelled by route change
        addErrorMessage("Stream error: " + err.message);
      })
      .finally(function () {
        if (currentSessionId === boundSessionId) {
          isStreaming = false;
          updateSendButton();
        }
      });
  }

  // ---- Manual SSE parser for ReadableStream ----

  function readSSEStream(reader, assistantIdx, boundSessionId) {
    var decoder = new TextDecoder();
    var buffer = "";
    var currentAssistantIdx = assistantIdx;
    var hadToolCard = false;
    var initialPlaceholderUsed = false; // tracks if the initial placeholder received text

    function isStale() {
      return boundSessionId !== undefined && currentSessionId !== boundSessionId;
    }

    function removeInitialPlaceholder() {
      // Remove the initial empty placeholder if it never received text
      if (!initialPlaceholderUsed &&
          messages[assistantIdx] &&
          messages[assistantIdx].role === "assistant" &&
          !messages[assistantIdx].content) {
        messages.splice(assistantIdx, 1);
        // Adjust currentAssistantIdx since we removed an element before it
        if (currentAssistantIdx > assistantIdx) {
          currentAssistantIdx--;
        }
        renderMessages();
      }
    }

    function process(result) {
      if (result.done || isStale()) {
        if (isStale()) reader.cancel();
        return;
      }

      buffer += decoder.decode(result.value, { stream: true });
      var lines = buffer.split("\n");
      buffer = lines.pop() || "";

      for (var i = 0; i < lines.length; i++) {
        var line = lines[i];
        if (line.indexOf("data: ") === 0) {
          try {
            var event = JSON.parse(line.substring(6));

            if (event.kind === "done") {
              // Remove empty trailing assistant placeholder
              if (messages[currentAssistantIdx] &&
                  messages[currentAssistantIdx].role === "assistant" &&
                  !messages[currentAssistantIdx].content) {
                messages.splice(currentAssistantIdx, 1);
                renderMessages();
              }
              return;
            }

            if (event.kind === "text") {
              if (hadToolCard) {
                // Start a new assistant bubble after tool cards
                currentAssistantIdx = addAssistantPlaceholder();
                hadToolCard = false;
              }
              initialPlaceholderUsed = true;
              appendToAssistant(currentAssistantIdx, event.text);
            } else if (event.kind === "tool_call" || event.kind === "tool_result" ||
                       event.kind === "file_change" || event.kind === "command") {
              // If tools arrive before any text, remove the empty placeholder
              if (!initialPlaceholderUsed && !hadToolCard) {
                removeInitialPlaceholder();
              }
              hadToolCard = true;
              addToolCard(event.kind, event);
            } else if (event.kind === "task_spec_ready") {
              addSpecReview(event.spec_id, event.spec_content);
            } else if (event.kind === "error") {
              addErrorMessage(event.text);
            }
            scrollToBottom();
          } catch (e) {
            // Ignore parse errors
          }
        }
      }

      return reader.read().then(process);
    }

    return reader.read().then(process);
  }

  // ---- Message rendering ----

  function addUserMessage(text) {
    messages.push({ role: "user", content: text });
    renderMessages();
    scrollToBottom();
  }

  function addAssistantPlaceholder() {
    var idx = messages.length;
    messages.push({ role: "assistant", content: "" });
    renderMessages();
    return idx;
  }

  function appendToAssistant(idx, text) {
    if (messages[idx]) {
      messages[idx].content += text;
      renderMessages();
    }
  }

  function addToolCard(type, event) {
    messages.push({ role: "card", cardType: type, event: event });
    renderMessages();
  }

  function addErrorMessage(text) {
    messages.push({ role: "error", content: text });
    renderMessages();
    scrollToBottom();
  }

  function addSpecReview(specId, specContent) {
    messages.push({ role: "spec_review", specId: specId, specContent: specContent });
    inReviewMode = true;
    renderMessages();
    scrollToBottom();
    // Disable chat input while review UI is showing
    setInputDisabled(true);
  }

  function setInputDisabled(disabled) {
    var input = document.getElementById("chat-input");
    var btn = document.getElementById("chat-send");
    if (input) input.disabled = disabled;
    if (btn) btn.disabled = disabled;
  }

  function renderMessages() {
    var container = document.getElementById("chat-messages");
    if (!container) return;

    var html = "";
    for (var i = 0; i < messages.length; i++) {
      var msg = messages[i];
      if (msg.role === "user") {
        html += '<div class="chat-msg chat-msg-user">';
        html += '<div class="chat-bubble chat-bubble-user">' + escapeHtml(msg.content) + "</div>";
        html += "</div>";
      } else if (msg.role === "assistant") {
        html += '<div class="chat-msg chat-msg-assistant">';
        html += '<div class="chat-bubble chat-bubble-assistant">' +
          (msg.content ? formatAssistantText(msg.content) : '<span class="chat-typing">Thinking...</span>') +
          "</div>";
        html += "</div>";
      } else if (msg.role === "card") {
        html += renderToolCard(msg.cardType, msg.event);
      } else if (msg.role === "spec_review") {
        html += '<div class="chat-msg chat-msg-spec-review">';
        html += '<div class="chat-bubble chat-bubble-spec-review">';
        html += '<h3>Task Spec: ' + escapeHtml(msg.specId) + '</h3>';
        html += '<pre class="spec-content">' + escapeHtml(msg.specContent) + '</pre>';
        html += '<div class="spec-review-actions">';
        html += '<button class="btn btn-primary spec-implement-btn" data-spec-id="' +
          escapeAttribute(msg.specId) + '">Implement</button> ';
        html += '<button class="btn btn-secondary spec-keep-editing-btn">Keep Editing</button>';
        html += '</div></div></div>';
      } else if (msg.role === "error") {
        html += '<div class="chat-msg chat-msg-error">';
        html += '<div class="chat-bubble chat-bubble-error">' + escapeHtml(msg.content) + "</div>";
        html += "</div>";
      }
    }
    container.innerHTML = html;

    // Attach toggle listeners to cards
    var toggles = container.querySelectorAll(".card-toggle");
    for (var t = 0; t < toggles.length; t++) {
      toggles[t].addEventListener("click", function () {
        var body = this.closest(".chat-card").querySelector(".card-body");
        if (body) {
          body.classList.toggle("card-expanded");
          this.textContent = body.classList.contains("card-expanded") ? "Collapse" : "Expand";
        }
      });
    }

    // Attach spec review action listeners
    var implBtns = container.querySelectorAll(".spec-implement-btn");
    for (var b = 0; b < implBtns.length; b++) {
      implBtns[b].addEventListener("click", function () {
        handleImplementClick(this.getAttribute("data-spec-id"));
      });
    }
    var keepBtns = container.querySelectorAll(".spec-keep-editing-btn");
    for (var k = 0; k < keepBtns.length; k++) {
      keepBtns[k].addEventListener("click", function () {
        handleKeepEditingClick();
      });
    }
  }

  function formatAssistantText(text) {
    // Basic formatting: preserve newlines, escape HTML, render inline code
    var escaped = escapeHtml(text);
    // Inline code
    escaped = escaped.replace(/`([^`]+)`/g, "<code>$1</code>");
    // Newlines to breaks
    escaped = escaped.replace(/\n/g, "<br>");
    return escaped;
  }

  // ---- Tool action cards ----

  function renderToolCard(type, event) {
    var html = '<div class="chat-card chat-card-' + escapeAttribute(type) + '">';

    switch (type) {
      case "file_change":
        html += '<div class="card-header">';
        html += '<span class="card-icon">&#128196;</span> ';
        html += '<span class="card-title">' + escapeHtml(event.path || "File change") + "</span>";
        html += ' <button class="card-toggle btn-sm">Expand</button>';
        html += "</div>";
        html += '<div class="card-body">';
        html += "<pre><code>" + escapeHtml(event.diff || "(no diff)") + "</code></pre>";
        html += "</div>";
        break;

      case "command":
        var exitLabel = event.exit_code !== null && event.exit_code !== undefined
          ? " (exit " + event.exit_code + ")"
          : "";
        html += '<div class="card-header">';
        html += '<span class="card-icon">&#9654;</span> ';
        html += '<span class="card-title"><code>' + escapeHtml(event.cmd || "command") + "</code>" + escapeHtml(exitLabel) + "</span>";
        html += ' <button class="card-toggle btn-sm">Expand</button>';
        html += "</div>";
        html += '<div class="card-body">';
        html += "<pre><code>" + escapeHtml(event.output || "(no output)") + "</code></pre>";
        html += "</div>";
        break;

      case "tool_call":
        html += '<div class="card-header">';
        html += '<span class="card-icon">&#128295;</span> ';
        html += '<span class="card-title">' + escapeHtml(event.tool_name || "Tool call") + "</span>";
        html += ' <button class="card-toggle btn-sm">Expand</button>';
        html += "</div>";
        html += '<div class="card-body">';
        html += "<pre><code>" + escapeHtml(event.tool_input || "") + "</code></pre>";
        html += "</div>";
        break;

      case "tool_result":
        html += '<div class="card-header">';
        html += '<span class="card-icon">&#9989;</span> ';
        html += '<span class="card-title">' + escapeHtml(event.tool_name || "Tool result") + "</span>";
        html += ' <button class="card-toggle btn-sm">Expand</button>';
        html += "</div>";
        html += '<div class="card-body">';
        html += "<pre><code>" + escapeHtml(event.tool_output || "") + "</code></pre>";
        html += "</div>";
        break;
    }

    html += "</div>";
    return html;
  }

  // ---- Spec review actions ----

  function handleImplementClick(specId) {
    if (!currentSessionId) return;
    if (!window.confirm("Hand this reviewed task to the implementation workflow?")) return;
    // Disable buttons to prevent double-click
    var btns = document.querySelectorAll(".spec-implement-btn, .spec-keep-editing-btn");
    for (var i = 0; i < btns.length; i++) btns[i].disabled = true;

    window.specFetch("/api/v1/chat/sessions/" + encodeURIComponent(currentSessionId) + "/implement", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ spec_id: specId, agent: selectedAgent || "claude" }),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.error) {
          addErrorMessage("Implement failed: " + data.error);
          // Re-enable review buttons so user can retry or keep editing
          var retryBtns = document.querySelectorAll(".spec-implement-btn, .spec-keep-editing-btn");
          for (var i = 0; i < retryBtns.length; i++) retryBtns[i].disabled = false;
          return;
        }
        // Remove the spec_review card so renderMessages() won't recreate
        // live Implement/Keep Editing buttons after the session is handed off.
        for (var j = messages.length - 1; j >= 0; j--) {
          if (messages[j].role === "spec_review") {
            messages.splice(j, 1);
            break;
          }
        }
        addUserMessage("Implementation started for spec: " + (data.spec_id || specId) +
          (data.run_id ? " (run " + data.run_id + ")" : ""));
        // Session is now completed — disable input
        inReviewMode = false;
        isSessionCompleted = true;
        updateSendButton();
      })
      .catch(function (err) {
        addErrorMessage("Implement request failed: " + err.message);
        // Re-enable review buttons so user can retry or keep editing
        var retryBtns = document.querySelectorAll(".spec-implement-btn, .spec-keep-editing-btn");
        for (var i = 0; i < retryBtns.length; i++) retryBtns[i].disabled = false;
      });
  }

  function handleKeepEditingClick() {
    // Clear server-side spec_review history first, then update UI on success.
    // If the clear fails the review card stays so dedupe remains consistent.
    var clearDone = currentSessionId
      ? window.specFetch("/api/v1/chat/sessions/" + encodeURIComponent(currentSessionId) + "/clear-review", {
          method: "POST",
        })
      : Promise.resolve();
    clearDone
      .then(function (resp) {
        if (resp && !resp.ok) throw new Error("clear-review returned " + resp.status);
        // Remove the spec review message so the UI returns to normal chat
        for (var i = messages.length - 1; i >= 0; i--) {
          if (messages[i].role === "spec_review") {
            messages.splice(i, 1);
            break;
          }
        }
        renderMessages();
        inReviewMode = false;
        updateSendButton();
        var input = document.getElementById("chat-input");
        if (input) input.focus();
      })
      .catch(function () {
        addErrorMessage("Failed to clear review — please try again.");
      });
  }

  // ---- Session controls ----

  function stopSession() {
    if (!currentSessionId) return;
    if (!window.confirm("Stop this chat? Its worktree and branch will be preserved.")) return;
    window.specFetch("/api/v1/chat/sessions/" + encodeURIComponent(currentSessionId) + "/stop", {
      method: "POST",
    }).then(function (response) {
      if (!response.ok) {
        return response.json().then(function (data) {
          throw new Error(data.error || "Failed to stop session");
        });
      }
      return response.json();
    }).then(function (data) {
      var preserved = [];
      if (data.worktree) preserved.push("worktree " + data.worktree);
      if (data.branch) preserved.push("branch " + data.branch);
      addErrorMessage("Session stopped." + (preserved.length ? " Preserved " + preserved.join("; ") + "." : ""));
      isStreaming = false;
      isSessionCompleted = true;
      if (activeAbortController) {
        activeAbortController.abort();
        activeAbortController = null;
      }
      updateSendButton();
    }).catch(function (err) {
      addErrorMessage("Failed to stop session: " + err.message);
    });
  }

  // ---- Reattach to live turn after navigation away ----

  function reattachToStream(sessionId, fromIdx) {
    isStreaming = true;
    updateSendButton();

    // Reuse the last assistant placeholder if one exists, otherwise create one
    var lastMsg = messages.length > 0 ? messages[messages.length - 1] : null;
    var assistantIdx = (lastMsg && lastMsg.role === "assistant")
      ? messages.length - 1
      : addAssistantPlaceholder();

    var controller = new AbortController();
    activeAbortController = controller;
    var boundSessionId = sessionId;

    window.specFetch("/api/v1/chat/sessions/" + encodeURIComponent(sessionId) + "/stream?from=" + fromIdx, {
      signal: controller.signal,
    })
      .then(function (response) {
        if (!response.ok) {
          if (currentSessionId === boundSessionId) {
            isStreaming = false;
            updateSendButton();
          }
          return;
        }
        return readSSEStream(response.body.getReader(), assistantIdx, boundSessionId);
      })
      .catch(function (err) {
        if (err.name === "AbortError") return;
        addErrorMessage("Reconnect error: " + err.message);
      })
      .finally(function () {
        if (currentSessionId === boundSessionId) {
          isStreaming = false;
          updateSendButton();
        }
      });
  }

  // ---- UI helpers ----

  function scrollToBottom() {
    var container = document.getElementById("chat-messages");
    if (container) {
      container.scrollTop = container.scrollHeight;
    }
  }

  function updateSendButton() {
    var btn = document.getElementById("chat-send");
    var input = document.getElementById("chat-input");
    var disabled = isStreaming || inReviewMode || isSessionCompleted;
    if (btn) {
      btn.disabled = disabled;
      btn.textContent = isStreaming ? "..." : "Send";
    }
    if (input) {
      input.disabled = disabled;
      if (isSessionCompleted) {
        input.placeholder = "Session completed or stopped";
      }
    }
  }
})();
