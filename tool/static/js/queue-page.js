/* Queue page — unified view over every generation.
   Self-contained: it wires its own listeners and refreshes on a poll while
   its page is active.
   Globals borrowed from app.js: api, post, toast, esc, $, Icon, Dlg.

   Robustness rules:
   * ONE delegated click/change listener on the page — survives re-renders.
   * A single poll interval started once, gated by page visibility inside
     the tick (no start/stop churn that can silently kill updates).
   * refresh() updates only the volatile parts (rows, hold label), never
     the header/controls, so handlers can never fall off. */
(function () {
  "use strict";

  let FLEET = [];
  let HOLD = false;
  let FILTER = "@active";
  let LAST_SUMMARY = null;

  const x = () => Icon.get("x");

  function statusPill(s) {
    return `<span class="vs-st ${esc(s)}">${esc(s)}</span>`;
  }

  const qurl = (id) => "/api/queue/" + encodeURIComponent(id);

  function actions(it) {
    const b = (verb, label, cls) =>
      `<button class="ds-btn tiny ${cls || ""}" data-verb="${verb}" data-id="${esc(it.id)}">${label}</button>`;
    switch (it.status) {
      case "draft": return b("start", "Start", "primary") + b("edit", "Edit") + b("remove", x(), "danger");
      case "held": return b("start", "Release", "primary") + b("edit", "Edit") + b("remove", x(), "danger");
      case "queued": return b("hold", "Hold") + b("raise", "↑") + b("lower", "↓") + b("cancel", x(), "danger");
      case "running": return b("cancel", "Cancel", "danger");
      case "failed": case "canceled": return b("retry", "Retry", "primary") + b("remove", x(), "danger");
      case "done": return b("remove", x());
      default: return "";
    }
  }

  function row(it) {
    const created = (it.created || "").replace("T", " ").slice(5, 16);
    const pct = it.progress_total ? Math.round(100 * it.progress_done / it.progress_total) : 0;
    const prog = it.progress_total
      ? esc(`${it.progress_done}/${it.progress_total} (${pct}%)`)
      : (it.status === "running" ? "…" : "—");
    const warn = it.error ? ` <span class="ds-meta-txt">${Icon.get("warning")}</span>` : "";
    const wsel = it.status === "running"
      ? `<span class="ds-meta-txt">${esc(it.lease || it.worker)}</span>`
      : `<select class="q-worker" data-id="${esc(it.id)}">${workerOptions(it.worker)}</select>`;
    const err = it.error ? ` title="${esc(it.error)}"` : "";
    return `<tr data-id="${esc(it.id)}"${err}>
      <td><span class="ds-badge">${esc(it.kind)}</span></td>
      <td class="q-label">${esc(it.label || it.id)}${warn}</td>
      <td>${wsel}</td>
      <td>${statusPill(it.status)}</td>
      <td><span class="ds-meta-txt">${prog}</span></td>
      <td><span class="ds-meta-txt">${esc(it.attempts)}/${esc(it.max_attempts)}</span></td>
      <td><span class="ds-meta-txt">${esc(created)}</span></td>
      <td><div class="ds-actions">${actions(it)}</div></td>
    </tr>`;
  }

  function workerOptions(cur) {
    const ids = ["local", "any"].concat(FLEET.map((w) => w.id));
    return ids.map((w) =>
      `<option value="${esc(w)}" ${w === cur ? "selected" : ""}>${esc(w)}</option>`).join("");
  }

  async function refresh() {
    try {
      const [fw, fq] = await Promise.all([
        api("/api/workers"), api("/api/queue"),
      ]);
      FLEET = ((fw && fw.workers) || []).filter((w) => w.online);
      HOLD = !!(fq && fq.summary && fq.summary.hold_all);
      LAST_SUMMARY = fq;
      const label = document.getElementById("queue-hold-label");
      if (label) label.textContent = HOLD ? "Resume dispatch" : "Hold all";
      let rows = (fq && fq.items) || [];
      if (FILTER === "@active") {
        const act = ["draft", "held", "queued", "running"];
        rows = rows.filter((r) => act.includes(r.status));
      }
      const rowsEl = document.getElementById("queue-rows");
      if (rowsEl) rowsEl.innerHTML = rows.map(row).join("");
      const empty = document.getElementById("queue-empty");
      if (empty) {
        // .hidden has no generic display:none rule in styles.css — the
        // codebase hides empty-states by swapping content, so match that.
        empty.classList.toggle("hidden", rows.length > 0);
        empty.style.display = rows.length ? "none" : "";
        if (!rows.length) {
          empty.innerHTML = Icon.get("video") + "<div>queue is empty</div>";
        }
      }
      // .q-worker changes are handled by the delegated listener below (a
      // per-row onchange here as well sent every PATCH twice).
    } catch (e) {
      toast("queue: " + (e.message || e), true);
    }
  }

  /* ---- draft payload editor (JSON, validated server-side on start) ---- */
  function openEditor(id) {
    api("/api/queue").then((q) => {
      const item = (q.items || []).find((x) => x.id === id);
      if (!item) return toast("item vanished", true);
      let veil = document.getElementById("q-edit-veil");
      if (!veil) {
        veil = document.createElement("div");
        veil.id = "q-edit-veil";
        veil.className = "dlg-veil";
        veil.innerHTML = `<div class="dlg-card" role="dialog" aria-modal="true">
          <div class="dlg-head"><h3>Edit queued work</h3></div>
          <div class="dlg-body">
            <p class="ds-meta-txt">Parameters are validated again when the item starts.</p>
            <textarea id="q-edit-payload" class="mono" rows="14" style="width:100%"></textarea>
          </div>
          <div class="dlg-actions">
            <button type="button" class="btn" data-qe-cancel>Cancel</button>
            <button type="button" class="btn primary" data-qe-save>Save</button>
          </div></div>`;
        document.body.appendChild(veil);
        veil.querySelector("[data-qe-cancel]").onclick = closeEditor;
        veil.querySelector("[data-qe-save]").onclick = saveEditor;
      }
      veil.querySelector("#q-edit-payload").value =
        JSON.stringify(item.payload || {}, null, 2);
    });
  }

  async function saveEditor() {
    const veil = document.getElementById("q-edit-veil");
    if (!veil) return;
    let payload;
    try {
      payload = JSON.parse(veil.querySelector("#q-edit-payload").value);
    } catch (e) { return toast("invalid JSON: " + e.message, true); }
    try {
      await api(qurl(window.__qEditId), {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ payload }),
      });
      closeEditor();
      refresh();
      toast("draft updated");
    } catch (e) { toast(e.message, true); }
  }

  function closeEditor() {
    const veil = document.getElementById("q-edit-veil");
    if (veil) veil.remove();
  }

  /* ------------------------------------------- movie pipeline assignment */
  function loadAssignments() {
    const pid = (document.getElementById("shotassign-project") || {}).value;
    const list = document.getElementById("shotassign-list");
    if (!pid) { list.textContent = "pick a project to see where it renders"; return; }
    api(`/api/queue/shot-assignments?project_id=${encodeURIComponent(pid)}`)
      .then((d) => {
        const a = (d && d.assignments) || [];
        list.innerHTML = a.length
          ? a.map((x) => `${x.shot_id ? "shot " + esc(x.shot_id)
            : "<b>project default</b>"} → <b>${esc(x.worker)}</b>`).join(" · ")
          : "no overrides — pipeline renders on this PC (local)";
      })
      .catch((e) => { list.textContent = String(e.message || e); });
  }

  function initAssignment() {
    const sel = document.getElementById("shotassign-worker");
    if (!sel) return;
    const proj = document.getElementById("shotassign-project");
    if (proj) {
      api("/api/projects").then((d) => {
        const ps = (d && d.projects) || [];
        proj.innerHTML = ps.length
          ? ps.map((p) => `<option value="${esc(p.id)}">${esc(p.title || p.id)}</option>`).join("")
          : '<option value="">no projects</option>';
        loadAssignments();
      }).catch(() => { /* the list stays empty */ });
    }
    const fill = () => {
      sel.innerHTML = ["local", "any"].concat(FLEET.map((w) => w.id))
        .map((w) => `<option value="${esc(w)}">${esc(w)}</option>`).join("");
    };
    fill();
    const t = setInterval(() => {
      const page = document.getElementById("page-queue");
      if (!page || !page.classList.contains("active")) return;
      if (FLEET.length) { fill(); clearInterval(t); }
    }, 3000);
  }

  /* ------------------------------------------------- delegated handlers */
  function onDelegatedClick(e) {
    const verbBtn = e.target.closest("[data-verb]");
    if (verbBtn) {
      const id = verbBtn.dataset.id, verb = verbBtn.dataset.verb;
      if (verb === "edit") { window.__qEditId = id; openEditor(id); return; }
      const run = async () => {
        try {
          if (verb === "remove") await api(qurl(id), { method: "DELETE" });
          else await post(`${qurl(id)}/${verb}`, {});
          refresh();
        } catch (err) { toast(err.message, true); }
      };
      run();
      return;
    }
    if (e.target.closest("#btn-queue-refresh")) return refresh();
    if (e.target.closest("#btn-queue-hold")) {
      post(`/api/queue/hold-all?on=${!HOLD}`, {}).then(refresh)
        .catch((err) => toast(err.message, true));
      return;
    }
    if (e.target.closest("#btn-shotassign-save")) {
      const project_id = (document.getElementById("shotassign-project") || {}).value;
      if (!project_id) return toast("pick a project first", true);
      api("/api/queue/shot-assignments", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id, shot_id: "",
                               worker: document.getElementById("shotassign-worker").value }),
      }).then(() => { toast("project default saved — the pipeline will render there"); loadAssignments(); })
        .catch((err) => toast(err.message, true));
      return;
    }
  }

  function onDelegatedChange(e) {
    if (e.target.id === "queue-filter-status") {
      FILTER = e.target.value || "@active";
      refresh();
    } else if (e.target.classList && e.target.classList.contains("q-worker")) {
      api(qurl(e.target.dataset.id), {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ worker: e.target.value }),
      }).then(() => toast("worker assigned")).catch((err) => toast(err.message, true));
    } else if (e.target.id === "shotassign-project") {
      loadAssignments();
    }
  }

  function init() {
    const page = document.getElementById("page-queue");
    page.addEventListener("click", onDelegatedClick);
    page.addEventListener("change", onDelegatedChange);
    initAssignment();
    // one gated poll — no start/stop churn
    setInterval(() => {
      if (page.classList.contains("active")) {
        try { refresh(); }
        catch (e) { toast("queue refresh: " + (e.message || e), true); }
      }
    }, 3000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
