/* Workers page — fleet status + install handoff.
   Self-contained: it wires its own buttons and refreshes on a poll while
   its page is active.
   Globals borrowed from app.js: api, post, toast, esc, $, Icon, Dlg.
   Every worker field is worker-reported (any fleet-token holder can
   register one): escape everything that reaches innerHTML. */
(function () {
  "use strict";

  let FLEET = [];

  const arr = (v) => (Array.isArray(v) ? v : []);
  const wurl = (id) => "/api/workers/" + encodeURIComponent(id);

  const pill = (online) =>
    `<span class="pill ${online ? "run" : "off"}">${online ? "online" : "offline"}</span>`;

  function gpuLine(w) {
    const g = (w.gpu && w.gpu[0]) || null;
    if (!g || !g.name) return '<span class="ds-meta-txt">no GPU data yet</span>';
    return `<span class="ds-meta-txt">${esc(g.name)}</span>
      <span class="ds-meta-txt">${esc(g.mem_used)}/${esc(g.mem_total)} MiB</span>
      <span class="ds-meta-txt">${esc(g.temp)}°C</span>
      <span class="ds-meta-txt">${esc(g.util)}% util</span>`;
  }

  function rolesChips(w) {
    const caps = w.capabilities || {};
    const roles = arr(caps.roles);
    if (!roles.length) return "";
    return roles.map((r) =>
      `<span class="ds-meta-txt">[${esc(r)}]</span>`).join(" ");
  }

  function diskChip(w) {
    const d = (w.capabilities || {}).disk || {};
    if (!d.free_gb && d.free_gb !== 0) return "";
    return `<span class="ds-meta-txt">disk ${esc(d.free_gb)}/${esc(d.total_gb)}GB free</span>`;
  }

  function blockedBanner(w) {
    const caps = w.capabilities || {};
    if (!caps.blocked) return "";
    const miss = arr(caps.required_missing).join(", ");
    return `<div style="color:#e0a35c;font-size:12px;margin:4px 0">` +
      `blocked — missing required models: ${esc(miss || "?")} — use “Install models”.</div>`;
  }

  function workerCard(w) {
    const models = (w.models && Object.values(w.models).flat()) || [];
    const seen = new Set();
    const nModels = models.filter((m) => !seen.has(m) && seen.add(m)).length;
    return `<div class="ds-card" data-worker="${esc(w.id)}">
      <div class="body">
        <div class="name">${esc(w.name)} ${pill(w.online)}
          <span class="ds-meta-txt">${esc(w.host)}:${esc(w.port)}</span></div>
        <div class="meta">${gpuLine(w)}
          <span class="ds-meta-txt">${nModels} model files</span>
          <span class="ds-meta-txt">agent v${esc(w.agent_version || "?")}</span>
        </div>
        <div class="meta">${rolesChips(w)} ${diskChip(w)}</div>
        ${blockedBanner(w)}
        <div class="meta ds-meta-txt">last heartbeat: ${esc(w.last_heartbeat || "never")}</div>
        <div class="ds-actions">
          <button class="ds-btn tiny" data-act="test">Test</button>
          <button class="ds-btn tiny" data-act="models">Models</button>
          <button class="ds-btn tiny" data-act="install">Push models</button>
          <button class="ds-btn tiny" data-act="group">Install group</button>
          <button class="ds-btn danger tiny" data-act="delmodel">Delete model</button>
          <button class="ds-btn danger tiny" data-act="remove">Remove</button>
        </div>
      </div>
    </div>`;
  }

  async function openInstall(id) {
    let pkgs = null;
    try {
      const r = await api("/api/workers/packages");
      pkgs = r && r.packages;
    } catch (e) { /* fall through */ }
    if (!pkgs) return toast("could not read the model packages manifest", true);
    const names = Object.keys(pkgs);
    if (!names.length) return toast("no packages defined", true);

    let html = '<div class="muted small" style="margin-bottom:6px">' +
      "Tick exactly what this PC should have — nothing else is copied. " +
      "Shared files (e.g. the Z-Image unet/VAE) appear once per package; " +
      "installing the same file twice is harmless.</div>" +
      '<div style="max-height:52vh;overflow:auto;border:1px solid #2a2e3a;' +
      'border-radius:4px;padding:8px">';
    names.forEach(function (n) {
      const p = pkgs[n] || {};
      html += '<div style="margin-bottom:10px">' +
        '<label style="display:flex;gap:8px;align-items:center;font-weight:600">' +
          '<input type="checkbox" class="wp-all" data-pkg="' + esc(n) + '">' +
          esc(n) + ' <span class="ds-meta-txt">' + esc(p.total_mb || 0) +
          " MB</span></label>" +
        '<div class="muted small" style="margin:2px 0 6px 24px">' +
          esc(p.description || "") + "</div>";
      (p.files || []).forEach(function (f) {
        html += '<label style="display:flex;gap:8px;align-items:center;' +
          'margin-left:24px">' +
          '<input type="checkbox" class="wf-one" data-pkg="' + esc(n) +
            '" value="' + esc(f.path) + '">' +
          '<span class="mono" style="font-size:12px">' + esc(f.path) +
            "</span>" +
          '<span class="ds-meta-txt">' +
            (f.size_mb != null ? esc(f.size_mb) + " MB" : "folder") +
          "</span></label>";
      });
      html += "</div>";
    });
    html += '</div><div id="wi-total" class="muted small" ' +
      'style="margin-top:6px"></div>';

    const pending = Dlg.choose({
      title: "Install models on " + id,
      message: html,
      choices: [
        { label: "Install selected", value: "go" },
        { label: "Cancel", value: "cancel" },
      ],
    });
    // Dlg builds its DOM synchronously; grab the boxes now (the refs stay
    // valid after the veil closes) and wire select-all + a live size total.
    const allBoxes = Array.prototype.slice.call(
      document.querySelectorAll(".wf-one"));
    const allToggles = Array.prototype.slice.call(
      document.querySelectorAll(".wp-all"));
    const totalEl = document.getElementById("wi-total");
    function syncTotal() {
      let mb = 0, n = 0;
      allBoxes.forEach(function (b) {
        if (!b.checked) return;
        n += 1;
        const t = b.parentElement.querySelector(".ds-meta-txt");
        const v = parseFloat(t && t.textContent);
        if (!isNaN(v)) mb += v;
      });
      if (totalEl) {
        totalEl.textContent = n
          ? n + " file(s) selected — " + Math.round(mb) + " MB"
          : "nothing selected yet";
      }
    }
    allToggles.forEach(function (t) {
      t.onchange = function () {
        allBoxes.forEach(function (b) {
          if (b.dataset.pkg === t.dataset.pkg) b.checked = t.checked;
        });
        syncTotal();
      };
    });
    allBoxes.forEach(function (b) { b.onchange = syncTotal; });
    syncTotal();

    const choice = await pending;
    if (choice !== "go") return;
    const files = allBoxes.filter(function (b) { return b.checked; })
      .map(function (b) { return b.value; });
    if (!files.length) return toast("nothing selected", true);
    toast("installing " + files.length + " file(s) on " + id +
          " — this can take a while…");
    try {
      const r = await post(`${wurl(id)}/install`, { files: files },
                           86400000);
      const results = (r && r.results) || [];
      const bad = results.filter(function (x) { return !x.ok; });
      toast(bad.length
        ? `${bad.length} of ${results.length} file(s) failed — check the worker log`
        : `installed ${results.length} file(s) on ${id} (sha256 verified)`,
        bad.length > 0);
      refresh();
    } catch (e) {
      toast("install failed: " + e.message, true);
    }
  }

  async function openGroupInstall(id) {
    let r = null;
    try {
      r = await api("/api/workers/groups");
    } catch (e) { /* fall through */ }
    if (!r || !r.ok) {
      return toast((r && r.error) || "groups manifest unreadable", true);
    }
    if (!r.hf_repo_set) {
      return toast("models not published yet — run "
        + "tool/scripts/publish_models_hf.py on the PC first", true);
    }
    const groups = r.groups || {};
    const names = Object.keys(groups);
    if (!names.length) return toast("no groups defined", true);
    let html = '<div class="muted small" style="margin-bottom:6px">'
      + "Pulled on the worker's own pipe from the private HF repo "
      + "(resume + sha256). Files already present with a matching size "
      + "are skipped.</div>"
      + '<div style="max-height:52vh;overflow:auto;border:1px solid #2a2e3a;'
      + 'border-radius:4px;padding:8px">';
    names.forEach(function (n) {
      const g = groups[n] || {};
      const size = g.total_gb != null ? g.total_gb + " GB" : "size unknown";
      html += '<label style="display:flex;gap:8px;align-items:center;'
        + 'margin:4px 0">'
        + '<input type="checkbox" class="wg-one" value="' + esc(n) + '">'
        + '<b>' + esc(n) + '</b>'
        + '<span class="ds-meta-txt">' + esc(g.files) + " file(s), " + esc(size)
        + "</span></label>"
        + '<div class="muted small" style="margin:0 0 6px 24px">'
        + esc(g.description || "") + "</div>";
    });
    html += "</div>";
    const choice = await Dlg.choose({
      title: "Install model group(s) on " + id,
      message: html,
      choices: [
        { label: "Install selected", value: "go" },
        { label: "Cancel", value: "cancel" },
      ],
    });
    if (choice !== "go") return;
    const sel = Array.prototype.slice.call(
      document.querySelectorAll(".wg-one"))
      .filter(function (b) { return b.checked; })
      .map(function (b) { return b.value; });
    if (!sel.length) return toast("nothing selected", true);
    let res;
    try {
      res = await post(`${wurl(id)}/install_group`, { groups: sel },
                       120000);
    } catch (e) {
      return toast("install failed: " + e.message, true);
    }
    if (!res || !res.ok) return toast((res && res.error) || "install failed", true);
    toast(`pulling ${res.files} file(s) on ${id} — background install ${res.install_id}`);
    pollGroup(id, res.install_id);
  }

  async function pollGroup(id, gid) {
    let lastCurrent = "";
    for (let i = 0; i < 1440; i++) {          // cap ~2h of polling
      await new Promise((r) => setTimeout(r, 5000));
      let st;
      try {
        st = await api(`${wurl(id)}/install_group/${encodeURIComponent(gid)}`);
      } catch (e) { continue; }               // transient — keep waiting
      if (st.status === "running") {
        if (st.current && st.current !== lastCurrent) {
          lastCurrent = st.current;
          const done = (st.files || []).filter((f) => f.done).length;
          toast(`${id}: pulling ${st.current.split("/").pop()} `
            + `(${done}/${(st.files || []).length})`);
        }
        continue;
      }
      if (st.status === "done") {
        toast(`group install finished on ${id} — all files verified`);
      } else {
        const bad = (st.files || []).filter((f) => !f.ok)
          .map((f) => f.path).join(", ");
        toast(`group install FAILED on ${id}: ${bad || st.error || "?"}`, true);
      }
      refresh();
      return;
    }
    toast("group install still running — check back later", true);
  }

  async function openDeleteModel(id) {
    const w = FLEET.find((x) => x.id === id) || {};
    const inv = w.models || {};
    const paths = Object.entries(inv).flatMap(([sub, files]) =>
      arr(files).map((f) => `${sub}/${f}`));
    if (!paths.length) return toast("worker reports no model files", true);
    let html = '<div class="muted small" style="margin-bottom:6px">'
      + "Deletes the file on the worker only — the PC copy and the HF "
      + "repo are untouched.</div>"
      + '<div style="max-height:52vh;overflow:auto;border:1px solid #2a2e3a;'
      + 'border-radius:4px;padding:8px">';
    paths.forEach((p) => {
      html += '<div style="display:flex;justify-content:space-between;'
        + 'gap:8px;margin:2px 0;align-items:center">'
        + `<span class="mono" style="font-size:12px">${esc(p)}</span>`
        + `<button class="ds-btn danger tiny" data-del="${esc(p)}">Delete</button>`
        + "</div>";
    });
    html += "</div>";
    Dlg.choose({
      title: "Delete models on " + id,
      message: html,
      choices: [{ label: "Close", value: "close" }],
    });
    document.querySelectorAll("[data-del]").forEach((b) => {
      b.onclick = async () => {
        const ok = await Dlg.confirm({
          title: "Delete model file",
          message: `Delete <b>${esc(b.dataset.del)}</b> from ${esc(id)}?`,
          okLabel: "Delete", danger: true,
        });
        if (!ok) return;
        try {
          const r = await api(`${wurl(id)}/models?path=${
            encodeURIComponent(b.dataset.del)}`, { method: "DELETE" });
          toast(r && r.ok
            ? `deleted (freed ${((r.freed || 0) / 2**30).toFixed(2)} GB)`
            : (r && r.error) || "delete failed", !(r && r.ok));
          if (r && r.ok) {
            const row = b.closest("div");
            if (row) row.remove();
            refresh();
          }
        } catch (e) { toast(e.message, true); }
      };
    });
  }

  async function refresh() {
    try {
      const d = await api("/api/workers");
      FLEET = d.workers || [];
      document.getElementById("workers-count").textContent =
        FLEET.length ? `${FLEET.filter((w) => w.online).length} of ${FLEET.length} online` : "";
      const grid = document.getElementById("workers-grid");
      grid.innerHTML = FLEET.map(workerCard).join("")
        || '<div class="ds-empty">no workers yet — install one on the second PC</div>';
      const tok = d.settings && d.settings.token_set;
      document.getElementById("workers-token-state").textContent = tok ? "set (shared secret)" : "MISSING - workers are refused until you generate one or restart the studio";
      bindCards();
    } catch (e) {
      toast("workers: " + e.message, true);
    }
  }

  function bindCards() {
    document.querySelectorAll("#workers-grid [data-act]").forEach((b) => {
      b.onclick = async () => {
        const id = b.closest("[data-worker]").dataset.worker;
        if (b.dataset.act === "test") {
          toast("probing " + id + "…");
          try {
            const r = await api(`${wurl(id)}/test`);
            if (!r.reachable) return toast(id + ": unreachable — " + (r.error || "?"), true);
            const st = r.selftest || {};
            const nodes = st.checks && st.checks.nodes || {};
            const missing = Object.entries(nodes).filter(([, ok]) => !ok).map(([k]) => k);
            toast(`${id}: comfy ${st.comfy ? "up" : "DOWN"}, ${st.disk_free_gb}GB free`
              + (missing.length ? `, missing nodes: ${missing.join(", ")}` : ", nodes ok"),
              !st.comfy || missing.length > 0);
          } catch (e) { toast("test failed: " + e.message, true); }
        } else if (b.dataset.act === "models") {
          const w = FLEET.find((x) => x.id === id) || {};
          const inv = w.models || {};
          const lines = Object.entries(inv).map(
            ([k, v]) => `${esc(k)}: ${arr(v).length} file(s)`).join("<br>");
          Dlg.choose({
            title: "Models on " + id,
            message: lines || "no models directory found",
            choices: [{ label: "Close", value: "close" }],
          });
        } else if (b.dataset.act === "install") {
          openInstall(id);
        } else if (b.dataset.act === "group") {
          openGroupInstall(id);
        } else if (b.dataset.act === "delmodel") {
          openDeleteModel(id);
        } else if (b.dataset.act === "remove") {
          const ok = await Dlg.confirm({
            title: "Remove worker",
            message: `Forget <b>${esc(id)}</b>? The worker PC keeps running; it will re-register if its agent reloads.`,
            okLabel: "Remove", danger: true,
          });
          if (!ok) return;
          // the route is DELETE-only (a POST here 405'd and aborted the handler)
          try {
            await api(wurl(id), { method: "DELETE" });
            toast("removed " + id);
          } catch (e) { toast("remove failed: " + e.message, true); }
          refresh();
        }
      };
    });
  }

  function pageActive() {
    return document.querySelector("#page-workers").classList.contains("active");
  }

  function init() {
    document.getElementById("btn-workers-refresh").onclick = refresh;
    document.getElementById("btn-workers-token").onclick = async () => {
      const ok = await Dlg.confirm({
        title: "New install token",
        message: "Regenerate the shared token? Existing workers stay online " +
          "(they re-register with their stored token — which stops matching). " +
          "Re-download INSTALL-WORKER.bat afterwards.",
        okLabel: "Regenerate", danger: true,
      });
      if (!ok) return;
      try {
        // r.token is the FLEET token (workers.shared_token): it is baked into
        // the installer server-side and must never be stored as the API token.
        const r = await post("/api/workers/token", {});
        toast(r.token ? "new token generated — download a fresh installer" : "failed", !r.token);
        refresh();
      } catch (e) { toast(e.message, true); }
    };
    document.getElementById("btn-workers-installer").onclick = async () => {
      try {
        const r = await window.Auth.fetch("/api/workers/installer");
        if (!r.ok) {
          const j = await r.json().catch(() => ({}));
          return toast(j.detail || j.error || "installer not generated yet", true);
        }
        const blob = await r.blob();
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = "INSTALL-WORKER.bat";
        a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 10000);
      } catch (e) { toast("download failed: " + e.message, true); }
    };
    // one gated poll — no start/stop churn
    setInterval(() => {
      if (pageActive()) refresh();
    }, 5000);   // same cadence as Video Studio jobs
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
