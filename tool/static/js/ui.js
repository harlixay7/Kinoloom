/* ui.js — shared DOM/format helpers for the filmmaking workspace (no framework). */
(function () {
  "use strict";
  /* API is read lazily so ui.js works regardless of script load order. */
  function api() { return window.API; }

  /* ------------------------------------------------------------ selectors */
  function $(id) {
    return document.getElementById(id);
  }
  function $$(sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  }

  /* ---------------------------------------------------------------- escape */
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (m) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m]));

  /* ----------------------------------------------------------------- toast */
  let toastTimer = null;
  function toast(msg, isErr) {
    const el = $("toast");
    if (!el) return;
    el.textContent = msg || "";
    el.className = "toast show" + (isErr ? " err" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.className = "toast"; }, 4500);
  }

  /* -------------------------------------------------------------- requests */
  async function get(url, timeoutMs) {
    const r = await api().get(url, timeoutMs);
    if (r === null) return null;
    if (r && r.ok === false) { toast(r.error || "request failed", true); return r; }
    return r;
  }
  async function post(url, body, timeoutMs) {
    const r = await api().post(url, body, timeoutMs);
    if (r === null) return null;
    if (r && r.ok === false) { toast(r.error || "request failed", true); return r; }
    return r;
  }

  /* ---------------------------------------------------------------- modals */
  function openModal(html, opts) {
    opts = opts || {};
    const root = $("modal-root");
    if (!root) return null;
    const veil = document.createElement("div");
    veil.className = "ws-modal";
    veil.innerHTML = '<div class="ws-modal-card' + (opts.wide ? " wide" : "") + '">' +
      (opts.title ? '<div class="ws-modal-head"><h3>' + esc(opts.title) +
        '</h3><button class="ws-modal-x" type="button" title="close">' + Icon.get("x") + "</button></div>" : "") +
      '<div class="ws-modal-body">' + html + "</div></div>";
    root.appendChild(veil);
    veil.addEventListener("click", function (e) {
      if (e.target === veil) closeModal(veil);
    });
    const x = veil.querySelector(".ws-modal-x");
    if (x) x.onclick = function () { closeModal(veil); };
    return veil;
  }
  function closeModal(veil) {
    if (veil && veil.parentNode) veil.parentNode.removeChild(veil);
  }
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    const root = $("modal-root");
    if (root && root.lastElementChild) closeModal(root.lastElementChild);
  });

  /* --------------------------------------------------------------- formats */
  const fmt = {
    num: function (x) { return Number(x || 0).toLocaleString(); },
    s: function (x) {
      x = Number(x || 0);
      if (x < 60) return Math.round(x) + "s";
      const m = Math.floor(x / 60), s = Math.round(x % 60);
      return m + "m" + (s ? " " + s + "s" : "");
    },
    min: function (x) {
      x = Number(x || 0);
      if (x <= 0) return "0m";
      return Math.round(x) + "m";
    },
    pct: function (x) { return Math.round(Number(x || 0) * 100) + "%"; },
    mb: function (x) { return Number(x || 0).toFixed(1) + " MB"; },
    ts: function (iso) {
      if (!iso) return "";
      try { return new Date(iso).toLocaleString(); } catch (e) { return String(iso); }
    },
    ago: function (iso) {
      if (!iso) return "";
      try {
        const d = (Date.now() - new Date(iso).getTime()) / 1000;
        if (d < 60) return Math.round(d) + "s ago";
        if (d < 3600) return Math.round(d / 60) + "m ago";
        return Math.round(d / 3600) + "h ago";
      } catch (e) { return ""; }
    },
  };

  /* ---------------------------------------------------------------- badges */
  const STATUS_COLOR = {
    queued: "amber", rendering: "blue", post: "blue", qa: "blue", drafting: "blue",
    passed: "green", review: "red", failed: "gray", pending: "amber", assembled: "green",
  };
  function statusClass(status) { return "st-" + (STATUS_COLOR[status] || "gray"); }
  function tierClass(tier) { return "tier-" + (tier || "unknown"); }
  function clsNum(x) {
    x = Number(x);
    if (isNaN(x) || x == null) return "dim";
    return x >= 0.7 ? "good" : x >= 0.45 ? "mid" : "bad";
  }

  /* ------------------------------------------------------------------ URLs */
  function normPath(p) { return String(p || "").replace(/\\/g, "/"); }
  function basename(p) {
    p = normPath(p);
    const i = p.lastIndexOf("/");
    return i >= 0 ? p.slice(i + 1) : p;
  }
  const URLS = {
    kf: function (sid, path) {
      if (!path) return null;
      const m = normPath(path).split("keyframes/");
      const rel = m.length > 1 ? m.pop() : normPath(path);
      const pid = (window.State && window.State.pid) || "_";
      return "/api/keyframes/" + encodeURIComponent(pid) + "/" +
        encodeURIComponent(sid) + "/" + encodeURIComponent(basename(rel));
    },
    clip: function (path) {
      if (!path) return null;
      // file-name lookup: the clip lives in output/videostudio (renders and
      // their _wsup upscales) or the legacy shots/video dirs — the old
      // /api/video/raw/<name> route only knew the legacy dir and 404'd
      return "/api/clip/" + encodeURIComponent(basename(path));
    },
    tail: function (sid) {
      const pid = (window.State && window.State.pid) || "_";
      return "/api/tails/" + encodeURIComponent(pid) + "/" + encodeURIComponent(sid);
    },
    video: function (folder, name) {
      return "/api/video/" + encodeURIComponent(folder) + "/" + encodeURIComponent(name);
    },
  };

  /* ------------------------------------------------------------ video player */
  function playVideo(src, title) {
    openModal('<video class="ws-player" controls autoplay src="' + esc(src) + '"></video>', {
      title: title || "Clip",
    });
  }

  /* ------------------------------------------------------------ empty state */
  function empty(msg, hint) {
    return '<div class="ws-empty"><div class="ws-empty-mark">' + Icon.get("film") + "</div><div>" +
      esc(msg) + "</div>" + (hint ? '<div class="ws-empty-hint">' + esc(hint) + "</div>" : "") +
      "</div>";
  }
  function noProject() {
    return empty("No project selected", "Create one with “+ New” in the sidebar.");
  }

  /* ============================================================= shot card */
  function shotCard(row, opts) {
    opts = opts || {};
    const j = row.json || {};
    const sid = row.shot_id;
    const kfUrl = URLS.kf(sid, row.keyframe_path);
    const clipUrl = URLS.clip(row.video_path);
    const finalUrl = row.final_path ? URLS.clip(row.final_path) : null;
    const tailUrl = row.tail_frame ? URLS.tail(sid) : null;
    // post state: the orchestrator marks EVERY shot "post" while the post
    // worker handles it — with post-processing OFF that is just the QA pass
    // (no upscale), so the label must not claim "upscaling" unless the
    // project actually has post enabled (it used to say "upscaling locally…"
    // on every shot, which read as a lie when post was off).
    const workerName = (window.State && State.project &&
      State.project.post_worker) || "";
    const postOn = !!(window.State && State.project &&
      State.project.post_processing);
    const inPost = (row.status === "post" && clipUrl && !finalUrl);
    const upscaling = inPost && postOn;
    let elapsed = "";
    try {
      const t = Date.parse(row.updated || "");
      if (t) elapsed = Math.max(0, Math.round((Date.now() - t) / 1000)) + "s";
    } catch (e) { /* no clock */ }
    const upPlaceholder = inPost
      ? '<div class="ph ph-up">' + Icon.get("refresh") + "<span>"
        + (upscaling ? "upscaling" : (postOn ? "post-processing queued"
                                              : "quality check"))
        + (workerName ? " on " + esc(workerName) : " locally")
        + (upscaling && elapsed ? " · " + elapsed : "") + "…</span></div>"
      : null;
    const chain = Array.isArray(j.selected_loras) ? j.selected_loras : [];
    const acts = j.primary_action || j.action || "";
    const loras = chain.map(function (c) {
      return '<span class="lora-chip' + (c.enabled === false ? " off" : "") + '">' +
        esc(c.name || "") + (c.enabled === false ? " · off" : "") + "</span>";
    }).join("");
    const qa = (opts.review || row.status === "review" || row.status === "failed") ? [
      ['clip', row.qa_clip], ['face', row.qa_face], ['motion', row.qa_motion],
    ].map(function (q) {
      return '<span class="qa-cell ' + clsNum(q[1]) + '" title="qa ' + q[0] + '">' +
        esc(q[0]) + " " + (q[1] == null ? "—" : Number(q[1]).toFixed(2)) + "</span>";
    }).join("") + (row.attempts ? '<span class="qa-cell dim" title="attempts">att ' +
      esc(row.attempts) + "</span>" : "") + (row.render_time_s
      ? '<span class="qa-cell dim" title="render time">' + fmt.s(row.render_time_s) + "</span>" : "") : "";
    const err = row.error
      ? '<div class="sc-error">' + Icon.get("warning") + "<span>" + esc(row.error) + "</span></div>" : "";
    // reference provenance: a shot that rendered WITHOUT its cast refs must
    // say so here (the warning lives in the shot JSON, not the error column)
    const refWarn = (Array.isArray(j.reference_warnings) && j.reference_warnings.length)
      ? '<div class="sc-error" style="color:#e0a35c">' + Icon.get("warning") +
        "<span>refs: " + esc(j.reference_warnings.slice(0, 2).join("; ")) + "</span></div>"
      : "";

    const el = document.createElement("div");
    el.innerHTML =
      '<article class="shot-card" data-sid="' + esc(sid) + '">' +
        '<header class="sc-head">' +
          '<span class="sc-id">' + esc(sid) + '</span>' +
          (opts.review ? '<span class="sc-scene">' + esc(row.scene || "") + '</span>' : "") +
          (j.take && j.take.id
            ? '<span class="sc-take" data-tip="Rendered with ' + esc((j.take.shots || []).join(", ")) +
              " as one seamless take (" + esc(j.take.id) + ")" +
              (j.take.seam_qa_passed === false ? ". Seam QA flagged a join" : "") + '">' + Icon.get("link") +
              esc(j.take.index) + "/" + esc((j.take.shots || []).length) + "</span>"
            : "") +
          '<span class="spacer"></span>' +
          '<span class="badge ' + tierClass(row.tier) + '">' + esc(row.tier || "—") + '</span>' +
          '<span class="badge ' + statusClass(row.status) + '">' + esc(row.status || "—") + '</span>' +
        '</header>' +
        (qa ? '<div class="sc-qa">' + qa + "</div>" : "") +
        '<div class="sc-media">' +
          '<div class="sc-frame">' +
            (kfUrl ? '<img loading="lazy" src="' + esc(kfUrl) + '" alt="">'
                   : '<div class="ph ph-kf">' + Icon.get("wand") + "<span>no keyframe</span></div>") +
            '<div class="sc-frame-tag">START</div>' +
          "</div>" +
          '<div class="sc-frame sc-clip">' +
            (finalUrl ? '<video src="' + esc(finalUrl) + '" preload="metadata" muted playsinline></video>'
                      : (upPlaceholder ? upPlaceholder
                         : (clipUrl
                            ? '<video src="' + esc(clipUrl) + '" preload="metadata" muted playsinline></video>'
                            : '<div class="ph ph-clip">' + Icon.get("play") + "<span>no clip</span></div>"))) +
            '<div class="sc-frame-tag">' + (finalUrl ? "FINAL" : "CLIP") + "</div>" +
            (finalUrl && clipUrl
              ? '<button class="sc-raw" type="button" data-act="raw" data-tip="Play the raw render (before post)">raw</button>'
              : "") +
          "</div>" +
          '<div class="sc-frame">' +
            (tailUrl ? '<img loading="lazy" src="' + esc(tailUrl) + '" alt="">'
                     : '<div class="ph">' + Icon.get("film") + "<span>no tail</span></div>") +
            '<div class="sc-frame-tag">TAIL</div>' +
          "</div>" +
        "</div>" +
        (acts ? '<div class="sc-action">' + esc(acts) + "</div>" : "") +
        (j.narrative || j.beat
          ? '<div class="sc-narr" data-tip="' + esc((j.narrative || "") + (j.beat ? " — " + j.beat : "")) + '">' +
            esc(j.narrative || "") + (j.beat ? " — " + esc(j.beat) : "") + "</div>" : "") +
        (loras ? '<div class="sc-loras">' + loras + "</div>" : "") +
        err +
        refWarn +
        '<footer class="sc-foot">' +
          '<button class="btn small ghost" type="button" data-act="' + (kfUrl ? "kf-regen" : "kf-gen") + '" data-tip="' +
            (kfUrl ? "Clear the keyframe and requeue the shot" : "Generate keyframe candidates") + '">' +
            (kfUrl ? Icon.get("refresh") : Icon.get("wand")) + "Keyframe</button>" +
          '<button class="btn small ghost" type="button" data-act="rerender" data-tip="Requeue the clip">' +
            Icon.get("refresh") + "Re-render</button>" +
          '<span class="spacer"></span>' +
          (opts.review
            ? '<button class="btn small danger" type="button" data-act="requeue">Requeue</button>' +
              '<button class="btn small primary" type="button" data-act="approve">Approve</button>'
            : "") +
          '<button class="btn small ghost icon" type="button" data-act="adv" data-tip="Advanced: prompt, LoRAs, profile">' +
            Icon.get("sliders") + "</button>" +
        "</footer>" +
        '<div class="adv" hidden></div>' +
      "</article>";
    const card = el.firstElementChild;
    bindShotCard(card, row, opts);
    return card;
  }

  function bindShotCard(card, row, opts) {
    opts = opts || {};
    const sid = row.shot_id;
    const pid = function () { return window.State.pid; };
    const changed = function () { window.Bus.emit("shots:changed"); };
    const v = card.querySelector(".sc-clip video");
    if (v) v.onclick = function () {
      playVideo(URLS.clip(row.final_path || row.video_path), sid + (row.final_path ? " — final" : " — clip"));
    };
    card.querySelectorAll("[data-act]").forEach(function (b) {
      b.onclick = async function () {
        const act = b.dataset.act;
        if (act === "raw") {
          playVideo(URLS.clip(row.video_path), sid + " — raw render");
        } else if (act === "adv") {
          const box = card.querySelector(".adv");
          const open = box.hidden;
          box.hidden = !open;
          b.classList.toggle("on", open);
          if (open && !box.dataset.bound) {
            box.appendChild(advSection(sid, row));
            box.dataset.bound = "1";
          }
        } else if (act === "kf-gen") {
          b.disabled = true;
          const r = await post("/api/projects/" + encodeURIComponent(pid()) +
            "/shots/" + encodeURIComponent(sid) + "/keyframes/generate", { count: 2 }, 900000);
          b.disabled = false;
          if (r) toast(r.ok
            ? "keyframes generated: " + ((r.candidates || []).length) + " candidate(s)"
            : r.error || "keyframe generation failed", !r.ok);
          changed();
        } else if (act === "kf-regen") {
          const r = await post("/api/projects/" + encodeURIComponent(pid()) +
            "/shots/" + encodeURIComponent(sid) + "/keyframes/regenerate", {}, 30000);
          if (r) toast(r.ok ? "keyframes cleared — shot requeued" : "failed", !r.ok);
          changed();
        } else if (act === "rerender") {
          const r = await post("/api/projects/" + encodeURIComponent(pid()) +
            "/shots/" + encodeURIComponent(sid) + "/rerender", {}, 30000);
          if (r) toast(r.ok ? "clip requeued" : "failed", !r.ok);
          changed();
        } else if (act === "approve" || act === "requeue") {
          const r = await post("/api/review/" + act,
            { shot_id: sid,
              project_id: (window.State && window.State.pid) || null },
            30000);
          if (r) toast(r.ok ? (act === "approve" ? "approved — passed" : "requeued") : "failed", !r.ok);
          changed();
        }
      };
    });
  }

  /* ======================================================= advanced expander */
  function advSection(sid, row) {
    const j = row.json || {};
    const chain = Array.isArray(j.selected_loras) ? j.selected_loras : [];
    const loraRows = chain.map(function (c, i) {
      const str = Number(c.strength != null ? c.strength : 0.7);
      return '<div class="adv-lora" data-i="' + i + '">' +
        '<label class="adv-lora-name"><input type="checkbox" data-lora-enabled' +
          (c.enabled === false ? "" : " checked") + "> " + esc(c.name || "?") + "</label>" +
        '<input type="range" min="0.05" max="1.5" step="0.05" value="' + str +
          '" data-lora-str><output>' + str.toFixed(2) + "</output></div>";
    }).join("");
    const states = JSON.stringify({
      start_state: j.start_state || {}, end_state: j.end_state || {},
      seed: row.seed || "auto",
    }, null, 2);
    const prof = j.render_profile || "";
    const regInfo = (function () {
      const loras = (window.State && State.loras) || [];
      const names = chain.filter(function (c) { return c.enabled !== false; })
        .map(function (c) { return c.name; });
      const hits = loras.filter(function (e) { return names.indexOf(e.id) >= 0 || names.indexOf(e.file) >= 0; });
      if (!hits.length) return "steps/CFG come from the enabled LoRA registry entries";
      return hits.map(function (e) {
        return esc(e.display || e.id) + ": steps " + (e.steps != null ? e.steps : "—") +
          " · cfg " + (e.cfg != null ? e.cfg : "—");
      }).join(" · ");
    })();
    const box = document.createElement("div");
    box.innerHTML =
      '<div class="adv-grid">' +
        '<div class="adv-col">' +
          '<div class="adv-label">Prompt</div>' +
          '<textarea class="adv-prompt mono" rows="4" spellcheck="false">' + esc(row.prompt || "") + "</textarea>" +
          '<div class="adv-label">Negative</div>' +
          '<textarea class="adv-neg mono" rows="3" spellcheck="false">' + esc(row.negative || "") + "</textarea>" +
          '<button class="btn small" type="button" data-act="save-prompt">Save prompts (requeues)</button>' +
        "</div>" +
        '<div class="adv-col">' +
          '<div class="adv-label">LoRA chain</div>' +
          (loraRows || '<div class="adv-note">empty — auto-resolved from the registry at render</div>') +
          '<div class="adv-note">' + regInfo + "</div>" +
          '<button class="btn small" type="button" data-act="save-loras">Save chain</button>' +
        "</div>" +
        '<div class="adv-col">' +
          '<div class="adv-label">Render profile</div>' +
          '<select class="adv-profile">' +
            '<option value="">auto</option>' +
            '<option value="preview"' + (prof === "preview" ? " selected" : "") + ">preview</option>" +
            '<option value="production_fast"' + (prof === "production_fast" ? " selected" : "") + ">production_fast</option>" +
            '<option value="effect_quality"' + (prof === "effect_quality" ? " selected" : "") + ">effect_quality</option>" +
          "</select>" +
          '<button class="btn small" type="button" data-act="profile-apply">Apply profile (requeues)</button>' +
          '<div class="adv-label">Continuity</div>' +
          '<select class="adv-continuity" data-tip="CUT = fresh composition · CONTINUE = start from the previous shot\'s tail frame · MATCH_ACTION = match the previous choreography">' +
            ["CUT", "CONTINUE", "MATCH_ACTION"].map(function (v) {
              return "<option" + ((j.continuity || "CUT") === v ? " selected" : "") + ">" + v + "</option>";
            }).join("") +
          "</select>" +
        "</div>" +
        '<div class="adv-col">' +
          '<div class="adv-label">States &amp; seed</div>' +
          '<pre class="mono adv-states">' + esc(states) + "</pre>" +
          '<div class="adv-label">Keyframe candidates</div>' +
          '<div class="adv-kf">' +
            '<button class="btn small" type="button" data-act="kf-load" data-tip="Load this shot\'s keyframe candidates for approval">Load candidates</button>' +
          "</div>" +
        "</div>" +
      "</div>";
    box.querySelector("[data-act=save-prompt]").onclick = async function () {
      const r = await post("/api/projects/" + encodeURIComponent(window.State.pid) +
        "/shots/" + encodeURIComponent(sid) + "/prompt", {
        prompt: box.querySelector(".adv-prompt").value,
        negative: box.querySelector(".adv-neg").value,
      }, 30000);
      if (r) toast(r.ok ? "prompt saved — shot requeued" : "failed", !r.ok);
      window.Bus.emit("shots:changed");
    };
    box.querySelector("[data-act=save-loras]").onclick = async function () {
      const out = [];
      box.querySelectorAll(".adv-lora").forEach(function (lr) {
        const name = chain[Number(lr.dataset.i)].name;
        out.push({
          name: name,
          strength: Number(lr.querySelector("[data-lora-str]").value),
          enabled: lr.querySelector("[data-lora-enabled]").checked,
        });
      });
      const r = await post("/api/projects/" + encodeURIComponent(window.State.pid) +
        "/shots/" + encodeURIComponent(sid) + "/loras", { chain: out }, 30000);
      if (r) toast(r.ok ? "LoRA chain saved — shot requeued" : "failed", !r.ok);
      window.Bus.emit("shots:changed");
    };
    box.querySelector("[data-act=profile-apply]").onclick = async function () {
      const r = await post("/api/projects/" + encodeURIComponent(window.State.pid) +
        "/shots/" + encodeURIComponent(sid) + "/rerender",
        { profile: box.querySelector(".adv-profile").value }, 30000);
      if (r) toast(r.ok ? "profile applied — shot requeued" : "failed", !r.ok);
      window.Bus.emit("shots:changed");
    };
    box.querySelector(".adv-continuity").onchange = async function () {
      const sel = this;
      const v = sel.value;
      const cur = (j.continuity || "CUT");
      if (v === cur) return;
      const r = await post("/api/projects/" + encodeURIComponent(window.State.pid) +
        "/shots/" + encodeURIComponent(sid) + "/continuity",
        { continuity: v }, 30000);
      if (r && r.ok) {
        j.continuity = v;
        toast("continuity → " + v + " (requeued)");
      } else {
        toast((r && r.error) || "failed", true);
        sel.value = cur;
      }
      window.Bus.emit("shots:changed");
    };
    box.querySelector("[data-act=kf-load]").onclick = async function () {
      const kfBox = box.querySelector(".adv-kf");
      kfBox.innerHTML = '<span class="muted small">loading…</span>';
      const d = await get("/api/projects/" + encodeURIComponent(window.State.pid) +
        "/shots/" + encodeURIComponent(sid), 20000);
      if (!d || !d.shot) {
        kfBox.innerHTML = '<span class="muted small">unavailable — backend restart required?</span>';
        return;
      }
      const kf = d.shot.keyframes || {};
      const files = (kf.files || []).map(function (f) {
        return typeof f === "string" ? { file: f } : f;
      });
      const approved = kf.approved || "";
      const apName = approved ? String(approved).split("/").pop() : "";
      if (!files.length) {
        kfBox.innerHTML = '<span class="muted small">no candidates — generate them with the Keyframe button</span>';
        return;
      }
      kfBox.innerHTML = files.map(function (f) {
        const name = String(f.file || "").split("/").pop();
        const isAp = name === apName;
        const url = "/api/keyframes/" + encodeURIComponent(window.State.pid) +
          "/" + encodeURIComponent(sid) + "/" + encodeURIComponent(name);
        return '<div class="adv-kf-item' + (isAp ? " approved" : "") + '">' +
          '<img loading="lazy" src="' + esc(url) + '" alt="candidate">' +
          '<div class="adv-kf-meta">' +
            (f.score != null ? "score " + Number(f.score).toFixed(3) : "") +
            (f.face ? " · face ok" : "") + (isAp ? " · APPROVED" : "") +
          "</div>" +
          (isAp
            ? ""
            : '<button class="btn small primary" type="button" data-kf-ap="' +
                esc(name) + '">Approve</button>') +
        "</div>";
      }).join("");
      kfBox.querySelectorAll("[data-kf-ap]").forEach(function (b) {
        b.onclick = async function () {
          const r = await post("/api/projects/" + encodeURIComponent(window.State.pid) +
            "/shots/" + encodeURIComponent(sid) + "/keyframes/approve",
            { file: b.dataset.kfAp }, 30000);
          if (r && r.ok) {
            toast("keyframe approved — shot requeued");
            window.Bus.emit("shots:changed");
          } else {
            toast((r && r.error) || "approve failed", true);
          }
        };
      });
    };
    box.querySelectorAll(".adv-lora").forEach(function (lr) {
      const range = lr.querySelector("[data-lora-str]");
      const out = lr.querySelector("output");
      range.oninput = function () { out.textContent = Number(range.value).toFixed(2); };
    });
    return box;
  }

  /* ---------------------------------------------------------- image upload */
  async function uploadImage(url, files) {
    const fd = new FormData();
    (Array.isArray(files) ? files : [files]).forEach(function (f) {
      if (f) fd.append("files", f, f.name || "image.png");
    });
    const ctrl = new AbortController();
    const timer = setTimeout(function () { ctrl.abort(); }, 120000);
    try {
      const res = await window.Auth.fetch(url, { method: "POST", body: fd, signal: ctrl.signal });
      const ct = res.headers.get("content-type") || "";
      let body = null;
      try {
        body = ct.indexOf("application/json") >= 0 ? await res.json() : await res.text();
      } catch (e) { body = null; }
      if (!res.ok) {
        const detail = (body && (body.detail || body.error)) || res.statusText;
        throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      }
      return body;
    } catch (e) {
      toast(e && e.name === "AbortError" ? "upload timed out" :
        "upload failed" + (e && e.message ? ": " + e.message : ""), true);
      return null;
    } finally {
      clearTimeout(timer);
    }
  }

  /* tooltips ([data-tip]) are the shell's (js/shell.js), shared with Studio Tools */
  function bindTips(root) { window.Shell.bindTips(root); }

  /* --------------------------------------------------------- power defaults */
  const POWER_DEFAULTS = {
    lightning: { descriptor: "arcing blue-white bolts snapping between fingers, electric crackle", color: "#00e5ff" },
    fire: { descriptor: "swirling orange flames roaring from the hands, heat shimmer", color: "#ff6a00" },
    ice: { descriptor: "crackling frost spiraling outward, freezing mist and crystalline shards", color: "#9fe8ff" },
    water: { descriptor: "coiling liquid torrents rising from the hands, spray and ripples", color: "#4fc3f7" },
    wind: { descriptor: "invisible gale whipping debris into a spiraling vortex, clothes rippling", color: "#cfe8ea" },
    earth: { descriptor: "chunks of stone and soil rising from the ground, dust billowing", color: "#8d6e63" },
    force: { descriptor: "distorted air-pressure shimmer, concussive wave pushing objects back", color: "#b0bec5" },
    arcane: { descriptor: "violet runes orbiting the hands, glyphs flickering with arcane light", color: "#b388ff" },
    divine: { descriptor: "radiant golden light erupting from open palms, halo flare", color: "#ffd76a" },
    shadow: { descriptor: "smoky black tendrils pooling from the hands, devouring the light", color: "#1a1033" },
    void: { descriptor: "absolute black sphere eating the light, edges warping inward", color: "#0b0b14" },
    plasma: { descriptor: "superheated pink plasma streams, ionized particles trailing sparks", color: "#ff4081" },
    nature: { descriptor: "vines and glowing leaves sprouting and coiling around the arms", color: "#7cb342" },
    custom: { descriptor: "", color: "#e8b04b" },
  };
  function powerDefaults(type) {
    return POWER_DEFAULTS[type] || POWER_DEFAULTS.custom;
  }

  /* --------------------------------------------------------------- exports */
  window.$ = $;
  window.$$ = $$;
  window.esc = window.esc || esc;
  window.toast = toast;
  window.get = get;
  window.post = post;
  window.fmt = fmt;
  window.statusClass = statusClass;
  window.tierClass = tierClass;
  window.clsNum = clsNum;
  window.URLS = URLS;
  window.openModal = openModal;
  window.closeModal = closeModal;
  window.playVideo = playVideo;
  window.empty = empty;
  window.noProject = noProject;
  window.uploadImage = uploadImage;
  window.bindTips = bindTips;
  window.powerDefaults = powerDefaults;
  window.asList = function (x) {
    if (Array.isArray(x)) return x;
    if (x && typeof x === "object") return Object.keys(x).map(function (k) { return x[k]; });
    return [];
  };
  const UI = {
    $: $, esc: window.esc || esc, toast: toast, fmt: fmt, modal: openModal, closeModal: closeModal,
    shotCard: shotCard, advSection: advSection, playVideo: playVideo, empty: empty,
    noProject: noProject, uploadImage: uploadImage, bindTips: bindTips,
    powerDefaults: powerDefaults,
  };
  window.UI = UI;
})();
