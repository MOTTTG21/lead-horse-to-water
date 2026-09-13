(function () {
  const chatLog = document.getElementById("chat-log");
  const chatForm = document.getElementById("chat-form");
  const chatInput = document.getElementById("chat-input");
  const trustBar = document.getElementById("trust-bar");
  const stageLabel = document.getElementById("stage-label");
  const unlockToast = document.getElementById("unlock-toast");
  const guideCounter = document.getElementById("guide-counter");
  const debriefOverlay = document.getElementById("debrief-overlay");

  const FIELD_GUIDE_TITLES = {}; // filled in from /field-guide responses

  function appendLine(speaker, text) {
    const div = document.createElement("div");
    div.className = "chat-line " + speaker;
    div.textContent = (speaker === "horse" ? "🐴 " : speaker === "system" ? "· " : "You: ") + text;
    chatLog.appendChild(div);
    chatLog.scrollTop = chatLog.scrollHeight;
  }

  function setMeters(trustLevel, stage) {
    trustBar.style.width = Math.round(trustLevel * 100) + "%";
    stageLabel.textContent = stage;
  }

  function showUnlocks(keys) {
    if (!keys || keys.length === 0) return;
    unlockToast.hidden = false;
    unlockToast.textContent =
      "Field guide unlocked: " + keys.map((k) => FIELD_GUIDE_TITLES[k] || k).join(", ");
    setTimeout(() => (unlockToast.hidden = true), 4000);
  }

  function describeActionOutcome(attemptedAction, outcome) {
    if (!attemptedAction || !outcome) return null;
    if (outcome.decision === "denied") {
      return attemptedAction + " denied (" + (outcome.reason || "policy") + ")";
    }
    if (attemptedAction === "let_horse_decide") {
      return "The horse decided to: " + outcome.picked_tool;
    }
    return attemptedAction + " done.";
  }

  async function postJSON(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || res.statusText);
    }
    return res.json();
  }

  async function maybeShowDebrief(gameOver) {
    if (!gameOver) return;
    const debrief = await fetch("/debrief").then((r) => r.json());
    document.getElementById("debrief-title").textContent = debrief.won
      ? "The horse drank the water."
      : "Game over -- trust ran out.";
    document.getElementById("debrief-explanation").textContent = debrief.explanation;
    const stats = document.getElementById("debrief-stats");
    stats.innerHTML = "";
    const rows = [
      ["Final trust", debrief.final_trust.toFixed(2)],
      ["Final stage", debrief.final_stage],
      ["Attempts", debrief.attempt_count],
      ["Field guide", debrief.field_guide.completion_count + " / " + debrief.field_guide.total_count],
    ];
    for (const [label, value] of rows) {
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = value;
      stats.appendChild(dt);
      stats.appendChild(dd);
    }
    debriefOverlay.hidden = false;
  }

  chatForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = chatInput.value.trim();
    if (!message) return;
    appendLine("player", message);
    chatInput.value = "";
    try {
      const data = await postJSON("/turn", { message });
      appendLine("horse", data.dialogue);
      setMeters(data.trust_level, data.stage);

      const outcomeLine = describeActionOutcome(data.attempted_action, data.action_outcome);
      if (outcomeLine) appendLine("system", outcomeLine);
      if (data.action_outcome && data.action_outcome.narration) {
        appendLine("horse", data.action_outcome.narration);
      }
      showUnlocks(data.newly_unlocked_field_guide);
      await maybeShowDebrief(data.game_over);
    } catch (err) {
      appendLine("system", "Error: " + err.message);
    }
  });

  document.querySelectorAll(".tool-buttons button[data-tool]").forEach((button) => {
    button.addEventListener("click", async () => {
      const tool = button.dataset.tool;
      try {
        const data = await postJSON("/tool/" + tool);
        appendLine(
          "system",
          data.decision === "denied"
            ? tool + " denied (" + (data.reason || "policy") + ")"
            : tool + " done."
        );
        showUnlocks(data.newly_unlocked_field_guide);
      } catch (err) {
        appendLine("system", "Error: " + err.message);
      }
    });
  });

  document.getElementById("let-horse-decide").addEventListener("click", async () => {
    try {
      const data = await postJSON("/let-horse-decide");
      if (data.decision === "denied") {
        appendLine("system", "let_horse_decide denied (" + (data.reason || "cooldown") + ")");
        return;
      }
      appendLine("system", "The horse decided to: " + data.picked_tool);
      if (data.narration) appendLine("horse", data.narration);
      showUnlocks(data.newly_unlocked_field_guide);
      await maybeShowDebrief(data.game_over);
    } catch (err) {
      appendLine("system", "Error: " + err.message);
    }
  });

  document.getElementById("debrief-close").addEventListener("click", async () => {
    debriefOverlay.hidden = true;
    try {
      await postJSON("/reset");
    } catch (err) {
      appendLine("system", "Error resetting: " + err.message);
      return;
    }
    chatLog.innerHTML = "";
    setMeters(0.5, "precontemplation");
    appendLine("system", "A new attempt begins.");
  });

  // --- tabs ---

  const tabs = {
    "tab-game": "panel-game",
    "tab-help": "panel-help",
    "tab-logbook": "panel-logbook",
    "tab-guide": "panel-guide",
  };

  function activateTab(tabId) {
    for (const [tab, panel] of Object.entries(tabs)) {
      document.getElementById(tab).classList.toggle("active", tab === tabId);
      document.getElementById(panel).classList.toggle("active", panel === tabs[tabId]);
    }
    if (tabId === "tab-logbook") loadLogbook();
    if (tabId === "tab-guide") loadFieldGuide();
  }

  Object.keys(tabs).forEach((tabId) => {
    document.getElementById(tabId).addEventListener("click", () => activateTab(tabId));
  });

  async function loadLogbook() {
    const data = await fetch("/logbook").then((r) => r.json());
    const tbody = document.querySelector("#logbook-table tbody");
    tbody.innerHTML = "";
    for (const row of data.rows) {
      const tr = document.createElement("tr");
      if (row.initiator === "llm_agent_loop") tr.classList.add("row-anomaly");
      tr.innerHTML =
        "<td>" + new Date(row.timestamp).toLocaleTimeString() + "</td>" +
        "<td>" + row.session_id.slice(0, 8) + "</td>" +
        "<td>" + row.role + "</td>" +
        "<td>" + row.tool_name + "</td>" +
        "<td>" + row.system + "</td>" +
        "<td>" + row.initiator + "</td>" +
        "<td>" + row.decision + "</td>";
      tbody.appendChild(tr);
    }
  }

  async function loadFieldGuide() {
    const data = await fetch("/field-guide").then((r) => r.json());
    guideCounter.textContent = "(" + data.completion_count + "/" + data.total_count + ")";

    const container = document.getElementById("guide-entries");
    container.innerHTML = "";
    for (const entry of data.entries) {
      FIELD_GUIDE_TITLES[entry.key] = entry.title;
      const card = document.createElement("div");
      card.className = "guide-card " + (entry.unlocked ? "unlocked" : "locked");
      card.innerHTML = entry.unlocked
        ? "<h3>" + entry.title + "</h3><p>" + entry.body + "</p>"
        : "<h3>🔒 ???</h3>";
      container.appendChild(card);
    }

    const discovered = document.getElementById("discovered-actions");
    discovered.innerHTML = "";
    if (data.discovered_actions.length === 0) {
      const p = document.createElement("p");
      p.className = "hint";
      p.textContent = "Nothing discovered yet -- try talking to the horse.";
      discovered.appendChild(p);
    }
    for (const action of data.discovered_actions) {
      const card = document.createElement("div");
      card.className = "guide-card unlocked discovered-action";
      card.innerHTML = "<h3>" + action.label + "</h3>";
      discovered.appendChild(card);
    }
  }
})();
