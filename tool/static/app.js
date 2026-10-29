/* Kinoloom — control room */
const $ = (id) => document.getElementById(id);
const _inflightGet = new Map();
const _pollCache = new Map();
let _pollGen = 0;
const POLL_COOLDOWN_MS = {
  "/api/status": 2000,
  "/api/queue": 2500,
  "/api/vram": 3000,
  "/api/workers": 4000,
};

/* Any mutation can change what a polled endpoint returns (POST
   /api/queue/{id}/hold -> /api/queue, DELETE /api/workers/{id} ->
   /api/workers, a training cancel -> /api/queue ...), so drop every cached
   poll and detach in-flight ones; the generation bump stops a GET that was
   already in flight from re-caching pre-mutation data. */
function invalidatePolls() {
  _pollGen++;
  _pollCache.clear();
  _inflightGet.clear();
}

/* API token + ?token= capture + the 401 prompt live in js/api.js (Auth). */
const api = async (path, opts = {}) => {
  const method = (opts.method || "GET").toUpperCase();
  const basePath = path.split("?")[0];
  const cooldown = method === "GET" ? POLL_COOLDOWN_MS[basePath] : 0;
  const pooled = !!cooldown && !opts.skipCooldown;

  if (method !== "GET") invalidatePolls();

  if (pooled) {
    const cached = _pollCache.get(path);
    if (cached && (Date.now() - cached.ts) < cooldown) {
      return cached.data;
    }
    if (_inflightGet.has(path)) {
      return _inflightGet.get(path);
    }
  }

  const isForm = typeof FormData !== "undefined" && opts.body instanceof FormData;
  const defaultHeaders = isForm ? {} : { "Content-Type": "application/json" };
  const headers = Object.assign({}, defaultHeaders, opts.headers || {});
  const gen = _pollGen;

  let fetchPromise = null;
  fetchPromise = (async () => {
    try {
      const res = await window.Auth.fetch(path, {
        ...opts,
        headers,
      });
      if (!res.ok) {
        let errText = `${path}: HTTP ${res.status}`;
        try {
          const errJson = await res.json();
          if (errJson && (errJson.detail || errJson.error)) {
            errText = errJson.detail || errJson.error;
          }
        } catch (_) {}
        throw new Error(errText);
      }
      const data = await res.json();
      if (cooldown && gen === _pollGen) {
        _pollCache.set(path, { ts: Date.now(), data });
      }
      return data;
    } finally {
      if (pooled && _inflightGet.get(path) === fetchPromise) {
        _inflightGet.delete(path);
      }
      if (method !== "GET") invalidatePolls();
    }
  })();

  if (pooled) {
    _inflightGet.set(path, fetchPromise);
  }

  return fetchPromise;
};
const post = (path, body = {}) =>
  api(path, { method: "POST", body: JSON.stringify(body) });
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (m) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m]));
if (typeof window !== "undefined") {
  window.esc = esc;
}

let toastTimer = null;
function toast(msg, isErr = false) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.toggle("err", !!isErr);
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 4200);
}

/* ------------------------------------------------------------- nav */
/* The sidebar is the shared shell nav (js/shell.js); this shell's pages are
   hash routes (/studio-tools#videostudio). */
const PAGE_LOADERS = {
  settings: () => { loadSettings(); setSettingsPane(SET_PANE); },
  casting: () => refreshDatasets(),
  videos: () => refreshVideos(),
  images: () => imagesPage(),
  videostudio: () => videoStudioPage(),
  logs: () => refreshLogs(),
};
function go(page) {
  if (!document.getElementById("page-" + page)) page = Shell.DEFAULT.tools;
  document.querySelectorAll(".content > .page").forEach((p) =>
    p.classList.toggle("active", p.id === `page-${page}`));
  Shell.setActive(page);
  window.dispatchEvent(new CustomEvent("tools:page", { detail: page }));
  if (PAGE_LOADERS[page]) PAGE_LOADERS[page]();
}
window.addEventListener("hashchange", () => go(location.hash.replace(/^#/, "")));

/* --------------------------------------------------------- training */
/* Casting: Generate looks | Pick the best | All batches, and the legacy
   Wan 2.2 LoRA panes (Train | Models), reached from the page foot. */

/* ---- pane switching ---- */
document.querySelectorAll("#train-tabs .ds-tab").forEach((t) => {
  t.onclick = () => {
    const pane = t.dataset.pane;
    document.querySelectorAll("#train-tabs .ds-tab").forEach((x) =>
      x.classList.toggle("active", x === t));
    document.querySelectorAll("#page-casting .ds-pane").forEach((p) =>
      p.classList.toggle("active", p.id === "pane-" + pane));
    // the Wan 2.2 LoRA tabs show only while one of their panes is open
    $("train-tabs").classList.toggle("legacy-open", pane === "train" || pane === "models");
    if (pane === "datasets") refreshDatasets();
    if (pane === "curate") loadCuration();
    if (pane === "train") { refreshTrainDatasets(); refreshRuns(); }
    if (pane === "models") refreshModels();
  };
});
function trainGo(pane) {
  const tab = document.querySelector(`#train-tabs [data-pane="${pane}"]`);
  if (tab) tab.click();
}
document.querySelectorAll("#casting-legacy [data-legacy]").forEach((b) => {
  b.onclick = () => { trainGo(b.dataset.legacy); document.querySelector(".content").scrollTo({ top: 0, behavior: "smooth" }); };
});

/* ======================= DATASETS PANE ======================= */
let CUR_DATASETS = [];
let CUR_FILTER_TYPE = "";
// what each kind of batch is, in words
const DS_KIND = { face: "character", environment: "place", power: "power", weapon: "weapon",
                  anatomy: "anatomy LoRA", quality: "quality LoRA" };

$("ds-new-dataset").onclick = () => {
  $("wiz-name").value = "";
  $("wiz-existing").textContent = "";
  wizStep(1);
  trainGo("generate");
};
$("ds-import-urls").onclick = () => $("ds-import-modal").classList.remove("hidden");
$("ds-imp-close").onclick = () => $("ds-import-modal").classList.add("hidden");
$("ds-imp-go").onclick = async () => {
  const urls = $("ds-imp-urls").value.split(",").map((u) => u.trim()).filter(Boolean);
  const r = await post("/api/training/import", {
    type: $("ds-imp-type").value, name: $("ds-imp-name").value.trim(),
    urls, license: "",
  });
  toast(r.ok ? "Importing the pictures" : "The import did not start: " + (r.error || JSON.stringify(r)), !r.ok);
  $("ds-import-modal").classList.add("hidden");
  refreshDatasets();
};
$("ds-filter-type").onchange = () => { CUR_FILTER_TYPE = $("ds-filter-type").value; refreshDatasets(); };

async function refreshDatasets() {
  const r = await api("/api/training/datasets");
  CUR_DATASETS = (r.datasets || []).filter((d) =>
    !CUR_FILTER_TYPE || d.type === CUR_FILTER_TYPE);
  const jr = await api("/api/training/jobs?limit=100").catch(() => ({ jobs: [] }));
  const runByKey = {};
  (jr.jobs || []).forEach((j) => {
    runByKey[j.dataset_type + "/" + j.dataset_name] = j;
  });
  $("ds-total").textContent = CUR_DATASETS.length + " batch" +
    (CUR_DATASETS.length === 1 ? "" : "es");
  fillCurPick();
  const el = $("ds-grid");
  if (!CUR_DATASETS.length) {
    el.innerHTML = `<div class="ds-empty"><span class="big">${Icon.get("box")}</span><b>No batches yet</b>Make looks under Generate looks and they show up here.</div>`;
    return;
  }
  el.innerHTML = CUR_DATASETS.map((d) => {
    const total = d.candidates.length;
    const models = Object.entries(d.models || {})
      .map(([m, n]) => `<span class="ds-badge">${escapeHtml(m)} ×${escapeHtml(n)}</span>`).join(" ");
    const run = runByKey[d.type + "/" + d.name];
    const st = run ? run.status : "";
    const resumable = run && ["partial", "failed", "canceled"].includes(st) &&
      (run.done || 0) < (run.total || 0);
    // "active 3/40" from the server, or the last run that stopped early
    const gs = /^(\w+) (\d+)\/(\d+)$/.exec(d.generation_status || "");
    const badgeTxt = gs ? (gs[1] === "active" ? "Making " : "Waiting, ") + gs[2] + " of " + gs[3]
      : st === "canceled" ? `Stopped at ${run.done || 0} of ${run.total || 0}`
      : st === "partial" ? `Partly made, ${run.done || 0} of ${run.total || 0}` : "";
    return `<div class="ds-card batch-card" data-name="${escapeHtml(d.name)}" data-type="${d.type}">
      <div class="ds-menu">
        <button title="Import URLs" data-act="import">${Icon.get("upload")}</button>
        <button title="Export zip" data-act="export">${Icon.get("download")}</button>
        <button title="Rename" data-act="rename">${Icon.get("pen")}</button>
        <button title="Duplicate" data-act="dup">${Icon.get("copy")}</button>
        <button title="Open folder (copy path)" data-act="path">${Icon.get("folder")}</button>
        <button title="Clear rejected" data-act="clr">${Icon.get("trash")}</button>
        <button title="Delete dataset" data-act="del" class="danger">${Icon.get("x")}</button>
      </div>
      <div class="poster" style="aspect-ratio:auto;height:120px" data-open>
        ${d.thumbnail
          ? `<img loading="lazy" src="/api/dataset_img?type=${d.type}&name=${encodeURIComponent(d.name)}&file=${encodeURIComponent(d.thumbnail)}" style="width:100%;height:100%;object-fit:cover">`
          : `<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--dim)">No pictures yet</div>`}
        ${badgeTxt
          ? `<span class="ds-badge" style="position:absolute;top:8px;left:8px;background:rgba(0,0,0,.7);color:var(--amber)">${escapeHtml(badgeTxt)}</span>`
          : ""}
        ${d.ready && ["anatomy", "quality"].includes(d.type)
          ? `<span class="ds-badge" style="position:absolute;bottom:8px;left:8px;background:rgba(0,0,0,.7);color:var(--green)" title="At least 5 kept: enough to train a LoRA">enough to train</span>`
          : ""}
      </div>
      <div class="body">
        <div class="name">${escapeHtml(d.name)} <span class="ds-badge ${d.type}">${escapeHtml(DS_KIND[d.type] || d.type)}</span></div>
        <div class="meta">
          <span class="ds-meta-txt">${d.kept} kept, ${d.rejected || 0} rejected, of ${total}</span>
          <span class="ds-meta-txt">${escapeHtml(d.last_modified || "")}</span>
        </div>
        ${models ? `<div class="meta">${models}</div>` : ""}
        <div class="ds-actions">
          <button class="ds-btn primary tiny" data-open>Pick the best</button>
          ${resumable ? `<button class="ds-btn warn tiny" data-continue data-run="${run.id}">Finish the batch</button>` : ""}
          ${["face", "environment", "power", "weapon"].includes(d.type)
            ? `<button class="ds-btn tiny" data-cast data-tip="Add it to a movie's Cast &amp; World: a character's description, looks, outfits and weapons, a place's pictures, a power's levels, a weapon's design">Send to Cast &amp; World</button>`
            : ""}
          ${["anatomy", "quality"].includes(d.type) ? `<button class="ds-btn tiny" data-train data-tip="Train a Wan 2.2 LoRA on it (legacy)">Train</button>` : ""}
          <button class="ds-btn tiny" data-more>Make more</button>
        </div>
      </div>
    </div>`;
  }).join("");
  el.querySelectorAll(".ds-card").forEach((card) => {
    const t = card.dataset.type, n = card.dataset.name;
    card.querySelector("[data-open]").onclick = () => {
      $("cur-type").value = t;
      $("cur-name").value = n;
      trainGo("curate");
      loadCuration();
    };
    card.querySelector("[data-more]").onclick = () => genMore(t, n);
    const cst = card.querySelector("[data-cast]");
    if (cst) cst.onclick = () => openCastImport(t, n);
    const tb = card.querySelector("[data-train]");
    if (tb) tb.onclick = () => {
      const cd = CUR_DATASETS.find((x) => x.type === t && x.name === n);
      openTrainConfig(t, n, (cd && cd.kept) || 0);
    };
    const cb = card.querySelector("[data-continue]");
    if (cb) cb.onclick = async () => {
      const rr = await post("/api/training/jobs/retry", { run_id: +cb.dataset.run });
      toast(rr.ok ? "Finishing the batch: only the missing pictures" : "It did not start: " + rr.error, !rr.ok);
      refreshDatasets();
    };
    card.querySelectorAll(".ds-menu button").forEach((b) => {
      b.onclick = async () => {
        const act = b.dataset.act;
        if (act === "import") {
          $("ds-imp-type").value = t;
          $("ds-imp-name").value = n;
          $("ds-import-modal").classList.remove("hidden");
        } else if (act === "export") {
          const r = await post("/api/training/dataset/export", { type: t, name: n });
          toast(r.ok ? "exported: " + r.path : "error: " + r.error, !r.ok);
        } else if (act === "rename") {
          const nn = await Dlg.prompt({ title: "Rename dataset", label: "New name for " + escapeHtml(n) + ":", value: n });
          if (nn && nn.trim()) {
            const r = await post("/api/training/dataset/rename", { type: t, name: n, new_name: nn.trim() });
            toast(r.ok ? "renamed" : "error: " + r.error, !r.ok);
            refreshDatasets();
          }
        } else if (act === "dup") {
          const nn = await Dlg.prompt({ title: "Duplicate dataset", label: "Duplicate " + escapeHtml(n) + " as:", value: n + " copy" });
          if (nn && nn.trim()) {
            const r = await post("/api/training/dataset/duplicate", { type: t, name: n, new_name: nn.trim() });
            toast(r.ok ? "duplicated" : "error: " + r.error, !r.ok);
            refreshDatasets();
          }
        } else if (act === "path") {
          const p = "roster/datasets/" + t + "\\" + n;
          await navigator.clipboard.writeText(p);
          toast("path copied: " + p);
        } else if (act === "clr") {
          const okClr = await Dlg.confirm({ title: "Clear rejected", message: "Delete ALL rejected images in <b>" + escapeHtml(n) + "</b>?", okLabel: "Clear", danger: true });
          if (!okClr) return;
          const r = await post("/api/training/dataset/clear_rejected", { type: t, name: n });
          toast(r.ok ? r.removed + " removed" : "error: " + r.error, !r.ok);
          refreshDatasets();
        } else if (act === "del") {
          const okDel = await Dlg.confirm({ title: "Delete dataset", message: "Delete dataset <b>" + escapeHtml(t) + "/" + escapeHtml(n) + "</b> entirely? This cannot be undone.", okLabel: "Delete", danger: true });
          if (!okDel) return;
          const r = await post("/api/training/dataset/clear", { type: t, name: n });
          toast(r.ok ? "deleted" : "error: " + r.error, !r.ok);
          refreshDatasets();
        }
      };
    });
  });
}

/* ================ SEND TO CAST (one-click batch import) ================ */
function syncWizSendCast() {
  const btn = $("wiz-sendcast");
  if (!btn) return;
  const ok = ["character", "environment", "power", "weapon"].includes(WIZ_MODE);
  const name = ($("wiz-name").value || "").trim();
  btn.style.display = ok && name ? "" : "none";
}

async function openCastImport(type, name) {
  const modal = $("ds-cast-modal");
  if (!modal) return;
  $("dsc-type").value = type;
  $("dsc-name").value = name;
  $("dsc-title").textContent = "Send " + type + " / " + name + " to the cast";
  const needsChar = type === "power" || type === "weapon";
  $("dsc-char-wrap").classList.toggle("hidden", !(type === "face" || needsChar));
  $("dsc-loc-wrap").classList.toggle("hidden", type !== "environment");
  $("dsc-power-wrap").classList.toggle("hidden", type !== "power");
  $("dsc-weapon-wrap").classList.toggle("hidden", type !== "weapon");
  $("dsc-tier-wrap").classList.toggle("hidden", type !== "face");
  $("dsc-char").value = type === "face" ? name : "";
  $("dsc-loc").value = type === "environment" ? name : "";
  $("dsc-power").value = type === "power" ? name : "";
  $("dsc-weapon").value = type === "weapon" ? name : "";
  $("dsc-display").value = "";
  $("dsc-overwrite").checked = false;
  $("dsc-summary").innerHTML = "";
  const note = $("dsc-type-note");
  if (note) {
    note.textContent = {
      face: "Plates become framing-tagged hero refs (close/medium/full); matrix looks become loadouts with their composite stills tagged by loadout name.",
      environment: "Each still's PLACE becomes a tagged reference — the story LLM then uses those names as `place`. People in a still are skipped by the face gate.",
      power: "Attach to an existing character: every still is tagged with its LEVEL, and the renderer picks the highest level at or below the shot's power level.",
      weapon: "Attach to an existing character: clean design stills become the weapon's extra references (H3 spare pictures).",
    }[type] || "";
    note.style.display = note.textContent ? "" : "none";
  }
  try {
    const pr = await api("/api/projects");
    const sel = $("dsc-proj");
    sel.innerHTML = ((pr && pr.projects) || []).map((p) =>
      '<option value="' + esc(p.id || "") + '">' + esc(p.title || p.id || "?") + "</option>").join("");
    if (!sel.innerHTML) {
      $("dsc-summary").innerHTML = '<span style="color:#e0a35c">no projects yet — create one in the studio first</span>';
    }
    await loadCastTargets();
  } catch (e) { /* keep whatever is there */ }
  modal.classList.remove("hidden");
}

/* Fill the character/location pickers with what the chosen project already
   has — a power import must be told WHICH character owns it. */
async function loadCastTargets() {
  const pid = $("dsc-proj") ? $("dsc-proj").value : "";
  const chars = $("dsc-chars-list"), locs = $("dsc-locs-list");
  if (chars) chars.innerHTML = "";
  if (locs) locs.innerHTML = "";
  if (!pid) return;
  try {
    const r = await api("/api/projects/" + encodeURIComponent(pid));
    const b = (r && r.bible) || {};
    const ch = Object.keys(b.characters || {}).sort();
    const lo = Object.keys(b.locations || {}).sort();
    if (chars) chars.innerHTML = ch.map((c) =>
      '<option value="' + esc(c) + '"></option>').join("");
    if (locs) locs.innerHTML = lo.map((l) =>
      '<option value="' + esc(l) + '"></option>').join("");
    const hint = $("dsc-char-hint");
    if (hint) hint.textContent = ch.length
      ? "existing: " + ch.join(", ") : "this project has no characters yet";
  } catch (e) { /* leave empty */ }
}

async function runCastImport(dry) {
  const pid = $("dsc-proj").value;
  if (!pid) return toast("pick a project first", true);
  const body = {
    type: $("dsc-type").value,
    name: $("dsc-name").value,
    character_id: $("dsc-char").value.trim(),
    location_id: $("dsc-loc").value.trim(),
    power_id: $("dsc-power").value.trim(),
    weapon_id: $("dsc-weapon").value.trim(),
    display_name: $("dsc-display").value.trim(),
    tier: $("dsc-tier").value,
    overwrite: $("dsc-overwrite").checked,
    dry_run: !!dry,
  };
  const out = $("dsc-summary");
  out.textContent = dry ? "checking…" : "importing…";
  const r = await post("/api/projects/" + encodeURIComponent(pid) +
    "/cast/import-batch", body, 120000);
  if (!r || !r.ok) {
    out.innerHTML = '<span style="color:#e05c5c">' + esc((r && r.error) || "import failed") + "</span>";
    return;
  }
  const bits = [];
  if (r.id) bits.push("<b>" + esc(r.id) + "</b> " + (r.created ? "(created)" : "(updated)"));
  if (r.plates != null) bits.push(r.plates + " identity plates (close/medium/full)");
  if (r.loadouts && r.loadouts.length) bits.push(r.loadouts.length + " loadouts: " + r.loadouts.map(esc).join(", "));
  if (r.weapons && r.weapons.length) bits.push(r.weapons.length + " weapon configs");
  if (r.places && r.places.length) bits.push(r.places.length + " places: " + r.places.map(esc).join(", "));
  if (r.levels && r.levels.length) bits.push("power levels " + r.levels.join("/"));
  if (r.refs_total != null) bits.push(r.refs_total + " refs total");
  if (r.blocked_people && r.blocked_people.length) {
    bits.push('<span style="color:#e0a35c">' + r.blocked_people.length +
      " stills with people skipped</span>");
  }
  out.innerHTML = (dry ? "<i>preview — nothing written:</i> " : "") +
    (bits.join(" · ") || "nothing to import");
  if (!dry) {
    toast("imported into " + esc(pid));
    refreshDatasets();
  }
}

(function wireCastImport() {
  const go = $("dsc-go");
  if (go) go.onclick = () => runCastImport(false);
  const dry = $("dsc-dry");
  if (dry) dry.onclick = () => runCastImport(true);
  const cl = $("dsc-close");
  if (cl) cl.onclick = () => $("ds-cast-modal").classList.add("hidden");
  // wizard sticky-bar shortcut: appears once the dataset has a name and the
  // mode has a cast target (character / environment / power / weapon)
  const wsc = $("wiz-sendcast");
  if (wsc) wsc.onclick = () => {
    const name = ($("wiz-name").value || "").trim();
    if (!name) return toast("give the dataset a name first", true);
    openCastImport(wizDtype(), name);
  };
  const wn = $("wiz-name");
  if (wn) wn.addEventListener("input", syncWizSendCast);
  const pj = $("dsc-proj");
  if (pj) pj.onchange = () => loadCastTargets();
})();

/* ======================= GENERATE (wizard) ======================= */
function wizStep(n) {
  WIZ_STEP = n;
  ["1", "2", "3", "4", "5"].forEach((s) => {
    const el = $("wiz-step" + s);
    if (el) el.classList.toggle("hidden", s !== String(n));
  });
  document.querySelectorAll("#wiz-steps-tabs .ds-tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.step === String(n)));
  if (n === 2) renderWizSubject();
  if (n === 3) renderWizVariation();
  if (n === 4) renderWizModels();
  if (n === 5) renderWizPrompt();
}
$("wiz-next1").onclick = () => wizStep(2);
$("wiz-next2").onclick = () => wizStep(3);
$("wiz-next3").onclick = () => wizStep(4);
$("wiz-next4").onclick = () => wizStep(5);
$("wiz-back1").onclick = () => wizStep(1);
$("wiz-back2").onclick = () => wizStep(2);
$("wiz-back3").onclick = () => wizStep(3);
$("wiz-back4").onclick = () => wizStep(4);
document.querySelectorAll("#wiz-steps-tabs .ds-tab").forEach((t) => {
  t.onclick = () => wizStep(parseInt(t.dataset.step));
});

/* type segmented */
document.querySelectorAll("#wiz-type-seg button").forEach((b) => {
  b.onclick = () => {
    // mode switch defaults (only while the model rows are untouched):
    // power -> QWEN21 (best power references), environment -> Z-IMAGE TURBO
    // (measured best for places), otherwise the SDXL workhorse.
    const prevMode = WIZ_MODE;
    WIZ_MODE = b.dataset.mode;
    if (!WIZ_MODELS_TOUCHED) {
      if (WIZ_MODE === "power") {
        WIZ_MODELS = [{ model: "qwen21", count: Math.max(2, WIZ_POWER_UI.level_count) }];
        renderWizModels();
      } else if (WIZ_MODE === "environment") {
        WIZ_MODELS = [{ model: "zimage", count: 12 }];
        renderWizModels();
      } else if (prevMode === "power" || prevMode === "environment") {
        WIZ_MODELS = [{ model: "realvis", count: 20 }];
        renderWizModels();
      }
    }
    document.querySelectorAll("#wiz-type-seg button").forEach((x) =>
      x.classList.toggle("on", x === b));
    renderWizSubject();
    renderWizVariation();
    const note = {
      character: "Identity, design, wardrobe and performance define WHO this is — fixed across the batch. Variation (step 3) drives outfits, weapons, poses, scenes, lighting.",
      power: "Power type and subject define the effect. The LEVEL LADDER generates one still per level — each level changes the manifestation, framing and subject action (aura → orb → beam → wave → eruption → dome → sky event → cataclysm). The renderer picks the highest level at or below the shot's power level.",
      environment: "World + the PLACES you select define WHERE this is. Every place carries its own camera, framing and description (aerial third-eye view, one-point corridor, low-angle staircase, pit floor…), so each selected place renders a genuinely different image. Times/atmosphere/weather multiply the variety.",
      weapon: "Weapon design stills: clean, centered, isolated reference images (product-style) — the exact design the loadout composites and the H3 extra weapon slots use.",
      photo: "Just a prompt + count — no stacking.",
    }[WIZ_MODE];
    $("wiz-mode-note").textContent = note;
    $("wiz-existing").textContent = "";
  };
});
$("wiz-name").addEventListener("input", wizExistingCount);

async function wizExistingCount() {
  const n = $("wiz-name").value.trim();
  if (!n) { $("wiz-existing").textContent = ""; return; }
  const r = await api("/api/training/datasets");
  const ds = (r.datasets || []).find((d) => d.name === n);
  $("wiz-existing").textContent = ds
    ? "existing dataset — " + ds.candidates.length + " images (new ones append)" : "";
}

/* ================= PromptSpec wizard (structured cinematic) ================= */
let WIZ_MODE = "character";
let WIZ_MODELS = [{ model: "realvis", count: 20 }];
// Weapon designer state (character mode): each row is a weapon config the
// batch uses for the composite loadout stills; the axis decides how many
// images carry it.
let WIZ_WEAPONS = [];
let WIZ_WEAPON_UI = { coverage: 100 };
// Power level-ladder state (power mode).
let WIZ_POWER_UI = { ladder: false, level_count: 10 };
// Selected places for the environment batch (multi-select per world).
let WIZ_ENV_PLACES = [];
/* wardrobe matrix (Tab 2): one row per look — outfit + its own model + how
   many weapon variants to generate ("" = unarmed). Empty = the classic
   outfits/wardrobe cycle axes drive the batch instead. */
let WIZ_MATRIX = [];
/* identity plates (Tab 1): the three framing stills (close/medium/full) */
let WIZ_PLATES = { model: "" };
// True once the user edits the model mix — mode-switch defaults must not
// overwrite a hand-tuned setup.
let WIZ_MODELS_TOUCHED = false;
let WIZ_MODEL_INFO = {};
let WIZ_STEP = 1;
let WIZ_RUN_ID = null;
let WIZ_STATE = {};
let WIZ_REFS = [];
let PS = {};                       // catalogs from /api/promptspec
let WIZ_INTENT = "cinematic";
let WIZ_PMODE = "structured";      // structured | add | override
let WIZ_SINGLE = {};               // per-mode single-select values
let WIZ_AXES = {};                 // per-mode stacking axes {k: {kind, sel[], locked}}

/* client-side preset fallback — the backend catalogs arrive on the next
   restart; until then presets still apply (same payloads as promptspec) */
const PS_FALLBACK = {
  ARCHETYPES: {
    "royal princess": { lineage: "human", faction: "royal house", palette: "ivory, rose gold and pale blue",
      materials: ["silk", "velvet", "brocade"], headwear: "tiara", jewelry: "pearl jewelry", armor: "none", condition: "pristine" },
    "warrior princess": { lineage: "human", faction: "royal house", palette: "crimson, black and gold",
      materials: ["leather", "gilded plate armor"], headwear: "circlet", jewelry: "gold jewelry", armor: "gilded plate armor", condition: "battle-worn" },
    "dark princess": { lineage: "dark elf", faction: "dark court", palette: "charcoal and amber",
      materials: ["velvet", "leather", "dark silk"], headwear: "circlet", jewelry: "silver jewelry", armor: "none", condition: "pristine" },
    "queen": { lineage: "human", faction: "royal house", palette: "royal purple and gold",
      materials: ["velvet", "brocade", "gold"], headwear: "crown", jewelry: "ruby and gold", armor: "none", condition: "ceremonial" },
    "elven royal": { lineage: "elf", faction: "royal house", palette: "emerald and bronze",
      materials: ["silk", "enchanted silk", "bronze"], headwear: "circlet", jewelry: "emerald and silver", armor: "none", condition: "pristine" },
    "knight": { lineage: "human", faction: "order of knights", palette: "steel grey and silver",
      materials: ["plate armor", "chainmail"], headwear: "helm", jewelry: "none", armor: "plate armor", condition: "battle-worn" },
    "mage": { lineage: "human", faction: "arcane circle", palette: "midnight blue and silver",
      materials: ["wool", "linen", "enchanted silk"], headwear: "hood", jewelry: "rune-carved jewelry", armor: "none", condition: "travel-worn" },
    "assassin": { lineage: "human", faction: "shadow guild", palette: "charcoal and deep grey",
      materials: ["leather", "dark linen"], headwear: "hood", jewelry: "none", armor: "studded leather", condition: "travel-worn" },
    "dark lord": { lineage: "dark elf", faction: "dark court", palette: "charcoal and amber",
      materials: ["velvet", "plate armor", "dark metal"], headwear: "crown", jewelry: "ruby and gold", armor: "rune-etched armor", condition: "weathered" },
    "celestial": { lineage: "celestial", faction: "holy order", palette: "pearl, lavender and gold",
      materials: ["silk", "enchanted silk", "gold"], headwear: "ornate headpiece", jewelry: "gold jewelry", armor: "gilded plate armor", condition: "pristine" },
    "vampire noble": { lineage: "vampire", faction: "royal house", palette: "midnight blue and silver",
      materials: ["velvet", "silk", "silver"], headwear: "none", jewelry: "silver jewelry", armor: "none", condition: "pristine" },
    "battle mage": { lineage: "human", faction: "arcane circle", palette: "emerald and bronze",
      materials: ["leather", "wool", "bronze"], headwear: "none", jewelry: "rune-carved jewelry", armor: "studded leather", condition: "battle-worn" },
    "dark knight": { lineage: "human", faction: "dark court", palette: "charcoal and crimson",
      materials: ["plate armor", "chainmail"], headwear: "helm", jewelry: "none", armor: "plate armor", condition: "battle-worn" },
    "elven queen": { lineage: "elf", faction: "royal house", palette: "snow white and ice blue",
      materials: ["enchanted silk", "silver", "crystal"], headwear: "crown", jewelry: "sapphire and gold", armor: "none", condition: "ceremonial" },
    "celestial warrior": { lineage: "celestial", faction: "holy order", palette: "pearl and gold",
      materials: ["gilded plate armor", "enchanted silk"], headwear: "ornate headpiece", jewelry: "gold jewelry", armor: "gilded plate armor", condition: "pristine" },
  },
  POWER_TYPES: {
    lightning: { color: "electric cyan", material: "electric arc", glow: "crackling cyan glow" },
    fire: { color: "molten orange", material: "living flame", glow: "orange firelight glow" },
    ice: { color: "pale blue", material: "crystalline frost", glow: "cold blue glow" },
    water: { color: "deep azure", material: "flowing water", glow: "aqua shimmer" },
    wind: { color: "white-grey", material: "swirling air", glow: "pale wind streaks" },
    earth: { color: "umber", material: "stone and soil", glow: "deep amber glow" },
    force: { color: "violet", material: "raw kinetic energy", glow: "violet distortion" },
    arcane: { color: "violet-blue", material: "glowing arcane glyphs", glow: "arcane blue-violet glow" },
    divine: { color: "radiant gold", material: "holy light", glow: "blinding golden light" },
    shadow: { color: "deep purple-black", material: "living shadow", glow: "smoky purple shadow" },
    void: { color: "black with violet rim", material: "collapsing void", glow: "violet-edged darkness" },
    plasma: { color: "white-hot magenta", material: "superheated plasma", glow: "intense plasma glow" },
    nature: { color: "emerald green", material: "living vine and bloom", glow: "soft green life-glow" },
  },
  ENV_WORLDS_KEYS: ["royal palace", "dark castle", "magical city", "medieval city",
    "ancient temple", "enchanted forest", "frozen kingdom", "mountain fortress",
    "floating realm", "celestial realm", "void realm", "ruins", "battlefield", "village"],
  PRESETS: {
    characters: { "Royal Princess": { archetype: "royal princess" }, "Dark Princess": { archetype: "dark princess" },      "Warrior Princess": { archetype: "warrior princess" }, "Elven Queen": { archetype: "elven queen" },
      "Battle Mage": { archetype: "battle mage" }, "Dark Knight": { archetype: "dark knight" },
      "Vampire Noble": { archetype: "vampire noble" }, "Celestial Warrior": { archetype: "celestial warrior" },
      "Queen": { archetype: "queen" }, "Elven Royal": { archetype: "elven royal" },
      "Knight": { archetype: "knight" }, "Mage": { archetype: "mage" },
      "Assassin": { archetype: "assassin" }, "Dark Lord": { archetype: "dark lord" },
      "Celestial": { archetype: "celestial" } },
    powers: { "Lightning": { power_type: "lightning", color: "electric cyan", behavior: "crackling", manifestation: "hands" },
      "Fire": { power_type: "fire", color: "molten orange", behavior: "blazing", manifestation: "hands" },
      "Divine": { power_type: "divine", color: "radiant gold", behavior: "radiating", manifestation: "aura" },
      "Shadow": { power_type: "shadow", color: "deep purple-black", behavior: "flowing", manifestation: "aura" },
      "Arcane": { power_type: "arcane", color: "violet-blue", behavior: "swirling", manifestation: "orb" },
      "Telekinesis": { power_type: "force", color: "violet", behavior: "pulsing", manifestation: "hands" },
      "Ice": { power_type: "ice", color: "pale blue", behavior: "streaming", manifestation: "hands" },
      "Nature": { power_type: "nature", color: "emerald green", behavior: "flowing", manifestation: "aura" },
      "Void": { power_type: "void", color: "black with violet rim", behavior: "swirling", manifestation: "shield" } },
    environments: { "Royal Palace": { world: "royal palace" }, "Magical City": { world: "magical city" },
      "Dark Castle": { world: "dark castle" }, "Enchanted Forest": { world: "enchanted forest" },
      "Ancient Temple": { world: "ancient temple" }, "Frozen Kingdom": { world: "frozen kingdom" },
      "Floating Realm": { world: "floating realm" }, "Celestial Realm": { world: "celestial realm" },
      "Void Realm": { world: "void realm" }, "Mountain Fortress": { world: "mountain fortress" },
      "Ruins": { world: "ruins" }, "Battlefield": { world: "battlefield" } },
  },
};

function wizEnsureState() {
  if (!WIZ_SINGLE || !WIZ_SINGLE[WIZ_MODE] ||
      !WIZ_AXES || !WIZ_AXES[WIZ_MODE]) {
    wizDefaults();
  }
  if (!WIZ_SINGLE[WIZ_MODE]) WIZ_SINGLE[WIZ_MODE] = {};
  if (!WIZ_AXES[WIZ_MODE]) WIZ_AXES[WIZ_MODE] = {};
}

/* ---- reference images (§7 typed refs: Identity/Wardrobe/Style/Pose) ---- */
function wizDtype() {
  return { character: "face", power: "power",
           environment: "environment", weapon: "weapon",
           photo: "face" }[WIZ_MODE] || "face";
}
function wizRefsUI() {
  const el = $("wiz-refs");
  if (!el) return;
  const name = $("wiz-name").value.trim();
  const types = ["Identity", "Wardrobe", "Style", "Pose"];
  el.innerHTML = WIZ_REFS.map((r, i) => {
    const file = typeof r === "string" ? r : (r.file || "");
    const type = typeof r === "string" ? "Identity" : (r.type || "Identity");
    return `<span class="ds-chip on" data-ref="${escapeHtml(file)}">
       ${name ? `<img src="/api/dataset_img?type=${wizDtype()}&name=${encodeURIComponent(name)}&file=${encodeURIComponent(file)}" style="width:26px;height:26px;object-fit:cover;border-radius:6px">` : ""}
       <select class="wiz-ref-type" data-i="${i}" style="border:none;background:none;font-size:10px;max-width:86px">
         ${types.map((t) => `<option ${t === type ? "selected" : ""}>${t}</option>`).join("")}
       </select>
       <button class="wiz-ref-del" data-i="${i}" style="background:none;border:0;color:var(--red);cursor:pointer;display:inline-flex;padding:0">${Icon.get("x")}</button>
     </span>`;
  }).join("") ||
    '<span class="ds-meta-txt">no references — characters will be whoever the model decides</span>';
  el.querySelectorAll(".wiz-ref-type").forEach((s) => {
    s.onchange = () => {
      const i = parseInt(s.dataset.i);
      const file = typeof WIZ_REFS[i] === "string" ? WIZ_REFS[i] : (WIZ_REFS[i].file || "");
      WIZ_REFS[i] = { file: file, type: s.value };
    };
  });
  el.querySelectorAll(".wiz-ref-del").forEach((b) => {
    b.onclick = (e) => {
      e.stopPropagation();
      WIZ_REFS.splice(parseInt(b.dataset.i), 1);
      wizRefsUI();
    };
  });
}
$("wiz-ref-add").onclick = () => {
  if (!WIZ_MODE || WIZ_MODE === "photo")
    return toast("references are for character/power/environment batches", true);
  if (!$("wiz-name").value.trim())
    return toast("set the dataset name first (step 1)", true);
  $("wiz-ref-file").click();
};
$("wiz-ref-file").onchange = async (e) => {
  const files = [...e.target.files].slice(0, 5 - WIZ_REFS.length);
  if (!files.length) return toast("max 5 references", true);
  const fd = new FormData();
  fd.append("type", wizDtype());
  fd.append("name", $("wiz-name").value.trim());
  files.forEach((f) => fd.append("files", f));
  try {
    const r = await window.Auth.fetch("/api/training/refs/upload", { method: "POST", body: fd });
    const j = await r.json();
    if (!j.ok) return toast("upload failed: " + j.error, true);
    WIZ_REFS = WIZ_REFS.concat(j.refs.map((f) => ({ file: f, type: "Identity" }))).slice(0, 5);
    wizRefsUI();
    toast(files.length + " reference(s) uploaded — tag each: Identity/Wardrobe/Style/Pose");
  } catch (err) {
    toast("upload failed: " + err.message, true);
  }
  e.target.value = "";
};
$("wiz-ref-str").oninput = () => { $("wiz-ref-str-val").textContent = parseFloat($("wiz-ref-str").value).toFixed(2); };

function wizDefaults() {
  WIZ_SINGLE = {
    character: { presentation: "woman", age: "mid-30s", hair_style: "long",
      hair_color: "dark brown", eye_color: "hazel", eye_shape: "almond",
      skin: "fair with light freckles", face: "oval", body: "lean",
      marks: "none", archetype: "", lineage: "human", faction: "", palette: "",
      condition: "", silhouette: "flowing gown", wardrobe_color: "ivory",
      detail: "none", jewelry: "none", headwear: "none", wardrobe_condition: "pristine",
      expression_intensity: "normal", gaze: "camera", pose: "standing",
      shot: "portrait", lens: "", anatomy_depth: "normal",
      scene: "neutral studio (no environment)",
      scene_location: "", lighting: "soft window light" },
    power: { power_type: "arcane", power: "", subject: "generic figure",      manifestation: "hands", behavior: "crackling", env_reaction: "light reflection",
      color: "", material: "", glow: "", intensity: "steady", shot: "medium",
      scene: "neutral studio (no environment)", scene_location: "", lighting: "dramatic chiaroscuro" },
    environment: { world: "royal palace", scope: "both", condition: "pristine", location: "",
      materials: [], features: [], time: "night", atmosphere: "epic", weather: "",
      foreground: "", population: "none", shot: "wide", lighting: "overcast daylight" },
    weapon: { weapon_type: "spear", material: "forged steel", style: "ornate",
      size: "two-handed", color: "", energy: "none", glow: "",
      ornament: "glowing runes along the fuller", condition: "pristine",
      lighting: "studio softbox" },
    photo: { prompt: "" },
  };
  WIZ_AXES = {
    character: {
      emotions: { kind: "multiply", sel: ["stoic", "calm", "cold"], locked: false },
      shots: { kind: "multiply", sel: ["portrait", "full body"], locked: false },
      outfits: { kind: "cycle", sel: [], locked: false },
      poses: { kind: "cycle", sel: ["standing"], locked: false },
      scenes: { kind: "cycle", sel: ["neutral studio (no environment)"], locked: false },
      lightings: { kind: "cycle", sel: ["soft window light"], locked: false },
      gazes: { kind: "cycle", sel: ["camera"], locked: false },
      wardrobes: { kind: "cycle", sel: [], locked: false },
    },
    power: {
      // The LEVEL LADDER drives everything (manifestation, framing, subject
      // action, destruction). No manual axes — a "cataclysmic" checkbox next
      // to an unchanged "hands, medium shot" composition was exactly what
      // made every image look identical.
    },
    environment: {
      times: { kind: "multiply", sel: ["night"], locked: false },
      atmospheres: { kind: "cycle", sel: ["epic"], locked: false },
      weathers: { kind: "cycle", sel: [], locked: false },
      foregrounds: { kind: "cycle", sel: [], locked: false },
    },
    weapon: {
      handling: { kind: "cycle", sel: ["no hands"], locked: false },
    },
    photo: {},
  };
}

function wizSingleSections() {
  const s = { character: [], power: [], environment: [] };
  s.character = [
    { title: "Identity (fixed)", fields: [
      ["presentation", "Presentation", PS.PRESENTATIONS || ["woman", "man"]],
      ["age", "Age", PS.AGES || ["mid-30s"]],
      ["hair_style", "Hair style", PS.HAIR_STYLES || []],
      ["hair_color", "Hair color", PS.HAIR_COLORS || []],
      ["eye_color", "Eye color", PS.EYE_COLORS || []],
      ["eye_shape", "Eye shape", PS.EYE_SHAPES || []],
      ["skin", "Skin tone", PS.SKIN_TONES || []],
      ["face", "Face structure", PS.FACE_STRUCTURES || []],
      ["body", "Body type", PS.BODY_TYPES || []],
      ["marks", "Marks", PS.MARKS || []],
    ] },
    { title: "Design", fields: [
      ["lineage", "Lineage", PS.LINEAGES || []],
      ["faction", "Faction", PS.FACTIONS || []],
      ["palette", "Palette", PS.PALETTES || []],
      ["condition", "Condition", PS.WARDROBE_CONDITIONS || []],
    ] },
    { title: "Wardrobe (composable)", fields: [
      ["silhouette", "Silhouette", PS.WARDROBE_SILHOUETTES || []],
      ["wardrobe_color", "Color", PS.WARDROBE_COLORS || []],
      ["detail", "Detail", PS.WARDROBE_DETAILS || []],
      ["jewelry", "Jewelry", PS.JEWELRY || []],
      ["headwear", "Headwear", PS.HEADWEAR || []],
      ["wardrobe_condition", "Condition", PS.WARDROBE_CONDITIONS || []],
    ], multi: [["materials", "Materials", PS.WARDROBE_MATERIALS || []]] },
    { title: "Performance", fields: [
      ["expression_intensity", "Expression intensity", ["subtle", "normal", "strong"]],
      ["gaze", "Gaze (base)", PS.GAZES || []],
      ["pose", "Pose (base)", PS.POSES || []],
    ] },
    { title: "Shot", fields: [
      ["shot", "Shot preset (base)", Object.keys(PS.SHOTS || { "portrait": 1 })],
      ["lens", "Lens (optional)", PS.LENSES || []],
      ["anatomy_depth", "Anatomy depth", ["subtle", "normal", "detailed"]],
    ] },
    { title: "Scene", fields: [
      ["scene", "Scene (base)", PS.CHARACTER_SCENES || []],
      ["scene_location", "Location (optional)", PS.SCENE_LOCATIONS || []],
    ] },
    { title: "Lighting", fields: [
      ["lighting", "Lighting (base)", PS.LIGHTING_SETUPS || []],
    ] },
  ];
  s.power = [
    { title: "Power", fields: [
      ["power_type", "Power type", Object.keys((PS.POWER_TYPES || PS_FALLBACK.POWER_TYPES) || { "arcane": 1 })],
      ["power", "Power name (dataset name)", []],
      ["subject", "Subject", PS.POWER_SUBJECTS || []],
    ] },
    { title: "Design", fields: [
      ["color", "Color", Object.keys((PS.POWER_TYPES || PS_FALLBACK.POWER_TYPES) || {}).map((t) => ((PS.POWER_TYPES || PS_FALLBACK.POWER_TYPES)[t] || {}).color || t)],
      ["material", "Material", Object.keys((PS.POWER_TYPES || PS_FALLBACK.POWER_TYPES) || {}).map((t) => ((PS.POWER_TYPES || PS_FALLBACK.POWER_TYPES)[t] || {}).material || t)],
      ["glow", "Glow", Object.keys((PS.POWER_TYPES || PS_FALLBACK.POWER_TYPES) || {}).map((t) => ((PS.POWER_TYPES || PS_FALLBACK.POWER_TYPES)[t] || {}).glow || t)],
    ] },
    { title: "Background (kept constant across levels)", fields: [
      ["scene", "Scene", PS.CHARACTER_SCENES || []],
      ["scene_location", "Location (optional)", PS.SCENE_LOCATIONS || []],
    ] },
    { title: "Lighting", fields: [["lighting", "Lighting", PS.LIGHTING_SETUPS || []]] },
  ];
  s.environment = [
    { title: "World", fields: [
      ["world", "World", Object.keys(PS.ENV_WORLDS || { "royal palace": 1 })],
      ["scope", "Place filter", ["both", "exterior", "interior"]],
    ],
      fallback: PS_FALLBACK.ENV_WORLDS_KEYS || [] },
    { title: "Design", fields: [
      ["condition", "Condition", PS.ENV_CONDITIONS || []],
      ["time", "Time", PS.ENV_TIMES || []],
    ], multi: [
      ["materials", "Materials", envMaterialOpts()],
      ["features", "Fantasy features", PS.ENV_FANTASY_FEATURES || []],
    ] },
    { title: "Atmosphere", fields: [
      ["atmosphere", "Atmosphere (base)", PS.ENV_ATMOSPHERES || []],
      ["weather", "Weather (base)", PS.ENV_WEATHERS || []],
      ["foreground", "Foreground (base)", PS.ENV_FOREGROUNDS || []],
      ["population", "Population", PS.ENV_POPULATIONS || []],
    ] },
    { title: "Shot", fields: [["shot", "Shot preset", Object.keys(PS.SHOTS || { "wide": 1 })]] },
    { title: "Lighting", fields: [["lighting", "Lighting", PS.LIGHTING_SETUPS || []]] },
  ];
  s.weapon = [
    { title: "Weapon", fields: [
      ["weapon_type", "Type", Object.keys(PS.WEAPON_TYPES || { "custom": 1 })],
      ["material", "Material", PS.WEAPON_MATERIALS || []],
      ["style", "Style", PS.WEAPON_STYLES || []],
      ["size", "Size", PS.WEAPON_SIZES || []],
      ["condition", "Condition", PS.WEAPON_CONDITIONS || []],
    ] },
    { title: "Design", fields: [
      ["ornament", "Ornament", PS.WEAPON_ORNAMENTS || []],
      ["energy", "Energy channel", PS.WEAPON_ENERGIES || []],
    ] },
    { title: "Lighting", fields: [["lighting", "Lighting", PS.LIGHTING_SETUPS || []]] },
  ];
  return s;
}

function wizAxisConf() {
  const c = { character: [], power: [], environment: [] };
  c.character = [
    { k: "emotions", label: "Expressions", opts: PS.EXPRESSIONS || [] },
    { k: "shots", label: "Shots", opts: Object.keys(PS.SHOTS || {}) },
    { k: "outfits", label: "Outfits", opts: PS.OUTFIT_VARIANTS || [] },
    { k: "poses", label: "Poses", opts: PS.POSES || [] },
    { k: "scenes", label: "Scenes", opts: PS.CHARACTER_SCENES || [] },
    { k: "lightings", label: "Lighting", opts: PS.LIGHTING_SETUPS || [] },
    { k: "gazes", label: "Gaze", opts: PS.GAZES || [] },
    { k: "wardrobes", label: "Wardrobe", opts: Object.keys(PS.WARDROBE_PRESETS || {}) },
  ];
  c.power = [];   // level ladder drives everything — no manual axes
  c.environment = [
    { k: "times", label: "Times", opts: PS.ENV_TIMES || [] },
    { k: "atmospheres", label: "Atmosphere", opts: PS.ENV_ATMOSPHERES || [] },
    { k: "weathers", label: "Weather", opts: PS.ENV_WEATHERS || [] },
    { k: "foregrounds", label: "Foreground", opts: PS.ENV_FOREGROUNDS || [] },
  ];
  c.weapon = [
    { k: "handling", label: "Handling", opts: ["no hands", "held at side",
      "two-handed grip", "raised overhead", "resting on shoulder",
      "planted in ground", "aimed", "channeling"] },
  ];
  return c;
}

function renderWizSubject() {
  const el = $("wiz-subject");
  wizEnsureState();
  if (WIZ_MODE === "photo") {
    el.innerHTML = `
      <div class="ds-group-row"><label>Prompt</label>
        <textarea id="wiz-photo-prompt" class="mono" rows="3" style="flex:1"
          placeholder="e.g. cinematic portrait of kael, lean 30s man, black tactical coat, golden hour rooftop"></textarea>
      </div>`;
    renderWizPresetCards();
    return;
  }
  const S = WIZ_SINGLE[WIZ_MODE] || (wizDefaults(), WIZ_SINGLE[WIZ_MODE]);
  const sections = (wizSingleSections())[WIZ_MODE] || [];
  el.innerHTML = sections.map((sec) => `
    <div class="ds-group">
      <h4>${sec.title} <span class="ds-meta-txt" style="text-transform:none;letter-spacing:0">(fixed)</span></h4>
      ${(sec.fields || []).map(([k, label, opts]) => {
        let realOpts = opts;
        if (k === "world" && (!opts || !opts.length) && sec.fallback && sec.fallback.length) {
          realOpts = sec.fallback;
        }
        return `<div class="ds-group-row"><label>${label}</label>
          <div class="ds-seg" data-single="${k}">
            ${realOpts.map((o) => `<button data-v="${escapeHtml(o)}" class="${S[k] === o ? "on" : ""}">${escapeHtml(o)}</button>`).join("")}
          </div>
        </div>`;
      }).join("")}
      ${(sec.multi || []).map(([k, label, opts]) => `
        <div class="ds-group-row"><label>${label} <span class="ds-meta-txt">(multi)</span></label>
          <div class="ds-chip-row" data-multi="${k}">
            ${opts.map((o) => `<span class="ds-chip ${(S[k] || []).includes(o) ? "on" : ""}" data-v="${escapeHtml(o)}">${escapeHtml(o)}</span>`).join("")}
          </div>
        </div>`).join("")}
    </div>`).join("");
  // bind single segs
  el.querySelectorAll("[data-single]").forEach((seg) => {
    seg.querySelectorAll("button").forEach((b) => {
      b.onclick = () => {
        S[seg.dataset.single] = b.dataset.v;
        seg.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
        if (seg.dataset.single === "world") {
          WIZ_ENV_PLACES = [];
          renderWizSubject();
        }
        if (seg.dataset.single === "scope") renderWizSubject();
        if (seg.dataset.single === "power_type") syncPowerPresetFields();
        if (seg.dataset.single === "scene") wizExistingCount();
      };
    });
  });
  // bind multi chips
  el.querySelectorAll("[data-multi]").forEach((row) => {
    row.querySelectorAll(".ds-chip").forEach((c) => {
      c.onclick = () => {
        c.classList.toggle("on");
        const k = row.dataset.multi;
        S[k] = S[k] || [];
        if (c.classList.contains("on")) { if (!S[k].includes(c.dataset.v)) S[k].push(c.dataset.v); }
        else S[k] = S[k].filter((x) => x !== c.dataset.v);
      };
    });
  });
  if (WIZ_MODE === "environment") { refreshEnvLocation(); renderWizPlacePicker(); }
  if (WIZ_MODE === "character") {
    renderWizWeaponDesigner();
    const pwrap = document.createElement("div");
    pwrap.innerHTML = wizPlatesHtml();
    el.appendChild(pwrap.firstElementChild);
    const psel = $("wzp-model");
    if (psel) psel.onchange = () => { WIZ_PLATES.model = psel.value; };
    const pgo = $("wzp-go");
    if (pgo) pgo.onclick = () => generateWizPlates();
  }
  if (WIZ_MODE === "power") renderWizPowerExtras();
  renderWizPresetCards();
}

function renderWizPlacePicker() {
  const el = $("wiz-subject");
  if (!el) return;
  const world = (WIZ_SINGLE.environment || {}).world || "royal palace";
  const prof = (PS.ENV_PLACES && PS.ENV_PLACES[world]) || {};
  const names = Object.keys(prof);
  if (!names.length) return;
  const groups = { exteriors: [], interiors: [] };
  names.forEach((n) => {
    const p = prof[n] || {};
    (p.k === "i" ? groups.interiors : groups.exteriors).push(n);
  });
  const scope = (WIZ_SINGLE.environment || {}).scope || "both";
  const visible = (g) => scope === "both" ||
    (scope === "interior" ? g === "interiors" : g === "exteriors");
  const chip = (n) => {
    const p = prof[n] || {};
    const on = WIZ_ENV_PLACES.indexOf(n) >= 0;
    const tip = (p.d || n) + (p.c ? "  ·  camera: " + p.c : "") +
      (p.f ? "  ·  " + p.f : "");
    return '<span class="ds-chip place-chip' + (on ? " on" : "") +
      '" data-place="' + escapeHtml(n) + '" data-tip="' + escapeHtml(tip) +
      '">' + escapeHtml(n) + "</span>";
  };
  const wrap = document.createElement("div");
  wrap.className = "ds-group wz-places";
  wrap.innerHTML =
    '<h4>Places <span class="ds-meta-txt" style="text-transform:none">(' +
      names.length + " for " + escapeHtml(world) + " — one image per selected place)</span></h4>" +
    (visible("exteriors")
      ? '<div class="ds-group-row"><label>Exteriors <span class="muted small">' +
        groups.exteriors.length + "</span></label><div class=\"ds-chip-row\">" +
        groups.exteriors.map(chip).join("") + "</div></div>"
      : "") +
    (visible("interiors")
      ? '<div class="ds-group-row"><label>Interiors <span class="muted small">' +
        groups.interiors.length + "</span></label><div class=\"ds-chip-row\">" +
        groups.interiors.map(chip).join("") + "</div></div>"
      : "") +
    '<div class="ctl-row" style="margin-top:6px">' +
      '<button class="btn small" type="button" data-pl="all">select all shown</button>' +
      '<button class="btn small" type="button" data-pl="none">clear</button>' +
      '<span class="muted small" id="pl-count"></span>' +
      '<span class="muted small" style="margin-left:8px">tip: select several places — each renders its own camera and composition, so no two images repeat.</span>' +
    "</div>";
  el.appendChild(wrap);
  const upd = () => {
    const c = wrap.querySelector("#pl-count");
    if (c) c.textContent = WIZ_ENV_PLACES.length + " selected";
    wizExistingCount();
  };
  wrap.querySelectorAll(".place-chip").forEach((ch) => {
    ch.onclick = () => {
      const n = ch.dataset.place;
      const i = WIZ_ENV_PLACES.indexOf(n);
      if (i >= 0) { WIZ_ENV_PLACES.splice(i, 1); ch.classList.remove("on"); }
      else { WIZ_ENV_PLACES.push(n); ch.classList.add("on"); }
      upd();
    };
  });
  wrap.querySelector('[data-pl="all"]').onclick = () => {
    const shown = [];
    if (visible("exteriors")) shown.push.apply(shown, groups.exteriors);
    if (visible("interiors")) shown.push.apply(shown, groups.interiors);
    shown.forEach((n) => { if (WIZ_ENV_PLACES.indexOf(n) < 0) WIZ_ENV_PLACES.push(n); });
    wrap.querySelectorAll(".place-chip").forEach((c) => c.classList.add("on"));
    upd();
  };
  wrap.querySelector('[data-pl="none"]').onclick = () => {
    WIZ_ENV_PLACES = [];
    wrap.querySelectorAll(".place-chip").forEach((c) => c.classList.remove("on"));
    upd();
  };
  upd();
}

function renderWizWeaponDesigner() {
  const el = $("wiz-subject");
  if (!el) return;
  const wrap = document.createElement("div");
  wrap.className = "ds-group wz-designer";
  wrap.innerHTML =
    '<h4>Weapons <span class="ds-meta-txt" style="text-transform:none">(composite loadout stills)</span></h4>' +
    (wizMatrixActive()
      ? '<div class="ds-meta-txt" style="margin:-4px 0 8px">Matrix mode: the coverage control is ignored — armed variants are chosen per look in step 3 (one unarmed + one per armed weapon). These configs define what those weapons look like.</div>'
      : "") +
    '<div class="ds-group-row"><label>Weapon coverage</label><div class="ds-seg" data-wz="coverage">' +
      [0, 25, 50, 100].map((p) => '<button data-v="' + p + '" class="' +
        (WIZ_WEAPON_UI.coverage === p ? "on" : "") + '">' +
        (p === 0 ? "none" : p + "%") + "</button>").join("") +
    "</div></div>" +
    '<div class="wz-rows"></div>' +
    '<div class="ctl-row" style="margin-top:6px">' +
      '<button class="btn small" type="button" data-wz="add">+ add weapon</button>' +
      '<span class="muted small" style="margin-left:8px">Each weapon config becomes a loadout composite (character wearing the outfit, holding the weapon) plus optional clean design stills.</span>' +
    "</div>";
  el.appendChild(wrap);
  wrap.querySelectorAll('[data-wz="coverage"] button').forEach((b) => {
    b.onclick = () => {
      WIZ_WEAPON_UI.coverage = parseInt(b.dataset.v, 10) || 0;
      wrap.querySelectorAll('[data-wz="coverage"] button').forEach((x) =>
        x.classList.toggle("on", x === b));
    };
  });
  wrap.querySelector('[data-wz="add"]').onclick = () => {
    WIZ_WEAPONS.push({ id: "weapon_" + (WIZ_WEAPONS.length + 1),
      type: "longsword", material: "forged steel", style: "ornate",
      size: "one-handed", color: "", energy: "none", glow: "",
      ornament: "none", condition: "pristine" });
    renderWizWeaponRows(wrap);
  };
  renderWizWeaponRows(wrap);
}

function renderWizWeaponRows(wrap) {
  const box = wrap.querySelector(".wz-rows");
  if (!box) return;
  const sel = (opts, val) => opts.map((o) =>
    '<option value="' + escapeHtml(o) + '"' + (val === o ? " selected" : "") +
    ">" + escapeHtml(o) + "</option>").join("");
  box.innerHTML = WIZ_WEAPONS.map((w, i) =>
    '<div class="wz-row" data-i="' + i + '">' +
      '<input type="text" class="wz-id" value="' + escapeHtml(w.id) +
        '" placeholder="weapon id (spear_of_dawn)" spellcheck="false">' +
      '<select class="wz-type" data-tip="Weapon family">' +
        sel(Object.keys(PS.WEAPON_TYPES || { custom: 1 }), w.type) + "</select>" +
      '<select class="wz-material">' + sel(PS.WEAPON_MATERIALS || [], w.material) + "</select>" +
      '<select class="wz-style">' + sel(PS.WEAPON_STYLES || [], w.style) + "</select>" +
      '<select class="wz-size">' + sel(PS.WEAPON_SIZES || [], w.size) + "</select>" +
      '<select class="wz-energy" data-tip="Channel the character power through the blade">' +
        sel(PS.WEAPON_ENERGIES || ["none"], w.energy) + "</select>" +
      '<input type="text" class="wz-color" value="' + escapeHtml(w.color || "") +
        '" placeholder="color #hex" spellcheck="false">' +
      '<button class="btn small danger" type="button" data-wzrm="' + i + '">×</button>' +
    "</div>").join("") ||
    '<div class="muted small">no weapons — the batch generates unarmed looks only</div>';
  box.querySelectorAll(".wz-row").forEach((row) => {
    const i = parseInt(row.dataset.i, 10);
    const bind = (cls, key) => {
      const el = row.querySelector(cls);
      if (el) el.onchange = () => { WIZ_WEAPONS[i][key] = el.value; };
    };
    bind(".wz-id", "id"); bind(".wz-type", "type"); bind(".wz-material", "material");
    bind(".wz-style", "style"); bind(".wz-size", "size"); bind(".wz-energy", "energy");
    bind(".wz-color", "color");
  });
  box.querySelectorAll("[data-wzrm]").forEach((b) => {
    b.onclick = () => {
      WIZ_WEAPONS.splice(parseInt(b.dataset.wzrm, 10), 1);
      renderWizWeaponRows(wrap);
    };
  });
}

function renderWizPowerExtras() {
  const el = $("wiz-subject");
  if (!el) return;
  const wrap = document.createElement("div");
  wrap.className = "ds-group";
  wrap.innerHTML =
    '<h4>Level ladder <span class="ds-meta-txt" style="text-transform:none">(one still per level — always on)</span></h4>' +
    '<div class="ds-group-row"><label>Levels</label>' +
      '<label>count<input type="number" id="wz-ladder-n" min="2" max="10" value="' +
        Math.min(10, WIZ_POWER_UI.level_count) + '" style="width:70px;margin-left:6px"></label>' +
      '<span class="muted small" style="margin-left:12px">Set the batch count to the level count (e.g. 10) for one image per level.</span>' +
    "</div>" +
    '<div class="muted small">Each level changes the <b>manifestation</b>, <b>framing</b> and <b>subject action</b> together, not just brightness:<br>' +
    "1 fingertips glimmer (close-up) · 3 full-body aura · 4 orb · 5 controlled beam · 6 expanding wave · " +
    "7 ground eruption (wide) · 8 expanding dome + shockwave · 9 sky-sized display · 10 world-shaking cataclysm.<br>" +
    "The renderer picks the highest level still at or below the shot's power level (floor rule).</div>";
  el.appendChild(wrap);
  const n = wrap.querySelector("#wz-ladder-n");
  if (n) n.onchange = () => {
    WIZ_POWER_UI.level_count = Math.max(2, Math.min(10, parseInt(n.value, 10) || 10));
  };
}

function renderWizPresetCards() {
  const el = $("wiz-preset-cards");
  const sets = (PS.PRESETS && PS.PRESETS.characters) ||
    (PS_FALLBACK.PRESETS.characters);
  const setsAll = { character: sets,
                    power: (PS.PRESETS && PS.PRESETS.powers) || PS_FALLBACK.PRESETS.powers,
                    environment: (PS.PRESETS && PS.PRESETS.environments) || PS_FALLBACK.PRESETS.environments };
  const use = setsAll[WIZ_MODE];
  if (!use) { el.innerHTML = ""; return; }
  el.innerHTML = Object.keys(use).map((name) =>
    `<span class="ds-chip preset-chip" data-name="${escapeHtml(name)}">${escapeHtml(name)}</span>`).join("");
  el.querySelectorAll(".preset-chip").forEach((c) => {
    c.onclick = () => {
      const name = c.dataset.name;
      const S = WIZ_SINGLE[WIZ_MODE];
      if (WIZ_MODE === "character") {
        const presetEntry = (PS.PRESETS && PS.PRESETS.characters && PS.PRESETS.characters[name]) ||
          PS_FALLBACK.PRESETS.characters[name];
        const archKey = presetEntry && presetEntry.archetype;
        const meta = archKey && ((PS.ARCHETYPES && PS.ARCHETYPES[archKey]) ||
          PS_FALLBACK.ARCHETYPES[archKey]);
        if (meta) {
          S.archetype = archKey;
          if (meta.lineage) S.lineage = meta.lineage;
          if (meta.faction) S.faction = meta.faction;
          if (meta.palette) S.palette = meta.palette;
          if (meta.condition) S.condition = meta.condition;
          if (meta.materials && meta.materials.length) S.materials = meta.materials.slice();
          if (meta.headwear) S.headwear = meta.headwear;
          if (meta.jewelry) S.jewelry = meta.jewelry;
          if (meta.armor && meta.armor !== "none") S.silhouette = meta.armor;
          else if (archKey && archKey.includes("princess")) S.silhouette = "flowing gown";
        }
      } else if (WIZ_MODE === "power") {
        const meta = (PS.PRESETS && PS.PRESETS.powers && PS.PRESETS.powers[name]) || PS_FALLBACK.PRESETS.powers[name];
        if (meta) {
          if (meta.power_type) S.power_type = meta.power_type;
          if (meta.color) S.color = meta.color;
          if (meta.behavior) S.behavior = meta.behavior;
          if (meta.manifestation) S.manifestation = meta.manifestation;
          syncPowerPresetFields();
        }
      } else {
        const meta = (PS.PRESETS && PS.PRESETS.environments && PS.PRESETS.environments[name]) || PS_FALLBACK.PRESETS.environments[name];
        if (meta && meta.world) { S.world = meta.world; refreshEnvLocation(); }
      }
      renderWizSubject();
      toast("preset applied: " + name);
    };
  });
}

function refreshEnvLocation() {
  // places are an explicit multi-select now (renderWizPlacePicker); this
  // only keeps the world's default materials/features in sync
  const S = WIZ_SINGLE.environment || {};
  const world = S.world || "royal palace";
  const worldMeta = (PS.ENV_WORLDS && PS.ENV_WORLDS[world]) || {};
  if (worldMeta.materials) S.materials = S.materials.length ? S.materials : worldMeta.materials.slice();
  if (worldMeta.features) S.features = S.features.length ? S.features : worldMeta.features.slice();
}

function syncPowerPresetFields() {
  const S = WIZ_SINGLE.power || {};
  const meta = (PS.POWER_TYPES && PS.POWER_TYPES[S.power_type]) || PS_FALLBACK.POWER_TYPES[S.power_type];
  if (meta) {
    if (!S.color) S.color = meta.color;
    if (!S.material) S.material = meta.material;
    if (!S.glow) S.glow = meta.glow;
  }
}

/* ---- wardrobe matrix (Tab 2): outfit x weapon, per-look model ---- */
function envMaterialOpts() {
  const out = [];
  Object.keys(PS.ENV_WORLDS || {}).forEach((w) => {
    (((PS.ENV_WORLDS || {})[w] || {}).materials || []).forEach((m) => {
      if (m && !out.includes(m)) out.push(m);
    });
  });
  return out;
}

function wizOutfitOpts() {
  const out = [];
  (PS.OUTFIT_VARIANTS || []).forEach((o) => { if (o && !out.includes(o)) out.push(o); });
  Object.keys(PS.WARDROBE_PRESETS || {}).forEach((o) => { if (o && !out.includes(o)) out.push(o); });
  return out;
}

function wizMatrixRows() {
  return WIZ_MATRIX.filter((r) => r.on !== false && (r.outfit || "").trim());
}

function wizMatrixActive() {
  return WIZ_MODE === "character" && wizMatrixRows().length > 0;
}

function wizMatrixTotal() {
  return wizMatrixRows().reduce((s, r) =>
    s + (1 + (r.weapons || []).length) * Math.max(1, r.count || 1), 0);
}

/* The matrix owns the model per look (per-clothing model selection): each
   wardrobe row's stills run on its own model; LoRA/hires/worker settings are
   inherited from the Model Mix row with the same model id. */
function wizModelRows() {
  if (wizMatrixActive()) {
    const byModel = {};
    wizMatrixRows().forEach((r) => {
      const mid = r.model || (WIZ_MODELS[0] && WIZ_MODELS[0].model) || "realvis";
      const n = (1 + (r.weapons || []).length) * Math.max(1, r.count || 1);
      const row = byModel[mid] = byModel[mid] || { model: mid, count: 0, outfits: [] };
      row.count += n;
      if (!row.outfits.includes(r.outfit)) row.outfits.push(r.outfit);
    });
    return Object.keys(byModel).map((mid) => {
      const src = WIZ_MODELS.find((m) => m.model === mid) || {};
      const out = { model: mid, count: byModel[mid].count, outfits: byModel[mid].outfits };
      if (src.loras && src.loras.length) out.loras = src.loras;
      if (src.hires) out.hires = true;
      if (src.worker) out.worker = src.worker;
      if (src.steps) out.steps = src.steps;
      if (src.cfg) out.cfg = src.cfg;
      return out;
    });
  }
  return WIZ_MODELS.map((r) => {
    const out = { model: r.model, count: r.count };
    if (r.steps) out.steps = r.steps;
    if (r.cfg) out.cfg = r.cfg;
    if (r.hires) out.hires = true;
    if (r.loras && r.loras.length) out.loras = r.loras;
    if (r.worker) out.worker = r.worker;
    return out;
  });
}

function wizMatrixHtml() {
  const modelOpts = (sel) => Object.keys(WIZ_MODEL_INFO).map((id) => {
    const lab = (WIZ_MODEL_INFO[id] || {}).name || id;
    return '<option value="' + esc(id) + '"' + ((sel || "") === id ? " selected" : "") + ">" + esc(lab) + "</option>";
  }).join("");
  const weaponChips = (r, i) => {
    const wz = WIZ_WEAPONS.filter((w) => (w.id || "").trim());
    if (!wz.length) return '<span class="ds-meta-txt">no weapons yet — add them in step 2</span>';
    return wz.map((w) => {
      const wid = w.id.trim();
      const on = (r.weapons || []).includes(wid);
      return '<span class="ds-chip ' + (on ? "on" : "") + '" data-mw="' + i + '" data-wid="' + esc(wid) +
        '" data-tip="armed variant: ' + esc(wid) + '">' + esc(wid) + "</span>";
    }).join("");
  };
  return '<div class="ds-group-row wzm-head">' +
      "<label>Wardrobe matrix<br><span class=\"ds-meta-txt\">each look on its own model · unarmed + armed variants</span></label>" +
      '<div style="flex:1">' +
        (WIZ_MATRIX.map((r, i) => '<div class="wzm-row" data-i="' + i + '">' +
          '<input class="wzm-outfit" list="wzm-outfits" value="' + esc(r.outfit || "") +
            '" placeholder="outfit name" data-tip="A wardrobe preset (e.g. evening gown — high slit) or free text">' +
          '<select class="wzm-model" data-tip="Model for THIS look (per-clothing model selection)">' + modelOpts(r.model) + "</select>" +
          '<select class="wzm-count" data-tip="Images per variant (unarmed + each armed look)">' +
            [1, 2, 3, 4].map((n) => '<option value="' + n + '"' + ((r.count || 1) === n ? " selected" : "") + ">" + n + "×</option>").join("") +
          "</select>" +
          '<span class="wzm-weapons" data-tip="Armed variants — each adds an outfit + weapon composite still">' + weaponChips(r, i) + "</span>" +
          '<span class="ds-meta-txt wzm-look-total" data-tip="variants × multiplier: 1 unarmed + N armed, repeated by the count">' +
            (1 + (r.weapons || []).length) + " × " + Math.max(1, r.count || 1) + " = " +
            ((1 + (r.weapons || []).length) * Math.max(1, r.count || 1)) + " img</span>" +
          '<label class="toggle" data-tip="Include this look"><input type="checkbox" class="wzm-on" ' + (r.on !== false ? "checked" : "") + "><span></span></label>" +
          '<button class="btn small ghost" type="button" data-wzrm2="' + i + '" data-tip="remove this look">' + Icon.get("x") + "</button>" +
        "</div>").join("") ||
          ('<span class="ds-meta-txt">no looks yet — add one. Until then the classic Outfits / Wardrobe axes drive the batch.</span>' +
           (((WIZ_AXES.character || {}).outfits || {}).sel || []).length
             ? ' <button class="btn small" type="button" id="wzm-seed" data-tip="Create one matrix look per outfit you ticked in the Outfits axis above, then choose a model and weapons per look">' +
               "+ use my " + (((WIZ_AXES.character || {}).outfits || {}).sel || []).length + " selected outfits as looks</button>"
             : "")) +
        '<div style="margin-top:6px;display:flex;gap:8px;align-items:center">' +
          '<button class="btn small" type="button" id="wzm-add">+ add look</button>' +
          '<span class="ds-meta-txt" id="wzm-total">' + wizMatrixTotal() + " images from the matrix (unarmed + armed)</span>" +
        "</div>" +
      "</div>" +
    "</div>" +
    '<datalist id="wzm-outfits">' +
      wizOutfitOpts().map((o) => '<option value="' + esc(o) + '"></option>').join("") +
    "</datalist>";
}

function wireWizMatrix() {
  const box = $("wiz-variation");
  if (!box) return;
  box.querySelectorAll(".wzm-row").forEach((row) => {
    const i = Number(row.dataset.i);
    const r = WIZ_MATRIX[i];
    if (!r) return;
    row.querySelector(".wzm-outfit").onchange = (e) => { r.outfit = e.target.value; wizSummary(); };
    row.querySelector(".wzm-model").onchange = (e) => { r.model = e.target.value; wizSummary(); };
    row.querySelector(".wzm-count").onchange = (e) => { r.count = Number(e.target.value) || 1; renderWizVariation(); };
    row.querySelector(".wzm-on").onchange = (e) => { r.on = e.target.checked; renderWizVariation(); };
    row.querySelector("[data-wzrm2]").onclick = () => { WIZ_MATRIX.splice(i, 1); renderWizVariation(); };
    row.querySelectorAll("[data-mw]").forEach((chip) => {
      chip.onclick = () => {
        const wid = chip.dataset.wid;
        const arr = r.weapons = r.weapons || [];
        const at = arr.indexOf(wid);
        if (at >= 0) arr.splice(at, 1); else arr.push(wid);
        renderWizVariation();
      };
    });
  });
  const add = $("wzm-add");
  if (add) add.onclick = () => {
    WIZ_MATRIX.push({ outfit: "", model: "", count: 1, weapons: [], on: true });
    renderWizVariation();
  };
  const seed = $("wzm-seed");
  if (seed) seed.onclick = () => {
    const sel = (((WIZ_AXES.character || {}).outfits || {}).sel) || [];
    if (!sel.length) return;
    WIZ_MATRIX = sel.map((o) => ({ outfit: o, model: "", count: 1, weapons: [], on: true }));
    // the matrix now owns the looks — clear the classic cycle so nothing is
    // generated twice
    WIZ_AXES.character.outfits.sel = [];
    renderWizVariation();
    toast("matrix seeded with " + sel.length + " looks — pick a model + weapons per look");
  };
}

/* ---- identity plates (Tab 1): close / medium / full, no weapon ---- */
function wizPlatesHtml() {
  const modelOpts = Object.keys(WIZ_MODEL_INFO).map((id) => {
    const lab = (WIZ_MODEL_INFO[id] || {}).name || id;
    return '<option value="' + esc(id) + '"' + (WIZ_PLATES.model === id ? " selected" : "") + ">" + esc(lab) + "</option>";
  }).join("");
  return '<div class="ds-group-row wzp-row">' +
      "<label>Identity plates<br><span class=\"ds-meta-txt\">close / medium / full · one stable expression · no weapon · plain studio</span></label>" +
      '<div style="flex:1;display:flex;gap:8px;align-items:center;flex-wrap:wrap">' +
        '<select id="wzp-model" data-tip="Model for the plates">' + modelOpts + "</select>" +
        '<button class="btn small primary" type="button" id="wzp-go" data-tip="Queue a 3-image run into this dataset: close-up, 3/4 body and full-body plates with the SAME neutral expression. The cast import tags them close/medium/full so the resolver matches each shot\'s framing.">' +
          Icon.get("wand") + " Generate identity plates</button>" +
        '<span class="ds-meta-txt">3 images · imported as framing-matched hero refs</span>' +
      "</div>" +
    "</div>";
}

async function generateWizPlates() {
  const name = ($("wiz-name").value || "").trim();
  if (!name) return toast("give the dataset a name (step 1)", true);
  const params = wizGatherParams();
  params.identity_plates = true;
  ["matrix", "outfits", "wardrobes", "weapons", "weapon_coverage",
   "poses", "scenes", "lightings", "gazes", "emotions", "shots",
   "angles", "scene_locations"].forEach((k) => { delete params[k]; });
  const model = WIZ_PLATES.model ||
    (WIZ_MODEL_INFO.realvis ? "realvis" : Object.keys(WIZ_MODEL_INFO)[0]);
  const r = await post("/api/training/generate",
    { mode: "character", params: { ...params, models: [{ model, count: 3 }] } });
  if (!r.ok) return toast("plates failed: " + r.error, true);
  WIZ_RUN_ID = r.run_id;
  toast("identity plates queued — 3 images (close / medium / full)");
  $("wiz-progress-txt").textContent = "queued…";
  $("wiz-stop").style.display = "";
}

function renderWizVariation() {
  const el = $("wiz-variation");
  if (WIZ_MODE === "photo") { el.innerHTML = ""; return; }
  wizEnsureState();
  const conf = (wizAxisConf())[WIZ_MODE] || [];
  const axes = WIZ_AXES[WIZ_MODE];
  el.innerHTML = conf.map((ax) => {
    const a = axes[ax.k];
    if (!a) return "";
    const mark = a.kind === "multiply" ? "×" : Icon.get("refresh");
    const opts = (ax.opts || []).filter((o) => o && o !== "");
    const sel = a.sel || [];
    return `<div class="ds-group-row">
      <label>${ax.label}<br><span class="ds-meta-txt">${mark} ${a.kind} ${a.locked ? "· fixed" : ""}</span></label>
      <div class="ds-chip-row" data-axis="${ax.k}" style="flex:1">
        ${opts.length
          ? opts.map((o) => `<span class="ds-chip ${sel.includes(o) ? "on" : ""}" data-v="${escapeHtml(o)}">${escapeHtml(o)}</span>`).join("")
          : '<span class="ds-meta-txt">(options load after restart — defaults are active)</span>'}
      </div>
      <label class="toggle" title="Locked: every picture uses the first value"><input type="checkbox" class="axis-lock" ${a.locked ? "checked" : ""}><span></span><em>Lock</em></label>
    </div>`;
  }).join("");
  el.querySelectorAll("[data-axis]").forEach((row) => {
    const k = row.dataset.axis;
    row.querySelectorAll(".ds-chip").forEach((c) => {
      c.onclick = () => {
        c.classList.toggle("on");
        const a = WIZ_AXES[WIZ_MODE][k];
        if (c.classList.contains("on")) { if (!a.sel.includes(c.dataset.v)) a.sel.push(c.dataset.v); }
        else a.sel = a.sel.filter((x) => x !== c.dataset.v);
        wizSummary();
      };
    });
  });
  el.querySelectorAll(".axis-lock").forEach((chk) => {
    chk.onchange = () => {
      const row = chk.closest(".ds-group-row");
      const k = row.querySelector("[data-axis]").dataset.axis;
      WIZ_AXES[WIZ_MODE][k].locked = chk.checked;
      renderWizVariation();
      wizSummary();
    };
  });
  if (WIZ_MODE === "character") {
    // insertAdjacentHTML (NOT innerHTML +=): reassigning innerHTML re-parses
    // the whole pane and DESTROYS every already-wired chip listener — the
    // axis chips went dead the moment the matrix section was appended.
    el.insertAdjacentHTML("beforeend", wizMatrixHtml());
    wireWizMatrix();
  }
  wizSummary();
}

$("wiz-randomize").onclick = () => {
  const S = WIZ_SINGLE[WIZ_MODE];
  const sections = (wizSingleSections())[WIZ_MODE] || [];
  const pick = (opts) => opts[Math.floor(Math.random() * opts.length)];
  sections.forEach((sec) => {
    (sec.fields || []).forEach(([k, , opts]) => {
      if (opts && opts.length && S[k] !== undefined) S[k] = pick(opts);
    });
  });
  renderWizSubject();
};
$("wiz-randomize-axes").onclick = () => {
  const axes = WIZ_AXES[WIZ_MODE];
  const conf = (wizAxisConf())[WIZ_MODE] || [];
  conf.forEach((ax) => {
    const a = axes[ax.k];
    if (!a || a.locked) return;
    const opts = (ax.opts || []).filter((o) => o && o !== "");
    if (!opts.length) return;
    const n = 1 + Math.floor(Math.random() * Math.min(3, opts.length));
    a.sel = [];
    while (a.sel.length < n) {
      const v = pick(opts);
      if (!a.sel.includes(v)) a.sel.push(v);
    }
  });
  renderWizVariation();
};

/* ---- profiles ---- */
$("wiz-profile-save").onclick = async () => {
  const name = await Dlg.prompt({ title: "Save profile", label: "Save wizard as profile:", okLabel: "Save" });
  if (!name) return;
  try {
    const all = JSON.parse(localStorage.getItem("studio_wiz_profiles") || "{}");
    all[name] = { mode: WIZ_MODE, single: WIZ_SINGLE, axes: WIZ_AXES,
                  models: WIZ_MODELS, intent: WIZ_INTENT, pmode: WIZ_PMODE };
    localStorage.setItem("studio_wiz_profiles", JSON.stringify(all));
    renderWizProfileList();
    toast("profile saved: " + name);
  } catch (e) { toast("save failed", true); }
};
function renderWizProfileList() {
  const sel = $("wiz-profile-load");
  try {
    const all = JSON.parse(localStorage.getItem("studio_wiz_profiles") || "{}");
    sel.innerHTML = '<option value="">Load profile…</option>' +
      Object.keys(all).map((n) => `<option>${escapeHtml(n)}</option>`).join("");
  } catch (e) { /* none */ }
}
$("wiz-profile-load").onchange = () => {
  const name = $("wiz-profile-load").value;
  if (!name) return;
  try {
    const all = JSON.parse(localStorage.getItem("studio_wiz_profiles") || "{}");
    const p = all[name];
    if (!p) return;
    WIZ_MODE = p.mode;
    WIZ_SINGLE = p.single;
    WIZ_AXES = p.axes;
    WIZ_MODELS = p.models;
    WIZ_INTENT = p.intent || "cinematic";
    WIZ_PMODE = p.pmode || "structured";
    document.querySelectorAll("#wiz-type-seg button").forEach((b) =>
      b.classList.toggle("on", b.dataset.mode === p.mode));
    renderWizSubject();
    renderWizVariation();
    wizStep(2);
    toast("profile loaded: " + name);
  } catch (e) { toast("load failed", true); }
};
renderWizProfileList();

/* model mix */
let WIZ_LORAS = {};   // generation LoRA registry (labels etc.)
function wizLoraOptions(selected) {
  let opts = '<option value="">— none —</option>';
  for (const [file, m] of Object.entries(WIZ_LORAS)) {
    opts += `<option value="${escapeHtml(file)}" ${file === selected ? "selected" : ""}>${escapeHtml(m.label)}</option>`;
  }
  return opts;
}
function renderWizModels() {
  const el = $("wiz-models");
  // When the wardrobe matrix is active it OWNS the models and the counts
  // (per-clothing model selection): show the derived plan read-only so the
  // wizard never asks for a "model mix" that would be ignored.
  const head = wizMatrixActive() ? (() => {
    const rows = wizModelRows();
    const total = rows.reduce((s, r) => s + r.count, 0);
    return '<div class="ds-group" style="margin-bottom:10px">' +
      '<div class="ds-group-row"><label>Driven by the matrix<br><span class="ds-meta-txt">step 3 · wardrobe matrix</span></label>' +
      '<div style="flex:1">' +
      wizMatrixRows().map((r) => {
        const mid = r.model || (WIZ_MODELS[0] && WIZ_MODELS[0].model) || "realvis";
        const variants = 1 + (r.weapons || []).length;
        const mult = Math.max(1, r.count || 1);
        return '<div class="ds-meta-txt" style="margin:2px 0">' + escapeHtml(r.outfit) +
          " → <b>" + escapeHtml((WIZ_MODEL_INFO[mid] || {}).name || mid) + "</b> · " +
          variants + " variant(s) × " + mult + " = <b>" + (variants * mult) + " images</b></div>";
      }).join("") +
      '<div class="ds-meta-txt" style="margin-top:6px">Total from the matrix: <b>' + total +
      " images</b> — change looks, weapons or the model per look in step 3. " +
      "The rows below only supply LoRAs / Hires / Run-on for a model.</div>" +
      "</div></div></div>";
  })() : "";
  el.innerHTML = head + WIZ_MODELS.map((row, i) => {
    const mi = WIZ_MODEL_INFO[row.model] || {};
    const lorasOk = mi.capabilities && mi.capabilities.loras;
    const hiresOk = mi.capabilities && mi.capabilities.hires;
    const loras = row.loras || [];
    const loraBox = lorasOk ? `
      <div style="width:100%;display:flex;flex-wrap:wrap;gap:8px;align-items:center">
        ${loras.map((l, li) => {
          const st = (typeof l.strength === "number") ? l.strength : 0.7;
          return `
          <span class="ds-chip on" style="padding:4px 8px">
            <select class="wiz-lora-sel" data-i="${i}" data-li="${li}" style="border:none;background:none;font-size:11px;max-width:130px">${wizLoraOptions(l.file)}</select>
            <input type="range" class="wiz-lora-str" data-i="${i}" data-li="${li}" min="0" max="1.5" step="0.05" value="${st}" style="width:60px">
            <span class="wiz-lora-val">${st.toFixed(2)}</span>
            <button class="wiz-lora-del" data-i="${i}" data-li="${li}" style="background:none;border:0;color:var(--red);cursor:pointer;display:inline-flex;padding:0">${Icon.get("x")}</button>
          </span>`;
        }).join("")}
        <button class="ds-btn tiny wiz-lora-add" data-i="${i}" ${loras.length >= 5 ? "disabled" : ""}>+ LoRA</button>
      </div>` : "";
    return `<div class="ds-model-row" data-i="${i}">
      <select class="wiz-model-sel">${Object.keys(WIZ_MODEL_INFO).map((m) =>
        `<option value="${m}" ${m === row.model ? "selected" : ""}>${escapeHtml((WIZ_MODEL_INFO[m] || {}).name || m)}</option>`).join("")}</select>
      <div class="ds-num">
        <button class="wiz-m-dec">−</button><span>${row.count}</span><button class="wiz-m-inc">+</button>
      </div>
      <select class="wiz-worker-sel" data-i="${i}" title="Render this row's images on" style="font-size:11px;max-width:130px">
        <option value="">local</option>
      </select>
      <span class="ds-meta-txt wiz-m-est">~${Math.round((mi.estimate_s || 120) * row.count / 60)} min</span>
      <span class="ds-meta-txt wiz-m-desc">${escapeHtml((mi.description || "").slice(0, 70))}</span>
      <button class="ds-btn tiny wiz-m-del">${Icon.get("x")}</button>
      ${lorasOk ? `<button class="ds-btn tiny wiz-lora-toggle">LoRAs ${loras.length ? "(" + loras.length + ")" : ""}</button>` : ""}
      ${hiresOk ? `<label class="toggle" style="margin-left:4px"><input type="checkbox" class="wiz-hires" ${row.hires ? "checked" : ""}><span></span><em>Sharpen</em></label>` : ""}
      ${lorasOk && row.loraOpen ? loraBox : ""}
    </div>`;
  }).join("");
  wsFleet().then(() => fillWorkerSelects($("wiz-models")));
  el.querySelectorAll(".ds-model-row").forEach((rowEl) => {
    const i = parseInt(rowEl.dataset.i);
    rowEl.querySelector(".wiz-model-sel").onchange = (e) => {
      WIZ_MODELS_TOUCHED = true;
      WIZ_MODELS[i].model = e.target.value;
      WIZ_MODELS[i].loras = [];
      WIZ_MODELS[i].loraOpen = false;
      renderWizModels();
    };
    rowEl.querySelector(".wiz-m-dec").onclick = () => {
      WIZ_MODELS_TOUCHED = true;
      WIZ_MODELS[i].count = Math.max(1, WIZ_MODELS[i].count - 1);
      renderWizModels();
    };
    rowEl.querySelector(".wiz-m-inc").onclick = () => {
      WIZ_MODELS_TOUCHED = true;
      WIZ_MODELS[i].count += 1;
      renderWizModels();
    };
    rowEl.querySelector(".wiz-m-del").onclick = () => {
      WIZ_MODELS_TOUCHED = true;
      WIZ_MODELS.splice(i, 1);
      renderWizModels();
    };
    const tg = rowEl.querySelector(".wiz-lora-toggle");
    if (tg) tg.onclick = () => {
      WIZ_MODELS[i].loraOpen = !WIZ_MODELS[i].loraOpen;
      renderWizModels();
    };
    const add = rowEl.querySelector(".wiz-lora-add");
    if (add) add.onclick = () => {
      const used = new Set((WIZ_MODELS[i].loras || []).map((l) => l.file));
      const free = Object.keys(WIZ_LORAS).filter((f) => !used.has(f));
      if (!free.length) return toast("Every LoRA is already added", true);
      WIZ_MODELS[i].loras = WIZ_MODELS[i].loras || [];
      WIZ_MODELS[i].loras.push({ file: free[0], strength: WIZ_LORAS[free[0]].strength || 0.7 });
      renderWizModels();
    };
    rowEl.querySelectorAll(".wiz-lora-sel").forEach((s) => {
      s.onchange = (e) => {
        const li = parseInt(s.dataset.li);
        WIZ_MODELS[i].loras[li].file = e.target.value;
        renderWizModels();
      };
    });
    rowEl.querySelectorAll(".wiz-lora-str").forEach((s) => {
      s.oninput = () => {
        const li = parseInt(s.dataset.li);
        WIZ_MODELS[i].loras[li].strength = parseFloat(s.value);
        s.parentElement.querySelector(".wiz-lora-val").textContent = parseFloat(s.value).toFixed(2);
      };
    });
    rowEl.querySelectorAll(".wiz-lora-del").forEach((b) => {
      b.onclick = () => {
        const li = parseInt(b.dataset.li);
        WIZ_MODELS[i].loras.splice(li, 1);
        renderWizModels();
      };
    });
    const hiresChk = rowEl.querySelector(".wiz-hires");
    if (hiresChk) hiresChk.onchange = () => {
      WIZ_MODELS[i].hires = hiresChk.checked;
      wizSummary();
    };
  });
  wizSummary();
}
$("wiz-add-model").onclick = () => {
  if (WIZ_MODELS.length >= 8) return toast("8 models is plenty — quality drops beyond that", true);
  const free = Object.keys(WIZ_MODEL_INFO).filter((m) =>
    !WIZ_MODELS.some((r) => r.model === m));
  WIZ_MODELS.push({ model: free[0] || "realvis", count: 10 });
  renderWizModels();
};

/* prompt preview (exact via backend endpoint; structural fallback) */
function wizGatherParams() {
  wizEnsureState();
  const S = WIZ_SINGLE[WIZ_MODE] || {};
  const A = WIZ_AXES[WIZ_MODE] || {};
  const params = { ...S, name: ($("wiz-name").value || "").trim() };
  Object.entries(A).forEach(([k, a]) => {
    if (!a) return;
    let sel = a.sel || [];
    if (a.locked) sel = sel.slice(0, 1);
    params[k] = sel;
  });
  if (WIZ_MODE === "power" && !params.power) params.power = params.name || "arcane";
  params.intent = WIZ_INTENT;
  params.mode = WIZ_PMODE;
  // legacy aliases: the PRE-restart backend still consumes the old field
  // names — sending both keeps batches correct everywhere (the new
  // backend prefers the new keys via spec_from_params)
  if (WIZ_MODE === "character") {
    params.gender = S.presentation || "woman";
    params.outfit = S.silhouette || "";
    params.build = S.body || "";
    params.distinctive = S.marks || "none";
    const shotToAngle = { "close-up": "front-facing", portrait: "front-facing",
      medium: "front-facing", "3/4 body": "three-quarter", "full body": "front-facing",
      wide: "front-facing", "low angle hero": "low angle", "high angle": "looking down",
      profile: "profile right", "over-the-shoulder": "over-shoulder" };
    const shotsSel = (A.shots && A.shots.sel) || [];
    params.angles = shotsSel.map((s) => shotToAngle[s] || "front-facing");
    // weapons: config rows + coverage; the batch composes them into loadout
    // composites (character wearing the outfit, holding the weapon).
    // MATRIX MODE (Tab 2): the combos carry outfit+weapon explicitly and the
    // per-row `outfits` filters route each look to its own model — the
    // coverage axis and the outfits/wardrobe cycles must NOT also run.
    const wz = WIZ_WEAPONS.filter((w) => (w.id || "").trim());
    const handling = ["held at side", "two-handed grip", "raised overhead",
                      "mid-swing", "planted in ground", "channeling"];
    const wcfg = {};
    wz.forEach((w) => { wcfg[w.id.trim()] = { ...w, id: w.id.trim() }; });
    if (wizMatrixActive()) {
      const matrix = [];
      wizMatrixRows().forEach((r) => {
        matrix.push({ outfit: r.outfit });
        (r.weapons || []).forEach((wid) => matrix.push({ outfit: r.outfit, weapon: wid }));
      });
      params.matrix = matrix;
      delete params.outfits;
      delete params.wardrobes;
      delete params.weapons;
      delete params.weapon_coverage;
      if (wz.length) {
        params.weapon_configs = wcfg;
        params.weapon_handling = handling;
      }
    } else if (wz.length && WIZ_WEAPON_UI.coverage > 0) {
      params.weapons = wz.map((w) => w.id.trim());
      params.weapon_configs = wcfg;
      params.weapon_coverage = WIZ_WEAPON_UI.coverage;
      params.weapon_handling = handling;
    }
  }
  if (WIZ_MODE === "power") {
    // the ladder is the power generator now — always on. NOTE: no legacy
    // aliases (forms/envs/cameras) may be sent here — they flip the backend
    // to the OLD power_prompt builder ("two raised hands…"), which is
    // exactly what made every power image identical.
    params.level_ladder = true;
    params.level_count = WIZ_POWER_UI.level_count;
  }
  if (WIZ_MODE === "environment") {
    // NOTE: no `subtypes` legacy alias — it flips the backend to the OLD
    // environment_prompt builder and the per-place camera profiles never run
    params.mood = S.atmosphere || "epic";
    // explicit multi-select of places (each carries its own camera/framing);
    // the scope only filters the picker, the selection is authoritative
    params.places = WIZ_ENV_PLACES.slice();
  }
  const cp = $("wiz-custom-prompt").value.trim();
  const cn = $("wiz-custom-negative").value.trim();
  if (WIZ_PMODE === "add" || WIZ_PMODE === "override") params.custom_prompt = cp;
  if (cn) params.custom_negative = cn;
  const seedVal = $("wiz-seed").value.trim();
  if (seedVal) params.seed = parseInt(seedVal);
  if (WIZ_REFS.length) {
    params.refs = WIZ_REFS.map((r) => typeof r === "string"
      ? { file: r, type: "Identity" } : r);
    params.ref_strength = parseFloat($("wiz-ref-str").value) || 0.8;
    params.anchor = $("wiz-ref-anchor").checked;
  }
  return params;
}

function wizCombosFromAxes() {
  if (wizMatrixActive()) {
    // the matrix owns the combos: one unarmed + one per armed weapon, per look
    const looks = wizMatrixRows();
    const n = looks.reduce((s, r) => s + 1 + (r.weapons || []).length, 0);
    return { n, detail: [looks.length + "x matrix looks (unarmed + armed)"] };
  }
  const A = WIZ_AXES[WIZ_MODE] || {};
  let n = 1;
  const detail = [];
  if (WIZ_MODE === "environment" && WIZ_ENV_PLACES.length) {
    n *= WIZ_ENV_PLACES.length;
    detail.push(WIZ_ENV_PLACES.length + "x places");
  }
  if (WIZ_MODE === "power") {
    n *= Math.max(1, WIZ_POWER_UI.level_count);
    detail.push(WIZ_POWER_UI.level_count + "x levels");
  }
  Object.entries(A).forEach(([k, a]) => {
    let sel = a.sel || [];
    if (a.locked) sel = sel.slice(0, 1);
    if (a.kind === "multiply" && sel.length) {
      n *= sel.length;
      detail.push(sel.length + "x " + k);
    }
  });
  return { n, detail };
}

async function renderWizPrompt() {
  try {
    wizEnsureState();
    const params = wizGatherParams();
    const pmodel = $("wiz-preview-model").value;
    try {
      const r = await post("/api/training/preview", { mode: WIZ_MODE, params, model: pmodel });
      if (r.ok && r.prompt) {
        $("wiz-prompt-preview").textContent = r.prompt;
        $("wiz-neg-preview").textContent = r.negative || "";
        return;
      }
    } catch (e) { /* backend staged — fall through */ }
    const S = WIZ_SINGLE[WIZ_MODE] || {};
    const cb = wizCombosFromAxes();
    const name = (WIZ_MODEL_INFO[pmodel] || {}).name || pmodel;
    const core = S.archetype || S.power_type || S.world || "custom";
    let txt = "[" + name + "] " + WIZ_MODE + " spec: " + core +
      " | " + (cb.detail.join(" · ") || "no multiply axes") +
      " | " + WIZ_INTENT + " | " + WIZ_PMODE;
    if (WIZ_PMODE === "override") txt += " | override: " + ($("wiz-custom-prompt").value.trim() || "—");
    $("wiz-prompt-preview").textContent = txt;
    $("wiz-neg-preview").textContent = "auto per model + LoRA guards";
  } catch (e) {
    $("wiz-prompt-preview").textContent = "preview unavailable: " + e.message;
    $("wiz-neg-preview").textContent = "auto per model + LoRA guards";
  }
}

/* intent / mode / preview wiring */
document.querySelectorAll("#wiz-intent-seg button").forEach((b) => {
  b.onclick = () => {
    WIZ_INTENT = b.dataset.intent;
    document.querySelectorAll("#wiz-intent-seg button").forEach((x) =>
      x.classList.toggle("on", x === b));
    renderWizPrompt();
  };
});
document.querySelectorAll("#wiz-mode-seg button").forEach((b) => {
  b.onclick = () => {
    WIZ_PMODE = b.dataset.pm;
    document.querySelectorAll("#wiz-mode-seg button").forEach((x) =>
      x.classList.toggle("on", x === b));
    $("wiz-custom-prompt").placeholder = b.dataset.pm === "override"
      ? "replaces the auto prompt entirely"
      : "structured: appends to the auto prompt";
    renderWizPrompt();
  };
});
$("wiz-custom-prompt").addEventListener("input", renderWizPrompt);
$("wiz-custom-negative").addEventListener("input", renderWizPrompt);
$("wiz-preview-model").onchange = renderWizPrompt;

/* summary + generate */
let wizEstTimer = null;
function wizSummary() {
  clearTimeout(wizEstTimer);
  wizEstTimer = setTimeout(doWizSummary, 250);
}
async function doWizSummary() {
  const rows = wizModelRows();
  const total = rows.reduce((s, r) => s + r.count, 0);
  const parts = rows.map((r) => {
    const mi = WIZ_MODEL_INFO[r.model] || {};
    return (mi.name || r.model) + " x" + r.count;
  }).join(" + ");
  const cb = wizCombosFromAxes();
  let estTxt = "est —";
  try {
    const est = await post("/api/training/estimate", { models: WIZ_MODELS });
    const mins = Math.round((est.minutes || 0) * 10) / 10;
    estTxt = "est ~" + mins + " min";
    const sampled = (est.rows || []).filter((r) => r.samples > 0);
    if (sampled.length) {
      const n = sampled.reduce((s, r) => s + r.samples, 0);
      estTxt += " · based on your last " + n + " renders";
    }
  } catch (e) {
    const fallback = wizModelRows().reduce((s, r) => {
      const mi = WIZ_MODEL_INFO[r.model] || {};
      return s + (mi.estimate_s || 120) * r.count;
    }, 0);
    estTxt = "est ~" + Math.round(fallback / 60) + " min (offline estimate)";
  }
  const warn = total > 120 ? '<span class="ds-warn">' + Icon.get("warning") + ' exceeds the 120 safety cap</span>' : "";
  $("wiz-summary").innerHTML =
    "<b>" + total + " images</b> · " + parts + "<br>" +
    '<span class="ds-meta-txt">' + cb.n + " prompt combinations (" +
    (cb.detail.join(" · ") || "no multiply axes") + ") · balanced distribution · " +
    estTxt + "</span> " + warn;
  $("wiz-mix-total").textContent = total + " images";
  syncWizSendCast();
}

$("wiz-generate").onclick = async () => {
  try {
    const name = $("wiz-name").value.trim();
    if (!name) return toast("give the dataset a name (step 1)", true);
    const total = wizModelRows().reduce((s, r) => s + r.count, 0);
    if (!total) return toast("no images requested — set counts in Model Mix (or add matrix looks)", true);
    if (total > 120) return toast("exceeds the 120 safety cap", true);
    const params = wizGatherParams();
    if (WIZ_MODE === "photo") {
      params.prompt = $("wiz-photo-prompt").value.trim();
      if (!params.prompt) return toast("write a prompt first", true);
      params.mode = "override";
      params.custom_prompt = params.prompt;
    }
    const cleanModels = wizModelRows();   // matrix-aware (per-look models)
    const r = await post("/api/training/generate", { mode: WIZ_MODE, params: { ...params, models: cleanModels } });
    if (!r.ok) return toast("error: " + r.error, true);
    WIZ_RUN_ID = r.run_id;
    try {
      localStorage.setItem("studio_wiz", JSON.stringify({
        mode: WIZ_MODE, name, models: WIZ_MODELS,
        seed: $("wiz-seed").value.trim(),
        single: WIZ_SINGLE, axes: WIZ_AXES, intent: WIZ_INTENT, pmode: WIZ_PMODE,
      }));
    } catch (e) { /* storage unavailable */ }
    toast("job queued — " + r.total + " images (runs on free machines now; "
      + "busy machines pick it up when their current run finishes)");
    // concurrent runs are allowed: each run gets its own lanes, and lanes on
    // busy machines queue behind the machine's current run automatically
    $("wiz-progress-txt").textContent = "queued…";
    $("wiz-stop").style.display = "";
  } catch (e) {
    toast("generate failed: " + e.message, true);
  }
};

$("wiz-stop").onclick = async () => {
  if (WIZ_RUN_ID) await post("/api/training/jobs/cancel", { run_id: WIZ_RUN_ID });
  $("wiz-stop").style.display = "none";
  toast("generation canceled");
};

setInterval(async () => {
  if (!document.getElementById("page-casting").classList.contains("active")) return;
  if (!WIZ_RUN_ID) return;
  const s = await api("/api/training/jobs/" + WIZ_RUN_ID);
  const j = (s && s.job) || {};
  if (!j || !j.status) {
    $("wiz-generate").disabled = false;
    $("wiz-stop").style.display = "none";
    WIZ_RUN_ID = null;
    refreshDatasets();
    return;
  }
  const total = j.total || 1;
  const done = j.done || 0;
  $("wiz-progress-fill").style.width = Math.round((done / total) * 100) + "%";
  $("wiz-progress-txt").textContent = done + "/" + total + " · " + j.status + (j.failed ? " · " + j.failed + " failed" : "");
  // LIVE dataset refresh while a run is active: the grid (and an open curate
  // pane on the running dataset) went stale until a manual page reload.
  if (["queued", "active"].includes(j.status) &&
      Date.now() - (window.__dsRefreshAt || 0) > 8000) {
    window.__dsRefreshAt = Date.now();
    const curName = $("cur-name") ? $("cur-name").value : "";
    const before = (CUR_DATASETS.find((d) => d.name === curName) || {})
      .candidates ? (CUR_DATASETS.find((d) => d.name === curName).candidates || []).length : 0;
    await refreshDatasets();
    if (curName) {
      const after = (CUR_DATASETS.find((d) => d.name === curName) || {})
        .candidates ? (CUR_DATASETS.find((d) => d.name === curName).candidates || []).length : 0;
      if (after !== before) loadCuration();
    }
  }
  if (!["queued", "active"].includes(j.status)) {
    $("wiz-generate").disabled = false;
    $("wiz-stop").style.display = "none";
    WIZ_RUN_ID = null;
    toast("generation finished (" + j.status + ")");
    refreshDatasets();
  }
}, 4000);

$("wiz-seed-dice").onclick = () => {
  $("wiz-seed").value = Math.floor(Math.random() * 900000) + 100000;
};

/* ======================= CURATE PANE ======================= */
let curDataset = null;
let delSelected = new Set();
let CUR_FILTER = "all";
let CUR_MODEL_F = "";

document.querySelectorAll("#cur-filter-seg button").forEach((b) => {
  b.onclick = () => {
    CUR_FILTER = b.dataset.f;
    document.querySelectorAll("#cur-filter-seg button").forEach((x) =>
      x.classList.toggle("on", x === b));
    loadCuration();
  };
});
$("cur-model-filter").onchange = () => { CUR_MODEL_F = $("cur-model-filter").value; loadCuration(); };
$("cur-source-filter").onchange = () => loadCuration();
if ($("cur-cat-filter")) $("cur-cat-filter").onchange = () => loadCuration();

$("btn-cur-load").onclick = loadCuration;
/* the batch picker: every batch, by name and kind */
function fillCurPick() {
  const sel = $("cur-pick");
  if (!sel) return;
  const cur = $("cur-type").value + "/" + $("cur-name").value;
  sel.innerHTML = '<option value="">' + (CUR_DATASETS.length ? "Pick a batch" : "No batches yet") + "</option>" +
    CUR_DATASETS.map((d) => {
      const v = d.type + "/" + d.name;
      return `<option value="${escapeHtml(v)}"${v === cur ? " selected" : ""}>${escapeHtml(d.name)} (${escapeHtml(DS_KIND[d.type] || d.type)})</option>`;
    }).join("");
}
$("cur-pick").onchange = () => {
  const v = $("cur-pick").value;
  const i = v.indexOf("/");
  $("cur-type").value = i > 0 ? v.slice(0, i) : "";
  $("cur-name").value = i > 0 ? v.slice(i + 1) : "";
  loadCuration();
};
async function loadCuration() {
  const t = $("cur-type").value, n = $("cur-name").value;
  fillCurPick();
  if (!n) {
    curDataset = null;
    $("curation-grid").innerHTML = `<div class="ds-empty"><span class="big">${Icon.get("grid")}</span><b>Pick a batch</b>` +
      (CUR_DATASETS.length ? "Choose one above to go through its pictures." : "Batches you make under Generate looks show up here.") + "</div>";
    return;
  }
  const r = await api("/api/training/datasets");
  const d = (r.datasets || []).find((x) => x.type === t && x.name === n);
  if (!d) {
    curDataset = null;
    $("curation-grid").innerHTML = `<div class="ds-empty"><b>That batch is gone</b>Pick another one above.</div>`;
    return;
  }
  curDataset = d;
  $("cur-caption").value = d.caption || "";
  $("cur-license").value = d.license || "";
  const models = new Set(Object.keys(d.models || {}));
  const mf = $("cur-model-filter");
  mf.innerHTML = '<option value="">All models</option>' +
    [...models].map((m) => `<option ${m === CUR_MODEL_F ? "selected" : ""}>${escapeHtml(m)}</option>`).join("");
  const srcFilter = $("cur-source-filter").value;
  // category = the look a still belongs to (wardrobe matrix / plates / power
  // levels), read from the recorded provenance so the operator sees each
  // clothing + weapon grouped instead of a flat wall of images
  let catMap = {};
  try {
    const cr = await api("/api/training/dataset_categories?type=" +
      encodeURIComponent(t) + "&name=" + encodeURIComponent(n));
    catMap = (cr && cr.categories) || {};
  } catch (e) { catMap = {}; }
  const catOf = (c) => {
    // meta.json is keyed by basename while candidates carry `stills/...`
    // sub-paths — accept both (the recorded category lives in `variations`)
    const v = catMap[c] || catMap[String(c).split("/").pop()] || {};
    return {
      outfit: String(v.outfit || "").trim(),
      weapon: String(v.weapon || "").trim(),
      plate: String(v.plate || "").trim(),
      level: v.level != null && v.level !== "" ? String(v.level) : "",
      place: String(v.place || "").trim(),
      model: String(v.model || "").trim(),
    };
  };
  const cats = new Set();
  (d.candidates || []).forEach((c) => {
    const k = catOf(c);
    if (k.outfit) cats.add("outfit:" + k.outfit);
    if (k.weapon) cats.add("weapon:" + k.weapon);
    if (k.plate) cats.add("plate:" + k.plate);
    if (k.level) cats.add("level:" + k.level);
  });
  const catSel = $("cur-cat-filter");
  if (catSel) {
    const cur = catSel.value;
    catSel.innerHTML = '<option value="">All looks</option>' +
      [...cats].sort().map((c) => `<option value="${escapeHtml(c)}"${c === cur ? " selected" : ""}>${escapeHtml(c.replace(":", ": "))}</option>`).join("");
  }
  const catFilter = catSel ? catSel.value : "";
  const cells = [];
  const ordered = [...(d.candidates || [])].sort((a, b) => {
    const ka = catOf(a), kb = catOf(b);
    return (ka.outfit || ka.plate || "").localeCompare(kb.outfit || kb.plate || "") ||
      ka.weapon.localeCompare(kb.weapon) || a.localeCompare(b);
  });
  let lastGroup = null;
  for (const c of ordered) {
    let keep = null;
    const st = (d.curationState && d.curationState[c]) || {};
    if (st.keep !== undefined) keep = st.keep;
    if (CUR_FILTER === "kept" && keep !== true) continue;
    if (CUR_FILTER === "rejected" && keep !== false) continue;
    if (CUR_FILTER === "pending" && keep !== null) continue;
    const src = (d.sources && d.sources[c]) || "generated";
    if (srcFilter && src !== srcFilter) continue;
    const cat = catOf(c);
    const model = cat.model || "";
    if (CUR_MODEL_F && model !== CUR_MODEL_F) continue;
    if (catFilter) {
      const key = catFilter.split(":")[0];
      const val = catFilter.slice(key.length + 1);
      if (String(cat[key] || "") !== val) continue;
    }
    const group = cat.outfit || (cat.plate ? "identity plates"
      : (cat.place ? cat.place : (cat.level ? "power level " + cat.level : "")));
    if (group && group !== lastGroup) {
      cells.push(`<div class="ds-group-head">${escapeHtml(group)}</div>`);
      lastGroup = group;
    }
    const catLabel = cat.outfit
      ? cat.outfit + (cat.weapon ? ", " + cat.weapon : ", unarmed")
      : (cat.plate || cat.place || (cat.level ? "power level " + cat.level : ""));
    cells.push(`<div class="ds-card cand${keep === true ? " kept" : keep === false ? " rejected" : ""}" data-file="${escapeHtml(c)}">
      <div class="cand-pic">
        <img loading="lazy" src="/api/dataset_img?type=${t}&name=${encodeURIComponent(n)}&file=${encodeURIComponent(c)}"
          data-zoom="${escapeHtml(c)}" title="Click to zoom" alt="">
        <span class="ds-badge cand-tag">${escapeHtml(model || "?")}${src === "imported" ? ", imported" : ""}</span>
        ${catLabel ? `<span class="ds-badge cand-look" title="The look it was made for">${escapeHtml(catLabel)}</span>` : ""}
        <label class="cand-sel" title="Select"><input type="checkbox" class="del-chk" value="${escapeHtml(c)}" aria-label="Select"><span>${Icon.get("check")}</span></label>
      </div>
      <div class="body">
        <div class="name">${escapeHtml(c.split("/").pop())}</div>
        <div class="ds-actions">
          <button class="ds-btn tiny ${keep === true ? "primary" : ""}" data-k="1">Keep</button>
          <button class="ds-btn tiny danger ${keep === false ? "on" : ""}" data-k="0">Reject</button>
          <button class="ds-btn tiny icon" data-meta="${escapeHtml(c)}" title="How it was made" aria-label="How it was made" style="margin-left:auto">${Icon.get("info")}</button>
        </div>
      </div>
    </div>`);
  }
  $("curation-grid").innerHTML = cells.join("") ||
    "<div class='ds-empty'><span class='big'>" + Icon.get("image") + "</span><b>No pictures match</b>Try another filter.</div>";
  delSelected.clear();
  $("cur-sel-count").textContent = "0 selected";
  document.querySelectorAll(".del-chk").forEach((chk) => {
    chk.onchange = () => {
      delSelected.has(chk.value) ? delSelected.delete(chk.value) : delSelected.add(chk.value);
      $("cur-sel-count").textContent = delSelected.size + " selected";
    };
  });
  document.querySelectorAll(".ds-card [data-k]").forEach((b) => {
    b.onclick = async () => {
      const c = b.closest(".ds-card").dataset.file;
      const keep = b.dataset.k === "1";
      b.closest(".ds-actions").querySelector('[data-k="1"]').classList.toggle("primary", keep);
      b.closest(".ds-actions").querySelector('[data-k="0"]').classList.toggle("on", !keep);
      await post("/api/training/curate", { type: t, name: n, keep: { [c]: keep } });
      await loadCuration();
    };
  });
  document.querySelectorAll("[data-meta]").forEach((b) => {
    b.onclick = async (e) => {
      e.stopPropagation();
      const c = b.dataset.meta;
      const m = await post("/api/training/images/meta", { type: t, name: n, file: c });
      $("cur-meta-body").textContent = m.ok ? JSON.stringify(m.meta, null, 2) : "No record of how this picture was made: it came in before the studio kept one.";
      $("cur-meta-modal").classList.remove("hidden");
    };
  });
  // image click = LIGHTBOX (zoom/pan); the "i" button keeps metadata
  document.querySelectorAll("[data-zoom]").forEach((im) => {
    im.onclick = (e) => {
      e.stopPropagation();
      const c = im.dataset.zoom;
      lbOpen("/api/dataset_img?type=" + encodeURIComponent(t) +
             "&name=" + encodeURIComponent(n) +
             "&file=" + encodeURIComponent(c), c);
    };
  });
}

/* --------------------------------------------------- dataset lightbox */
const LB = { img: null, stage: null, scale: 1, tx: 0, ty: 0, drag: null };

function lbApply() {
  if (!LB.img) return;
  LB.img.style.transform = "translate(" + LB.tx + "px," + LB.ty +
    "px) scale(" + LB.scale + ")";
  if (LB.stage) LB.stage.style.cursor = LB.scale > 1.01 ? "grab" : "zoom-in";
}

function lbOpen(url, caption) {
  const modal = $("lb-modal");
  if (!modal) return;
  LB.img = $("lb-img");
  LB.stage = $("lb-stage");
  LB.scale = 1; LB.tx = 0; LB.ty = 0; LB.drag = null;
  LB.img.src = url;
  $("lb-title").textContent = caption || "Preview";
  $("lb-cap").textContent = "click to zoom · wheel to zoom · drag to pan · Esc closes";
  lbApply();
  modal.classList.remove("hidden");
}

function lbClose() {
  const modal = $("lb-modal");
  if (modal) modal.classList.add("hidden");
  LB.img = null; LB.stage = null; LB.drag = null;
}
$("lb-close").onclick = lbClose;
$("lb-zoom-in").onclick = function () {
  LB.scale = Math.min(8, LB.scale * 1.3); lbApply();
};
$("lb-zoom-out").onclick = function () {
  LB.scale = Math.max(1, LB.scale / 1.3);
  if (LB.scale <= 1.01) { LB.tx = 0; LB.ty = 0; }
  lbApply();
};
$("lb-modal").addEventListener("click", function (e) {
  if (e.target === $("lb-modal")) lbClose();
});
$("lb-stage").onclick = function (e) {
  e.stopPropagation();
  if (LB.drag && LB.drag.moved > 4) return;   // was a pan, not a click
  if (LB.scale > 1.01) { LB.scale = 1; LB.tx = 0; LB.ty = 0; }
  else { LB.scale = 2.5; }
  lbApply();
};
$("lb-stage").onwheel = function (e) {
  e.preventDefault();
  LB.scale = Math.max(1, Math.min(8, LB.scale * (e.deltaY < 0 ? 1.15 : 0.87)));
  if (LB.scale <= 1.01) { LB.tx = 0; LB.ty = 0; }
  lbApply();
};
$("lb-stage").onmousedown = function (e) {
  e.preventDefault();
  LB.drag = { x: e.clientX, y: e.clientY, ox: LB.tx, oy: LB.ty, moved: 0 };
};
document.addEventListener("mousemove", function (e) {
  if (!LB.drag || !LB.img) return;
  LB.drag.moved += Math.abs(e.movementX || 0) + Math.abs(e.movementY || 0);
  LB.tx = LB.drag.ox + (e.clientX - LB.drag.x);
  LB.ty = LB.drag.oy + (e.clientY - LB.drag.y);
  lbApply();
});
document.addEventListener("mouseup", function () {
  if (LB.drag) setTimeout(function () { LB.drag = null; }, 0);
});
document.addEventListener("keydown", function (e) {
  if (e.key === "Escape" && LB.img) lbClose();
});
$("cur-meta-close").onclick = () => $("cur-meta-modal").classList.add("hidden");
$("cur-meta-modal").addEventListener("click", (e) => {
  if (e.target === $("cur-meta-modal")) $("cur-meta-modal").classList.add("hidden");
});
$("btn-cur-regen-sel").onclick = async () => {
  if (!curDataset || !delSelected.size) return toast("Select some pictures first", true);
  const okRegen = await Dlg.confirm({ title: "Make these again?", message: "Each of the " + delSelected.size + " selected pictures is made again with its own model, prompt and seed.", okLabel: "Make again" });
  if (!okRegen) return;
  let okCount = 0;
  for (const f of delSelected) {
    const r = await post("/api/training/images/regenerate", { type: curDataset.type, name: curDataset.name, file: f });
    if (r.ok) okCount++;
  }
  toast(okCount + (okCount === 1 ? " picture" : " pictures") + " queued to be made again");
  delSelected.clear();
};
$("btn-cur-keep-sel").onclick = () => bulkCurate(true);
$("btn-cur-reject-sel").onclick = () => bulkCurate(false);
async function bulkCurate(keep) {
  if (!curDataset || !delSelected.size) return toast("Select some pictures first", true);
  const keepMap = {};
  delSelected.forEach((c) => { keepMap[c] = keep; });
  await post("/api/training/curate", { type: curDataset.type, name: curDataset.name, keep: keepMap });
  toast(keep ? "Kept" : "Rejected", false);
  delSelected.clear();
  loadCuration();
}
$("btn-cur-save").onclick = async () => {
  if (!curDataset) return toast("Pick a batch first", true);
  await post("/api/training/curate", { type: curDataset.type, name: curDataset.name, caption: $("cur-caption").value, license: $("cur-license").value });
  toast("Caption and licence saved");
};
$("btn-cur-autofilter").onclick = async () => {
  if (!curDataset) return toast("Pick a batch first", true);
  const r = await post("/api/training/autofilter", { type: $("cur-type").value, name: $("cur-name").value });
  toast(r.ok ? `Kept ${r.kept ?? "?"} of ${r.scored ?? "?"} pictures` : "Auto-pick failed: " + (r.error || JSON.stringify(r)), !r.ok);
  loadCuration();
};
$("btn-cur-clear").onclick = async () => {
  if (!curDataset) return toast("Pick a batch first", true);
  const okClear = await Dlg.confirm({ title: "Clear this batch?", message: "Every picture in <b>" + escapeHtml(curDataset.name) + "</b> is removed.", okLabel: "Clear the batch", danger: true });
  if (!okClear) return;
  await post("/api/training/dataset/clear", { type: curDataset.type, name: curDataset.name });
  toast("Batch cleared");
  refreshDatasets();
  loadCuration();
};
$("btn-del-selected").onclick = async () => {
  if (!curDataset || !delSelected.size) return toast("Select some pictures first", true);
  const okDel = await Dlg.confirm({ title: "Delete these pictures?", message: "The " + delSelected.size + " selected pictures are deleted from the batch.", okLabel: "Delete", danger: true });
  if (!okDel) return;
  const r = await post("/api/training/dataset/delete_files", { type: curDataset.type, name: curDataset.name, files: [...delSelected] });
  delSelected.clear();
  toast(r.removed + (r.removed === 1 ? " picture" : " pictures") + " deleted");
  refreshDatasets();
  loadCuration();
};
$("btn-train-cta").onclick = () => {
  if (!curDataset) return toast("Pick a batch first", true);
  openTrainConfig(curDataset.type, curDataset.name, curDataset.kept || 0);
};

/* ======================= TRAIN CONFIG MODAL ======================= */
let TC_DATASET = null;
let TC_PRESETS = {};
(async function loadTrainPresets() {
  try { TC_PRESETS = (await api("/api/training/presets")).presets || {}; }
  catch (e) { TC_PRESETS = {}; }
})();
function openTrainConfig(type, name, kept) {
  TC_DATASET = { type, name };
  $("tc-dataset").textContent = name + " (" + type + ")";
  $("tc-type").textContent = type;
  $("tc-idname").value = name;
  const p = TC_PRESETS[type] || {};
  $("tc-steps").value = p.steps || "";
  $("tc-rank").value = p.rank || "";
  $("tc-lr").value = p.lr != null ? p.lr : "";
  $("tc-resume").checked = false;
  $("tc-hint").textContent = kept > 0
    ? "Trains on " + kept + " kept image" + (kept === 1 ? "" : "s")
    : "No images kept yet — trains on ALL images in the dataset";
  $("train-confirm-modal").classList.remove("hidden");
}
$("tc-close").onclick = () => $("train-confirm-modal").classList.add("hidden");
$("tc-go").onclick = async () => {
  if (!TC_DATASET) return;
  const body = {
    type: TC_DATASET.type, dataset: TC_DATASET.name,
    steps: parseInt($("tc-steps").value) || null,
    rank: parseInt($("tc-rank").value) || null,
    lr: $("tc-lr").value ? parseFloat($("tc-lr").value) : null,
    id_name: $("tc-idname").value.trim() || null,
    resume: $("tc-resume").checked,
  };
  $("tc-go").disabled = true;
  const r = await post("/api/training/start", body);
  $("tc-go").disabled = false;
  toast(r.ok ? "training started: " + r.run_id : "error: " + r.error, !r.ok);
  if (r.ok) {
    $("train-confirm-modal").classList.add("hidden");
    trainGo("train");
    refreshRuns();
  }
};

/* ======================= TRAIN PANE ======================= */
async function refreshTrainDatasets() {
  const r = await api("/api/training/datasets");
  const sel = $("run-dataset");
  sel.innerHTML = '<option value="">— select —</option>' +
    (r.datasets || [])
      .map((d) => `<option value="${d.type}/${d.name}">${escapeHtml(d.name)} (${d.type}, ${d.kept} kept${d.kept === 0 ? " → trains all" : ""})</option>`).join("");
}
$("btn-train-start").onclick = async () => {
  const v = $("run-dataset").value;
  if (!v) return toast("select a curated dataset first", true);
  const [type, dataset] = v.split("/");
  const body = {
    type: $("run-type").value || type, dataset,
    steps: parseInt($("run-steps").value) || null,
    rank: parseInt($("run-rank").value) || null,
    lr: $("run-lr").value ? parseFloat($("run-lr").value) : null,
    id_name: $("cur-idname").value || null,
    resume: $("run-resume").checked,
  };
  const r = await post("/api/training/start", body);
  toast(r.ok ? "training started: " + r.run_id : "error: " + r.error, !r.ok);
  refreshRuns();
};
$("btn-train-stop").onclick = async () => { await post("/api/training/stop"); refreshRuns(); };
setInterval(async () => {
  if (!document.getElementById("page-casting").classList.contains("active")) return;
  const tp = document.getElementById("pane-train");
  if (!tp || !tp.classList.contains("active")) return;
  refreshRuns();
}, 3000);
async function refreshRuns() {
  const r = await api("/api/training/runs");
  const runs = Object.values(r.runs || {});
  const active = runs.find((x) => x.status === "running");
  $("train-progress").textContent = active
    ? `step ${active.step || 0}/${active.total_steps || "?"} · loss ${active.loss ?? "?"} · ${active.it_s ?? "?"} it/s · ETA ${active.eta_s ? Math.round(active.eta_s / 60) + "min" : "?"}`
    : "idle";
  const tb = $("runs-table").querySelector("tbody");
  tb.innerHTML = runs.slice().reverse().map((x) => `
    <tr><td>${esc(x.id)}</td><td>${esc(x.type)}</td><td>${esc(x.dataset)}</td>
    <td>${esc(x.step ?? "")}/${esc(x.total_steps ?? x.steps ?? "")}</td><td>${esc(x.it_s ?? "")}</td>
    <td>${esc(x.loss ?? "")}</td><td>${esc(x.status)}</td>
    <td>${x.validate_ok === true ? "✔" : x.validate_ok === false ? "✘" : "-"}</td>
    <td>${esc(x.verdict ?? "-")}</td>
    <td>${x.verdict ? "" : `<button class="ds-btn tiny" data-run-act="verdict" data-run-id="${esc(x.id)}">verdict</button>`}
        ${x.log ? `<button class="ds-btn tiny" data-run-act="logs" data-run-id="${esc(x.id)}" data-run-log="${esc(x.log)}">logs</button>` : ""}</td></tr>`).join("") ||
    "<tr><td colspan='10' class='muted'>no training runs yet</td></tr>";
  // data-* + one delegated handler: server strings never reach inline JS
  tb.onclick = (e) => {
    const b = e.target.closest("[data-run-act]");
    if (!b) return;
    if (b.dataset.runAct === "verdict") window.runVerdict(b.dataset.runId);
    else window.runLogs(b.dataset.runId, b.dataset.runLog);
  };
}
window.runLogs = async (id, logPath) => {
  let p = String(logPath || "");
  try { p = decodeURIComponent(p); } catch (_) { /* raw path with a bare % */ }
  const name = p.split(/[\\/]/).pop().replace(/\.log$/, "");
  const r = await api("/api/logs?name=" + encodeURIComponent(name));
  $("cur-meta-body").textContent = (r.lines || []).join("\n") || "(empty log)";
  $("cur-meta-modal").classList.remove("hidden");
};
window.runVerdict = async (id) => {
  const v = await Dlg.prompt({ title: "Verdict", label: "Verdict for " + escapeHtml(id) + " (e.g. A, B, pass, fail):", okLabel: "Save" });
  if (v) { await post("/api/training/verdict", { run_id: id, verdict: v }); refreshRuns(); }
};

/* ======================= MODELS PANE ======================= */
async function refreshModels() {
  const l = await api("/api/lora");
  const entries = Object.entries(l.lora || {});
  const el = $("models-grid");
  if (!entries.length) {
    el.innerHTML = `<div class="ds-empty"><span class="big">${Icon.get("box")}</span>No trained LoRAs yet — finish a training run and it appears here.</div>`;
    return;
  }
  el.innerHTML = entries.map(([f, m]) => {
    const archived = m.status === "archived";
    return `<div class="ds-card">
      <div class="ds-menu">
        <button title="Copy path" data-act="path">${Icon.get("folder")}</button>
        <button title="Export copy" data-act="export">${Icon.get("download")}</button>
        <button title="Rename" data-act="rename">${Icon.get("pen")}</button>
        <button title="${archived ? "Unarchive" : "Archive"}" data-act="arch">${Icon.get("box")}</button>
        <button title="Delete" data-act="del" class="danger">${Icon.get("x")}</button>
      </div>
      <div class="body">
        <div class="name">${escapeHtml(f)}</div>
        <div class="meta"><span class="ds-badge ${m.type || "lora"}">${escapeHtml(m.type || "lora")}</span>
          <span class="ds-badge ${archived ? "" : "films"}">${m.status}</span></div>
        <div class="meta">
          <span class="ds-meta-txt">${m.char_id ? "char: " + escapeHtml(m.char_id) : ""}${m.power_id ? "power: " + escapeHtml(m.power_id) : ""}${m.location ? "env: " + escapeHtml(m.location) : ""}</span>
        </div>
        <div class="meta"><span class="ds-meta-txt">${m.steps ? m.steps + " steps" : ""}${m.trained_at ? " · " + new Date(m.trained_at * 1000).toLocaleDateString() : ""}</span></div>
        <div class="meta"><span class="ds-meta-txt">verdict: ${m.verdict ?? "—"}</span></div>
        <div class="ds-actions">
          <span class="ds-meta-txt">${archived ? "archived — not auto-applied" : "auto-applies at render"}</span>
        </div>
      </div>
    </div>`;
  }).join("");
  el.querySelectorAll(".ds-menu button").forEach((b) => {
    const f = b.closest(".ds-card").querySelector(".name").textContent;
    const act = b.dataset.act;
    b.onclick = async () => {
      if (act === "path") {
        await navigator.clipboard.writeText("models/loras/" + f);
        toast("path copied");
      } else if (act === "export") {
        const r = await post("/api/lora/export", { filename: f });
        toast(r.ok ? "exported: " + r.path : "error: " + r.error, !r.ok);
      } else if (act === "rename") {
        const nn = await Dlg.prompt({ title: "Rename LoRA", label: "New name for " + escapeHtml(f) + ":", value: f });
        if (nn && nn.trim()) {
          const r = await post("/api/lora/rename", { filename: f, new_name: nn.trim() });
          toast(r.ok ? "renamed to " + r.new_name : "error: " + r.error, !r.ok);
          refreshModels();
        }
      } else if (act === "arch") {
        const archived = b.title === "Unarchive";
        await post(archived ? "/api/lora/unarchive" : "/api/lora/archive", { filename: f });
        refreshModels();
      } else if (act === "del") {
        const okDel = await Dlg.confirm({ title: "Delete LoRA", message: "Delete LoRA <b>" + escapeHtml(f) + "</b>?", okLabel: "Delete", danger: true });
        if (!okDel) return;
        await post("/api/lora/delete", { filename: f });
        refreshModels();
      }
    };
  });
}

/* ======================= generate-more (continuation) ======================= */
async function genMore(type, name) {
  const r = await api("/api/training/datasets");
  const ds = (r.datasets || []).find((x) => x.type === type && x.name === name);
  $("wiz-name").value = name;
  const mode = { face: "character", power: "power", environment: "environment" }[type] || "photo";
  WIZ_MODE = mode;
  document.querySelectorAll("#wiz-type-seg button").forEach((b) =>
    b.classList.toggle("on", b.dataset.mode === mode));
  wizDefaults();
  const meta = (ds && ds.meta) || {};
  const p = meta._params || {};
  if (p && typeof p === "object") {
    const S = WIZ_SINGLE[mode] || {};
    Object.entries(p).forEach(([k, v]) => {
      if (typeof v === "string" && S[k] !== undefined) S[k] = v;
    });
  }
  if (mode === "character") {
    const em = meta._emotions || [];
    const an = meta._angles || [];
    if (em.length) WIZ_AXES.character.emotions.sel = em;
    if (an.length) WIZ_AXES.character.shots.sel = an;
  }
  renderWizSubject();
  renderWizVariation();
  $("wiz-existing").textContent = ds
    ? "existing dataset - " + ds.candidates.length + " images (new ones append)" : "";
  const dist = Object.entries(ds.models || {});
  if (dist.length) {
    WIZ_MODELS = dist.map(([m, n]) => ({ model: m, count: n }));
  } else {
    WIZ_MODELS = [{ model: "realvis", count: 10 }];
  }
  WIZ_REFS = (ds.refs || []).slice(0, 5).map((f) => ({ file: f, type: "Identity" }));
  wizRefsUI();
  wizStep(4);
  trainGo("generate");
  toast("configure models - new images append to " + name);
}

/* boot */
let ENV_PLACES = null;
async function loadEnvPlaces() {
  try {
    const r = await api("/api/training/env_options");
    ENV_PLACES = r.places || null;
    if (WIZ_MODE === "environment") renderWizVariation();
  } catch (e) { /* offline */ }
}
async function trainBoot() {
  try {
    const m = await api("/api/models");
    WIZ_MODEL_INFO = {};
    (m.models || []).forEach((mi) => { WIZ_MODEL_INFO[mi.id] = mi; });
    WIZ_LORAS = m.loras || {};
    const pm = $("wiz-preview-model");
    pm.innerHTML = Object.keys(WIZ_MODEL_INFO).map((id) =>
      `<option value="${id}">${escapeHtml((WIZ_MODEL_INFO[id] || {}).name || id)}</option>`).join("");
    if (!WIZ_MODEL_INFO.realvis) WIZ_MODEL_INFO = { realvis: { id: "realvis", name: "RealVisXL V5", estimate_s: 150 } };
    try {
      const ps = await api("/api/promptspec");
      PS = ps || {};
    } catch (e) { /* backend staged */ }
    wizDefaults();
    try {
      const saved = JSON.parse(localStorage.getItem("studio_wiz") || "{}");
      if (saved.models && saved.models.length) WIZ_MODELS = saved.models;
      if (saved.mode) {
        WIZ_MODE = saved.mode;
        document.querySelectorAll("#wiz-type-seg button").forEach((b) =>
          b.classList.toggle("on", b.dataset.mode === saved.mode));
      }
      if (saved.name) $("wiz-name").value = saved.name;
      if (saved.seed) $("wiz-seed").value = saved.seed;
      if (saved.single) {
        Object.keys(WIZ_SINGLE).forEach((md) => {
          if (saved.single[md]) WIZ_SINGLE[md] = { ...WIZ_SINGLE[md], ...saved.single[md] };
        });
      }
      if (saved.axes) {
        Object.keys(WIZ_AXES).forEach((md) => {
          const s = saved.axes[md];
          if (!s) return;
          Object.keys(s).forEach((k) => {
            if (WIZ_AXES[md][k]) WIZ_AXES[md][k] = { ...WIZ_AXES[md][k], ...s[k] };
            else WIZ_AXES[md][k] = s[k];
          });
        });
      }
      if (saved.intent) WIZ_INTENT = saved.intent;
      if (saved.pmode) WIZ_PMODE = saved.pmode;
      document.querySelectorAll("#wiz-intent-seg button").forEach((b) =>
        b.classList.toggle("on", b.dataset.intent === WIZ_INTENT));
      document.querySelectorAll("#wiz-mode-seg button").forEach((b) =>
        b.classList.toggle("on", b.dataset.pm === WIZ_PMODE));
    } catch (e) { /* first run */ }
    renderWizSubject();
    renderWizVariation();
    wizStep(1);
  } catch (e) { /* boot */ }
}
loadEnvPlaces();
trainBoot();

/* --------------------------------------------------------- videos */
let VID_DATA = { films: [], scenes: [], drafts: [], raw: [] };
let VID_CAT = "all";
let VID_QUERY = "";

const VID_SECTIONS = [["films", "Films"], ["scenes", "Scenes"], ["videostudio", "Video Studio"],
                      ["drafts", "Drafts"], ["raw", "Raw renders"]];
/* "00:00:35" reads "0:35", "00:12:04" reads "12:04" */
function vidDuration(d) {
  const m = /^(\d+):(\d\d):(\d\d)/.exec(String(d || ""));
  if (!m) return String(d || "");
  const h = parseInt(m[1], 10);
  if (!h && m[2] === "00" && m[3] === "00") return "";
  return h ? h + ":" + m[2] + ":" + m[3] : parseInt(m[2], 10) + ":" + m[3];
}
function vidCard(item, cat) {
  return `<div class="ds-card" data-cat="${cat}" data-name="${escapeHtml(item.name)}">
    <div class="poster" data-play="${cat}" data-file="${escapeHtml(item.name)}">
      <img loading="lazy" src="/api/video/poster?folder=${cat}&name=${encodeURIComponent(item.name)}" alt="" onerror="this.style.display='none'">
      <div class="play">${Icon.get("play")}</div>
      ${vidDuration(item.duration) ? `<span class="dur">${escapeHtml(vidDuration(item.duration))}</span>` : ""}
    </div>
    <div class="body">
      <div class="name" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</div>
      <div class="meta">
        <span class="ds-meta-txt">${escapeHtml(item.date)}</span>
        <span class="ds-meta-txt">${item.size_mb} MB</span>
      </div>
      <div class="vid-acts">
        <a class="btn small ghost icon" href="/api/video/${cat}/${encodeURIComponent(item.name)}" download data-tip="Download">${Icon.get("download")}</a>
        <button class="btn small ghost icon" data-upscale="${escapeHtml(item.name)}" data-cat="${cat}"
          data-tip="Upscale on a worker (RIFE 2× + RealESRGAN 4×)">${Icon.get("wand")}</button>
        <span class="spacer"></span>
        <button class="btn small ghost icon vid-del" data-del="${escapeHtml(item.name)}" data-cat="${cat}" data-tip="Delete">${Icon.get("trash")}</button>
      </div>
    </div>
  </div>`;
}
const VID_EMPTY = {
  films: "Films you put together on the Film page land here.",
  scenes: "Scenes cut from rendered shots land here.",
  videostudio: "Clips and chains from Video Studio land here.",
  drafts: "A quick-play copy of each finished shot lands here.",
  raw: "Shot renders before finishing land here.",
};
function vidGrid(list, cat) {
  if (!list.length) {
    return `<div class="ds-empty"><span class="big">${Icon.get("video")}</span><b>Nothing here yet</b>${VID_EMPTY[cat] || ""}</div>`;
  }
  return `<div class="ds-grid">${list.map((i) => vidCard(i, cat)).join("")}</div>`;
}
/* the counts ride on the tabs */
function renderVidStats() {
  let total = 0;
  document.querySelectorAll("#vid-tabs .ds-tab").forEach((t) => {
    const cat = t.dataset.cat;
    const n = cat === "all" ? null : (VID_DATA[cat] || []).length;
    if (n != null) total += n;
    let c = t.querySelector(".sec-count");
    if (!c) { c = document.createElement("span"); c.className = "sec-count"; t.appendChild(c); }
    c.textContent = n == null ? "" : n;
    c.hidden = n == null;
  });
  const all = document.querySelector('#vid-tabs [data-cat="all"] .sec-count');
  if (all) { all.textContent = total; all.hidden = false; }
}
function vidFiltered(cat) {
  let list = cat === "all" ? [] : (VID_DATA[cat] || []);
  if (cat === "all") {
    return null;
  }
  if (VID_QUERY) {
    list = list.filter((i) => i.name.toLowerCase().includes(VID_QUERY));
  }
  const sort = $("vid-sort").value;
  list = list.slice().sort((a, b) => {
    if (sort === "name") return a.name.localeCompare(b.name);
    if (sort === "size") return b.size_mb - a.size_mb;
    if (sort === "old") return a.date.localeCompare(b.date);
    return b.date.localeCompare(a.date);
  });
  return list;
}
/* All: the newest four of each kind that has any */
function renderVidAll() {
  const q = VID_QUERY;
  let html = "";
  for (const [cat, title] of VID_SECTIONS) {
    let list = (VID_DATA[cat] || []).slice();
    if (q) list = list.filter((i) => i.name.toLowerCase().includes(q));
    if (!list.length) continue;
    const more = list.length > 4;
    list = list.sort((a, b) => b.date.localeCompare(a.date)).slice(0, 4);
    html += `<div class="ds-section-title">${title}<span class="sec-count">${(VID_DATA[cat] || []).length}</span>` +
      (more ? `<button class="linkish vid-more" type="button" data-more="${cat}">See all</button>` : "") + "</div>" +
      `<div class="ds-grid">${list.map((i) => vidCard(i, cat)).join("")}</div>`;
  }
  $("vid-content").innerHTML = html || (q
    ? `<div class="ds-empty"><span class="big">${Icon.get("search")}</span><b>No matches</b>Nothing is called "${escapeHtml(q)}".</div>`
    : `<div class="ds-empty"><span class="big">${Icon.get("video")}</span><b>No videos yet</b>Films, scenes and Video Studio clips land here once they render.</div>`);
  $("vid-content").querySelectorAll("[data-more]").forEach((b) => {
    b.onclick = () => { const t = document.querySelector(`#vid-tabs [data-cat="${b.dataset.more}"]`); if (t) t.click(); };
  });
}
async function upscaleOnWorker(folder, name) {
  let fleet = [];
  try {
    const r = await api("/api/workers");
    fleet = ((r && r.workers) || []).filter((w) => {
      const caps = w.capabilities || {};
      const roles = caps.roles || [];
      return w.online && !caps.blocked &&
        (roles.length === 0 || roles.indexOf("upscale") >= 0);
    });
  } catch (e) { /* no fleet */ }
  if (!fleet.length) {
    toast("No worker PC with the upscale role is online. Set one up under Worker PCs.", true);
    return;
  }
  const choice = await Dlg.choose({
    title: "Upscale on a worker PC",
    message: "Send <b>" + escapeHtml(name) + "</b> to a worker for " +
      "RIFE 2x + RealESRGAN x4. The result lands next to the original as " +
      "<i>_upscaled.mp4</i>. Nothing changes if the worker is offline.",
    choices: fleet.map((w) => ({
      label: (w.name || w.id) + " (" + w.id + ")", value: w.id,
    })).concat([{ label: "Cancel", value: null }]),
  });
  if (!choice) return;
  toast("Upscaling " + name + " on " + choice + ". This takes a few minutes.");
  try {
    const r = await post("/api/upscale/file",
      { folder: folder, name: name, worker: choice }, 7200000);
    if (r && r.ok) {
      toast("Upscaled: " + r.name);
      if (typeof refreshVideos === "function") refreshVideos();
    } else {
      toast((r && r.error) || "The upscale failed", true);
    }
  } catch (e) {
    toast("The upscale failed: " + e.message, true);
  }
}

function refreshVideos() {
  try {
    api("/api/outputs").then((o) => {
      VID_DATA = o;
      renderVidStats();
      if (VID_CAT === "all") renderVidAll();
      else renderVidCategory(VID_CAT);
    }).catch(() => {});
  } catch (e) { /* boot */ }
}
function renderVidCategory(cat) {
  const list = vidFiltered(cat);
  if (list === null) { renderVidAll(); return; }
  $("vid-content").innerHTML = vidGrid(list, cat);
  if (!list.length && VID_QUERY) {
    $("vid-content").innerHTML =
      `<div class="ds-empty"><span class="big">${Icon.get("search")}</span><b>No matches</b>Nothing is called "${escapeHtml(VID_QUERY)}".</div>`;
  }
}
document.querySelectorAll("#vid-tabs .ds-tab").forEach((t) => {
  t.onclick = () => {
    VID_CAT = t.dataset.cat;
    document.querySelectorAll("#vid-tabs .ds-tab").forEach((x) =>
      x.classList.toggle("active", x === t));
    renderVidCategory(VID_CAT);
  };
});
$("vid-search").addEventListener("input", (e) => {
  VID_QUERY = e.target.value.trim().toLowerCase();
  renderVidCategory(VID_CAT);
});
$("vid-sort").onchange = () => renderVidCategory(VID_CAT);

document.addEventListener("click", (e) => {
  const play = e.target.closest("[data-play]");
  if (play) {
    const cat = play.dataset.play;
    const file = play.dataset.file;
    $("vid-player-video").src =
      `/api/video/${cat}/${encodeURIComponent(file)}`;
    $("vid-player").classList.remove("hidden");
    return;
  }
  const del = e.target.closest("[data-del]");
  if (del) {
    const cat = del.dataset.cat || "raw";
    Dlg.confirm({ title: "Delete this video?", message: "<b>" + escapeHtml(del.dataset.del) + "</b> is removed from this PC.", okLabel: "Delete", danger: true })
      .then((ok) => {
        if (ok) post("/api/videos/delete", { folder: cat, name: del.dataset.del })
          .then((r) => {
            if (r && r.ok === false) toast(r.error || "The video was not deleted", true);
            refreshVideos();
          });
      });
    return;
  }
  const ups = e.target.closest("[data-upscale]");
  if (ups) {
    upscaleOnWorker(ups.dataset.cat || "raw", ups.dataset.upscale);
    return;
  }
  const vsUp = e.target.closest("[data-vs-up]");
  if (vsUp) {
    upscaleOnWorker("videostudio", vsUp.dataset.vsUp);
    return;
  }
});
$("vid-player-close").onclick = () => {
  $("vid-player").classList.add("hidden");
  $("vid-player-video").pause();
  $("vid-player-video").src = "";
};
$("vid-player").addEventListener("click", (e) => {
  if (e.target === $("vid-player")) $("vid-player-close").click();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("vid-player").classList.contains("hidden")) {
    $("vid-player-close").click();
  }
});

/* --------------------------------------------------------- settings */
/* Four panes: the studio configuration (settings.yaml as JSON), the model
   files (where each weight file is, the model folders, and the model files a
   user already has), the global bible files (universe, crowd types, legacy
   characters / locations / scenes; every save is a git commit) and this
   browser's API token. */
document.querySelectorAll("#set-tabs button").forEach((b) => {
  b.addEventListener("click", () => setSettingsPane(b.dataset.pane));
});
let SET_PANE = "models";         // the pane Settings opens on
function setSettingsPane(pane) {
  SET_PANE = pane;
  document.querySelectorAll("#set-tabs button").forEach((b) =>
    b.classList.toggle("on", b.dataset.pane === pane));
  document.querySelectorAll("#page-settings .set-pane").forEach((p) =>
    p.classList.toggle("hidden", p.dataset.pane !== pane));
  if (pane === "bible") loadBibleFiles();
  if (pane === "models") loadModelFiles();
  // shown again unedited: the current settings (model folders may have changed)
  if (pane === "config" && $("settings-yaml").value === SETTINGS_TEXT) loadSettings();
  if (pane === "access") renderTokenSettings();
}

function renderTokenSettings() {
  const inp = $("settings-token-input");
  if (inp) inp.value = window.Auth.getToken();
}
$("btn-settings-token-toggle").onclick = () => {
  const inp = $("settings-token-input");
  const isPwd = inp.type === "password";
  inp.type = isPwd ? "text" : "password";
  $("btn-settings-token-toggle").textContent = isPwd ? "Hide" : "Show";
};
$("btn-settings-token-save").onclick = () => {
  const val = $("settings-token-input").value.trim();
  if (!val) {
    window.Auth.clearToken();
    toast("API token cleared");
  } else if (window.Auth.setToken(val)) {
    toast("API token saved");
  } else {
    // "********" is the redaction placeholder from GET /api/settings
    toast("That is the hidden placeholder. Paste the real token from tool/settings.yaml.", true);
  }
};
$("btn-settings-token-clear").onclick = () => {
  window.Auth.clearToken();
  $("settings-token-input").value = "";
  toast("API token cleared");
};

let SETTINGS_TEXT = "";          // the document as loaded, to tell an edited one apart
async function loadSettings() {
  const s = await api("/api/settings");
  SETTINGS_TEXT = JSON.stringify(s.settings || s, null, 2);
  $("settings-yaml").value = SETTINGS_TEXT;
}
/* Model folders change on the Model files pane, and through the installer, while
   this document is open: the saved document keeps the current list unless its
   own list was edited (a save replaces the whole settings file). */
async function settingsWithCurrentFolders(doc) {
  const dirs = (d) => JSON.stringify(((d || {}).paths || {}).extra_model_dirs || []);
  let base;
  try { base = JSON.parse(SETTINGS_TEXT || "{}"); } catch (e) { return doc; }
  if (!doc.paths || dirs(doc) !== dirs(base)) return doc;
  const now = await api("/api/settings");
  const cur = (now.settings || now).paths || {};
  if (dirs({ paths: cur }) !== dirs(base)) doc.paths.extra_model_dirs = cur.extra_model_dirs;
  return doc;
}
$("btn-settings-format").onclick = () => {
  try {
    $("settings-yaml").value = JSON.stringify(JSON.parse($("settings-yaml").value), null, 2);
    toast("Tidied");
  } catch (e) { toast("That is not valid JSON: " + e.message, true); }
};
$("btn-settings-save").onclick = async () => {
  let parsed;
  try { parsed = JSON.parse($("settings-yaml").value); }
  catch (e) { toast("That is not valid JSON: " + e.message, true); return; }
  try {
    // secrets come back from GET as "********"; the server keeps the stored
    // value for any placeholder left untouched
    const r = await post("/api/settings", { settings: await settingsWithCurrentFolders(parsed) });
    toast(r.ok ? "Settings saved" : "Not saved: " + r.error, !r.ok);
    if (r.ok) loadSettings();
  } catch (e) { toast("Settings not saved: " + e.message, true); }
};

/* Model files (routers/model_folders.py, tool/model_files.py). Folders are
   typed as Windows shows them; the server reads them, so no JSON escaping. */
let mfPollTimer = null;
const MF_STATE = { running: "searching", done: "done", failed: "failed", idle: "" };
function mfGB(bytes) {
  if (!bytes) return "";
  return (bytes / 1e9).toFixed(bytes < 1e9 ? 2 : 1) + " GB";
}
function mfWhere(f) {
  if (f.state === "ok") {
    if (f.how === "link" || f.how === "symlink") return "linked to " + esc(f.from);
    if (f.how === "drivelink") return "linked to " + esc(f.from) +
      "<br><span class='muted'>the link: " + esc(f.where) + "</span>";
    if (f.how === "copy") return "copied from " + esc(f.from);
    if (f.how === "folder") return "in " + esc(f.where);
    return "in Kinoloom's models folder";
  }
  if (f.state === "other build") return esc(f.where) + " has another size (another build)";
  if (f.state === "letter case") return "saved as " + esc(f.where) +
    ": Find, or the next start of Kinoloom, gives it the letter case Kinoloom loads";
  let s = f.manual ? "missing: you bring this one" : (f.made_here ? "missing: the installer makes it" : "missing");
  (f.other_builds || []).forEach((p) => { s += "<br><span class='muted'>" + esc(p) + " has another size (another build)</span>"; });
  return s;
}
async function loadModelFiles() {
  let r;
  try { r = await api("/api/model-files"); }
  catch (e) { $("mf-summary").textContent = "could not load: " + e.message; return; }
  renderMfFolders(r.folders || []);
  renderMfFiles(r.files || []);
  renderMfJob(r.job || {});
  if (r.job && r.job.state === "running") mfPoll();
}
function renderMfFolders(folders) {
  const box = $("mf-folders");
  if (!folders.length) {
    box.innerHTML = '<span class="muted">No model folders yet: Kinoloom reads only its own models folder.</span>';
    return;
  }
  box.innerHTML = folders.map((f, i) =>
    '<div class="ctl-row" style="margin:4px 0"><span class="mono">' + esc(f.path) + "</span>" +
    (f.exists ? "" : ' <span class="pill paused">not found</span>') +
    '<span class="spacer"></span><button class="btn small" type="button" data-mf-remove="' + i + '">Remove</button></div>'
  ).join("");
  box.querySelectorAll("[data-mf-remove]").forEach((b) => {
    b.onclick = () => mfFolder(folders[+b.dataset.mfRemove].path, "remove");
  });
}
function renderMfFiles(files) {
  const have = files.filter((f) => f.state === "ok").length;
  $("mf-summary").textContent = have + " of " + files.length + " files in place";
  const parts = [];
  files.forEach((f) => { const p = f.parts[0]; if (parts.indexOf(p) < 0) parts.push(p); });
  let html = "<thead><tr><th>File</th><th>Size</th><th>Where</th></tr></thead><tbody>";
  parts.forEach((p) => {
    html += '<tr><td colspan="3" class="muted small" style="padding-top:14px"><b>' + esc(p) + "</b></td></tr>";
    files.filter((f) => f.parts[0] === p).forEach((f) => {
      const cls = f.state === "ok" ? "on" : (f.state === "missing" ? "off" : "paused");
      html += '<tr><td class="mono small">' + esc(f.file) + '</td><td class="small" style="white-space:nowrap">' +
        mfGB(f.size) + '</td><td class="small"><span class="pill ' + cls + '">' +
        (f.state === "ok" ? "ok" : esc(f.state)) + "</span> " + mfWhere(f) + "</td></tr>";
    });
  });
  $("mf-table").innerHTML = html + "</tbody>";
}
function renderMfJob(job) {
  const box = $("mf-job");
  if (!job || !job.kind) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  const st = $("mf-job-state");
  st.className = "pill " + (job.state === "running" ? "run" : (job.state === "failed" ? "paused" : "on"));
  st.textContent = (job.kind === "copy" ? (job.state === "running" ? "copying" : "copy " + MF_STATE[job.state])
    : MF_STATE[job.state]) || job.state;
  const bits = [];
  if ((job.found || []).length) bits.push(job.found.length + " file(s) now in use");
  if ((job.added || []).length) bits.push("model folder added: restart Kinoloom so its render engines see it");
  if (job.error) bits.push(job.error);
  $("mf-job-sum").textContent = bits.join(" · ");
  const log = $("mf-job-log");
  log.textContent = (job.lines || []).join("\n");
  log.scrollTop = log.scrollHeight;
  const cp = $("mf-copy");
  if ((job.copy || []).length && job.state !== "running") {
    const one = job.copy.length === 1;
    $("mf-copy-text").textContent = (one
      ? "1 file can't be linked from where it is (the log above says why), so Kinoloom can use it only as a copy. Copying it takes "
      : job.copy.length + " files can't be linked from where they are (the log above says why), so Kinoloom can use them only as copies. Copying them takes ") +
      mfGB(job.copy.reduce((n, c) => n + (c.size || 0), 0)) + " on Kinoloom's drive. With Windows Developer Mode on " +
      "(Settings > System > For developers), Find links " + (one ? "it" : "them") + " instead, with no extra space.";
    $("btn-mf-copy").textContent = one ? "Copy it" : "Copy them";
    cp.classList.remove("hidden");
  } else {
    cp.classList.add("hidden");
  }
  $("btn-mf-find").disabled = job.state === "running";
}
function mfPoll() {
  clearTimeout(mfPollTimer);
  mfPollTimer = setTimeout(async () => {
    let job;
    try { job = await api("/api/model-files/job"); } catch (e) { return; }
    renderMfJob(job);
    if (job.state === "running") mfPoll();
    else loadModelFiles();
  }, 1500);
}
async function mfFolder(folder, action) {
  try {
    const r = await post("/api/model-files/folders", { folder: folder, action: action });
    toast(/^(added|removed) /.test(r.message)
      ? r.message + " · restart Kinoloom so its render engines see it" : r.message);
    if (action === "add") $("mf-folder-input").value = "";
    loadModelFiles();
  } catch (e) { toast(e.message, true); }
}
$("btn-mf-folder-add").onclick = () => mfFolder($("mf-folder-input").value, "add");
$("btn-mf-find").onclick = async () => {
  try {
    const r = await post("/api/model-files/find", { folder: $("mf-find-input").value });
    renderMfJob(r.job);
    mfPoll();
  } catch (e) { toast(e.message, true); }
};
$("btn-mf-copy").onclick = async () => {
  try {
    const r = await post("/api/model-files/copy", {});
    renderMfJob(r.job);
    mfPoll();
  } catch (e) { toast(e.message, true); }
};
$("btn-mf-refresh").onclick = () => loadModelFiles();

/* global bible files (projects edit their own cast on the Workspace's
   Cast & World page) */
const BIBLE_DEFAULTS = {
  universe: `genre: sci-fi-fantasy\ntone: epic, grounded\ngrade: cinematic_teal_amber.lut\ncinematography:\n  default_lens: "35mm anamorphic"\n  default_film_stock: "Kodak Vision3 500T"\n  color_temp_key: 5600K\n  color_temp_fill: 3200K\n  depth_of_field: "shallow, f/2.0"\n  grain: "fine cinematic grain"`,
  character: `id: \nname: \ntier: main\nbody: ""\noutfit: ""\npowers:\nvoice:\nemotion_palette: []\narc_summary: ""`,
  location: `id: \ndescriptor: ""\nlighting: ""`,
  scene: `id: \nlocation: \ntime: \nbeats:\n  - ""\nmusic_mood: ""`,
  crowd: `crowd_types:\n  guards:\n    tier: crowd\n    descriptor: "palace guards in silver armor"`,
};
function bibleNeedsId() {
  return ["character", "location", "scene"].indexOf($("bible-kind").value) >= 0;
}
// universe and crowd types load as soon as they are picked; a character,
// place or scene waits for its id and Load
$("bible-kind").onchange = () => {
  $("bible-id").classList.toggle("hidden", !bibleNeedsId());
  if (!bibleNeedsId()) $("btn-bible-load").click();
  else { $("bible-yaml").value = ""; $("bible-id").focus(); }
};
$("btn-bible-load").onclick = async () => {
  const kind = $("bible-kind").value;
  const b = await api("/api/bible");
  if (!bibleNeedsId()) {
    $("bible-yaml").value = JSON.stringify(kind === "universe" ? b.universe : b.crowd_types, null, 2);
    return;
  }
  const id = $("bible-id").value.trim();
  const list = kind === "character" ? b.characters : kind === "location" ? b.locations : b.scenes;
  const item = (list || {})[id];
  $("bible-yaml").value = item ? JSON.stringify(item, null, 2) : BIBLE_DEFAULTS[kind];
};
$("btn-bible-save").onclick = async () => {
  let data;
  try { data = JSON.parse($("bible-yaml").value); }
  catch (e) { toast("That is not valid JSON: " + e.message, true); return; }
  const r = await post("/api/bible/save", { kind: $("bible-kind").value, id: $("bible-id").value.trim() || undefined, data });
  toast(r.ok ? "Saved as version " + r.commit : "Not saved: " + r.error, !r.ok);
  loadBibleFiles();
};
async function loadBibleFiles() {
  const [b, log] = await Promise.all([api("/api/bible"), api("/api/bible/log")]);
  $("bible-commit").textContent = b && b.commit ? "version " + b.commit : "";
  $("bible-log").textContent = (log && log.log && log.log.join("\n")) || "No changes saved yet.";
  if (!$("bible-yaml").value.trim() && !bibleNeedsId()) $("btn-bible-load").click();
}

/* --------------------------------------------------------- logs */
async function refreshLogs() {
  const r = await api("/api/logs?name=" + encodeURIComponent($("log-name").value));
  const lines = r.lines || [];
  $("log-view").textContent = lines.length ? lines.join("\n") : "This log is empty.";
  $("log-view").classList.toggle("empty", !lines.length);
  $("log-view").scrollTop = $("log-view").scrollHeight;
}
$("btn-refresh-logs").onclick = refreshLogs;

/* --------------------------------------------------------- images */
/* Standalone image studio — its own mini-app inside the control room. */
const IMS_RATIOS = {
  "1024x1024": "Square", "832x1216": "Portrait", "1216x832": "Landscape",
  "768x768": "Square", "768x1344": "Tall", "1344x768": "Wide",
  "768x1152": "Portrait", "1152x768": "Landscape",
};
/* Model card names — from the backend registry (/api/images/stacks), so new
   models never need a frontend edit. "Qwen-Image 2.1 (fast, DMD 8-step)"
   reads "Qwen-Image 2.1" on its card and "Fast" on the card's switch. */
function imsShortName(m) {
  return String((m && (m.name || m.label || m.id)) || "?").replace(/\s*\(.*\)\s*$/, "");
}
function imsVariantName(m) {
  const p = /\(([^,)]+)/.exec(String((m && m.name) || ""));
  const w = p ? p[1].trim() : "Standard";
  return w.charAt(0).toUpperCase() + w.slice(1);
}
function imsHasVariants(key) {
  const st = IMS_STACKS[key] || {};
  return !!st.variant_of || Object.keys(IMS_STACKS).some((k) => IMS_STACKS[k].variant_of === key);
}
let IMS_STACKS = {};
let IMS_PRESETS = [];
let IMS_LORAS = {};          // LoRA registry from backend
let IMS_LORA_EXTRA = "";     // extra negative terms for LoRAs
let IMS_LORA_ROWS = [];      // LoRA rows the user added: {file, strength}
let IMS_REF_ROWS = [];       // active reference images: {file, type}
const IMS_REF_TYPES = ["Identity", "Wardrobe", "Style", "Pose"];
let IMS_REF_STRENGTH = 0.8;
let IMS_REF_ANCHOR = false;
let IMS_MODEL = "realvis";
let IMS_SIZE = null;
let IMS_COUNT = 1;
let IMS_ALBUM = "__all__";   // gallery filter
let IMS_ALBUMS = [];         // album names
let IMS_DATA = [];           // gallery items
let IMS_LB = null;           // lightbox item
let IMS_PRESETS_SHOWN = false;

/* Persist the user's generation settings (model/size/steps/cfg/count/hires).
   Without this a reload silently reset the model to RealVis and the sliders
   to whatever st.steps || 20 happened to be — the "same model, different
   settings" trap. */
const IMS_LS_KEY = "kinoloom.ims.params.v1";
const IMS_LS_LEGACY = "aistudio.ims.params.v1";   // saved before the rename to Kinoloom
function imsLoadParams() {
  try {
    return JSON.parse(localStorage.getItem(IMS_LS_KEY) ||
                      localStorage.getItem(IMS_LS_LEGACY) || "{}") || {};
  } catch (e) { return {}; }
}
function imsSaveParams() {
  try {
    localStorage.setItem(IMS_LS_KEY, JSON.stringify({
      model: IMS_MODEL, size: IMS_SIZE,
      steps: $("ims-steps").value ? parseInt($("ims-steps").value, 10) : null,
      cfg: $("ims-cfg").value !== "" ? parseFloat($("ims-cfg").value) : null,
      count: IMS_COUNT,
      hires: $("ims-hires").checked,
    }));
  } catch (e) { /* storage unavailable — defaults still apply */ }
}

async function imagesPage() {
  try {
    const s = await api("/api/images/stacks");
    IMS_STACKS = s.stacks || {};
    IMS_PRESETS = s.presets || [];
    const saved = imsLoadParams();
    if (saved.model && IMS_STACKS[saved.model]) IMS_MODEL = saved.model;
    // start on an installed model: the first one when the saved pick is gone
    if (!IMS_STACKS[IMS_MODEL] || IMS_STACKS[IMS_MODEL].available === false) {
      const first = Object.keys(IMS_STACKS).find((k) =>
        IMS_STACKS[k].available !== false && IMS_STACKS[k].family !== "wan");
      if (first) IMS_MODEL = first;
    }
    if (saved.size) IMS_SIZE = saved.size;
    wsRefreshSelects();   // fill the "Run on" worker selects
    try {
      const l = await api("/api/images/loras");
      IMS_LORAS = l.loras || {};
      IMS_LORA_EXTRA = l.extra_negative || "";
    } catch (e) { /* loras optional */ }
    renderImsModels();
    renderImsPresets();
    renderImsLoras();
    renderImsRefs();
    if (!IMS_SIZE || !IMS_STACKS[IMS_MODEL] || !IMS_STACKS[IMS_MODEL].sizes.includes(IMS_SIZE)) {
      IMS_SIZE = (IMS_STACKS[IMS_MODEL] && IMS_STACKS[IMS_MODEL].default_size) || "1024x1024";
    }
    syncImsControls();
    if (saved.model === IMS_MODEL) {
      if (saved.steps != null) {
        $("ims-steps").value = saved.steps;
        $("ims-steps-val").textContent = saved.steps;
      }
      if (saved.cfg != null) {
        $("ims-cfg").value = saved.cfg;
        $("ims-cfg-val").textContent = saved.cfg;
      }
      if (saved.count) imsSetCount(saved.count);
      if (saved.hires && !$("ims-hires").disabled) $("ims-hires").checked = true;
      imsGenSub();
    }
    refreshIms();
  } catch (e) { /* boot */ }
}

/* ---- LoRAs: rows the user adds and tunes; nothing is applied or
   added to the prompt automatically. SDXL models only. ---- */
function imsLorasSupported() {
  const st = IMS_STACKS[IMS_MODEL] || {};
  return !!(st.capabilities && st.capabilities.loras);
}
function loraOptions(exclude, selected) {
  let opts = '<option value="">— none —</option>';
  for (const [file, m] of Object.entries(IMS_LORAS)) {
    if (file === exclude) continue;
    opts += `<option value="${escapeHtml(file)}" ${file === selected ? "selected" : ""}>${escapeHtml(m.label)}</option>`;
  }
  return opts;
}
function renderImsLoras() {
  const list = $("ims-lora-list");
  list.innerHTML = IMS_LORA_ROWS.map((row, i) => {
    const m = IMS_LORAS[row.file] || {};
    return `<div class="ims-lora-row" data-idx="${i}">
      <div class="l-top">
        <select>${loraOptions("", row.file)}</select>
        <input type="range" min="0" max="1.5" step="0.05" value="${row.strength}">
        <output>${parseFloat(row.strength).toFixed(2)}</output>
        <button class="ims-btn danger tiny" title="remove">${Icon.get("x")}</button>
      </div>
      <div class="l-trig">${m.trigger ? "Trigger words, if you want them in the prompt: " + escapeHtml(m.trigger) : "No trigger word needed"}${m.note ? ". " + escapeHtml(m.note) : ""}</div>
    </div>`;
  }).join("");
  $("ims-lora-add").style.display =
    IMS_LORA_ROWS.length >= 6 ? "none" : "";
  list.querySelectorAll(".ims-lora-row").forEach((el) => {
    const i = parseInt(el.dataset.idx);
    const sel = el.querySelector("select");
    sel.onchange = () => {
      IMS_LORA_ROWS[i].file = sel.value;
      renderImsLoras();
    };
    const rng = el.querySelector("input[type=range]");
    rng.oninput = () => {
      IMS_LORA_ROWS[i].strength = parseFloat(rng.value);
      el.querySelector("output").textContent = parseFloat(rng.value).toFixed(2);
    };
    el.querySelector("button").onclick = () => {
      IMS_LORA_ROWS.splice(i, 1);
      renderImsLoras();
    };
  });
}
$("ims-lora-add").onclick = () => {
  const used = new Set(IMS_LORA_ROWS.map((r) => r.file));
  const free = Object.keys(IMS_LORAS).filter((f) => !used.has(f));
  if (!free.length) return toast("Every LoRA is already added", true);
  IMS_LORA_ROWS.push({ file: free[0], strength: IMS_LORAS[free[0]].strength || 0.7 });
  renderImsLoras();
};

/* ---------------- reference images (SDXL IP-Adapter) ---------------- */
function imsRefsSupported() {
  const st = IMS_STACKS[IMS_MODEL] || {};
  return !!(st.capabilities && st.capabilities.refs);
}

function renderImsRefs() {
  const sec = $("ims-ref-section");
  const list = $("ims-ref-list");
  const note = $("ims-ref-note");
  if (!sec || !list) return;
  const ok = imsRefsSupported();
  sec.classList.toggle("hidden", !ok);
  if (!ok) {
    if (note) note.textContent = "Reference images need an SDXL model (IP-Adapter). Switch to RealVisXL / CyberRealistic XL to use them.";
    return;
  }
  if (note) note.textContent = "";
  list.innerHTML = IMS_REF_ROWS.length
    ? IMS_REF_ROWS.map((r, i) => {
        const escF = escapeHtml(r.file);
        return `<div class="ims-ref-row" data-i="${i}">
          <img class="ims-ref-thumb" src="/api/images/refs/${encodeURIComponent(r.file)}" alt="ref">
          <span class="ims-ref-name" title="${escF}">${escF}</span>
          <select class="ims-ref-type" data-i="${i}">
            ${IMS_REF_TYPES.map((t) => `<option${r.type === t ? " selected" : ""}>${t}</option>`).join("")}
          </select>
          <button class="ims-btn danger tiny" data-rm-ref="${i}" title="remove">${Icon.get("x")}</button>
        </div>`;
      }).join("")
    : '<div class="ims-note">No reference pictures. Add up to 5.</div>';
  list.querySelectorAll("[data-rm-ref]").forEach((b) => {
    b.onclick = () => { IMS_REF_ROWS.splice(Number(b.dataset.rmRef), 1); renderImsRefs(); imsGenSub(); };
  });
  list.querySelectorAll(".ims-ref-type").forEach((sel) => {
    sel.onchange = () => { IMS_REF_ROWS[Number(sel.dataset.i)].type = sel.value; };
  });
}

$("ims-ref-add").onclick = () => { $("ims-ref-file").click(); };
$("ims-ref-file").onchange = async () => {
  const maxAllowed = Math.max(0, 5 - IMS_REF_ROWS.length);
  if (maxAllowed <= 0) {
    $("ims-ref-file").value = "";
    return toast("Maximum 5 reference images already added", true);
  }
  const files = Array.prototype.slice.call($("ims-ref-file").files || []).slice(0, maxAllowed);
  $("ims-ref-file").value = "";
  if (!files.length) return;
  const fd = new FormData();
  files.forEach((f) => fd.append("files", f, f.name || "ref.png"));
  try {
    const r = await api("/api/images/refs/upload", { method: "POST", body: fd });
    if (!r || !r.ok || !r.refs || !r.refs.length) {
      toast((r && r.error) ? r.error : "0 images were imported", true);
      return;
    }
    r.refs.forEach((f) => {
      if (IMS_REF_ROWS.length >= 5) return;
      IMS_REF_ROWS.push({ file: f, type: "Identity" });
    });
    renderImsRefs();
    imsGenSub();
    toast(r.refs.length + " reference image" + (r.refs.length > 1 ? "s" : "") + " added");
  } catch (err) {
    toast("reference upload failed: " + (err.message || err), true);
  }
};
$("ims-ref-strength").oninput = () => {
  IMS_REF_STRENGTH = parseFloat($("ims-ref-strength").value) || 0.8;
  $("ims-ref-strength-val").textContent = IMS_REF_STRENGTH.toFixed(2);
};
$("ims-ref-anchor").onchange = () => { IMS_REF_ANCHOR = $("ims-ref-anchor").checked; };

/* ---- model cards ---- */
/* one card per model; a variant (variant_of) is a switch on its base's card,
   and Wan 2.2 (legacy) gets a card only while it is the picked model */
function renderImsModels() {
  const el = $("ims-models");
  const keys = Object.keys(IMS_STACKS);
  const famOf = (k) => [k].concat(keys.filter((v) => IMS_STACKS[v].variant_of === k));
  const installed = (k) => famOf(k).some((v) => IMS_STACKS[v].available !== false);
  const bases = keys.filter((k) => !IMS_STACKS[k].variant_of &&
    (IMS_STACKS[k].family !== "wan" || famOf(k).includes(IMS_MODEL)))
    .sort((a, b) => installed(b) - installed(a));   // installed models first
  el.innerHTML = bases.map((base) => {
    const fam = famOf(base);
    const on = fam.includes(IMS_MODEL);
    const key = on ? IMS_MODEL : base;
    const m = IMS_STACKS[key];
    const ok = m.available !== false;
    const seg = fam.length > 1
      ? '<span class="m-seg">' + fam.map((k) =>
          `<button type="button" data-variant="${escapeHtml(k)}" class="${k === key ? "on" : ""}">${escapeHtml(imsVariantName(IMS_STACKS[k]))}</button>`).join("") + "</span>"
      : "";
    return `<div class="ims-model${on ? " selected" : ""}${ok ? "" : " off"}" data-model="${escapeHtml(key)}"
        role="radio" aria-checked="${on}" tabindex="0" title="${escapeHtml(m.description || "")}">
      <span class="m-dot"></span>
      <span class="m-body">
        <span class="m-top"><span class="m-name">${escapeHtml(imsShortName(m))}</span>
          ${m.family === "wan" ? '<span class="m-meta"><i>legacy</i></span>' : ""}${ok ? "" : '<span class="m-meta"><i class="bad">not installed</i></span>'}${seg}</span>
        <span class="m-tag">${escapeHtml(m.best_for || "")}</span>
      </span>
    </div>`;
  }).join("");
  const pick = (key) => {
    const st = IMS_STACKS[key];
    if (!st) return;
    if (st.available === false) {
      toast(imsShortName(st) + " is not installed. Install it, or point Settings at the file you already have.", true);
      return;
    }
    IMS_MODEL = key;
    IMS_SIZE = st.sizes.includes(IMS_SIZE) ? IMS_SIZE : st.default_size;
    renderImsModels();
    renderImsRatios();
    syncImsControls();
    renderImsRefs();
    imsSaveParams();
  };
  el.querySelectorAll(".ims-model").forEach((c) => {
    c.onclick = () => pick(c.dataset.model);
    c.onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(c.dataset.model); }
    };
  });
  el.querySelectorAll("[data-variant]").forEach((b) => {
    b.onclick = (e) => { e.stopPropagation(); pick(b.dataset.variant); };
  });
}

/* ---- presets ---- */
function renderImsPresets() {
  const el = $("ims-presets");
  el.innerHTML = IMS_PRESETS.map((p) =>
    `<button class="ims-preset" type="button" data-preset="${escapeHtml(p.id)}">${escapeHtml(p.title)}</button>`).join("");
  el.querySelectorAll(".ims-preset").forEach((c) => {
    c.onclick = () => {
      const p = IMS_PRESETS.find((x) => x.id === c.dataset.preset);
      if (!p) return;
      $("ims-prompt").value = p.prompt;
      imsPromptCount();
      el.querySelectorAll(".ims-preset").forEach((o) =>
        o.classList.toggle("selected", o === c));
    };
  });
}
$("ims-preset-toggle").onclick = () => {
  IMS_PRESETS_SHOWN = !IMS_PRESETS_SHOWN;
  $("ims-presets").classList.toggle("hidden", !IMS_PRESETS_SHOWN);
  $("ims-preset-toggle").classList.toggle("on", IMS_PRESETS_SHOWN);
  $("ims-preset-toggle").setAttribute("aria-expanded", String(IMS_PRESETS_SHOWN));
};

/* ---- ratios / controls ---- */
function renderImsRatios() {
  const st = IMS_STACKS[IMS_MODEL] || {};
  const sizes = st.sizes || ["1024x1024"];
  const el = $("ims-ratios");
  el.innerHTML = sizes.map((s) =>
    `<button data-size="${s}" class="${s === IMS_SIZE ? "selected" : ""}">${IMS_RATIOS[s] || s.replace("x", "×")}</button>`).join("");
  el.querySelectorAll("button").forEach((b) => {
    b.onclick = () => { IMS_SIZE = b.dataset.size; renderImsRatios(); imsGenSub(); imsSaveParams(); };
  });
}
function syncImsControls() {
  const st = IMS_STACKS[IMS_MODEL];
  renderImsRatios();
  if (!st) {
    /* the model registry could not be read — never guess defaults */
    $("ims-generate").disabled = true;
    $("ims-gen-sub").textContent = "The model list did not load. Reload the page.";
    return;
  }
  const steps = $("ims-steps");
  if (st.steps != null) steps.value = st.steps;
  $("ims-steps-val").textContent = steps.value;
  const cfg = $("ims-cfg");
  if (st.cfg != null) cfg.value = st.cfg;
  $("ims-cfg-val").textContent = cfg.value;
  /* capability-driven hires: a model that cannot use it never shows it */
  const hs = $("ims-hires");
  if (hs) {
    const hiresOk = !!(st.capabilities && st.capabilities.hires);
    hs.disabled = !hiresOk;
    if (!hiresOk) hs.checked = false;
    const row = hs.closest(".ims-tune-item");
    if (row) row.classList.toggle("hidden", !hiresOk);
  }
  /* LoRA rows only for models that take LoRAs (SDXL); the rows are kept for
     when the user switches back */
  const lorasOk = imsLorasSupported();
  $("ims-lora-manual").classList.toggle("hidden", !lorasOk);
  $("ims-lora-none").classList.toggle("hidden", lorasOk);
  $("ims-lora-fold").classList.toggle("hidden", !lorasOk);
  $("ims-negative").placeholder = "The model's own";
  $("ims-negative").title = st.negative ? "Left empty, it uses: " + st.negative : "";
  const hint = $("ims-hint");
  if (st.hint) { hint.textContent = st.hint; hint.classList.remove("hidden"); }
  else hint.classList.add("hidden");
  imsGenSub();
}
$("ims-steps").oninput = () => { $("ims-steps-val").textContent = $("ims-steps").value; imsGenSub(); imsSaveParams(); };
$("ims-cfg").oninput = () => { $("ims-cfg-val").textContent = $("ims-cfg").value; imsSaveParams(); };
function imsGenSub() {
  const st = IMS_STACKS[IMS_MODEL] || {};
  $("ims-gen-sub").textContent =
    IMS_COUNT + (IMS_COUNT > 1 ? " pictures" : " picture") + ", " +
    String(IMS_RATIOS[IMS_SIZE] || IMS_SIZE || "").toLowerCase() + ", with " +
    imsShortName(st) + (imsHasVariants(IMS_MODEL) ? " " + imsVariantName(st) : "");
}
$("ims-prompt").addEventListener("input", imsPromptCount);
function imsPromptCount() {
  const w = ($("ims-prompt").value.trim().match(/\S+/g) || []).length;
  $("ims-prompt-count").textContent = w + " word" + (w === 1 ? "" : "s");
}
$("ims-clear-prompt").onclick = () => { $("ims-prompt").value = ""; imsPromptCount(); $("ims-prompt").focus(); };
$("ims-count-minus").onclick = () => imsSetCount(Math.max(1, IMS_COUNT - 1));
$("ims-count-plus").onclick = () => imsSetCount(Math.min(8, IMS_COUNT + 1));
function imsSetCount(n) { IMS_COUNT = n; $("ims-count-val").textContent = n; imsGenSub(); imsSaveParams(); }
$("ims-seed-dice").onclick = () => {
  $("ims-seed").value = Math.floor(Math.random() * 900000) + 100000;
};
$("ims-hires").onchange = imsSaveParams;

/* ---- albums ---- */
function imsAlbumTargetOptions(selected) {
  const opts = ['<option value="">Unsorted</option>'];
  for (const a of IMS_ALBUMS) {
    opts.push(`<option value="${escapeHtml(a)}" ${a === selected ? "selected" : ""}>${escapeHtml(a)}</option>`);
  }
  return opts.join("");
}
function renderImsAlbumTarget() {
  const t = $("ims-album-target");
  t.innerHTML = imsAlbumTargetOptions("");
  if (IMS_ALBUM !== "__all__" && IMS_ALBUM !== "__unsorted__") {
    t.value = IMS_ALBUM;
  }
}
function renderImsAlbumTabs() {
  const counts = { "__all__": IMS_DATA.length, "__unsorted__": 0 };
  for (const a of IMS_ALBUMS) counts[a] = 0;
  for (const im of IMS_DATA) {
    const key = im.album || "__unsorted__";
    if (counts[key] !== undefined) counts[key]++;
  }
  const tabs = $("ims-album-tabs");
  tabs.innerHTML = IMS_ALBUMS.map((a) =>
    `<button class="ims-album-tab ${IMS_ALBUM === a ? "active" : ""}" data-album="${escapeHtml(a)}" title="Right-click to rename or delete">${escapeHtml(a)}<span class="cnt">${counts[a] || 0}</span></button>`).join("");
  tabs.querySelectorAll(".ims-album-tab").forEach((b) => {
    b.onclick = () => { IMS_ALBUM = b.dataset.album; renderImsAlbumTabs(); renderImsGallery(); renderImsAlbumTarget(); };
    b.oncontextmenu = (e) => {
      e.preventDefault();
      const a = b.dataset.album;
      Dlg.choose({
        title: "Album — " + a,
        message: "Rename or delete this album?",
        choices: [
          { label: "Rename", value: "rename" },
          { label: "Delete", value: "delete", danger: true },
        ],
      }).then(async (act) => {
        if (act === "rename") {
          const nn = await Dlg.prompt({ title: "Rename album", label: "New name for " + escapeHtml(a) + ":", value: a, okLabel: "Rename" });
          if (nn && nn.trim()) imsAlbumAction("rename", a, nn.trim());
        } else if (act === "delete") {
          const ok = await Dlg.confirm({ title: "Delete album", message: "Delete album <b>" + escapeHtml(a) + "</b>? Images move to Unsorted.", okLabel: "Delete", danger: true });
          if (ok) imsAlbumAction("delete", a);
        }
      });
    };
  });
  document.querySelectorAll("#page-images .ims-albums > .ims-album-tab").forEach((b) => {
    b.onclick = () => { IMS_ALBUM = b.dataset.album; renderImsAlbumTabs(); renderImsGallery(); renderImsAlbumTarget(); };
  });
}
async function imsAlbumAction(action, title, newTitle) {
  const body = { action };
  if (action === "create") body.title = title;
  if (action === "rename") { body.old = title; body.new = newTitle; }
  if (action === "delete") body.title = title;
  const r = await post("/api/images/albums", body);
  if (!r.ok) return toast("error: " + r.error, true);
  toast("album " + action + "d");
  if (action === "delete" && IMS_ALBUM === title) IMS_ALBUM = "__all__";
  refreshIms();
}
$("ims-album-new").onclick = async () => {
  const t = await Dlg.prompt({ title: "New album", label: "Album name:", okLabel: "Create" });
  if (t && t.trim()) await imsAlbumAction("create", t.trim());
};

/* ---- gallery ---- */
function imsFiltered() {
  if (IMS_ALBUM === "__all__") return IMS_DATA;
  if (IMS_ALBUM === "__unsorted__") return IMS_DATA.filter((im) => !im.album);
  return IMS_DATA.filter((im) => im.album === IMS_ALBUM);
}
function renderImsGallery() {
  const el = $("ims-grid");
  const imgs = imsFiltered();
  const head = $("ims-album-head");
  const name = IMS_ALBUM === "__all__" ? "All pictures" : IMS_ALBUM === "__unsorted__" ? "Unsorted" : IMS_ALBUM;
  head.innerHTML = `<h2>${escapeHtml(name)}</h2><span class="sec-count">${imgs.length}</span>`;
  if (!imgs.length) {
    el.innerHTML = `<div class="ims-empty"><span class="big">${Icon.get("image")}</span><b>No pictures here yet</b>Describe one on the left and press Generate.</div>`;
    return;
  }
  el.innerHTML = imgs.map((im) => {
    const m = IMS_STACKS[im.model] || {};
    return `<div class="ims-cell" data-file="${im.file}">
      <img loading="lazy" src="/api/images/file?name=${encodeURIComponent(im.file)}" alt="">
      <div class="ovl">
        <div class="o-prompt">${escapeHtml(im.prompt || "")}</div>
        <div class="o-row">
          <span class="o-model">${escapeHtml(m.name || im.model || "unknown")}</span>
          <span class="o-tools">
            <button data-act="view" title="view">${Icon.get("eye")}</button>
            <button data-act="dl" title="download">${Icon.get("download")}</button>
            <button data-act="del" class="danger" title="delete">${Icon.get("x")}</button>
          </span>
        </div>
      </div>
    </div>`;
  }).join("");
  el.querySelectorAll(".ims-cell").forEach((c) => {
    c.querySelector("[data-act=view]").onclick = (e) => { e.stopPropagation(); imsOpen(c.dataset.file); };
    c.querySelector("[data-act=dl]").onclick = (e) => { e.stopPropagation(); imsDl(c.dataset.file); };
    c.querySelector("[data-act=del]").onclick = (e) => { e.stopPropagation(); imsDel(c.dataset.file); };
    c.onclick = () => imsOpen(c.dataset.file);
  });
}
async function refreshIms() {
  try {
    const r = await api("/api/images/list");
    IMS_DATA = r.images || [];
    IMS_ALBUMS = r.albums || [];
    renderImsAlbumTabs();
    renderImsGallery();
    renderImsAlbumTarget();
  } catch (e) { /* boot */ }
}
function escapeHtml(s) {
  return esc(s);
}
async function imsDl(file) {
  const a = document.createElement("a");
  a.href = "/api/images/file?name=" + encodeURIComponent(file);
  a.download = file;
  a.click();
}
async function imsDel(file) {
  const ok = await Dlg.confirm({ title: "Delete image", message: "Delete <b>" + escapeHtml(file) + "</b>?", okLabel: "Delete", danger: true });
  if (!ok) return;
  await post("/api/images/delete", { name: file });
  if (IMS_LB && IMS_LB.file === file) imsClose();
  refreshIms();
}

/* ---- lightbox ---- */
async function imsOpen(file) {
  const r = await api("/api/images/list");
  IMS_LB = (r.images || []).find((im) => im.file === file);
  IMS_ALBUMS = r.albums || [];
  if (!IMS_LB) return;
  $("ims-lb-img").src = "/api/images/file?name=" + encodeURIComponent(file);
  $("ims-lb-prompt").textContent = IMS_LB.prompt || "(no prompt saved)";
  const m = IMS_STACKS[IMS_LB.model] || {};
  const extra = [];
  if (IMS_LB.hires) extra.push("hires");
  if (IMS_LB.queue_wait_s != null) extra.push("queued " + IMS_LB.queue_wait_s + "s");
  if (IMS_LB.exec_s != null) extra.push("render " + IMS_LB.exec_s + "s");
  $("ims-lb-info").textContent =
    (m.name || IMS_LB.model || "unknown") + " · " + IMS_LB.width + "×" + IMS_LB.height +
    " · " + IMS_LB.steps + " steps · cfg " + IMS_LB.cfg + " · seed " + IMS_LB.seed +
    (extra.length ? " · " + extra.join(" · ") : "") +
    (IMS_LB.ts ? " · " + IMS_LB.ts : "");
  const sel = $("ims-lb-album");
  sel.innerHTML = imsAlbumTargetOptions(IMS_LB.album || "");
  sel.onchange = async () => {
    const r2 = await post("/api/images/assign", { name: file, album: sel.value });
    if (r2.ok) { toast("moved to " + (sel.value || "Unsorted")); refreshIms(); }
  };
  $("ims-lightbox").classList.remove("hidden");
}
function imsClose() { $("ims-lightbox").classList.add("hidden"); IMS_LB = null; }
$("ims-lb-close").onclick = imsClose;
$("ims-lightbox").addEventListener("click", (e) => {
  if (e.target === $("ims-lightbox")) imsClose();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("ims-lightbox").classList.contains("hidden")) imsClose();
});
$("ims-lb-download").onclick = () => IMS_LB && imsDl(IMS_LB.file);
$("ims-lb-reuse").onclick = () => {
  if (!IMS_LB) return;
  $("ims-prompt").value = IMS_LB.prompt || "";
  imsPromptCount();
  imsClose();
  toast("prompt loaded — tweak and generate");
};
$("ims-lb-ref").onclick = async () => {
  if (!IMS_LB || !IMS_LB.file) return;
  if (!imsRefsSupported()) {
    return toast("Reference images need an SDXL model (RealVisXL / CyberRealistic XL)", true);
  }
  if (IMS_REF_ROWS.length >= 5) {
    return toast("Maximum 5 reference images already added", true);
  }
  try {
    const r = await post("/api/images/refs/from-gallery", { file: IMS_LB.file });
    if (r && r.ok && r.ref) {
      IMS_REF_ROWS.push({ file: r.ref, type: "Identity" });
      renderImsRefs();
      imsGenSub();
      imsClose();
      toast("Image added as a reference");
    } else {
      toast((r && r.error) ? r.error : "Failed to use as reference", true);
    }
  } catch (err) {
    toast("Failed: " + (err.message || err), true);
  }
};
$("ims-lb-delete").onclick = () => IMS_LB && imsDel(IMS_LB.file);

/* ---- generate ---- */
$("ims-generate").onclick = async () => {
  const prompt = $("ims-prompt").value.trim();
  if (!prompt) return toast("type a prompt first", true);
  const st = IMS_STACKS[IMS_MODEL] || {};
  const wh = IMS_SIZE.split("x");
  const seedVal = $("ims-seed").value.trim();
  // exactly the LoRA rows the user set; the prompt is sent as typed
  const activeLoras = imsLorasSupported() ? IMS_LORA_ROWS.filter((r) => r.file) : [];
  imsSaveParams();
  // routing target (this PC or a worker) + optional hold-for-queue
  const wsel = document.getElementById("ims-worker");
  const holdCb = document.getElementById("ims-hold");
  const worker = (wsel && wsel.value) || "local";
  const body = {
    prompt,
    model: IMS_MODEL,
    width: parseInt(wh[0]), height: parseInt(wh[1]),
    steps: parseInt($("ims-steps").value) || undefined,
    cfg: parseFloat($("ims-cfg").value) || undefined,
    seed: seedVal ? parseInt(seedVal) : undefined,
    count: IMS_COUNT,
    negative: $("ims-negative").value.trim() || undefined,
    album: $("ims-album-target").value || "",
    loras: activeLoras.map((r) => ({ file: r.file, strength: r.strength })),
    hires: $("ims-hires").checked,
    refs: IMS_REF_ROWS.map((r) => ({ file: r.file, type: r.type || "Identity" })),
    ref_strength: IMS_REF_STRENGTH,
    anchor: IMS_REF_ANCHOR,
    worker,
  };
  if (holdCb && holdCb.checked) {
    const q = await post("/api/queue", {
      kind: "image", label: "image · " + IMS_MODEL + " · " + prompt.slice(0, 40),
      payload: body, worker,
    });
    toast(q && q.id ? "held in queue as " + q.id : (q && q.error) || "hold failed",
          !(q && q.id));
    return;
  }
  const r = await post("/api/images/generate", body);
  if (!r.ok) return toast("error: " + r.error, true);
  toast("generating " + IMS_COUNT + " image" + (IMS_COUNT > 1 ? "s" : "") + " with " + (st.name || st.label || st.id || "?") + "…"
        + (worker !== "local" ? " on " + worker : ""));
  $("ims-generate").disabled = true;
  $("ims-gen-label").textContent = "Rendering…";
  $("ims-progress-wrap").classList.remove("hidden");
  $("ims-seed").value = "";
};
$("ims-stop").onclick = async () => { await post("/api/images/stop"); toast("Stopped"); };
setInterval(async () => {
  if (!document.getElementById("page-images").classList.contains("active")) return;
  const s = await api("/api/images/status");
  const g = s.gen || {};
  const pct = g.total ? Math.round((g.done / g.total) * 100) : 0;
  $("ims-progress-fill").style.width = pct + "%";
  $("ims-progress-txt").textContent = g.running
    ? (g.done + "/" + g.total + " · " + (g.message || "")) : (g.message || "");
  if (!g.running && $("ims-generate").disabled && IMS_STACKS[IMS_MODEL]) {
    $("ims-generate").disabled = false;
    $("ims-gen-label").textContent = "Generate";
    $("ims-progress-wrap").classList.add("hidden");
    if (g.message === "done") toast("images done");
    refreshIms();
  }
}, 2500);

/* ------------------------------------------------- video studio ---- */
/* Experimental sandbox. Reads its model list and its
   capability flags from the backend registry — it never hardcodes a model
   name or an assumption about which controls apply. Controls that the selected
   model does not support are hidden, not merely disabled.
   Left: the composer (model, prompt + chain segments, references, continue;
   sampling, cache, LoRAs and the worker under Advanced). Right: History —
   every clip and chain the studio rendered, the movie's takes included. */

let VS_MODELS = [];
let VS_CURRENT = null;
let VS_HIST_TIMER = null;
let VS_TICK_TIMER = null;
// LTX-2.5 reference stills chosen for the next Video Studio job. Each entry is
// {slot, type, name, file}; the server resolves the batch path and uploads the
// image, so the browser never needs the filesystem layout.
let VS_REFS = [];
// Continuity: the previous clip's staged tail frame
// {path, job_id, input_filename}. Sent as `continuation` on generate —
// H3 pins it as the frame-0 identity anchor, LTX folds it into a pic slot.
// A chain opens on it instead: its start_image is input_filename, the copy
// staged in the ComfyUI input folders. Zero VRAM carryover between clips.
let VS_CONT = null;

function vsEl(id) { return document.getElementById(id); }

async function videoStudioPage() {
  await vsLoadModels();
  vsHistBind();
  vsRefreshHistory();
  // References survive a refresh (losing them silently turned reference
  // renders into text-to-video — the H3 pixelation incident).
  vsLoadRefs();
  // LTX reference picker wiring (panel visibility is driven by the model's
  // supported_controls in vsOnModelChange)
  const addBtn = vsEl("vs-ref-add");
  if (addBtn) addBtn.onclick = vsPickRefs;
  const clrBtn = vsEl("vs-ref-clear");
  if (clrBtn) clrBtn.onclick = function () { VS_REFS = []; vsSaveRefs(); vsRenderRefs(); };
  const ccBtn = vsEl("vs-ref-copycast");
  if (ccBtn) ccBtn.onclick = vsCopyCast;
  // References from an arbitrary disk image (not just training batches):
  // file picker -> upload to output/uploads/vs_refs -> VS_REFS entry with an
  // absolute path (server resolves it for LTX AND H3; H3 pins ref #1 as the
  // frame-0 identity anchor).
  const refUpBtn = vsEl("vs-ref-upload");
  const refFile = vsEl("vs-ref-file");
  if (refUpBtn && refFile) {
    refUpBtn.onclick = function () { refFile.click(); };
    refFile.onchange = async function () {
      const f = refFile.files && refFile.files[0];
      if (!f) return;
      if (!/^image\/(png|jpeg|webp)$/.test(f.type)) {
        toast("reference must be png, jpg or webp", true);
        refFile.value = "";
        return;
      }
      if (f.size > 25 * 1024 * 1024) {
        toast("reference too large (25 MB max)", true);
        refFile.value = "";
        return;
      }
      refUpBtn.disabled = true;
      try {
        const fd = new FormData();
        fd.append("file", f, f.name);
        const r = await api("/api/videostudio/upload_ref",
                            { method: "POST", body: fd });
        if (r && r.ok) {
          vsAddRef({ type: "character", name: f.name.replace(/\.[^.]+$/, ""),
                     file: r.filename, path: r.path, url: r.url });
        } else {
          toast((r && r.error) || "reference upload failed", true);
        }
      } catch (e) {
        toast("reference upload failed: " + e, true);
      } finally {
        refUpBtn.disabled = false;
        refFile.value = "";
      }
    };
  }
  // Start image: Browse, a click on the thumbnail or a dropped picture ->
  // upload into ComfyUI input/ -> the START_IMAGE field (typing the name of
  // a file already there still works).
  const startBrowse = vsEl("vs-startimg-browse");
  const startFile = vsEl("vs-startimg-file");
  if (startBrowse && startFile) {
    startBrowse.onclick = function () { startFile.click(); };
    vsEl("vs-startimg-thumb").onclick = function () { startFile.click(); };
    startFile.onchange = function () {
      const f = startFile.files && startFile.files[0];
      startFile.value = "";
      if (f) vsUploadStartImage(f);
    };
    vsEl("vs-startimg-clear").onclick = function () { vsSetStartImage("", null); };
    const zone = vsEl("vs-startimg-wrap");
    const isFile = function (e) {
      return !!e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], "Files") >= 0;
    };
    zone.ondragover = function (e) {
      if (!isFile(e)) return;
      e.preventDefault();
      zone.classList.add("drop");
    };
    zone.ondragleave = function (e) {
      if (!zone.contains(e.relatedTarget)) zone.classList.remove("drop");
    };
    zone.ondrop = function (e) {
      zone.classList.remove("drop");
      if (!isFile(e)) return;
      e.preventDefault();
      const f = e.dataTransfer.files[0];
      if (f) vsUploadStartImage(f);
    };
  }
  // Continuity: the newest finished clip or take in History — its tail frame
  // becomes the opening anchor of the next generation.
  const contBtn = vsEl("vs-cont-btn");
  if (contBtn) {
    contBtn.onclick = async function () {
      contBtn.disabled = true;
      try {
        await vsRefreshHistory();
        const last = VS_HIST.find(function (it) { return it.status === "completed" && it.video; });
        if (!last) { toast("nothing finished to continue from yet", true); return; }
        await vsContinueFrom(last);
      } finally {
        contBtn.disabled = false;
      }
    };
  }
  vsRenderRefs();
  vsRenderContChip();
  // chain segments: the prompt is segment 1, each added segment follows it;
  // Length's + and − add one or remove the last
  const chainAdd = vsEl("vs-chain-add");
  if (chainAdd) chainAdd.onclick = function () { vsSetLength(vsChainSegments() + 2); };
  const chainLess = vsEl("vs-length-less");
  if (chainLess) chainLess.onclick = function () { vsSetLength(vsChainSegments()); };
  vsChainRenumber();
  // prompt check and the agent's reply; a shown check follows the edits
  const checkBtn = vsEl("vs-check-btn");
  if (checkBtn) checkBtn.onclick = function () { vsRunCheck(true); };
  const pasteBtn = vsEl("vs-paste-reply");
  if (pasteBtn) pasteBtn.onclick = vsPasteReply;
  const promptBlock = vsEl("vs-prompt").closest(".vs-block");
  if (promptBlock && !promptBlock.dataset.checkBound) {
    promptBlock.dataset.checkBound = "1";
    promptBlock.addEventListener("input", function (e) {
      if (e.target && e.target.tagName === "TEXTAREA") vsCheckSoon();
    });
  }
  vsEl("vs-startimg").oninput = vsStartImageChanged;
  if (VS_HIST_TIMER) clearInterval(VS_HIST_TIMER);
  VS_HIST_TIMER = setInterval(() => {
    const page = vsEl("page-videostudio");
    if (page && page.classList.contains("active") && document.visibilityState === "visible" &&
        VS_HIST.some(vsIsLive)) vsRefreshHistory();
  }, 5000);
  if (VS_TICK_TIMER) clearInterval(VS_TICK_TIMER);
  VS_TICK_TIMER = setInterval(vsTick, 1000);
}

/* Stage `it` (a History item) as the opening anchor of the next render. */
async function vsContinueFrom(it) {
  const r = await post("/api/videostudio/continue_from",
    it.kind === "clip" ? { job_id: it.id } : { name: it.video });
  if (!r || !r.ok) { toast((r && r.error) || "continue failed", true); return; }
  VS_CONT = { path: r.path, job_id: it.id, input_filename: r.input_filename,
              prev_model: r.prev_model || it.model, url: r.url };
  vsRenderContChip();
  vsApplyResolutionHint();
  vsRenderRefs();
  toast("continuing from " + (it.kind === "clip" ? "clip " : "take ") + it.id);
}

async function vsLoadModels() {
  const r = await api("/api/videostudio/models");
  VS_MODELS = (r && r.models) || [];
  const sel = vsEl("vs-model");
  if (!sel) return;
  // grouped by family/quality, recommended first, short labels; unavailable
  // models stay listed but cannot be selected (ModelCatalog)
  const prev = sel.value;
  const prod = (r && r.production_model) || null;
  const pick = [prev, prod].find((id) => id && VS_MODELS.some((m) => m.id === id && m.available))
    || ((VS_MODELS.find((m) => m.available) || VS_MODELS[0] || {}).id) || "";
  sel.innerHTML = ModelCatalog.optionsHtml(r, pick);
  sel.value = pick;
  const lab = document.querySelector('label[for="vs-model"]');
  if (lab) lab.dataset.tip = ModelCatalog.SEAMLESS_NOTE + " · ★ = recommended config";
  sel.onchange = () => vsOnModelChange();
  ["vs-width", "vs-height", "vs-steps"].forEach((id) => {
    vsEl(id).oninput = vsAdvMeta;
  });
  // the Length buttons and the Generate label count in clips of this many frames
  vsEl("vs-frames").oninput = () => { vsAdvMeta(); vsChainRenumber(); };
  vsEl("vs-generate").onclick = () => vsGenerate();
  wsRefreshSelects();
  await vsOnModelChange();
}

/* Fill every [data-worker-select] with the online fleet.
   Non-fatal when the fleet is empty or endpoints are mid-upgrade. */
let WS_FLEET = [];
let WS_FLEET_TS = 0;
async function wsFleet() {
  if (Date.now() - WS_FLEET_TS < 30000) return WS_FLEET;
  try {
    const d = await api("/api/workers");
    WS_FLEET = (d.workers || []).filter((w) => w.online);
    WS_FLEET_TS = Date.now();
  } catch (e) { WS_FLEET = []; }
  return WS_FLEET;
}
async function wsRefreshSelects() {
  try {
    const fleet = await wsFleet();
    document.querySelectorAll("[data-worker-select]").forEach((sel) => {
      const cur = sel.value;
      sel.innerHTML = ['<option value="local">This PC (local)</option>']
        .concat(fleet.map((w) =>
          `<option value="${esc(w.id)}">${esc(w.name)} (${esc(w.id)})</option>`))
        .join("");
      if (cur) sel.value = cur;
    });
  } catch (e) { /* fleet optional */ }
}
function fillWorkerSelects(container) {
  container.querySelectorAll(".wiz-worker-sel").forEach((sel) => {
    const i = parseInt(sel.dataset.i);
    const cur = (WIZ_MODELS[i] || {}).worker || "";
    sel.innerHTML = ['<option value="">local</option>']
      .concat(WS_FLEET.map((w) =>
        `<option value="${esc(w.id)}">${esc(w.name)} (${esc(w.id)})</option>`))
      .join("");
    if (cur) sel.value = cur;
    sel.onchange = () => { WIZ_MODELS[i].worker = sel.value || ""; };
  });
}

// ---------------------------------------------------------------------------
// Video Studio grid helpers (proven-config safety net).
// Every model has a frame grid and a proven resolution: LTX-2.5 renders
// 1920x1088 at 121 frames (1+8k), MiniMax H3 runs its phase 1 at 960x544 and
// lands on 124 frames (5+17k). The old UI left the frames field at its HTML
// default (25) and always took the first `native` entry, which produced
// off-grid errors (H3) and 1-second, reference-less clips that looked nothing
// like the proven results.
function vsSnapFrames(m, n) {
  const fl = (m && m.frame_limits) || {};
  const lo = fl.min != null ? Number(fl.min) : 9;
  const stp = fl.step != null ? Number(fl.step) : 8;
  const mx = fl.max != null ? Number(fl.max) : 100000;
  n = Math.max(lo, Math.min(Number(n) || lo, mx));
  if ((n - lo) % stp === 0) return n;
  const snap = (m && m.frame_snap) || "floor";
  const k = snap === "nearest" ? Math.round((n - lo) / stp)
                               : Math.floor((n - lo) / stp);
  return Math.max(lo, Math.min(lo + k * stp, mx));
}
function vsProvenFrames(m) { return vsSnapFrames(m, 121); }   // 5 s @ 24 fps
function vsSnapDims(m, w, h, portraitOk) {
  const rl = (m && m.resolution_limits) || {};
  const stp = rl.step != null ? Number(rl.step) : 32;
  let mn = rl.min || [256, 256];
  let mx = rl.max || [8192, 8192];
  // the limits are written landscape; a path that renders portrait (an LTX
  // seamless chain) gets them turned instead of clamping 1088x1920 to a square
  if (portraitOk && Number(h) > Number(w) && mx[0] > mx[1]) {
    mn = [mn[1], mn[0]];
    mx = [mx[1], mx[0]];
  }
  const sn = (v, lo, hi) => Math.max(lo,
    Math.min(Math.round(Number(v) / stp) * stp, Math.floor(hi / stp) * stp));
  return { width: sn(w, mn[0], mx[0]), height: sn(h, mn[1], mx[1]) };
}
function vsProvenSize(m) {
  const d = (m && m.defaults) || {};
  if (d.width && d.height) return { width: Number(d.width), height: Number(d.height) };
  const lim = (m && m.resolution_limits) || {};
  if (lim.native && lim.native.length) {
    const [nw, nh] = lim.native[0].split("x");
    return { width: Number(nw), height: Number(nh) };
  }
  return null;
}

async function vsOnModelChange() {
  const id = vsEl("vs-model").value;
  const m = VS_MODELS.find((x) => x.id === id);
  VS_CURRENT = m || null;
  if (!m) return;

  const controls = m.supported_controls || [];
  const modes = m.supported_modes || [];

  // ---- start from: Image (the default) or Text only, as far as the model
  // offers them; a choice the new model also offers is kept
  const modeIn = vsEl("vs-mode");
  if (!modes.includes(modeIn.value)) {
    modeIn.value = modes.includes("i2v") ? "i2v" : (modes[0] || "t2v");
  }
  vsEl("vs-mode-seg").querySelectorAll("button").forEach((b) => {
    b.style.display = modes.includes(b.dataset.mode) ? "" : "none";
    b.onclick = () => { modeIn.value = b.dataset.mode; vsApplyMode(); };
  });
  vsEl("vs-mode-seg").style.display = modes.length > 1 ? "" : "none";
  vsEl("vs-mode-label").textContent = modes.length > 1 ? "Start from" : "Start image";
  vsEl("vs-start-block").style.display = modes.includes("i2v") ? "" : "none";

  // ---- defaults from the registry
  const d = m.defaults || {};
  if (d.steps != null) vsEl("vs-steps").value = d.steps;
  if (d.cfg != null) vsEl("vs-cfg").value = d.cfg;
  // Proven size: `defaults.width/height` wins (H3 hero phase 1 = 960x544),
  // then the declared native output resolution (LTX = 1920x1088).
  const psize = vsProvenSize(m);
  if (psize) {
    vsEl("vs-width").value = psize.width;
    vsEl("vs-height").value = psize.height;
  }
  const fl = m.frame_limits || {};
  if (fl.min != null) vsEl("vs-frames").min = fl.min;
  if (fl.max != null) vsEl("vs-frames").max = fl.max;
  if (fl.step != null) vsEl("vs-frames").step = fl.step;
  // Proven frame count for a 5 s clip, snapped to THIS model's grid
  // (LTX 121, H3 124) — the old UI left the 25-frame HTML default in place,
  // which is why recent VS renders were ~1 s long.
  vsEl("vs-frames").value = vsProvenFrames(m);

  // ---- hide controls this model does not support
  const has = (c) => controls.includes(c);
  vsEl("vs-neg-wrap").style.display = has("negative") ? "" : "none";
  vsEl("vs-steps").disabled = !has("steps");
  vsEl("vs-cfg").disabled = !has("cfg");
  vsEl("vs-nocache").disabled = !has("cache");
  vsEl("vs-tea-thresh").disabled = !has("cache");
  vsEl("vs-tea-end").disabled = !has("cache");
  // LTX-2.5 reference picker — only for models that declare the capability
  const refsWrap = vsEl("vs-refs-wrap");
  if (refsWrap) refsWrap.style.display = has("refs") ? "" : "none";
  if (has("refs")) {
    vsSyncRefsToModel();
    vsRenderRefs();
  }
  vsApplyResolutionHint();

  // one quiet line of facts; the rest of the registry entry is its tooltip
  const chips = [(m.catalog || {}).detail, m.fps ? m.fps + " fps" : "",
    m.vram_estimate_mb ? "~" + (m.vram_estimate_mb / 1024).toFixed(1) + " GB" : ""].filter(Boolean);
  const meta = vsEl("vs-model-meta");
  meta.innerHTML = chips.map((c) => "<span>" + escapeHtml(c) + "</span>").join("") +
    '<span class="vs-help">' + Icon.get("help") + "</span>";
  meta.querySelector(".vs-help").dataset.tip = [m.note, m.vram_note,
    ["quant " + (m.quantization || "?"), "modes " + modes.join("/"),
      m.moe ? "MoE pair (2 experts)" : "", m.source && m.source !== "studio" ? "mounted from " + m.source : "",
      m.declared_status || "", "id " + m.id].filter(Boolean).join(" · ")].filter(Boolean).join("\n\n");
  // a config that is rarely the right pick says so under the picker
  vsEl("vs-model-caution").textContent = (m.catalog || {}).caution || "";
  vsChainRenumber();
  vsAdvMeta();

  await vsLoadLoras(id);
  vsApplyMode();
}

function vsApplyMode() {
  const mode = vsEl("vs-mode").value;
  vsEl("vs-mode-seg").querySelectorAll("button").forEach(function (b) {
    const on = b.dataset.mode === mode;
    b.classList.toggle("on", on);
    b.setAttribute("aria-checked", on ? "true" : "false");
  });
  vsEl("vs-startimg-wrap").style.display = mode === "i2v" ? "" : "none";
  vsStartImageChanged();
}

/* ------------------------------------------------------------- start image */
let VS_START_THUMB = null;   // {name, url}: the local picture behind the field

/* Upload a picked or dropped start image into the ComfyUI input folders and
   select it; the thumbnail shows the local file. */
async function vsUploadStartImage(f) {
  if (!/^image\/(png|jpeg|webp)$/.test(f.type)) {
    toast("the start image must be a png, jpg or webp", true);
    return;
  }
  if (f.size > 25 * 1024 * 1024) {
    toast("the start image is too large (25 MB max)", true);
    return;
  }
  const btn = vsEl("vs-startimg-browse");
  btn.disabled = true;
  try {
    const fd = new FormData();
    fd.append("file", f, f.name);
    const r = await api("/api/videostudio/upload_start_image", { method: "POST", body: fd });
    if (r && r.ok) {
      vsSetStartImage(r.filename, URL.createObjectURL(f));
      toast("start image: " + f.name);
    } else {
      toast((r && r.error) || "upload failed", true);
    }
  } catch (e) {
    toast("upload failed: " + e, true);
  } finally {
    btn.disabled = false;
  }
}

function vsSetStartImage(name, url) {
  if (VS_START_THUMB) URL.revokeObjectURL(VS_START_THUMB.url);
  VS_START_THUMB = url ? { name: name, url: url } : null;
  vsEl("vs-startimg").value = name;
  vsStartImageChanged();
}

/* The thumbnail, the clear button and the line under the start image follow
   the field, the mode and the model; so do the references' empty state, the
   H3 size and a shown prompt check. */
function vsStartImageChanged() {
  const name = vsEl("vs-startimg").value.trim();
  const thumb = vsEl("vs-startimg-thumb");
  const url = VS_START_THUMB && VS_START_THUMB.name === name ? VS_START_THUMB.url : "";
  thumb.classList.toggle("has", !!url);
  thumb.innerHTML = url ? '<img src="' + esc(url) + '" alt="">' : Icon.get("image");
  thumb.dataset.tip = url ? name : "Pick a start image";
  vsEl("vs-startimg-clear").style.display = name ? "" : "none";
  // what the model does with it, in prompt_check's words
  const fam = String((VS_CURRENT && VS_CURRENT.family) || "");
  vsEl("vs-startimg-note").textContent = !name
    ? "No picture yet: without one the video is made from the prompt alone."
    : fam.indexOf("ltx") === 0
      ? "LTX-2.5 uses this picture as its key image: it guides the render but is not pinned as the exact first frame."
      : "The video opens exactly on this picture: it is the first frame.";
  vsRenderRefs();
}

async function vsLoadLoras(model) {
  const box = vsEl("vs-loras");
  box.innerHTML = "";
  const r = await api("/api/videostudio/loras?model=" + encodeURIComponent(model));
  const compat = (r && r.compatible) || {};
  const keys = Object.keys(compat);
  const count = vsEl("vs-lora-count");
  count.textContent = keys.length ? String(keys.length) : "";
  count.dataset.tip = "Adapters for this model's family; " + ((r && r.incompatible_count) || 0) +
    " for other families are hidden";
  if (!keys.length) {
    box.innerHTML = '<div class="vs-none">None for this model family</div>';
    return;
  }
  keys.forEach((k) => {
    const e = compat[k];
    const row = document.createElement("div");
    row.className = "vs-lora";
    row.innerHTML =
      '<input type="checkbox" data-file="' + esc(k) + '">'
      + '<span class="vs-lora-name" title="' + esc(e.family || "") + '">'
      + esc(e.display || k) + "</span>"
      + '<input type="number" step="0.05" min="0" max="2" value="'
      + (e.strength != null ? esc(e.strength) : 0.8) + '" data-str="' + esc(k) + '">';
    box.appendChild(row);
  });
}

function vsCollectLoras() {
  const out = [];
  document.querySelectorAll("#vs-loras input[type=checkbox]").forEach((cb) => {
    if (!cb.checked) return;
    const f = cb.dataset.file;
    const s = document.querySelector('#vs-loras input[data-str="' + CSS.escape(f) + '"]');
    out.push({ file: f, strength: s ? parseFloat(s.value) : 0.8 });
  });
  return out;
}

/* ---------------------------------------------------------- chain segments */
/* The prompt box is segment 1; every added segment continues it. Two or more
   segments render as a chain (one video). */
function vsChainAddSegment(focus) {
  const box = vsEl("vs-chain-segments");
  if (!box) return;
  const div = document.createElement("div");
  div.className = "vs-seg";
  div.innerHTML = '<span class="vs-seg-n"></span>' +
    '<textarea class="vs-chain-prompt" rows="2"></textarea>' +
    '<button type="button" class="btn small ghost icon vs-chain-del" data-tip="Remove segment">' +
    Icon.get("x") + "</button>";
  div.querySelector(".vs-chain-del").onclick = function () { div.remove(); vsChainRenumber(); vsCheckSoon(); };
  box.appendChild(div);
  vsChainRenumber();
  if (focus) div.querySelector("textarea").focus();
}

function vsChainSegments() {
  return document.querySelectorAll("#vs-chain-segments .vs-seg").length;
}

function vsChainRenumber() {
  const segs = document.querySelectorAll("#vs-chain-segments .vs-seg");
  segs.forEach(function (s, i) {
    s.querySelector(".vs-seg-n").textContent = String(i + 2);
    s.querySelector("textarea").placeholder = "Segment " + (i + 2) + ": what happens next";
  });
  const chain = segs.length > 0;
  const n = segs.length + 1;
  vsEl("vs-prompt-n").textContent = chain ? "1" : "";
  vsEl("vs-prompt-label").textContent = chain ? "Prompts" : "Prompt";
  vsEl("vs-prompt").placeholder = chain
    ? "Segment 1: how it opens: who is in it, where, what happens, how the camera moves"
    : "Describe the shot: who is in it, where, what happens, how the camera moves";
  vsEl("vs-generate-label").textContent = chain
    ? "Generate · " + n + " segments, about " + vsLengthSeconds(n) + " s" : "Generate";
  vsRenderLength();
  vsChainEngineNote();
}

/* ------------------------------------------------------------------ length */
/* Length is the number of segments, set with + and − (no top: a chain takes
   as many segments as you write). A segment is one clip of the Frames field
   (5 s on the LTX-2.5 and H3 tiers); later segments add one frame less, since
   a take carries its last frame on. A stretched tier (slow motion) plays longer. */
function vsClipSeconds() {
  const m = VS_CURRENT || {};
  const fps = Number(m.fps) || 24;
  const fr = parseInt(vsEl("vs-frames").value, 10) || 121;
  const x = Number((m.catalog || {}).stretch) || 1;
  return { first: fr / fps * x, later: (fr - 1) / fps * x };
}

function vsLengthSeconds(n) {
  const s = vsClipSeconds();
  return Math.round(s.first + (n - 1) * s.later);
}

function vsRenderLength() {
  const val = vsEl("vs-length-val");
  if (!val) return;
  const n = vsChainSegments() + 1;
  val.innerHTML = "<b>" + vsLengthSeconds(n) + " s</b> · " + (n === 1 ? "1 clip" : n + " segments");
  vsEl("vs-length-less").disabled = n === 1;
  vsEl("vs-chain-add").dataset.tip = "One segment more: about " + Math.round(vsClipSeconds().later) +
    " s of new action, with its own prompt";
}

/* Show `n` segment boxes. Removing a box that holds a prompt asks first. */
async function vsSetLength(n) {
  const box = vsEl("vs-chain-segments");
  const drop = Array.prototype.slice.call(box.querySelectorAll(".vs-seg")).slice(n - 1);
  const written = drop.filter(function (s) { return s.querySelector("textarea").value.trim(); });
  if (written.length) {
    const nums = written.map(function (s) { return s.querySelector(".vs-seg-n").textContent; });
    const ok = await Dlg.confirm({
      title: drop.length === 1 ? "Remove a segment?" : "Remove " + drop.length + " segments?",
      message: (nums.length === 1 ? "Segment " + nums[0] + " has" : "Segments " + nums.join(", ") + " have") +
        " a prompt, which is removed with " + (nums.length === 1 ? "it." : "them."),
      okLabel: "Remove", danger: true,
    });
    if (!ok) return;
  }
  drop.forEach(function (s) { s.remove(); });
  const had = vsChainSegments();
  while (vsChainSegments() < n - 1) vsChainAddSegment(false);
  vsChainRenumber();
  const first = box.querySelectorAll(".vs-chain-prompt")[had];
  if (first) first.focus();
  vsCheckSoon();
}

/* Which continuation the selected model's chains use (shown once there is a
   chain to render), and the preview-first switch (seamless models with a
   finish pass: the draft take is assembled and QA'd, the 1080p pass runs
   when you press Finish). */
function vsChainEngineNote() {
  const m = VS_CURRENT || {};
  const note = vsEl("vs-chain-engine");
  const prev = vsEl("vs-chain-preview-wrap");
  const chain = vsChainSegments() > 0;
  const kind = m.seamless === "h3" ? "MiniMax H3" : (m.seamless === "ltx" ? "LTX-2.5" : "");
  if (note) {
    note.style.display = chain ? "" : "none";
    note.className = "vs-engine" + (kind ? " seamless" : "");
    note.textContent = kind ? "⛓ one seamless take" : "tail-frame chain";
    note.dataset.tip = kind
      ? "Seamless " + kind + " take: the segments render as one continuous shot — " +
        (m.seamless === "h3" ? "exact" : "soft-pinned") + " latent carry between segments, " +
        "one cast for the whole take, decoded once, every seam QA'd"
      : "Each segment opens on the previous segment's last frame; the clips are joined. " +
        "Pick a ⛓ model for one seamless take";
  }
  // base-only H3 tiers deliver the draft take itself: nothing to finish
  if (prev) prev.style.display = chain && kind && m.chain_finish ? "" : "none";
  // what the chosen length renders, in one line under the buttons
  const txt = vsEl("vs-length-text");
  if (txt) {
    const s = vsClipSeconds();
    const per = Math.round(s.later);
    const stretched = (Number((m.catalog || {}).stretch) || 1) > 1;
    txt.textContent = chain
      ? (kind ? "Write what happens in each segment (about " + per + " s each): they render as one continuous shot."
              : "Each segment (about " + per + " s) opens on the last frame of the one before, and the clips are joined.")
      : stretched
        ? "One clip played as about " + Math.round(s.first) + " s of slow motion."
        : "One clip. For a longer video, add segments with +: each adds about " + per +
          " s of new action, with its own prompt.";
  }
}

/* Advanced summary: what the hidden fields will render. */
function vsAdvMeta() {
  const el = vsEl("vs-adv-meta");
  if (!el) return;
  const fps = (VS_CURRENT && VS_CURRENT.fps) || 24;
  const fr = parseInt(vsEl("vs-frames").value, 10) || 0;
  el.textContent = vsEl("vs-width").value + "×" + vsEl("vs-height").value + " · " +
    fr + " f (" + (fr / fps).toFixed(1) + " s) · " + vsEl("vs-steps").value + " steps";
}

function vsSeamQa(label, qa) {
  if (!qa || !qa.seams) return "";
  const seams = qa.seams || [];
  const ok = seams.filter(function (s) { return s.pass; }).length;
  const chips = seams.map(function (s) {
    const tip = "frame " + s.frame + " · pop " + Number(s.pop_ratio || 0).toFixed(2) +
      " · dSat " + Number(s.d_sat || 0).toFixed(2) + " · dL " + Number(s.d_L || 0).toFixed(2) +
      ((s.fails || []).length ? " · " + s.fails.join(", ") : "");
    return '<span class="vs-seam ' + (s.pass ? "ok" : "bad") + '" data-tip="' + escapeHtml(tip) + '">' +
      s.frame + (s.pass ? " ✓" : " ✗") + "</span>";
  }).join("");
  return '<div class="vs-kv"><span>' + escapeHtml(label) + "</span><b>" + ok + "/" + seams.length +
    " seams pass" + (qa.drift != null ? " · drift " + Math.round(qa.drift) + "%" : "") + "</b></div>" +
    '<div class="vs-seams">' + chips + "</div>";
}

function vsCastReport(d) {
  const s = (d && d.summary) || {};
  const names = Object.keys(s);
  if (!names.length) return "";
  return names.map(function (n) {
    const x = s[n];
    return '<div class="vs-kv"><span>' + escapeHtml(n) + "</span><b>identity " +
      Number(x.sim_first).toFixed(2) + "→" + Number(x.sim_last).toFixed(2) + " · age " +
      Math.round(x.age_first) + "→" + Math.round(x.age_last) + "</b></div>";
  }).join("");
}

/* ---------------------------------------------------------- LTX references */
function vsRenderContChip() {
  const chip = vsEl("vs-cont-chip");
  if (!chip) return;
  if (!VS_CONT) { chip.style.display = "none"; return; }
  chip.style.display = "";
  chip.innerHTML = (VS_CONT.url ? '<img src="' + escapeHtml(VS_CONT.url) + '" alt="">' : "") +
    '<span data-tip="Its last frame opens the next render">Opens on the end of <b class="mono">' +
    escapeHtml(VS_CONT.job_id) + "</b></span>" +
    '<button type="button" class="btn small ghost icon" id="vs-cont-clear" data-tip="Start fresh">' +
    Icon.get("x") + "</button>";
  const x = chip.querySelector("#vs-cont-clear");
  if (x) x.onclick = function () {
    VS_CONT = null;
    vsRenderContChip();
    vsApplyResolutionHint();
    vsRenderRefs();
    vsCheckSoon();
  };
}

const VS_TYPE_OPTS = ["character", "power", "environment"];

/* Model-aware slot contract — MUST match the backend:
   H3 Ref2VA: 6 reference slots (pic1..pic6 + background), 6 refs total;
   LTX MSR:   4 subject slots (pic1..pic4) + background, 5 refs total. */
function vsModelSlotInfo() {
  const fam = String((VS_CURRENT && VS_CURRENT.family) || "");
  if (fam.indexOf("h3") === 0) {
    return { slots: ["pic1", "pic2", "pic3", "pic4", "pic5", "pic6",
                     "background"],
             subjects: ["pic1", "pic2", "pic3", "pic4", "pic5", "pic6"],
             maxRefs: 6 };
  }
  return { slots: ["pic1", "pic2", "pic3", "pic4", "background"],
           subjects: ["pic1", "pic2", "pic3", "pic4"],
           maxRefs: 5 };
}

/* What each slot MEANS on the selected model — shown in the slot dropdown
   so the operator never has to memorize the mapping. An H3 slot's
   <Picture N> label is shown on the image itself (vsRenderRefs): the
   numbers follow the filled slots, so pic3 is Picture 2 when pic2 is empty. */
function vsSlotLabel(s) {
  const fam = String((VS_CURRENT && VS_CURRENT.family) || "");
  if (fam.indexOf("ltx") === 0) {
    if (s === "pic1") return "pic1 — lead person (required)";
    if (s === "background") return "background — environment";
    return s + " — person";
  }
  if (fam.indexOf("h3") === 0) {
    if (s === "pic1") return "pic1 — lead person";
    if (s === "background") return "background — environment (last Picture)";
    return s + " — person";
  }
  return s;
}

function vsRefsHint() {
  const fam = String((VS_CURRENT && VS_CURRENT.family) || "");
  if (fam.indexOf("ltx") === 0)
    return "LTX MSR (max 4 people + 1 environment): no labels in the prompt, " +
           "people are matched by how the prompt describes them. pic1 must " +
           "hold a person: a start image takes pic1 and the people move " +
           "down to pic2..pic4. The environment goes to background.";
  if (fam.indexOf("h3") === 0)
    return "H3 (max 6 references): each image becomes a <Picture N> label " +
           "in slot order, the background last (the label is shown on each " +
           "image). Write the label exactly in prompts, e.g. <Picture 1>. The " +
           "start image, else Picture 1, is the exact first frame.";
  return "slots assign automatically for this model";
}

/* Keep the saved cast legal for the selected model: remap slots the model
   does not offer (e.g. H3's pic5/pic6 when switching to LTX) and trim over
   the model's reference capacity — with explicit toasts, never silently. */
function vsSyncRefsToModel() {
  const info = vsModelSlotInfo();
  let changed = false;
  // drop over-capacity entries first (lowest priority = last entries)
  while (VS_REFS.length > info.maxRefs) {
    const dropped = VS_REFS.pop();
    toast("cast trimmed for this model: dropped " +
          (dropped.name || dropped.slot) + " (max " + info.maxRefs + ")", true);
    changed = true;
  }
  const used = () => VS_REFS.map(r => r.slot);
  const freeSubject = () => info.subjects.find(s => used().indexOf(s) < 0);
  VS_REFS.forEach(function (r) {
    if (info.slots.indexOf(r.slot) >= 0) return;
    const isEnv = r.type === "environment";
    const bgTaken = used().indexOf("background") >= 0;
    const next = isEnv ? (bgTaken ? freeSubject() : "background")
                       : freeSubject();
    if (next) {
      toast((r.name || r.slot) + " remapped: " + r.slot + " → " + next);
      r.slot = next;
    } else {
      toast("cast trimmed for this model: dropped " +
            (r.name || r.slot) + " (no free slot)", true);
      r._drop = true;
    }
    changed = true;
  });
  if (VS_REFS.some(r => r._drop)) {
    VS_REFS = VS_REFS.filter(r => !r._drop);
  }
  if (changed) vsSaveRefs();
  return changed;
}

function vsRenderRefs() {
  const box = vsEl("vs-refs");
  if (!box) return;
  const hint = vsEl("vs-refs-hint");
  if (hint) hint.dataset.tip = vsRefsHint() + " Applied to single clips and every chain segment.";
  vsApplyResolutionHint();
  if (!VS_REFS.length) {
    // mode-aware empty state: with a start image — or continuing from a
    // previous clip — the render is NOT text-to-video
    const si = vsEl("vs-startimg");
    const startImg = vsEl("vs-mode").value === "i2v" && si && si.value.trim();
    box.innerHTML = '<div class="vs-none">' + (startImg
      ? "No other references: the start image leads the render"
      : VS_CONT ? "No other references: the previous clip's last frame leads the render"
        : "No references: text-to-video") + "</div>";
    vsCheckSoon();
    return;
  }
  // H3 numbers its <Picture N> labels over the filled slots in slot order
  // (video_studio._apply_references_h3), the background last
  const picture = {};
  if (String((VS_CURRENT && VS_CURRENT.family) || "").indexOf("h3") === 0) {
    const order = vsModelSlotInfo().slots;
    VS_REFS.map(function (r) { return r.slot; })
      .filter(function (s) { return order.indexOf(s) >= 0; })
      .sort(function (a, b) { return order.indexOf(a) - order.indexOf(b); })
      .forEach(function (s, k) { picture[s] = k + 1; });
  }
  box.innerHTML = VS_REFS.map(function (r, i) {
    const url = r.url || ("/api/dataset_img?type=" + encodeURIComponent(r.type) +
      "&name=" + encodeURIComponent(r.name) + "&file=" + encodeURIComponent(r.file));
    const info = vsModelSlotInfo();
    const slotOpts = info.slots.map(function (s) {
      return '<option value="' + s + '"' + (r.slot === s ? " selected" : "") +
        ">" + escapeHtml(vsSlotLabel(s)) + "</option>";
    }).join("");
    // guard: a slot the model no longer offers stays visible, never blank
    const opts = r.slot && info.slots.indexOf(r.slot) < 0
      ? '<option value="' + escapeHtml(r.slot) + '" selected>' +
        escapeHtml(r.slot) + " (not available on this model)</option>" + slotOpts
      : slotOpts;
    const typeOpts = VS_TYPE_OPTS.map(function (t) {
      return '<option value="' + t + '"' + (r.type === t ? " selected" : "") +
        ">" + t + "</option>";
    }).join("");
    return '<div class="vs-ref">' +
      '<div class="vs-ref-img">' +
        '<img src="' + url + '" alt="reference" loading="lazy">' +
        (picture[r.slot] ? '<span class="vs-ref-tag" data-tip="The label H3 reads for this image: write it exactly in the prompt">' +
          "&lt;Picture " + picture[r.slot] + "&gt;</span>" : "") +
        '<button type="button" class="vs-ref-x" data-i="' + i + '" data-tip="Remove">' + Icon.get("x") + "</button>" +
      "</div>" +
      '<div class="vs-ref-fields">' +
        '<select class="vs-ref-slot" data-i="' + i + '" data-tip="Which reference this image is">' + opts + "</select>" +
        '<select class="vs-ref-type" data-i="' + i + '" data-tip="What kind of reference">' + typeOpts + "</select>" +
        '<input class="vs-ref-name" data-i="' + i + '" value="' + escapeHtml(r.name || "") +
          '" placeholder="name" data-tip="The name the cast sheet uses">' +
        (r.type === "character" || r.type === "face" || !r.type
          ? '<input class="vs-ref-desc" data-i="' + i + '" value="' + escapeHtml(r.description || "") +
            '" placeholder="look: age, hair, outfit" maxlength="400" ' +
            'data-tip="The person\'s look. The cast sheet gives it to the prompt writer, and LTX seamless chains repeat it in every segment prompt so the person keeps the same age and look (blank = from the face dataset)">'
          : "") +
      "</div>" +
    "</div>";
  }).join("");
  box.querySelectorAll(".vs-ref-x").forEach(function (b) {
    b.onclick = function () {
      VS_REFS.splice(Number(b.dataset.i), 1);
      vsRenderRefs();
      vsSaveRefs();
    };
  });
  box.querySelectorAll(".vs-ref-slot").forEach(function (sel) {
    sel.onchange = function () {
      const i = Number(sel.dataset.i);
      const other = VS_REFS.findIndex(function (r, j) {
        return j !== i && r.slot === sel.value;
      });
      if (other >= 0) {
        const old = VS_REFS[i].slot;
        VS_REFS[other].slot = old;  // swap, never clobber
        toast("swapped slots: " + sel.value + " <-> " + old);
      }
      VS_REFS[i].slot = sel.value;
      vsSaveRefs();
      vsRenderRefs();
    };
  });
  box.querySelectorAll(".vs-ref-type").forEach(function (sel) {
    sel.onchange = function () {
      VS_REFS[Number(sel.dataset.i)].type = sel.value;
      vsSaveRefs();
      vsRenderRefs();
    };
  });
  box.querySelectorAll(".vs-ref-name").forEach(function (inp) {
    inp.onchange = function () {
      VS_REFS[Number(inp.dataset.i)].name = inp.value.trim();
      vsSaveRefs();
      vsCheckSoon();
    };
  });
  box.querySelectorAll(".vs-ref-desc").forEach(function (inp) {
    inp.onchange = function () {
      VS_REFS[Number(inp.dataset.i)].description = inp.value.trim();
      vsSaveRefs();
      vsCheckSoon();
    };
  });
  vsCheckSoon();
}

/* ---------------------------------------------------------- prompt check */
/* The request Generate sends, shared by the prompt check and the cast sheet.
   One segment box is a clip, two or more a chain (Length); a chain opens on
   the start image or on the ↪ clip's staged last frame, a clip carries the
   continuation. A chain sends every box, so the check names an empty one
   instead of rendering fewer segments than Length shows. `boxes` are the
   textareas the prompts came from (the fixes edit them); `segments` counts
   them. The start image counts only when the video starts from an image. */
function vsRenderRequest() {
  const m = VS_CURRENT;
  const all = [vsEl("vs-prompt")].concat(Array.prototype.slice.call(
    document.querySelectorAll("#vs-chain-segments .vs-chain-prompt")));
  const boxes = all.length > 1 ? all : all.filter(function (ta) { return ta.value.trim(); });
  const si = vsEl("vs-mode").value === "i2v"
    ? ((vsEl("vs-startimg") && vsEl("vs-startimg").value) || "").trim() : "";
  const frames = parseInt(vsEl("vs-frames").value, 10);
  const req = {
    model: vsEl("vs-model").value,
    prompts: boxes.map(function (ta) { return ta.value.trim(); }),
    segments: all.length,
    boxes: boxes,
    references: (VS_REFS.length && m && (m.supported_controls || []).includes("refs"))
      ? VS_REFS.map(function (r) {
          return { slot: r.slot, type: r.type, name: r.name, file: r.file,
                   path: r.path, description: r.description };
        })
      : [],
    frames: m ? vsSnapFrames(m, frames) : frames,
  };
  if (all.length > 1) {
    req.start_image = si || (VS_CONT ? VS_CONT.input_filename : null);
    req.start_from_clip = !si && !!VS_CONT;
  } else {
    req.start_image = si || null;
    req.continuation = VS_CONT ? VS_CONT.path : null;
  }
  return req;
}

function vsCheckBody(req) {
  const body = Object.assign({}, req);
  delete body.boxes;
  return body;
}

const VS_CHECK = { seq: 0, timer: null };

function vsCheckVisible() {
  const el = vsEl("vs-check");
  return !!el && el.style.display !== "none";
}

/* A shown check follows the prompts, the references and the model. */
function vsCheckSoon() {
  if (!vsCheckVisible()) return;
  clearTimeout(VS_CHECK.timer);
  VS_CHECK.timer = setTimeout(function () { vsRunCheck(false); }, 500);
}

/* Run the prompt check (prompt_check.py) on `req` (default: the composer as it
   is) and show it when `show` is true, when it is already shown, or, with
   show "issues", when it found any. Returns the report, or null when the check
   could not run: Generate then goes on, and the server checks the request again. */
async function vsRunCheck(show, req) {
  if (!VS_CURRENT) return null;
  req = req || vsRenderRequest();
  const seq = ++VS_CHECK.seq;
  let rep = null;
  try {
    rep = await post("/api/videostudio/prompt_check", vsCheckBody(req));
  } catch (e) {
    if (show) toast("prompt check failed: " + e.message, true);
    return null;
  }
  if (!rep || rep.ok === false) return null;
  if (seq === VS_CHECK.seq && (show === true || vsCheckVisible() ||
                               (show === "issues" && (rep.issues || []).length))) {
    vsRenderCheck(rep, req);
  }
  return rep;
}

/* Errors (red) stop Generate; warnings are suggestions and never do. `rep` is
   a check report or a refused render, which carries its issues as well. */
function vsRenderCheck(rep, req) {
  const el = vsEl("vs-check");
  if (!el) return;
  const issues = rep.issues || [];
  const chain = req.prompts.length > 1;
  const where = function (i) {
    return i.segment ? (chain ? "Segment " + i.segment : "Prompt") : "Setup";
  };
  const count = function (level) {
    return issues.filter(function (i) { return i.level === level; }).length;
  };
  const n = function (k, one, many) { return k + " " + (k === 1 ? one : many); };
  const errors = count("error"), warnings = count("warning");
  const sum = (errors ? '<b class="vs-chk-sum error">' + n(errors, "error", "errors") + "</b>" : "") +
    (warnings ? '<span class="vs-chk-sum warning">' + n(warnings, "suggestion", "suggestions") + "</span>" : "") +
    (issues.length ? "" : '<span class="vs-chk-sum ok">All clear</span>');
  const fixable = issues.some(function (i) { return (i.fixes || []).length; });
  el.innerHTML =
    '<div class="vs-chk-head"><span class="vs-label">Prompt check</span>' + sum +
      '<span class="spacer"></span>' +
      (fixable ? '<button type="button" class="btn small" id="vs-chk-fixall" data-tip="Apply every suggested text fix">Fix all</button>' : "") +
      '<button type="button" class="btn small ghost icon" id="vs-chk-close" data-tip="Hide the check">' +
        Icon.get("x") + "</button>" +
    "</div>" +
    (issues.length ? '<div class="vs-chk-list">' + issues.map(function (i, k) {
      const fixes = i.fixes || [];
      return '<div class="vs-chk-item ' + escapeHtml(i.level) + '">' +
        '<span class="vs-chk-where">' + escapeHtml(where(i)) + "</span>" +
        '<span class="vs-chk-msg">' + escapeHtml(i.message) + "</span>" +
        (fixes.length ? '<button type="button" class="btn small ghost" data-fix="' + k + '">' +
          escapeHtml(fixes[0].label + (fixes.length > 1 ? " ×" + fixes.length : "")) + "</button>" : "") +
      "</div>";
    }).join("") + "</div>" : "");
  el.style.display = "";
  el.querySelector("#vs-chk-close").onclick = function () { el.style.display = "none"; };
  const all = el.querySelector("#vs-chk-fixall");
  if (all) all.onclick = function () { vsApplyFixes(issues, req); };
  el.querySelectorAll("[data-fix]").forEach(function (b) {
    b.onclick = function () { vsApplyFixes([issues[Number(b.dataset.fix)]], req); };
  });
}

/* Apply the text fixes of `issues` to the boxes the prompts were read from,
   each box from its end backwards. A fix whose text changed since the check,
   or that overlaps one already applied, is skipped. Then the check runs again. */
function vsApplyFixes(issues, req) {
  const bySeg = {};
  issues.forEach(function (i) {
    (i.fixes || []).forEach(function (f) {
      (bySeg[i.segment] = bySeg[i.segment] || []).push(f);
    });
  });
  let skipped = 0;
  Object.keys(bySeg).forEach(function (seg) {
    const ta = req.boxes[Number(seg) - 1];
    if (!ta) { skipped += bySeg[seg].length; return; }
    let text = ta.value;
    const lead = text.length - text.replace(/^\s+/, "").length;   // the check read it trimmed
    let floor = Infinity;
    bySeg[seg].sort(function (a, b) { return b.start - a.start || b.end - a.end; })
      .forEach(function (f) {
        const s = lead + f.start, e = lead + f.end;
        if (e > floor || text.slice(s, e) !== f.find) { skipped += 1; return; }
        text = text.slice(0, s) + f.replace + text.slice(e);
        floor = s;
      });
    ta.value = text;
  });
  if (skipped) toast(skipped + (skipped === 1 ? " fix was" : " fixes were") +
                     " skipped: the prompt changed since the check", true);
  vsRunCheck(true);
}

/* Paste the agent's reply: its "Segment N:" blocks (markdown bold or headers
   are fine) fill the prompt and the segment boxes; a reply without them is one
   prompt. The check runs on the result. */
function vsParseReply(text) {
  // a code fence or one pair of quotes around a whole block is the reply's
  // wrapping, not the prompt (quotes inside it are dialogue and stay)
  const clean = function (s) {
    return s.trim().replace(/^```[^\n]*\n?|\n?```$/g, "").trim()
      .replace(/^\*+|\*+$/g, "").replace(/^"([^"]*)"$/, "$1").trim();
  };
  const t = clean(String(text || "").replace(/\r\n?/g, "\n"));
  const head = /^[ \t>#*_-]*segment[ \t]+\d+\b(?:[^\n:]*:|[^\n]*\n)[ \t*_]*/gim;
  const marks = [];
  let m;
  while ((m = head.exec(t)) !== null) marks.push({ at: m.index, body: m.index + m[0].length });
  if (!marks.length) return t ? [clean(t)] : [];
  return marks.map(function (mk, k) {
    return clean(t.slice(mk.body, k + 1 < marks.length ? marks[k + 1].at : t.length));
  }).filter(Boolean);
}

function vsFillSegments(segs) {
  const box = vsEl("vs-chain-segments");
  vsEl("vs-prompt").value = segs[0];
  while (vsChainSegments() < segs.length - 1) vsChainAddSegment(false);
  const extra = box.querySelectorAll(".vs-seg");
  for (let k = extra.length - 1; k >= segs.length - 1; k--) extra[k].remove();
  box.querySelectorAll(".vs-chain-prompt").forEach(function (ta, k) { ta.value = segs[k + 1]; });
  vsChainRenumber();
}

function vsPasteReply() {
  const veil = document.createElement("div");
  veil.className = "dlg-veil";
  veil.innerHTML =
    '<div class="dlg-card" role="dialog" aria-modal="true" style="max-width:760px;width:92vw">' +
      '<div class="dlg-head"><h3>Paste the agent\'s reply</h3></div>' +
      '<div class="dlg-body">' +
        "<p>Each \"Segment N:\" block fills one segment box; a reply without them fills the prompt.</p>" +
        '<textarea class="dlg-input vs-reply" rows="14" spellcheck="false"></textarea>' +
      "</div>" +
      '<div class="dlg-actions">' +
        '<button type="button" class="btn" data-dlg-cancel>Cancel</button>' +
        '<button type="button" class="btn primary" data-dlg-ok>Fill the prompts</button>' +
      "</div>" +
    "</div>";
  document.body.appendChild(veil);
  const ta = veil.querySelector("textarea");
  const onKey = function (e) { if (e.key === "Escape") close(); };
  const close = function () { document.removeEventListener("keydown", onKey); veil.remove(); };
  document.addEventListener("keydown", onKey);
  veil.querySelector("[data-dlg-cancel]").onclick = close;
  veil.addEventListener("mousedown", function (e) { if (e.target === veil) close(); });
  veil.querySelector("[data-dlg-ok]").onclick = async function () {
    const segs = vsParseReply(ta.value);
    if (!segs.length) { toast("nothing to paste", true); return; }
    const boxes = [vsEl("vs-prompt")].concat(Array.prototype.slice.call(
      document.querySelectorAll("#vs-chain-segments .vs-chain-prompt")));
    if (boxes.some(function (b) { return b.value.trim(); }) && !(await Dlg.confirm({
      title: "Replace the prompts?",
      message: "The current prompts are replaced by the " +
               (segs.length === 1 ? "pasted prompt." : segs.length + " pasted segments."),
      okLabel: "Replace" }))) return;
    close();
    vsFillSegments(segs);
    toast(segs.length === 1 ? "prompt filled" : segs.length + " segments filled");
    vsRunCheck(true);
  };
  ta.focus();
}

/* Cast sheet: the brief for the agent that writes the prompts, built by the
   server from the same plan the render uses (prompt_check.cast_sheet): how
   the selected model reads a prompt, the images it receives in its own order,
   how segment 1 opens and how later segments continue, and the reply format
   Paste reply reads. */
async function vsCopyCast() {
  if (!VS_CURRENT) { toast("pick a model first", true); return; }
  let r = null;
  try {
    r = await post("/api/videostudio/cast_sheet", vsCheckBody(vsRenderRequest()));
  } catch (e) {
    toast("cast sheet failed: " + e.message, true);
    return;
  }
  if (!r || r.ok === false) { toast((r && r.error) || "cast sheet failed", true); return; }
  try { navigator.clipboard.writeText(r.text); } catch (e) { /* clipboard blocked */ }
  const problems = (r.issues || []).filter(function (i) { return i.level === "error"; });
  Dlg.confirm({
    title: "Cast sheet — copied to clipboard",
    message: (problems.length
      ? '<span class="vs-chk-sum error">Fix first: ' + escapeHtml(problems.map(function (i) {
          return i.message; }).join(" ")) + "</span><br><br>" : "") +
      "Give it to the agent that writes the prompts, then bring its reply back " +
      "with <b>Paste reply</b>." +
      "<pre class='vs-sheet'>" + escapeHtml(r.text) + "</pre>",
    okLabel: "Close",
  });
}

function vsPickRefs() {
  const type = (VS_CURRENT && (VS_CURRENT.supported_controls || []).includes("refs"))
    ? "face" : "face";
  const veil = document.createElement("div");
  veil.className = "dlg-veil";
  veil.innerHTML =
    '<div class="dlg-card" style="max-width:820px;width:92vw">' +
      '<div class="dlg-head"><h3>Pick a reference image</h3></div>' +
      '<div class="dlg-body">' +
        '<div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">' +
          '<select id="vsp-type" class="dlg-input" style="max-width:190px">' +
            '<option value="face">characters (face)</option>' +
            '<option value="power">power / VFX</option>' +
            '<option value="environment">environments</option>' +
          "</select>" +
          '<select id="vsp-batch" class="dlg-input"></select>' +
          '<span class="muted small" id="vsp-note"></span>' +
        "</div>" +
        '<div id="vsp-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:6px;max-height:56vh;overflow:auto">' +
          '<span class="muted small">loading…</span>' +
        "</div>" +
      "</div>" +
      '<div class="dlg-actions"><button type="button" class="btn" data-dlg-cancel>Cancel</button></div>' +
    "</div>";
  document.body.appendChild(veil);
  const close = function (v) { veil.remove(); if (v) vsAddRef(v); };
  veil.querySelector("[data-dlg-cancel]").onclick = function () { close(null); };
  veil.addEventListener("mousedown", function (e) { if (e.target === veil) close(null); });

  let datasets = [];
  async function load() {
    const t = veil.querySelector("#vsp-type").value;
    const grid = veil.querySelector("#vsp-grid");
    grid.innerHTML = '<span class="muted small">loading…</span>';
    const r = await api("/api/pick/images?type=" + encodeURIComponent(t));
    datasets = (r && r.datasets) || [];
    const sel = veil.querySelector("#vsp-batch");
    sel.innerHTML = datasets.map(function (d, i) {
      return '<option value="' + i + '">' + escapeHtml(d.name) + " (" + (d.kept || 0) + " kept)</option>";
    }).join("") || '<option value="">no batches</option>';
    sel.onchange = renderGrid;
    renderGrid();
  }
  function renderGrid() {
    const t = veil.querySelector("#vsp-type").value;
    const sel = veil.querySelector("#vsp-batch");
    const grid = veil.querySelector("#vsp-grid");
    const note = veil.querySelector("#vsp-note");
    const d = datasets[Number(sel.value)];
    if (!d) { grid.innerHTML = '<span class="muted small">no images</span>'; return; }
    const cands = (d.candidates || []).slice(0, 120);
    if (note) note.textContent = (d.candidates || []).length + " images";
    grid.innerHTML = cands.map(function (c) {
      const url = "/api/dataset_img?type=" + encodeURIComponent(t) +
        "&name=" + encodeURIComponent(d.name) + "&file=" + encodeURIComponent(c);
      return '<div class="vsp-thumb" data-file="' + escapeHtml(c) + '" ' +
        'style="cursor:pointer;border:1px solid transparent;border-radius:6px;overflow:hidden">' +
        '<img loading="lazy" src="' + url + '" style="width:100%;height:88px;object-fit:cover;display:block" alt="batch image">' +
      "</div>";
    }).join("");
    grid.querySelectorAll(".vsp-thumb").forEach(function (th) {
      th.onclick = function () { close({ type: t, name: d.name, file: th.dataset.file }); };
    });
  }
  veil.querySelector("#vsp-type").onchange = load;
  load();
}

function vsSaveRefs() {
  try { localStorage.setItem("vs_refs", JSON.stringify(VS_REFS)); } catch (e) {}
}
function vsLoadRefs() {
  // References survive a page refresh — losing them silently turned intended
  // reference renders into plain text-to-video (the H3 pixelation incident).
  if (VS_REFS.length) return;
  try {
    const raw = localStorage.getItem("vs_refs");
    if (raw) {
      const arr = JSON.parse(raw);
      if (Array.isArray(arr)) {
        // name is OPTIONAL (Copy cast falls back to the filename) — the
        // only requirements are a slot and a resolvable source
        VS_REFS = arr.filter((r) => r && r.slot && (r.file || r.path));
      }
    }
  } catch (e) { /* storage unavailable */ }
}

function vsAddRef(pick) {
  // TYPE-AWARE SLOT ASSIGNMENT (automatic, model-aware): the batch you pick
  // from decides the slot — environment -> background, power -> the next
  // free subject slot (pic2 first), anything else (face/character) -> the
  // next free subject slot (pic1 first). Capacity follows the selected
  // model: H3 = 6 refs total, LTX = 4 subjects + 1 environment.
  const info = vsModelSlotInfo();
  if (VS_REFS.length >= info.maxRefs) {
    toast("cast is full for this model (" + info.maxRefs +
          " references max) — remove one first", true);
    return;
  }
  const used = VS_REFS.map(function (r) { return r.slot; });
  const free = function (order) {
    return order.find(function (s) { return used.indexOf(s) < 0; });
  };
  let slot;
  if (pick.type === "environment") {
    slot = "background";
  } else if (pick.type === "power") {
    slot = free(info.subjects.filter(function (s) { return s !== "pic1"; })
                  .concat(["pic1"]));
  } else {
    slot = free(info.subjects);
  }
  if (!slot) { toast("all reference slots are full (" + info.maxRefs +
                     " references max on this model)", true); return; }
  VS_REFS = VS_REFS.filter(function (r) { return r.slot !== slot; });
  // uploaded-from-disk refs carry an absolute `path` (server-side resolution
  // via ltx_refs.resolve_reference_entry) and a stable thumbnail `url`;
  // batch refs keep their {type,name,file} shape (undefined fields are
  // dropped by JSON).
  VS_REFS.push({ slot: slot, type: pick.type, name: pick.name, file: pick.file,
                 path: pick.path, url: pick.url });
  vsRenderRefs();
  vsSaveRefs();
  const label = pick.type === "environment" ? "environment"
    : (pick.type === "power" ? "power" : "character");
  toast(label + " reference → " + slot + " (" + pick.name + "/" + pick.file + ")");
}

function vsApplyResolutionHint() {
  // H3's proven hero tier is TWO-PHASE: phase 1 renders at half resolution
  // with the references, then the upscaler doubles it and refines. Without
  // references the graph falls back to single-pass — which is only proven up
  // to 1344x768, NOT 1080p. The width/height fields must therefore follow the
  // template that will actually run, or the "final" output silently becomes
  // the small phase-1 canvas (the pixelated renders).
  const m = VS_CURRENT;
  const note = document.getElementById("vs-refs-note");
  if (!m) return;
  const fam = String(m.family || "");
  if (fam.indexOf("h3") === 0) {
    const d = m.defaults || {};
    // An I2V start image is promoted server-side to the H3 frame-0 anchor, and
    // an explicit continuation anchors the same way — both select the
    // two-phase refs graph, so dim snapping treats them like references.
    const _si = vsEl("vs-startimg");
    const _anchored = VS_CONT ||
      (vsEl("vs-mode").value === "i2v" && _si && _si.value);
    if (VS_REFS.length || _anchored) {
      if (d.width && d.height) {
        vsEl("vs-width").value = d.width;
        vsEl("vs-height").value = d.height;
      }
      if (note) {
        note.className = "vs-note";
        note.textContent = VS_REFS.length
          ? "Two-phase: " + (d.width || 960) + "×" + (d.height || 544) +
            " with the references, then upscaled and refined to 1920×1088"
          : "The start frame anchors identity (two-phase 1080p); add references for the full config";
      }
    } else {
      vsEl("vs-width").value = 1344;
      vsEl("vs-height").value = 768;
      if (note) {
        note.className = "vs-note warn";
        note.textContent = "Without references H3 renders single-pass at 1344×768 " +
          "(no upscaler, no identity anchor)";
      }
    }
    vsAdvMeta();
    return;
  }
  if (note) {
    // LTX: an I2V start image is promoted server-side to the pic1 subject
    // reference — same stage-1 injection as picked references.
    note.className = "vs-note";
    note.textContent = "";
  }
  vsAdvMeta();
}

async function vsGenerate() {
  const btn = vsEl("vs-generate");
  btn.disabled = true;
  try {
    // ── the request the prompt check reads: the prompt is segment 1, then
    // every added segment; two or more make a chain ───────────────────────
    const req = vsRenderRequest();
    if (!req.prompts.length) { toast("write a prompt first", true); return; }
    const isChain = req.prompts.length > 1;
    const chainPrompts = req.prompts;
    // errors in the prompt check stop here (the server refuses them too);
    // suggestions are shown and never block
    const chk = await vsRunCheck("issues", req);
    if (chk && chk.errors) {
      toast("fix the red items in the prompt check first", true);
      return;
    }

    // "Image" with no picture picked renders from the prompt alone
    const modes = (VS_CURRENT && VS_CURRENT.supported_modes) || [];
    const body = {
      model: req.model,
      mode: !req.start_image && modes.includes("t2v") ? "t2v" : vsEl("vs-mode").value,
      prompt: req.prompts[0],
      negative: vsEl("vs-negative").value,
      seed: parseInt(vsEl("vs-seed").value, 10),
      width: parseInt(vsEl("vs-width").value, 10),
      height: parseInt(vsEl("vs-height").value, 10),
      frames: parseInt(vsEl("vs-frames").value, 10),
      steps: parseInt(vsEl("vs-steps").value, 10),
      cfg: parseFloat(vsEl("vs-cfg").value),
      teacache_thresh: parseFloat(vsEl("vs-tea-thresh").value),
      teacache_end: parseFloat(vsEl("vs-tea-end").value),
      no_cache: vsEl("vs-nocache").checked,
      loras: vsCollectLoras(),
      continuation: req.continuation || null,
    };

    // proven-config safety net (chain AND single clip): snap dims + frames
    // to this model's grids so off-grid values never reach the server.
    // H3 without refs renders single-pass (no upscaler, no identity anchor)
    // — that is exactly how an intended hero render silently became a
    // pixelated 960x544 clip. EXCEPTION: an I2V start image on H3 or LTX is
    // promoted server-side (H3: frame-0 identity anchor; LTX: pic1 subject
    // reference applied at stage 1), an explicit continuation is anchored by
    // definition, and a multi-segment chain anchors from segment 2 onward —
    // the warning would be wrong in all three cases. "Text only" on LTX-2.5
    // is a choice already made; on H3 it still changes the tier, so it asks.
    if (VS_CURRENT) {
      const _fam = String(VS_CURRENT.family || "");
      const _anchored = (_fam.indexOf("h3") === 0 || _fam.indexOf("ltx") === 0) &&
        (req.start_image || VS_CONT);
      const _h3 = _fam.indexOf("h3") === 0;
      if ((VS_CURRENT.supported_controls || []).includes("refs") &&
          !VS_REFS.length && !_anchored && !isChain &&
          (vsEl("vs-mode").value === "i2v" || _h3)) {
        const ok = await Dlg.confirm({
          title: "No start image or references",
          message: "This clip will be made from the prompt alone." +
            (_h3
              ? " For MiniMax H3 that is the single-pass fallback at 1344×768 " +
                "(no upscaler, no identity anchor)."
              : "") +
            "<br><br>For the recommended result, add a start image or cast " +
            "references (References → Batch or Upload). Or go on without them.",
          okLabel: "Make it from the prompt",
        });
        if (!ok) { btn.disabled = false; return; }
      }
      const dims = vsSnapDims(VS_CURRENT, body.width, body.height,
                              isChain && VS_CURRENT.seamless === "ltx");
      const fr = vsSnapFrames(VS_CURRENT, body.frames);
      if (dims.width !== body.width || dims.height !== body.height ||
          fr !== body.frames) {
        toast("adjusted to " + VS_CURRENT.id + "'s grid: " + dims.width + "x" +
              dims.height + " · " + fr + " frames");
        vsEl("vs-width").value = dims.width;
        vsEl("vs-height").value = dims.height;
        vsEl("vs-frames").value = fr;
      }
      body.width = dims.width;
      body.height = dims.height;
      body.frames = fr;
    }

    // Reference slots (server resolves + uploads them) — used by BOTH the
    // single-clip and the chain path (vsRenderRequest). `path` is what makes
    // disk-uploaded refs resolve server-side (resolve_reference_entry checks
    // `path` first); undefined fields are dropped by JSON, so batch refs are
    // unaffected. `name` feeds the cast sheet and the backend logs.
    if (req.references.length) body.references = req.references;

    if (isChain) {
      // ── MULTI-SEGMENT CHAIN ────────────────────────────────────────────
      // ⛓ seamless models (LTX-2.5 quality, every H3 tier) render the
      // segments as ONE take: latent carry, one cast, decoded once, seam QA;
      // "preview first" stops at the draft take until Finish. Other models
      // continue each segment from the previous tail frame and concatenate.
      const seamless = !!(VS_CURRENT && VS_CURRENT.seamless);
      const previewCb = vsEl("vs-chain-preview");
      const wselC = vsEl("vs-worker");
      const r = await post("/api/videostudio/chain", {
        model: body.model,
        prompts: chainPrompts,
        mode: body.mode,
        negative: body.negative,
        seed: body.seed,
        width: body.width,
        height: body.height,
        frames: body.frames,
        steps: body.steps,
        cfg: body.cfg,
        references: req.references,
        start_image: req.start_image,
        worker: (wselC && wselC.value && wselC.value !== "local")
                  ? wselC.value : "local",
        draft_only: seamless && !!VS_CURRENT.chain_finish && !!(previewCb && previewCb.checked),
      });
      if (r && r.ok === false) {
        if (r.issues) vsRenderCheck(r, req);
        toast(r.error || "chain failed", true);
        return;
      }
      toast("chain " + r.id + " started: " + chainPrompts.length + " segments" +
            (seamless ? ", one seamless take" + (r.draft_only ? ", stops at the preview" : "") : ""));
      await vsRefreshHistory();
      return;
    }

    // ── SINGLE CLIP ──────────────────────────────────────────────────────
    // (references were mapped above — shared with the chain path)
    if (req.start_image) body.start_image = req.start_image;
    const wsel = vsEl("vs-worker");
    const hold = vsEl("vs-hold");
    if (wsel && wsel.value && wsel.value !== "local") body.worker = wsel.value;
    if (hold && hold.checked) {
      const r = await post("/api/queue", {
        kind: "video", label: "video · " + body.model + " · " +
          body.prompt.slice(0, 40), payload: body,
        worker: body.worker || "local",
      });
      toast(r && r.id ? "held in queue as " + r.id : (r && r.error) || "hold failed",
            !(r && r.id));
      return;
    }
    const r = await post("/api/videostudio/generate", body);
    if (r && r.ok === false) {
      if (r.issues) vsRenderCheck(r, req);
      toast(r.error || "failed", true);
      return;
    }
    toast("queued " + (r.id || "") + (body.worker && body.worker !== "local"
          ? " on " + body.worker : ""));
    await vsRefreshHistory();
  } finally {
    btn.disabled = false;
  }
}

/* ------------------------------------------------ live render progress
   Real progress straight from the farm's own websocket: step count, s/it and
   per-stage ETA. ComfyUI's host/origin middleware 403s a cross-port websocket,
   so we embed a tiny relay page served BY the farm (same origin) that forwards
   progress via postMessage. Strictly read-only — it never posts to the queue,
   so opening or closing the page cannot disturb a running render. */
// heard: the farm has sent an event for this render (a chain's live line stays
// empty until then: an older studio queued chain steps where the relay can't hear)
const VS_LIVE = { jid: null, port: null, last: null, heard: false,
                  spi: null, label: "", labels: {}, pct: 0, txt: "" };
const VS_RELAY = {};   // port -> hidden iframe, kept alive for the session

function vsLivePort(model) {
  const m = String(model || "");
  if (m.indexOf("minimax_h3") === 0) return 8199;
  if (m.indexOf("ltx25") === 0) return 8189;
  return 8188;
}

function vsRelayEnsure(port) {
  if (VS_RELAY[port] || !document.body) return;
  const f = document.createElement("iframe");
  f.style.display = "none";
  f.title = "farm progress relay";
  f.src = "http://127.0.0.1:" + port + "/progress_relay.html";
  document.body.appendChild(f);
  VS_RELAY[port] = f;
}

function vsLivePaint(jid) {
  const el = document.getElementById("vs-prog-" + jid);
  if (!el) return;
  const fill = el.querySelector(".vs-prog-fill");
  const txt = el.querySelector(".vs-prog-txt");
  if (fill) fill.style.width = (VS_LIVE.pct || 0) + "%";
  if (txt) txt.textContent = VS_LIVE.txt || "waiting for the farm…";
  document.querySelectorAll('[data-live="' + jid + '"]').forEach(vsLivePaintEl);
}

// a chain's live line (card and player): the sampler step of the running step
function vsLivePaintEl(el) {
  if (!el) return;
  const t = el.dataset.live === VS_LIVE.jid && VS_LIVE.heard ? VS_LIVE.txt : "";
  if (el.textContent !== t) el.textContent = t;
}

function vsLiveDetach() {
  VS_LIVE.jid = null; VS_LIVE.last = null; VS_LIVE.spi = null; VS_LIVE.heard = false;
  VS_LIVE.label = ""; VS_LIVE.labels = {}; VS_LIVE.pct = 0; VS_LIVE.txt = "";
}

function vsFarmEvent(msg) {
  const jid = VS_LIVE.jid;
  if (!jid || !msg) return;
  if (msg.type === "progress" && msg.data) {
    const v = msg.data.value || 0, mx = msg.data.max || 0;
    const now = performance.now();
    if (VS_LIVE.last && v > VS_LIVE.last.v) {
      const inst = (now - VS_LIVE.last.t) / 1000 / (v - VS_LIVE.last.v);
      VS_LIVE.spi = VS_LIVE.spi ? VS_LIVE.spi * 0.65 + inst * 0.35 : inst;
    }
    VS_LIVE.last = { v: v, t: now };
    VS_LIVE.heard = true;
    VS_LIVE.pct = mx ? Math.round((v / mx) * 100) : 0;
    let t = (VS_LIVE.label ? VS_LIVE.label + " · " : "") + "step " + v + "/" + mx;
    if (VS_LIVE.spi) {
      t += " · " + VS_LIVE.spi.toFixed(2) + " s/it";
      const left = Math.max(0, Math.round((mx - v) * VS_LIVE.spi));
      if (left) t += " · ~" + (left >= 60 ? Math.round(left / 60) + " min" : left + " s") + " left in stage";
    }
    VS_LIVE.txt = t;
    vsLivePaint(jid);
  } else if (msg.type === "executing" && msg.data) {
    const node = msg.data.node;
    if (node == null) {                     // prompt finished → refresh the list
      vsLiveDetach();
      vsRefreshHistory();
      return;
    }
    VS_LIVE.last = null; VS_LIVE.spi = null; VS_LIVE.heard = true;
    VS_LIVE.label = VS_LIVE.labels[node] || ("node " + node);
    VS_LIVE.txt = VS_LIVE.label + " …";
    VS_LIVE.pct = 0;
    vsLivePaint(jid);
  }
}

window.addEventListener("message", function (ev) {
  const d = ev.data;
  if (!d || typeof d !== "object" || !d.port) return;
  if (String(d.port) !== String(VS_LIVE.port)) return;
  if (d.type === "vs-relay") {
    VS_LIVE.txt = (d.state === "open")
      ? "connected — waiting for the first step…"
      : "farm relay " + d.state + " — retrying…";
    vsLivePaint(VS_LIVE.jid);
    return;
  }
  if (d.type === "vs-labels" && d.labels) { VS_LIVE.labels = d.labels; return; }
  if (d.type === "vs-farm") vsFarmEvent(d.msg);
});

function vsLiveAttach(j) {
  if (VS_LIVE.jid === j.id && String(VS_LIVE.port) === String(vsLivePort(j.model))) {
    vsLivePaint(j.id);
    return;
  }
  vsLiveDetach();
  VS_LIVE.jid = j.id;
  VS_LIVE.port = vsLivePort(j.model);
  vsRelayEnsure(VS_LIVE.port);
  VS_LIVE.txt = "waiting for the farm…";
  vsLivePaint(j.id);
}

/* --------------------------------------------------------------- history
   One list of everything the studio rendered (GET /api/videostudio/history):
   single clips and chains — seamless takes, segment chains, the movie's
   continuous takes, and chains from before chain records (rebuilt from their
   files). Cards stay compact (thumbnail, prompt, model, state, actions); the
   player holds the details. Cards are keyed, so a refresh only rebuilds the
   ones that changed and the thumbnails never flash. */
let VS_HIST = [];
let VS_HIST_FILTER = "all";
const VS_SETTLED = ["completed", "failed", "draft_ready", "interrupted", "partial"];
const VS_THUMBS = "IntersectionObserver" in window ? new IntersectionObserver(function (entries) {
  entries.forEach(function (e) {
    if (!e.isIntersecting) return;
    const v = e.target;
    VS_THUMBS.unobserve(v);
    if (v.dataset.src) { v.src = v.dataset.src; v.removeAttribute("data-src"); }
  });
}, { rootMargin: "200px" }) : null;

function vsIsLive(it) { return VS_SETTLED.indexOf(it.status) < 0; }
function vsFileUrl(name) { return "/api/videostudio/file/" + encodeURIComponent(name); }
function vsItemKey(it) { return it.kind + ":" + it.id; }
function vsIsTake(it) { return !!(it.origin && it.origin.project); }
// the seamless engine of a chain ("ltx" / "h3"); older records carry `engine`
function vsSeamless(it) {
  if (it.seamless) return it.seamless;
  const e = String(it.engine || "");
  return e.indexOf("seamless-") === 0 ? e.slice(9) : null;
}

function vsModelLabel(id) {
  const m = VS_MODELS.find(function (x) { return x.id === id; });
  return m ? ((m.catalog || {}).label || m.display_name || m.id) : (id || "");
}

function vsAgo(ts) {
  const t = Date.parse(ts || "");
  if (!t) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  if (s < 86400) return Math.round(s / 3600) + " h ago";
  if (s < 86400 * 7) return Math.round(s / 86400) + " d ago";
  return new Date(t).toLocaleDateString();
}

function vsState(it) {
  const s = it.status || "";
  if (s === "completed") return { cls: "ok", label: "done" };
  if (s === "failed") return { cls: "bad", label: "failed" };
  if (s === "draft_ready") return { cls: "accent", label: "preview" };
  if (s === "interrupted" || s === "partial") return { cls: "dim", label: s };
  if (s === "queued") return { cls: "info", label: "queued" };
  // one label for every live step: the card's html must not change with each step
  // (a rebuilt card reloads its thumbnail); the progress shows the step instead
  return { cls: "run", label: "rendering" };
}

function vsTitle(it) {
  const p = it.kind === "clip" ? it.prompt : (it.prompts || [])[0];
  return p || (it.kind === "clip" ? "Clip " : "Chain ") + it.id;
}

function vsSource(it) {
  if (it.legacy) {
    return '<span data-tip="Rendered before chains kept a record: rebuilt from its files">earlier chain</span>';
  }
  return "<span>" + escapeHtml(vsModelLabel(it.model) || it.kind) + "</span>";
}

function vsWarnings(it) {
  // reference problems must SAY so: a silently dropped reference made H3 fall
  // back to single-pass and the operator only saw pixelation
  if ((it.reference_warnings || []).length) return "references: " + it.reference_warnings.join("; ");
  if (it.kind !== "clip" || (it.references || []).length || it.continuation || it.start_image) return "";
  const fam = String((VS_MODELS.find(function (x) { return x.id === it.model; }) || {}).family || "");
  if (fam.indexOf("h3") === 0) return "no references: single-pass fallback";
  return "";
}

/* ------------------------------------------------ seamless chain progress
   A seamless chain's record carries `plan` (chain_studio.chain_plan): every step
   in order with its state, start (t0, on the server's clock) and seconds. The
   bar gives each step a share by its expected time, and the time left rescales
   those expectations to the pace of the chain's own finished steps. Expected
   times, [fixed, per segment] in seconds, from the showcase chains (docs/
   SHOWCASE.md 5-6): LTX drafts ~130 s, 1080p finishes 108-134 s, take decodes
   44 s (draft) and 167 s (1080p) for 4 segments; H3 base links ~650 s, the
   refine 1470 s and its decode 314 s for 3 segments. A record from before `plan`
   gets one rebuilt from its status (LTX only). */
const VS_STEP_EST = {
  draft: [150, 0], audio_take: [3, 0], take_draft: [0, 11], assemble_draft: [0, 30],
  finish: [130, 0], take_final: [0, 42], assemble_final: [0, 40],
  link: [650, 0], decode_B0: [20, 45], assemble_B0: [0, 30],
  refcond: [160, 0], refine: [0, 490], decode_R0: [13, 105], assemble_R0: [0, 40],
};
let VS_CLOCK_SKEW = 0;    // server clock minus this browser's, s (history's `now`)

function vsStepKind(id) {
  if (/^draft_l\d+$/.test(id)) return "draft";
  if (/^finish_\w+_l\d+$/.test(id)) return "finish";
  if (/^link_l\d+$/.test(id)) return "link";
  if (/^take_final/.test(id)) return "take_final";
  if (/^refcond_/.test(id)) return "refcond";
  if (/^refine_/.test(id)) return "refine";
  return id;
}
function vsStepLink(id) { const m = /_l(\d+)$/.exec(id); return m ? Number(m[1]) : 0; }
// the four rows of the stepper: draft links, the preview, the 1080p pass, the delivery
function vsStepStage(id) {
  const k = vsStepKind(id);
  if (k === "draft" || k === "link") return 0;
  if (k === "finish" || k === "refcond" || k === "refine" || k === "decode_R0") return 2;
  if (k === "take_final" || k === "assemble_final" || k === "assemble_R0") return 3;
  return 1;
}
function vsStepEst(id, n) {
  const e = VS_STEP_EST[vsStepKind(id)] || [30, 0];
  return e[0] + e[1] * Math.max(1, n);
}
function vsStepLabel(id, n, short) {
  const k = vsStepKind(id), l = vsStepLink(id);
  if (k === "draft" || k === "link" || k === "finish") {
    if (short) return String(l);
    return (k === "finish" ? "1080p" : k === "link" ? "Base" : "Draft") + " · segment " + l + " of " + n;
  }
  const names = {
    audio_take: ["Sound", "Joining the sound"],
    take_draft: ["Decode", "Decoding the draft take"],
    decode_B0: ["Decode", "Decoding the base take"],
    assemble_draft: ["Preview", "Draft preview + seam check"],
    assemble_B0: ["Preview", "Base preview + seam check"],
    refcond: ["Prompts", "1080p · encoding the prompts"],
    refine: ["Refine", "1080p · refining the whole take"],
    decode_R0: ["Decode", "Decoding the 1080p take"],
    take_final: ["Decode", "Decoding the 1080p take"],
    assemble_final: ["Encode", "Final encode + seam check"],
    assemble_R0: ["Encode", "Final encode + seam check"],
  }[k];
  return names ? names[short ? 0 : 1] : id;
}

function vsChainPlan(it) {
  if (Array.isArray(it.plan) && it.plan.length) return it.plan;
  if (it.kind !== "chain" || it.legacy || vsSeamless(it) !== "ltx") return null;
  const n = (it.prompts || []).length;
  if (!n) return null;
  const ids = [];
  for (let i = 1; i <= n; i++) ids.push("draft_l" + i);
  if (n > 1) ids.push("audio_take");
  ids.push("take_draft", "assemble_draft");
  for (let i = 1; i <= n; i++) ids.push("finish_F0_l" + i);
  ids.push("take_final_F0", "assemble_final");
  const s = String(it.status || "");
  const step = /^rendering (\S+)$/.exec(s), fin = /^finishing link (\d+)\//.exec(s);
  const cur = step ? ids.indexOf(step[1]) : fin ? ids.indexOf("finish_F0_l" + fin[1]) : ids.indexOf({
    "decoding the take's audio": "audio_take", "decoding the draft take": "take_draft",
    "assembling draft preview": "assemble_draft", "decoding the take": "take_final_F0",
    "assembling": "assemble_final" }[s]);
  const doneTo = s === "completed" ? ids.length
    : s === "draft_ready" ? ids.indexOf("assemble_draft") + 1 : Math.max(0, cur);
  return ids.map(function (id, k) {
    return { id: id, state: k < doneTo ? "done" : (k === cur && vsIsLive(it) ? "running" : "pending") };
  });
}

function vsChainProgress(it) {
  const plan = vsChainPlan(it);
  if (!plan) return null;
  const n = (it.prompts || []).length || 1;
  const now = Date.now() / 1000 + VS_CLOCK_SKEW;
  let tot = 0, done = 0, act = 0, actEst = 0, pend = 0, run = null, runEst = 0;
  plan.forEach(function (s) {
    const e = vsStepEst(s.id, n);
    tot += e;
    if (s.state === "done") {
      done += e;
      if (s.seconds != null && !s.reused) { act += s.seconds; actEst += e; }
    } else if (s.state === "running") { run = s; runEst = e; } else pend += e;
  });
  const pace = actEst > 0 ? act / actEst : 1;      // this machine against the estimates
  let runFrac = 0, runLeft = runEst * pace;
  if (run && run.t0) {
    const el = Math.max(0, now - run.t0);
    runFrac = Math.min(0.95, el / (runEst * pace));
    runLeft = Math.max(runEst * pace * 0.05, runEst * pace - el);
  }
  const live = vsIsLive(it);
  return {
    plan: plan, n: n, run: live ? run : null, runFrac: runFrac,
    failed: !live && !!run,                     // the run stopped inside this step
    pct: tot ? Math.min(100, (100 * (done + runEst * runFrac)) / tot) : 0,
    // without any timing (a rebuilt plan) there is nothing to estimate from
    left: live && (actEst > 0 || (run && run.t0)) ? runLeft + pend * pace : null,
    took: act,
  };
}

function vsDur(s) {
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + " s";
  if (s < 3600) return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  return Math.floor(s / 3600) + " h " + String(Math.floor((s % 3600) / 60)).padStart(2, "0") + " min";
}
function vsLeft(s) {
  if (s == null) return "";
  return s < 60 ? "under a minute left" : "~" + Math.round(s / 60) + " min left";
}

/* Fill (first call) or update (later calls) a progress element: the segmented
   bar, the current step, the percentage and time left, and the farm's live
   sampler line. Only text and widths change between calls, so nothing flashes. */
function vsChainPaint(el, it) {
  const p = vsChainProgress(it);
  if (!p) { el.textContent = ""; return; }
  const sig = p.plan.map(function (s) { return s.id; }).join(",");
  if (el.__sig !== sig) {
    el.__sig = sig;
    el.innerHTML = '<div class="vs-cbar">' + p.plan.map(function (s) {
      return '<i class="k' + vsStepStage(s.id) + '" style="flex-grow:' + vsStepEst(s.id, p.n) +
        '" data-tip="' + escapeHtml(vsStepLabel(s.id, p.n)) + '"><b></b></i>';
    }).join("") + "</div>" +
      '<div class="vs-cprog-row"><span class="lbl"></span><span class="pct"></span></div>' +
      '<div class="vs-cprog-sub"><span class="live" data-live="' + escapeHtml(it.id) + '"></span>' +
      '<span class="eta"></span></div>';
  }
  const blocks = el.querySelectorAll(".vs-cbar i");
  p.plan.forEach(function (s, k) {
    const b = blocks[k];
    const cls = "k" + vsStepStage(s.id) + " " +
      (s.state === "running" ? (p.failed ? "fail" : "run") : s.state);
    if (b.className !== cls) b.className = cls;
    const w = s.state === "running" && !p.failed ? Math.round(p.runFrac * 100) + "%" : "";
    if (b.firstChild.style.width !== w) b.firstChild.style.width = w;
  });
  const lbl = p.failed ? "Stopped at: " + vsStepLabel(p.plan.find(function (s) { return s.state === "running"; }).id, p.n)
    : p.run ? vsStepLabel(p.run.id, p.n) : (it.status === "draft_ready" ? "Draft ready · 1080p not started" : "");
  const set = function (sel, txt) {
    const x = el.querySelector(sel);
    if (x && x.textContent !== txt) x.textContent = txt;
  };
  set(".lbl", lbl);
  set(".pct", vsIsLive(it) ? Math.floor(p.pct) + "%" : "");
  set(".eta", vsLeft(p.left));
  vsLivePaintEl(el.querySelector("[data-live]"));
}

/* the stepper in the player: one row per stage, one pill per step with its time */
function vsStepperHtml(it, p) {
  const sz = function (a) { return Array.isArray(a) && a.length === 2 ? a[0] + "×" + a[1] : ""; };
  const h3 = vsSeamless(it) === "h3";
  const rows = [[h3 ? "Base" : "Draft", sz(it.draft_size)], ["Preview", ""],
                ["1080p", sz(it.final_size)], ["Delivery", ""]];
  const by = [[], [], [], []];
  p.plan.forEach(function (s) { by[vsStepStage(s.id)].push(s); });
  return '<div class="vs-stages">' + rows.map(function (r, k) {
    if (!by[k].length) return "";
    return '<div class="vs-stg"><div class="vs-stg-h"><b>' + r[0] + "</b>" +
      (r[1] ? "<span>" + r[1] + "</span>" : "") + '</div><div class="vs-stg-steps">' +
      by[k].map(function (s) {
        const state = s.state === "running" ? (p.failed ? "fail" : "running") : s.state;
        const t = s.state === "done" ? (s.seconds != null ? vsDur(s.seconds) : (s.reused ? "reused" : ""))
          : s.state === "running" && !p.failed && s.t0 ? '<span data-since="' + s.t0 + '"></span>' : "";
        const tip = vsStepLabel(s.id, p.n) + (s.redrawn ? " · drawn twice (seam check)" : "");
        return '<span class="vs-pill ' + state + (s.reused ? " reused" : "") + '" data-tip="' +
          escapeHtml(tip) + '"><em>' + escapeHtml(vsStepLabel(s.id, p.n, true)) + "</em>" +
          (t ? "<small>" + t + "</small>" : "") + "</span>";
      }).join("") + "</div></div>";
  }).join("") + "</div>";
}

/* every playable piece of a chain, in order: each segment (its 1080p preview once
   finished, else its draft), then the draft take and the final take */
function vsChainSources(it) {
  const out = [];
  const lp = it.link_previews || {};
  const p = vsChainProgress(it);
  const n = (it.prompts || []).length;
  const url = function (name) {
    return "/api/videostudio/chain/" + encodeURIComponent(it.id) + "/preview/" + encodeURIComponent(name);
  };
  if (vsSeamless(it) === "ltx" && it.link_previews) {     // records from before previews have none
    for (let i = 1; i <= n; i++) {
      const pv = lp[String(i)] || {};
      const st = function (pre) {
        const s = p && p.plan.find(function (x) { return x.id === pre + i; });
        return s ? s.state : "";
      };
      const fin = st("finish_F0_l"), dr = st("draft_l");
      out.push({ key: "seg" + i, n: i, kind: "seg",
        url: pv.final ? url(pv.final) : pv.draft ? url(pv.draft) : null,
        tag: pv.final ? "1080p" : pv.draft ? "draft" : (dr === "running" || fin === "running") ? "rendering" : "waiting",
        cap: "Segment " + i + " of " + n + (pv.final ? " · 1080p" : pv.draft ? " · draft" : ""),
        busy: (dr === "running" || fin === "running") && vsIsLive(it) });
    }
  }
  const nm = function (p2) { return p2 ? String(p2).split(/[\\/]/).pop() : null; };
  if (it.draft_video) {
    out.push({ key: "draft", kind: "take", url: vsFileUrl(nm(it.draft_video)), tag: "draft take",
               cap: "Draft take · the whole chain" });
  }
  if (it.final_video) {
    out.push({ key: "final", kind: "take", url: vsFileUrl(nm(it.final_video)), tag: "final",
               cap: "Final take · 1080p" });
  }
  return out;
}

function vsCardHtml(it) {
  const st = vsState(it);
  const live = vsIsLive(it);
  const take = vsIsTake(it);
  const key = escapeHtml(vsItemKey(it));
  const warn = vsWarnings(it);
  // a live chain without a take yet shows its newest segment
  const seg = !it.video && it.kind === "chain"
    ? vsChainSources(it).filter(function (s) { return s.url; }).pop() : null;
  const src = it.video ? vsFileUrl(it.video) : seg ? seg.url : null;
  const thumb = src
    ? '<video muted loop playsinline preload="metadata" data-src="' + src + '#t=0.1"></video>'
    : '<div class="vs-thumb-ph">' + Icon.get(live ? "clock" : (it.status === "failed" ? "warning" : "video")) + "</div>";
  const tags = [];
  if (it.kind === "chain") {
    tags.push('<span class="vs-tag" data-tip="' + (it.segments || "?") + " segments, " +
      (vsSeamless(it) ? "one seamless take" : "joined into one video") + '">' + Icon.get("link") +
      (it.segments || "?") + "</span>");
  }
  if (take) {
    tags.push('<span class="vs-tag accent" data-tip="Continuous take of the movie: project ' +
      escapeHtml(it.origin.project) + ", shots " + escapeHtml((it.origin.shots || []).join(", ")) + '">movie</span>');
  }
  let progress = "";
  if (it.kind === "clip" && it.status === "running") {
    progress = '<div class="vs-prog" id="vs-prog-' + escapeHtml(it.id) + '">' +
      '<div class="vs-prog-bar"><div class="vs-prog-fill"></div></div>' +
      '<div class="vs-prog-txt">waiting for the farm…</div></div>';
  } else if (it.kind === "chain" && live && vsChainPlan(it)) {
    // filled and updated in place by vsChainPaint
    progress = '<div class="vs-cprog" data-cprog="' + escapeHtml(it.id) + '"></div>';
  } else if (it.kind === "chain" && live) {
    const pct = it.total ? Math.round(((it.current || 0) / it.total) * 100) : 0;
    progress = '<div class="vs-prog"><div class="vs-prog-bar"><div class="vs-prog-fill" style="width:' + pct +
      '%"></div></div><div class="vs-prog-txt">' + escapeHtml(it.status || "") + "</div></div>";
  }
  const acts = [];
  if (it.status === "draft_ready" && vsSeamless(it) && !it.legacy) {
    acts.push('<button class="btn small primary" type="button" data-vs-finish="' + escapeHtml(it.id) +
      '" data-tip="Render the 1080p pass from the draft">Finish 1080p</button>');
  }
  if (it.video && it.status === "completed") {
    acts.push('<button class="btn small ghost icon" type="button" data-vs-cont="' + key +
      '" data-tip="Continue from its last frame">' + Icon.get("link") + "</button>");
    acts.push('<button class="btn small ghost icon" type="button" data-vs-up="' + escapeHtml(it.video) +
      '" data-tip="Upscale on a worker (RIFE 2× + RealESRGAN 4×)">' + Icon.get("wand") + "</button>");
  }
  if (it.video) {
    acts.push('<a class="btn small ghost icon" href="' + vsFileUrl(it.video) + '" download data-tip="Download">' +
      Icon.get("download") + "</a>");
  }
  return '<div class="vs-thumb" data-vs-open="' + key + '">' + thumb +
      '<span class="vs-st ' + st.cls + '">' + escapeHtml(st.label) + "</span>" +
      (tags.length ? '<span class="vs-tags">' + tags.join("") + "</span>" : "") +
      '<span class="vs-dur"></span>' +
    "</div>" +
    '<div class="vs-item-body">' +
      '<div class="vs-item-title" data-vs-open="' + key + '">' + escapeHtml(vsTitle(it)) + "</div>" +
      '<div class="vs-item-meta">' + vsSource(it) + "<span>" + escapeHtml(vsAgo(it.ts)) + "</span></div>" +
      (warn ? '<div class="vs-item-warn">' + escapeHtml(warn) + "</div>" : "") +
      (it.error && it.status === "failed" ? '<div class="vs-item-err" data-tip="' + escapeHtml(it.error) + '">' +
        escapeHtml(String(it.error).split("\n")[0]) + "</div>" : "") +
      progress +
      (acts.length ? '<div class="vs-item-acts">' + acts.join("") + "</div>" : "") +
    "</div>";
}

async function vsRefreshHistory() {
  let r = null;
  try { r = await api("/api/videostudio/history?limit=80"); } catch (e) { return; }
  if (!r || !Array.isArray(r.items)) return;
  if (typeof r.now === "number") VS_CLOCK_SKEW = r.now - Date.now() / 1000;
  VS_HIST = r.items;
  vsHistPaint();
}

/* every second while a chain renders: the bar, the time left and the step clocks
   move on their own between the 5 s refreshes (text and widths only) */
function vsTick() {
  const page = vsEl("page-videostudio");
  if (!page || !page.classList.contains("active") || document.visibilityState !== "visible") return;
  if (!VS_HIST.some(function (it) { return it.kind === "chain" && vsIsLive(it); })) return;
  document.querySelectorAll("[data-cprog]").forEach(function (el) {
    const it = vsHistFind("chain:" + el.dataset.cprog);
    if (it) vsChainPaint(el, it);
  });
  const now = Date.now() / 1000 + VS_CLOCK_SKEW;
  document.querySelectorAll("[data-since]").forEach(function (el) {
    const t = vsDur(now - Number(el.dataset.since));
    if (el.textContent !== t) el.textContent = t;
  });
}

function vsHistPaint() {
  const box = vsEl("vs-jobs");
  if (!box) return;
  const items = VS_HIST.filter(function (it) {
    if (VS_HIST_FILTER === "movie") return vsIsTake(it);
    return VS_HIST_FILTER === "all" || it.kind === VS_HIST_FILTER;
  });
  const clips = VS_HIST.filter(function (it) { return it.kind === "clip"; }).length;
  const meta = vsEl("vs-jobs-meta");
  const metaTxt = VS_HIST.length ? clips + " clips · " + (VS_HIST.length - clips) + " chains" : "";
  if (meta.textContent !== metaTxt) meta.textContent = metaTxt;
  if (!items.length) {
    box.innerHTML = '<div class="empty">' + Icon.get("video") +
      "<div>" + (VS_HIST.length ? "Nothing in this view" : "No renders yet") + "</div>" +
      '<div class="empty-hint">' + (VS_HIST.length ? "Pick another filter"
        : "Clips and chains you generate appear here, the movie's takes too") + "</div></div>";
    vsLiveDetach();
    return;
  }
  const prev = new Map();
  box.querySelectorAll(":scope > .vs-item").forEach(function (el) { prev.set(el.dataset.key, el); });
  const els = items.map(function (it) {
    const key = vsItemKey(it);
    const html = vsCardHtml(it);
    let el = prev.get(key);
    if (!el || el.__html !== html) {
      el = document.createElement("article");
      el.className = "vs-item" + (vsIsLive(it) ? " live" : "");
      el.dataset.key = key;
      el.innerHTML = html;
      el.__html = html;
      vsThumbBind(el);
    }
    return el;
  });
  // Touch only what changed: removing and re-adding every card (replaceChildren)
  // reset every thumbnail video, so the whole list flashed on each 5 s refresh
  // while a render was live. Cards that are gone or rebuilt leave, new ones go
  // in at their place, and unchanged cards stay where they are.
  const want = new Set(els);
  Array.from(box.children).forEach(function (c) { if (!want.has(c)) c.remove(); });
  let cur = box.firstElementChild;
  els.forEach(function (el) {
    if (el === cur) { cur = cur.nextElementSibling; return; }
    box.insertBefore(el, cur);
  });
  // the farm's live sampler line follows a rendering clip, else a seamless chain
  const running = items.find(function (it) { return it.kind === "clip" && it.status === "running"; }) ||
    items.find(function (it) { return it.kind === "chain" && vsIsLive(it) && vsSeamless(it); });
  if (running) vsLiveAttach(running); else vsLiveDetach();
  const veil = vsEl("vs-player");
  if (veil && !veil.classList.contains("hidden") && VS_PLAYER.key) {
    const it = vsHistFind(VS_PLAYER.key);
    if (it) vsPlayerUpdate(it, false);
  }
  vsTick();
}

/* lazy first frame, muted preview on hover, real duration once known */
function vsThumbBind(card) {
  const v = card.querySelector(".vs-thumb video");
  if (!v) return;
  if (VS_THUMBS) VS_THUMBS.observe(v);
  else { v.src = v.dataset.src; v.removeAttribute("data-src"); }
  v.addEventListener("loadedmetadata", function () {
    const d = card.querySelector(".vs-dur");
    if (d && isFinite(v.duration)) d.textContent = v.duration.toFixed(1) + " s";
    // a vertical video is shown whole in the 16:9 thumbnail, not cropped to its middle
    card.querySelector(".vs-thumb").classList.toggle("portrait", v.videoHeight > v.videoWidth);
  });
  const thumb = card.querySelector(".vs-thumb");
  thumb.addEventListener("mouseenter", function () {
    if (v.src) v.play().catch(function () { /* not loaded yet */ });
  });
  thumb.addEventListener("mouseleave", function () {
    v.pause();
    if (v.readyState) v.currentTime = 0.1;
  });
}

function vsHistFind(key) {
  return VS_HIST.find(function (it) { return vsItemKey(it) === key; }) || null;
}

async function vsFinishChain(cid, btn) {
  if (btn) btn.disabled = true;
  const r = await post("/api/videostudio/chain/" + encodeURIComponent(cid) + "/finish", {});
  if (!r || r.ok === false) {
    toast((r && r.error) || "finish failed", true);
    if (btn) btn.disabled = false;
    return;
  }
  toast("finishing " + cid + " at 1080p");
  vsRefreshHistory();
}

/* -------------------------------------------------------------- player
   A clip: its video and details. A seamless chain: the stage shows the newest
   piece (the final take, else the newest 1080p segment, else the draft take,
   else the newest draft segment); the strip below holds every segment as it
   arrives plus both takes; the side panel leads with the progress and a stepper
   of every step with its time. An open player follows the History refreshes:
   new pieces join the strip without reloading the ones already there, and the
   stage switches by itself only while nothing plays and nothing was picked. */
const VS_PLAYER = { key: null, pick: null, url: null, sources: [] };

function vsPlayerOpen(it) {
  VS_PLAYER.key = vsItemKey(it);
  VS_PLAYER.pick = null;
  VS_PLAYER.url = null;
  const veil = vsEl("vs-player");
  veil.dataset.key = VS_PLAYER.key;
  vsEl("vs-player-strip").replaceChildren();
  vsEl("vs-player-info").__html = null;
  vsPlayerUpdate(it, true);
  veil.classList.remove("hidden");
  vsTick();
}

function vsPlayerClose() {
  const veil = vsEl("vs-player");
  if (!veil || veil.classList.contains("hidden")) return;
  const v = vsEl("vs-player-video");
  v.pause();
  v.removeAttribute("src");
  v.load();
  vsEl("vs-player-strip").replaceChildren();
  VS_PLAYER.key = VS_PLAYER.url = VS_PLAYER.pick = null;
  veil.classList.add("hidden");
}

function vsPlayerSources(it) {
  if (it.kind === "chain" && vsSeamless(it)) return vsChainSources(it);
  return it.video ? [{ key: "main", kind: "take", url: vsFileUrl(it.video), cap: "", tag: "" }] : [];
}

function vsPlayerUpdate(it, first) {
  const card = document.querySelector("#vs-player .vs-player-card");
  const sources = vsPlayerSources(it);
  VS_PLAYER.sources = sources;
  const media = sources.filter(function (s) { return s.url; });
  const chain = it.kind === "chain" && !!vsSeamless(it);
  const p = chain ? vsChainProgress(it) : null;
  const waiting = chain && vsIsLive(it) && !media.length;
  const strip = chain && sources.length > 1;
  card.classList.toggle("no-media", !media.length && !waiting);
  card.classList.toggle("has-strip", strip);
  const size = it.final_size || it.draft_size;
  vsEl("vs-player-strip").style.setProperty("--ar", Array.isArray(size) ? size[0] + " / " + size[1] : "16 / 9");
  vsStripPaint(strip ? sources : []);

  const v = vsEl("vs-player-video");
  const newest = function (k, tag) {
    return media.filter(function (s) { return s.kind === k && (!tag || s.tag === tag); }).pop();
  };
  const best = media.find(function (s) { return s.key === "final" || s.key === "main"; }) ||
    newest("seg", "1080p") || media.find(function (s) { return s.key === "draft"; }) || newest("seg");
  const target = (VS_PLAYER.pick && media.find(function (s) { return s.key === VS_PLAYER.pick; })) || best;
  if (target && target.url !== VS_PLAYER.url && (first || !VS_PLAYER.url || v.paused || v.ended)) {
    vsPlayerShow(target, first);
  }
  v.classList.toggle("hidden", !VS_PLAYER.url);
  if (!VS_PLAYER.url) vsEl("vs-player-cap").textContent = "";
  const wait = vsEl("vs-player-wait");
  wait.classList.toggle("hidden", !waiting);
  if (waiting) {
    const lbl = p && p.run ? vsStepLabel(p.run.id, p.n) : "Starting";
    const html = Icon.get("clock") + "<b>" + escapeHtml(lbl) + "</b>" +
      "<span>Each segment shows up here and in the strip below as soon as it is rendered.</span>";
    if (wait.__html !== html) { wait.innerHTML = html; wait.__html = html; }
  }

  const info = vsEl("vs-player-info");
  const html = vsPlayerInfo(it);
  if (info.__html !== html) {
    const top = info.scrollTop;
    info.innerHTML = html;
    info.__html = html;
    info.scrollTop = top;
  }
  info.querySelectorAll("[data-cprog]").forEach(function (el) { vsChainPaint(el, it); });
}

function vsPlayerShow(s, play) {
  const v = vsEl("vs-player-video");
  v.src = s.url;
  v.classList.remove("hidden");
  VS_PLAYER.url = s.url;
  const cap = vsEl("vs-player-cap");
  cap.textContent = s.cap || "";
  vsEl("vs-player-strip").querySelectorAll(".vs-tile").forEach(function (t) {
    t.classList.toggle("on", t.dataset.url === s.url);
  });
  if (play) v.play().catch(function () { /* needs a user gesture */ });
}

/* keyed like the History cards: a tile is rebuilt only when its video changes */
function vsStripPaint(sources) {
  const strip = vsEl("vs-player-strip");
  strip.classList.toggle("hidden", !sources.length);
  const prev = new Map();
  strip.querySelectorAll(":scope > .vs-tile").forEach(function (t) { prev.set(t.dataset.key, t); });
  const els = sources.map(function (s) {
    let t = prev.get(s.key);
    if (!t || t.dataset.url !== (s.url || "")) {
      t = document.createElement("button");
      t.type = "button";
      t.dataset.key = s.key;
      t.dataset.url = s.url || "";
      t.innerHTML = (s.url ? '<video muted loop playsinline preload="metadata" src="' + s.url + '#t=0.1"></video>'
                           : '<div class="vs-tile-ph">' + Icon.get("clock") + "</div>") +
        (s.n ? '<span class="vs-tile-n">' + s.n + "</span>" : "") + '<span class="vs-tile-tag"></span>';
      const vid = t.querySelector("video");
      if (vid) {
        t.addEventListener("mouseenter", function () { vid.play().catch(function () { /* not loaded yet */ }); });
        t.addEventListener("mouseleave", function () { vid.pause(); });
      }
    }
    const cls = "vs-tile" + (s.kind === "take" ? " take" : "") + (s.url ? "" : " pend") +
      (s.busy ? " busy" : "") + (s.url && s.url === VS_PLAYER.url ? " on" : "");
    if (t.className !== cls) t.className = cls;
    const tag = t.querySelector(".vs-tile-tag");
    if (tag.textContent !== s.tag) tag.textContent = s.tag;
    tag.classList.toggle("hi", s.tag === "1080p" || s.tag === "final");
    t.dataset.tip = s.cap;
    t.disabled = !s.url;
    return t;
  });
  const want = new Set(els);
  Array.from(strip.children).forEach(function (c) { if (!want.has(c)) c.remove(); });
  let cur = strip.firstElementChild;
  els.forEach(function (el) {
    if (el === cur) { cur = cur.nextElementSibling; return; }
    strip.insertBefore(el, cur);
  });
}

function vsPlayerInfo(it) {
  const st = vsState(it);
  const kv = function (k, val) {
    return val == null || val === "" ? "" :
      '<div class="vs-kv"><span>' + escapeHtml(k) + "</span><b>" + escapeHtml(val) + "</b></div>";
  };
  const prompts = it.kind === "clip" ? [it.prompt].filter(Boolean) : (it.prompts || []);
  const head = '<div class="vs-pi-head"><span class="vs-st ' + st.cls + '">' + escapeHtml(st.label) + "</span>" +
    '<span class="mono">' + escapeHtml(it.id) + "</span>" +
    '<span class="spacer"></span><span class="muted small">' + escapeHtml(vsAgo(it.ts)) + "</span></div>";
  const p = it.kind === "chain" ? vsChainProgress(it) : null;
  // the bar while it renders (or where it stopped), then every step with its time
  const progHtml = p
    ? ((vsIsLive(it) || p.failed || it.status === "draft_ready")
        ? '<div class="vs-cprog big" data-cprog="' + escapeHtml(it.id) + '"></div>' : "") +
      vsStepperHtml(it, p)
    : "";
  const runLink = p && p.run ? vsStepLink(p.run.id) : 0;
  const promptHtml = prompts.length
    ? (p ? '<div class="vs-pi-sec">Segments</div>' : "") +
      '<ol class="vs-pi-prompts' + (prompts.length > 1 ? "" : " single") + '">' +
      prompts.map(function (t, i) {
        return "<li" + (runLink === i + 1 ? ' class="on"' : "") + ">" + escapeHtml(t) + "</li>";
      }).join("") + "</ol>"
    : "";
  const sz = function (a) { return Array.isArray(a) && a.length === 2 ? a[0] + "×" + a[1] : ""; };
  // an LTX seamless take: link 1 plus each later link's new frames (it renders
  // the carry plus frames - 1 new ones: chain_studio.ltx_link_frames)
  const lf = it.link_frames || [];
  const carry = lf.length > 1 ? lf[1] - lf[0] + 1 : 0;
  const takeFrames = vsSeamless(it) === "ltx" && lf.length
    ? lf.slice(1).reduce(function (a, f) { return a + f - carry; }, lf[0]) : 0;
  const rows = [
    kv("Model", vsModelLabel(it.model)),
    vsIsTake(it) ? kv("Movie", it.origin.project + " · " + (it.origin.shots || []).join(", ")) : "",
    it.kind === "chain" ? kv("Chain", (it.segments || "?") + " segments · " +
      (vsSeamless(it) ? "seamless " + vsSeamless(it).toUpperCase() + " take" : "tail-frame continuation")) : "",
    it.kind === "chain" && it.final_size
      ? kv("Size", sz(it.final_size) + (it.draft_size ? " · draft " + sz(it.draft_size) : "")) : "",
    it.width ? kv("Size", it.width + "×" + it.height) : "",
    takeFrames ? kv("Length", takeFrames + " frames · " + (takeFrames / 24).toFixed(2) + " s at 24 fps") : "",
    kv(it.kind === "chain" ? "Frames / segment" : "Frames", it.frames),
    kv("Steps", it.steps),
    it.seed != null && it.seed !== -1 ? kv("Seed", it.seed) : "",
    it.seconds != null ? kv("Render", Math.round(it.seconds) + " s") : "",
    p && it.status === "completed" && p.took ? kv("Render", "about " + Math.round(p.took / 60) + " min (all steps)") : "",
    (it.references || []).length ? kv("References", it.references.length) : "",
    (it.loras || []).length ? kv("LoRAs", it.loras.length) : "",
    it.worker && it.worker !== "local" ? kv("Worker", it.worker) : "",
    !p && vsIsLive(it) && ["running", "queued"].indexOf(it.status) < 0 ? kv("Stage", it.status) : "",
  ].join("");
  const qa = [vsSeamQa("Draft", it.draft_qa), vsSeamQa("Final", it.qa),
              vsCastReport(it.cast || it.draft_cast)].join("");
  const warn = vsWarnings(it);
  const files = [];
  if (it.draft_video && it.video && String(it.draft_video).indexOf(it.video) < 0) {
    const dn = String(it.draft_video).split(/[\\/]/).pop();
    files.push('<a href="' + vsFileUrl(dn) + '" target="_blank" rel="noopener">draft take</a>');
  }
  if (it.legacy || (it.kind === "chain" && !vsSeamless(it))) {
    (it.outputs || []).forEach(function (o, i) {
      const n = String(o).split(/[\\/]/).pop();
      if (n !== it.video) files.push('<a href="' + vsFileUrl(n) + '" target="_blank" rel="noopener">segment ' + (i + 1) + "</a>");
    });
  }
  const acts = [];
  if (it.status === "draft_ready" && vsSeamless(it) && !it.legacy) {
    acts.push('<button class="btn primary" type="button" data-vs-finish="' + escapeHtml(it.id) + '">Finish at 1080p</button>');
  }
  if (it.video && it.status === "completed") {
    acts.push('<button class="btn" type="button" data-vs-cont="' + escapeHtml(vsItemKey(it)) + '">' +
      Icon.get("link") + "Continue from here</button>");
    acts.push('<button class="btn" type="button" data-vs-up="' + escapeHtml(it.video) + '">' +
      Icon.get("wand") + "Upscale</button>");
  }
  if (it.video) {
    acts.push('<a class="btn" href="' + vsFileUrl(it.video) + '" download>' + Icon.get("download") + "Download</a>");
  }
  return head + progHtml + promptHtml +
    '<div class="vs-pi-rows">' + rows + "</div>" +
    (qa ? '<div class="vs-pi-sec">Quality</div><div class="vs-pi-rows">' + qa + "</div>" : "") +
    (files.length ? '<div class="vs-pi-sec">Files</div><div class="vs-pi-files">' + files.join("") + "</div>" : "") +
    (warn ? '<div class="vs-item-warn">' + escapeHtml(warn) + "</div>" : "") +
    (it.error ? '<pre class="vs-pi-err">' + escapeHtml(it.error) + "</pre>" : "") +
    (acts.length ? '<div class="vs-pi-acts">' + acts.join("") + "</div>" : "");
}

/* one-time wiring of the history panel and the player */
let VS_HIST_BOUND = false;
function vsHistBind() {
  if (VS_HIST_BOUND) return;
  VS_HIST_BOUND = true;
  document.querySelectorAll("#vs-hist-filter button").forEach(function (b) {
    b.onclick = function () {
      VS_HIST_FILTER = b.dataset.f;
      document.querySelectorAll("#vs-hist-filter button").forEach(function (x) {
        x.classList.toggle("on", x === b);
      });
      vsHistPaint();
    };
  });
  const onAct = function (e) {
    const open = e.target.closest("[data-vs-open]");
    const fin = e.target.closest("[data-vs-finish]");
    const cont = e.target.closest("[data-vs-cont]");
    if (fin) { vsFinishChain(fin.dataset.vsFinish, fin); return; }
    if (cont) {
      const it = vsHistFind(cont.dataset.vsCont);
      if (it) { vsPlayerClose(); vsContinueFrom(it); }
      return;
    }
    if (open) {
      const it = vsHistFind(open.dataset.vsOpen);
      if (it) vsPlayerOpen(it);
    }
  };
  vsEl("vs-jobs").addEventListener("click", onAct);
  vsEl("vs-player-info").addEventListener("click", onAct);
  vsEl("vs-player-strip").addEventListener("click", function (e) {
    const t = e.target.closest(".vs-tile");
    const s = t && !t.disabled && VS_PLAYER.sources.find(function (x) { return x.key === t.dataset.key; });
    if (s && s.url) {
      VS_PLAYER.pick = s.key;
      vsPlayerShow(s, true);
    }
  });
  vsEl("vs-player-close").onclick = vsPlayerClose;
  vsEl("vs-player").addEventListener("mousedown", function (e) {
    if (e.target === vsEl("vs-player")) vsPlayerClose();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") vsPlayerClose();
  });
}

/* --------------------------------------------------------- boot */
Shell.mount("tools");
{
  const first = Shell.initialPage();
  if (first) go(first);
}
