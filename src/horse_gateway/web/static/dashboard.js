(function () {
  async function refresh() {
    const data = await fetch("/metrics").then((r) => r.json());
    document.getElementById("stat-total-calls").textContent = data.total_calls;
    document.getElementById("stat-deny-rate").textContent = (data.deny_rate * 100).toFixed(1) + "%";
    document.getElementById("stat-loop-rate").textContent =
      (data.llm_agent_loop_rate * 100).toFixed(1) + "%";
    document.getElementById("stat-social-denials").textContent = data.post_to_stable_social_denials;
    document.getElementById("stat-llm-calls").textContent = data.total_llm_calls;
    document.getElementById("stat-cost").textContent = "$" + data.total_estimated_cost_usd.toFixed(4);

    const alertsDiv = document.getElementById("alerts");
    alertsDiv.innerHTML = "";
    for (const alert of data.alerts) {
      const div = document.createElement("div");
      div.className = "alert";
      div.textContent = "⚠ " + alert.message;
      alertsDiv.appendChild(div);
    }
  }

  refresh();
  setInterval(refresh, 10000);
})();
