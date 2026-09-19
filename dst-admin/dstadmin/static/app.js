const dstAdmin = {
  async fetchJSON(url) {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error(url + " → HTTP " + r.status);
    return r.json();
  },
  async fetchText(url) {
    const r = await fetch(url, { cache: "no-store" });
    return r.ok ? r.text() : "(HTTP " + r.status + ")";
  },
  setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  },
  async refreshStatus() {
    try {
      const s = await this.fetchJSON("/api/status");
      const c = s.container;
      const state = document.getElementById("c-state");
      state.textContent = c.status;
      state.className = "pill " + (c.status === "running" ? "good" : "bad");
      let detail = c.exists ? (c.status === "running" ? "since " + c.started_at : "exit code " + c.exit_code)
                            : "container not created — run: podman compose up -d";
      if (s.podman_error) detail = s.podman_error;
      this.setText("c-detail", detail);
      const live = s.saves ? s.saves.live : null;
      this.setText("day", live && live.cycles >= 0 ? String(live.cycles + 1) : "–");
      this.setText("players", live ? String(live.players) : "–");
      const tbody = document.querySelector("#shards tbody");
      if (tbody) {
        tbody.textContent = "";
        if (live) for (const [name, sh] of Object.entries(live.shards)) {
          const tr = document.createElement("tr");
          const pill = `<span class="pill ${sh.alive ? "good" : "bad"}">${sh.alive ? "live" : "down"}</span>`;
          tr.innerHTML = `<td>${name}</td><td>${pill}</td><td>${sh.players}</td>`;
          tbody.appendChild(tr);
        }
      }
      const lb = s.saves && s.saves.last_backup;
      this.setText("last-backup", lb ? `${lb.name} (${lb.tag}${lb.uploaded ? ", uploaded" : ""})` : "none yet");
      const lu = s.saves && s.saves.last_upload;
      this.setText("last-upload", lu ? `${lu.name} at ${lu.at}` : "none yet");
      if (s.saves) {
        const p = s.saves.policy;
        this.setText("policy", `R2 upload every ${p.r2_every_days} in-game days, on empty: ${p.r2_on_empty ? "yes" : "no"}; keep ${p.r2_keep} in R2, ${p.local_keep} local`);
      }
      if (s.saves_error) this.setText("policy", "dst-saves: " + s.saves_error);
    } catch (e) {
      this.setText("c-detail", String(e));
    }
  },
  async refreshLogs() {
    for (const pre of document.querySelectorAll("pre.log[data-log]")) {
      const text = await this.fetchText("/api/logs/" + encodeURIComponent(pre.dataset.log));
      const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40 || !pre.textContent;
      pre.textContent = text;
      if (atBottom) pre.scrollTop = pre.scrollHeight;
    }
  },
  startDashboard() {
    this.refreshStatus();
    this.refreshLogs();
    setInterval(() => this.refreshStatus(), 5000);
    setInterval(() => this.refreshLogs(), 5000);
  },
  startLogs() {
    this.refreshLogs();
    setInterval(() => this.refreshLogs(), 5000);
  },
};
