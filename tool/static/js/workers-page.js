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

  /* the GPU as a memory meter, with its temperature and load beside it */
  function gpuBlock(w) {
    const g = (w.gpu && w.gpu[0]) || null;
    if (!g || !g.name) return '<div class="wk-gpu"><span class="ds-meta-txt">No GPU readout yet</span></div>';
    const used = Number(g.mem_used) || 0, total = Number(g.mem_total) || 0;
    const pct = total ? Math.min(100, Math.round(100 * used / total)) : 0;
    const gb = (mib) => (mib / 1024).toFixed(1);
    // an offline PC's last readout is stale: name and size only
    return `<div class="wk-gpu">
        <div class="wk-gpu-top"><b>${esc(g.name)}</b><span class="spacer"></span>
          ${w.online ? `<span class="ds-meta-txt">${esc(g.temp)}°C</span><span class="ds-meta-txt">${esc(g.util)}% busy</span>` : ""}</div>
        <div class="wk-bar"><i style="width:${w.online ? pct : 0}%"></i></div>
        <div class="ds-meta-txt">${!total ? "" : w.online ? gb(used) + " of " + gb(total) + " GB in use" : gb(total) + " GB"}</div>
      </div>`;
  }

  function rolesChips(w) {
    const caps = w.capabilities || {};
    const roles = arr(caps.roles);
    return roles.map((r) => `<span class="chip">${esc(r)}</span>`).join("");
  }

  function diskChip(w) {
    const d = (w.capabilities || {}).disk || {};
    if (!d.free_gb && d.free_gb !== 0) return "";
    return `<span class="ds-meta-txt">${esc(d.free_gb)} of ${esc(d.total_gb)} GB free</span>`;
  }

  function blockedBanner(w) {
    const caps = w.capabilities || {};
    if (!caps.blocked) return "";
    const miss = arr(caps.required_missing).join(", ");
    return `<div class="wk-blocked">${Icon.get("warning")}<span>Gets no work until it has these models: ` +
      `${esc(miss || "?")}. Use Add models.</span></div>`;
  }

  function workerCard(w) {
    const models = (w.models && Object.values(w.models).flat()) || [];
    const seen = new Set();
    const nModels = models.filter((m) => !seen.has(m) && seen.add(m)).length;
    return `<div class="ds-card wk-card${w.online ? "" : " off"}" data-worker="${esc(w.id)}">
      <div class="body">
        <div class="wk-head">
          <span class="wk-dot" title="${w.online ? "Online" : "Offline"}"></span>
          <span class="name">${esc(w.name)}</span>
          <span class="ds-meta-txt">${w.online ? "Online" : "Offline"}</span>
          <span class="spacer"></span>
          <span class="ds-meta-txt mono">${esc(w.host)}:${esc(w.port)}</span>
        </div>
        ${gpuBlock(w)}
        <div class="meta">${rolesChips(w)}
          <button class="linkish wk-models" type="button" data-act="models" data-tip="See the model files on this PC">${nModels} model ${nModels === 1 ? "file" : "files"}</button>${diskChip(w)}
          <span class="ds-meta-txt">Agent ${esc(w.agent_version || "?")}</span>
        </div>
        ${blockedBanner(w)}
        <div class="ds-meta-txt wk-seen">Last heard from ${esc(w.last_heartbeat || "never")}</div>
        <div class="ds-actions">
          <button class="btn small" type="button" data-act="test">Test</button>
          <button class="btn small ghost" type="button" data-act="install">Add models</button>
          <button class="btn small ghost" type="button" data-act="group">Add a group</button>
          <span class="spacer"></span>
          <button class="btn small ghost icon" type="button" data-act="delmodel" data-tip="Delete a model file on this PC">${Icon.get("trash")}</button>
          <button class="btn small ghost icon" type="button" data-act="remove" data-tip="Remove this worker PC" aria-label="Remove this worker PC">${Icon.get("x")}</button>
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
    if (!pkgs) return toast("The model package list did not load", true);
    const names = Object.keys(pkgs);
    if (!names.length) return toast("No model packages are defined", true);

    let html = '<div class="muted small" style="margin-bottom:6px">' +
      "Tick what this PC should have. Nothing else is copied. " +
      "A file two packages share (such as the Z-Image unet and VAE) shows in both; " +
      "copying it twice does no harm.</div>" +
      '<div class="wk-list">';
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
      title: "Add models to " + id,
      message: html,
      choices: [
        { label: "Copy the ticked files", value: "go" },
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
          ? n + (n === 1 ? " file, " : " files, ") + Math.round(mb) + " MB"
          : "Nothing ticked yet";
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
    if (!files.length) return toast("Tick at least one file", true);
    toast("Copying " + files.length + (files.length === 1 ? " file" : " files") + " to " + id +
          ". Large models take a while.");
    try {
      const r = await post(`${wurl(id)}/install`, { files: files },
                           86400000);
      const results = (r && r.results) || [];
      const bad = results.filter(function (x) { return !x.ok; });
      toast(bad.length
        ? `${bad.length} of ${results.length} files did not copy. The worker's log says why.`
        : `Copied ${results.length} ${results.length === 1 ? "file" : "files"} to ${id}, checksums verified`,
        bad.length > 0);
      refresh();
    } catch (e) {
      toast("The copy failed: " + e.message, true);
    }
  }

  async function openGroupInstall(id) {
    let r = null;
    try {
      r = await api("/api/workers/groups");
    } catch (e) { /* fall through */ }
    if (!r || !r.ok) {
      return toast((r && r.error) || "The model group list did not load", true);
    }
    if (!r.hf_repo_set) {
      return toast("No model repository is set up yet. Run "
        + "tool/scripts/publish_models_hf.py on this PC first.", true);
    }
    const groups = r.groups || {};
    const names = Object.keys(groups);
    if (!names.length) return toast("No model groups are defined", true);
    let html = '<div class="muted small" style="margin-bottom:6px">'
      + "The worker downloads these itself from your private Hugging Face repository, "
      + "resuming and checking every file. Files it already has are skipped.</div>"
      + '<div class="wk-list">';
    names.forEach(function (n) {
      const g = groups[n] || {};
      const size = g.total_gb != null ? g.total_gb + " GB" : "size not known";
      html += '<label style="display:flex;gap:8px;align-items:center;'
        + 'margin:4px 0">'
        + '<input type="checkbox" class="wg-one" value="' + esc(n) + '">'
        + '<b>' + esc(n) + '</b>'
        + '<span class="ds-meta-txt">' + esc(g.files) + " files, " + esc(size)
        + "</span></label>"
        + '<div class="muted small" style="margin:0 0 6px 24px">'
        + esc(g.description || "") + "</div>";
    });
    html += "</div>";
    const choice = await Dlg.choose({
      title: "Add model groups to " + id,
      message: html,
      choices: [
        { label: "Download the ticked groups", value: "go" },
        { label: "Cancel", value: "cancel" },
      ],
    });
    if (choice !== "go") return;
    const sel = Array.prototype.slice.call(
      document.querySelectorAll(".wg-one"))
      .filter(function (b) { return b.checked; })
      .map(function (b) { return b.value; });
    if (!sel.length) return toast("Tick at least one group", true);
    let res;
    try {
      res = await post(`${wurl(id)}/install_group`, { groups: sel },
                       120000);
    } catch (e) {
      return toast("The download did not start: " + e.message, true);
    }
    if (!res || !res.ok) return toast((res && res.error) || "The download did not start", true);
    toast(`${id} is downloading ${res.files} files in the background`);
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
          toast(`${id} is downloading ${st.current.split("/").pop()} `
            + `(${done} of ${(st.files || []).length})`);
        }
        continue;
      }
      if (st.status === "done") {
        toast(`${id} has the model groups, every file checked`);
      } else {
        const bad = (st.files || []).filter((f) => !f.ok)
          .map((f) => f.path).join(", ");
        toast(`The download on ${id} failed: ${bad || st.error || "?"}`, true);
      }
      refresh();
      return;
    }
    toast("The download is still running. Check back later.", true);
  }

  async function openDeleteModel(id) {
    const w = FLEET.find((x) => x.id === id) || {};
    const inv = w.models || {};
    const paths = Object.entries(inv).flatMap(([sub, files]) =>
      arr(files).map((f) => `${sub}/${f}`));
    if (!paths.length) return toast("This worker reports no model files", true);
    let html = '<div class="muted small" style="margin-bottom:6px">'
      + "Deletes the file on the worker only. The copy on this PC and in your "
      + "Hugging Face repository stay.</div>"
      + '<div class="wk-list">';
    paths.forEach((p) => {
      html += '<div style="display:flex;justify-content:space-between;'
        + 'gap:8px;margin:2px 0;align-items:center">'
        + `<span class="mono" style="font-size:12px">${esc(p)}</span>`
        + `<button class="ds-btn danger tiny" data-del="${esc(p)}">Delete</button>`
        + "</div>";
    });
    html += "</div>";
    Dlg.choose({
      title: "Delete models from " + id,
      message: html,
      choices: [{ label: "Close", value: "close" }],
    });
    document.querySelectorAll("[data-del]").forEach((b) => {
      b.onclick = async () => {
        const ok = await Dlg.confirm({
          title: "Delete this model file?",
          message: `Delete <b>${esc(b.dataset.del)}</b> from ${esc(id)}?`,
          okLabel: "Delete", danger: true,
        });
        if (!ok) return;
        try {
          const r = await api(`${wurl(id)}/models?path=${
            encodeURIComponent(b.dataset.del)}`, { method: "DELETE" });
          toast(r && r.ok
            ? `Deleted, ${((r.freed || 0) / 2**30).toFixed(2)} GB freed`
            : (r && r.error) || "The file was not deleted", !(r && r.ok));
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
      document.getElementById("workers-count").textContent = FLEET.length
        ? `${FLEET.filter((w) => w.online).length} of ${FLEET.length} online`
        : "Other PCs that render for this studio.";
      const grid = document.getElementById("workers-grid");
      grid.innerHTML = FLEET.map(workerCard).join("")
        || `<div class="ds-empty"><span class="big">${Icon.get("server")}</span><b>No worker PCs yet</b>Add one below: it renders clips and pictures next to this PC.</div>`;
      const tok = d.settings && d.settings.token_set;
      const ts = document.getElementById("workers-token-state");
      ts.textContent = tok ? "Install token set" : "No install token: worker PCs cannot connect until you make one";
      ts.classList.toggle("bad", !tok);
      bindCards();
    } catch (e) {
      toast("The worker PCs did not load: " + e.message, true);
    }
  }

  function bindCards() {
    document.querySelectorAll("#workers-grid [data-act]").forEach((b) => {
      b.onclick = async () => {
        const id = b.closest("[data-worker]").dataset.worker;
        if (b.dataset.act === "test") {
          toast("Testing " + id + "…");
          try {
            const r = await api(`${wurl(id)}/test`);
            if (!r.reachable) return toast(id + " did not answer: " + (r.error || "?"), true);
            const st = r.selftest || {};
            const nodes = st.checks && st.checks.nodes || {};
            const missing = Object.entries(nodes).filter(([, ok]) => !ok).map(([k]) => k);
            toast(`${id}: ComfyUI ${st.comfy ? "running" : "not running"}, ${st.disk_free_gb} GB free`
              + (missing.length ? `, missing nodes: ${missing.join(", ")}` : ", every node there"),
              !st.comfy || missing.length > 0);
          } catch (e) { toast("The test failed: " + e.message, true); }
        } else if (b.dataset.act === "models") {
          const w = FLEET.find((x) => x.id === id) || {};
          const inv = w.models || {};
          const lines = Object.entries(inv).map(
            ([k, v]) => `${esc(k)}: ${arr(v).length} ${arr(v).length === 1 ? "file" : "files"}`).join("<br>");
          Dlg.choose({
            title: "Models on " + id,
            message: lines || "This PC has no models folder yet.",
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
            title: "Remove this worker PC?",
            message: `The studio forgets <b>${esc(id)}</b>. The PC keeps running and shows up again when its agent restarts.`,
            okLabel: "Remove", danger: true,
          });
          if (!ok) return;
          // the route is DELETE-only (a POST here 405'd and aborted the handler)
          try {
            await api(wurl(id), { method: "DELETE" });
            toast("Removed " + id);
          } catch (e) { toast("It was not removed: " + e.message, true); }
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
        title: "Make a new install token?",
        message: "Worker PCs that are online stay online, but their saved token stops " +
          "matching, so they cannot connect again after a restart. Download " +
          "INSTALL-WORKER.bat again afterwards and run it on each PC.",
        okLabel: "Make a new token", danger: true,
      });
      if (!ok) return;
      try {
        // r.token is the FLEET token (workers.shared_token): it is baked into
        // the installer server-side and must never be stored as the API token.
        const r = await post("/api/workers/token", {});
        toast(r.token ? "New token made. Download the installer again." : "No new token was made", !r.token);
        refresh();
      } catch (e) { toast(e.message, true); }
    };
    document.getElementById("btn-workers-installer").onclick = async () => {
      try {
        const r = await window.Auth.fetch("/api/workers/installer");
        if (!r.ok) {
          const j = await r.json().catch(() => ({}));
          return toast(j.detail || j.error || "The installer is not ready yet", true);
        }
        const blob = await r.blob();
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = "INSTALL-WORKER.bat";
        a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 10000);
      } catch (e) { toast("The download failed: " + e.message, true); }
    };
    // one gated poll — no start/stop churn
    setInterval(() => {
      if (pageActive()) refresh();
    }, 5000);   // same cadence as Video Studio jobs
    // and at once when the page opens
    window.addEventListener("tools:page", (e) => { if (e.detail === "workers") refresh(); });
    if (pageActive()) refresh();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
