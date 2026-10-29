/* generate.js — the Render page: how this movie renders. The video model
   as one card per family (the full list and the per-tier overrides in a
   fold), seamless takes, then folds for the shot queue (per-shot profiles),
   finishing (post-processing: the per-project switch, the worker lane, the
   studio-wide upscaler), GPU memory, and the legacy Wan 2.2 LoRA registry.
   The director transport lives in the topbar; the renderer controls here
   render the queue without the story steps. */
(function () {
  "use strict";
  let loraCat = "";
  const folds = {};         // which folds are open, kept across re-renders
  let videoModels = null;   // cached /api/videostudio/models payload
  let vramInfo = null;      // cached /api/vram payload
  let vramInfoAt = 0;
  let postWorkers = null;   // cached /api/workers payload (post-lane picker)
  let upscale = null;       // cached /api/upscale/state payload (studio-wide)

  async function loadVideoModels() {
    if (videoModels) return videoModels;
    try {
      videoModels = await API.get("/api/videostudio/models", 20000, true) || { models: [] };
    } catch (e) {
      videoModels = { models: [] };
    }
    return videoModels;
  }

  async function loadPostWorkers() {
    try {
      postWorkers = await API.get("/api/workers", 20000, true) || { workers: [] };
    } catch (e) {
      postWorkers = { workers: [] };
    }
    return postWorkers;
  }

  async function loadUpscale() {
    try {
      upscale = await API.get("/api/upscale/state", 15000, true) || null;
    } catch (e) {
      upscale = null;
    }
    return upscale;
  }

  // throttled VRAM status refresh; resolves true only when a fetch actually
  // happened (the caller re-renders once — no fetch/render loop)
  async function refreshVram(force) {
    if (!force && vramInfo && (Date.now() - vramInfoAt) < 8000) return false;
    try {
      vramInfo = await API.get("/api/vram", 20000, true) || vramInfo;
      vramInfoAt = Date.now();
      return true;
    } catch (e) {
      return false;
    }
  }

  function sw(id, on, opts) {
    const o = opts || {};
    return '<label class="toggle"' + (o.tip ? ' data-tip="' + esc(o.tip) + '"' : "") + ">" +
      '<input type="checkbox" id="' + id + '"' + (on ? " checked" : "") + (o.disabled ? " disabled" : "") +
      "><span></span>" + (o.label ? "<em>" + o.label + "</em>" : "") + "</label>";
  }

  function help(tip) {
    return '<span class="gen-help" data-tip="' + esc(tip) + '">' + Icon.get("help") + "</span>";
  }

  /* a fold whose open state survives the re-renders (the status poll) */
  function fold(id, title, meta, body, cls) {
    return '<details class="fold' + (cls ? " " + cls : "") + '" id="' + id + '"' + (folds[id] ? " open" : "") + ">" +
      '<summary><span class="fold-t">' + title + '</span><span class="fold-d">' + meta + "</span></summary>" +
      '<div class="fold-body">' + body + "</div></details>";
  }

  /* --------------------------------------------------------------- header */
  function intro() {
    const orch = State.orch || {};
    const state = orch.running ? (orch.paused ? "paused" : "running") : "stopped";
    const btn = function (id, icon, label, cls, tip) {
      return '<button class="btn small' + (cls ? " " + cls : "") + '" type="button" id="' + id + '" data-tip="' +
        esc(tip) + '">' + Icon.get(icon) + label + "</button>";
    };
    const acts = state === "stopped"
      ? btn("gen-orch-start", "play", "Render the queue only", "ghost",
          "Render this movie's queued shots now, without the director's other steps")
      : (state === "paused"
        ? btn("gen-orch-resume", "resume", "Resume", "", "Resume rendering")
        : btn("gen-orch-pause", "pause", "Pause", "ghost", "Pause after the current shot")) +
        btn("gen-orch-stop", "stop", "Stop", "danger", "Stop the renderer");
    const word = { running: "Rendering", paused: "Paused", stopped: "Not rendering" }[state];
    const scope = orch.running && !orch.project
      ? '<div class="gen-note warn">The renderer is working through every movie&apos;s queue. ' +
        "Stop it and start it here to render this movie only.</div>"
      : (orch.running && orch.project && orch.project !== State.pid
        ? '<div class="gen-note">The renderer is busy with another movie, <b>' + esc(orch.project) + "</b>.</div>" : "");
    return '<header class="page-intro">' +
        '<div class="pi-text"><h1>Render</h1>' +
          "<p>Pick the video model, then start production in the top bar. The director renders the shots in order, " +
          "checks each one and puts it on Review.</p></div>" +
        '<div class="pi-acts">' +
          '<span class="run-state ' + state + '"><span class="tb-dot"></span>' + word + "</span>" + acts +
        "</div>" +
      "</header>" + scope +
      (orch.last_error ? '<div class="gen-note bad" data-tip="' + esc(orch.last_error) + '">The renderer stopped on an error: ' +
        esc(String(orch.last_error).split("\n")[0]) + "</div>" : "");
  }

  /* ---------------------------------------------------------- video model */
  const FAMILY_TEXT = {
    ltx25: "The everyday model. Renders 1080p with sound, opening on your cast's pictures.",
    h3_1080p: "Renders from the cast pictures in their order, with sound. Slower than LTX-2.5; keep finishing off.",
    h3_draft: "Smaller MiniMax H3 renders, to check shots before the full one.",
    wan: "The old model, kept for old projects: 832x480, softer faces, no cast pictures.",
  };

  function groupOf(m) {
    return ((videoModels && videoModels.groups) || []).find(function (g) { return g.id === (m.catalog || {}).group; }) || {};
  }

  function tags(m) {
    return String((m.catalog || {}).detail || "").split("·").map(function (x) { return x.trim(); })
      .filter(function (x) { return x && x !== "recommended" && x !== "legacy"; });
  }

  /* one card per model family: its recommended config (or the one in use) */
  function modelCards(cur) {
    const models = (videoModels && videoModels.models) || [];
    const dflt = (videoModels || {}).default_model;
    const active = cur || dflt;
    const activeGroup = ((ModelCatalog.find(videoModels, active) || {}).catalog || {}).group;
    // Wan 2.2 (legacy) gets a card only on a project that already uses it
    const groups = ((videoModels && videoModels.groups) || []).filter(function (g) {
      return g.id !== "wan" || activeGroup === "wan";
    });
    if (!models.length) {
      return '<div class="vm-cards"><div class="vm-card none"><span class="vm-name">No video models found</span>' +
        '<span class="vm-d">Install LTX-2.5 with INSTALL.bat, or point Settings at the weight files you already have.</span></div></div>';
    }
    return '<div class="vm-cards">' + groups.map(function (g) {
      const members = models.filter(function (m) { return (m.catalog || {}).group === g.id; });
      if (!members.length) return "";
      const pick = members.find(function (m) { return m.id === active; }) ||
        members.find(function (m) { return (m.catalog || {}).recommended; }) || members[0];
      const on = pick.id === active;
      const parts = String(g.label || "").split("·");
      return '<button class="vm-card' + (on ? " on" : "") + (pick.available ? "" : " off") + '" type="button" data-model="' +
          esc(pick.id) + '"' + (pick.available ? "" : " disabled") + ' title="' + esc(ModelCatalog.tooltip(pick)) + '">' +
        '<span class="vm-top"><span class="vm-fam">' + esc(parts[0].trim()) + "</span>" +
          (parts[1] ? '<span class="vm-sub">' + esc(parts.slice(1).join(" ").replace(/legacy, not recommended/, "legacy").trim()) + "</span>" : "") +
          '<span class="spacer"></span>' +
          (pick.id === dflt ? '<span class="c-tag">Default</span>' : "") +
          (pick.seamless ? '<span class="vm-chain" data-tip="Shots that continue each other can render as one seamless take">' + Icon.get("link") + "</span>" : "") +
        "</span>" +
        '<span class="vm-name">' + esc((pick.catalog || {}).label || pick.display_name || pick.id) + "</span>" +
        '<span class="vm-d">' + esc(FAMILY_TEXT[g.id] || "") + "</span>" +
        '<span class="vm-tags">' + tags(pick).map(function (t) { return "<i>" + esc(t) + "</i>"; }).join("") +
          (pick.available ? "" : "<i class=\"bad\">" + (pick.gpu ? "not on this graphics card" : "not installed") +
            "</i>") + "</span>" +
      "</button>";
    }).join("") + "</div>";
  }

  function videoModelCard() {
    const cur = (State.project && State.project.video_family) || "";
    const perTier = (State.project && State.project.video_models) || {};
    const takesOn = !(State.project && State.project.seamless_takes === 0);
    const curModel = ModelCatalog.find(videoModels, cur || (videoModels || {}).default_model) || {};
    // the models this project actually renders with (movie model + tier overrides)
    const used = [cur || (videoModels || {}).default_model, perTier.hero, perTier.standard, perTier.volume].filter(Boolean);
    const seamlessUsed = used.filter(function (id) {
      return (ModelCatalog.find(videoModels, id) || {}).seamless;
    });
    const bible = State.bible || {};
    const withRefs = function (o) {
      return Object.keys(o || {}).filter(function (k) { return ((o[k] || {}).reference_images || []).length > 0; }).length;
    };
    const chars = Object.keys(bible.characters || {}).length;
    const looks = withRefs(bible.characters);
    const opt = function (sel) {
      return ModelCatalog.optionsHtml(videoModels, sel, { empty: "Same as the movie" });
    };
    const tierNames = ["hero", "standard", "volume"].filter(function (t) { return perTier[t]; });
    const TIER = { hero: "Hero shots", standard: "Standard shots", volume: "Crowd and filler shots" };
    const castLine = !chars
      ? "There is no cast yet, so the shots render from their prompts alone."
      : looks === chars
        ? "Every cast member has a picked look, so their shots open on it."
        : looks + " of " + chars + " cast members have a picked look. The others&apos; shots render from the prompt.";
    return '<section class="card render-model">' +
      '<div class="card-head"><h2>Video model</h2><span class="spacer"></span>' +
        '<a class="linkish" href="' + Shell.href("cast") + '" data-page="cast" data-shell="project">' + castLine + "</a></div>" +
      modelCards(cur) +
      ((curModel.catalog || {}).caution
        ? '<div class="gen-caution">' + Icon.get("warning") + esc(curModel.catalog.caution) + "</div>" : "") +
      '<div class="takes-row' + (seamlessUsed.length ? "" : " dim") + '">' +
        sw("gen-takes-on", takesOn && !!seamlessUsed.length, { disabled: !seamlessUsed.length }) +
        '<div class="takes-text"><b>Seamless takes</b>' +
          "<span>" + (!seamlessUsed.length
            ? "This model renders every shot on its own. Pick a model with " + Icon.get("link") + " to join shots."
            : "Shots that continue each other in one scene render as one long take, like a Video Studio chain, " +
              "then are cut back into shots. A retake renders the take again from that shot on.") + "</span>" +
          (seamlessUsed.length && takesOn ? takeLength() : "") + "</div>" +
      "</div>" +
      fold("gen-more", "More choices",
        (cur && !(curModel.catalog || {}).recommended ? "Using " + esc((curModel.catalog || {}).label || cur) : "Every configuration, and a model per kind of shot") +
        (tierNames.length ? ", " + tierNames.length + " kind" + (tierNames.length === 1 ? "" : "s") + " of shot on another model" : ""),
        '<label class="gen-field wide"><span>Every configuration ' + help(ModelCatalog.SEAMLESS_NOTE + ". ★ = recommended") + "</span>" +
          '<select id="gen-video-model">' +
            ModelCatalog.optionsHtml(videoModels, cur, { empty: "Studio default" }) + "</select></label>" +
        '<div class="gen-tiers">' +
          ["hero", "standard", "volume"].map(function (t) {
            return '<label class="gen-field"><span>' + TIER[t] + '</span><select id="gen-vm-' + t + '">' +
              opt(perTier[t] || "") + "</select></label>";
          }).join("") +
        "</div>" +
        '<p class="gen-hint">Hero shots are the close-ups and big moments. For example: MiniMax H3 for the hero shots and LTX-2.5 for the rest.</p>') +
    "</section>";
  }

  /* the most shots in one take: the project's, empty = the studio's */
  function takeLength() {
    const tl = State.takeLength || {};
    const own = (State.project && State.project.take_max_shots) || 0;
    const studio = tl.studio || 6;
    const tip = "A longer run of continuing shots goes on in a new take, which opens on a new angle " +
      "made from the last frame of the take before. Empty: the studio's take length (" + studio +
      ", in Settings). In testing, an 8-shot LTX-2.5 take kept the cast in every shot; shots 7 and 8 " +
      "had larger steps at their joins, though none showed. MiniMax H3 takes hold at most 4 shots.";
    return '<label class="takes-len"><span>Shots per take ' + help(tip) + "</span>" +
      '<input id="gen-take-len" type="number" min="1" max="' + (tl.top || 50) + '" step="1" inputmode="numeric"' +
        ' value="' + (own || "") + '" placeholder="' + studio + '" aria-label="Shots per take">' +
      (own && tl.now && tl.now < own ? "<em>" + tl.now + " on MiniMax H3</em>" : "") +
      (own ? "" : "<em>the studio&apos;s</em>") + "</label>";
  }

  /* ------------------------------------------------------ post-processing */
  function postBody() {
    const on = !!(State.project && State.project.post_processing);
    const pw = (State.project && State.project.post_worker) || "";
    const workers = (postWorkers && postWorkers.workers) || [];
    const laneOptions = '<option value=""' + (pw === "" ? " selected" : "") +
      ">This PC</option>" +
      workers.map(function (w) {
        const caps = w.capabilities || {};
        const roles = caps.roles || [];
        const canPost = roles.indexOf("upscale") >= 0 && !caps.blocked;
        const bits = [];
        if (!w.online) bits.push("offline");
        if (caps.blocked) bits.push("missing models");
        if (roles.indexOf("upscale") < 0) bits.push("no upscale role");
        return '<option value="' + esc(w.id) + '"' + (pw === w.id ? " selected" : "") +
          (canPost ? "" : " disabled") + ">" + esc(w.name || w.id) +
          (bits.length ? " (" + esc(bits.join(", ")) + ")" : "") + "</option>";
      }).join("");
    const method = upscale ? (upscale.enabled ? upscale.method : "none") : null;
    const costs = (upscale && upscale.cost_note) || {};
    const methodTip = "For every movie: the upscaler finishing uses. " +
      Object.keys(costs).map(function (k) { return k + ": " + costs[k]; }).join(". ");
    return '<div class="takes-row">' +
        sw("gen-post-on", on) +
        '<div class="takes-text"><b>Finish every shot</b><span>' + (on
          ? "Each finished shot is upscaled and smoothed (RIFE 2× and RealESRGAN ×4) on the side, while rendering goes on."
          : "Off: shots stay exactly as the model rendered them.") +
          " It works shot by shot, so inside a seamless take the joins can show.</span></div>" +
      "</div>" +
      '<div class="gen-grid2">' +
        '<label class="gen-field"><span>Run it on</span><select id="gen-post-worker"' +
          ' data-tip="An offline helper PC falls back to this PC">' + laneOptions + "</select></label>" +
        '<label class="gen-field"><span>Upscaler, for every movie</span>' +
          (method == null
            ? '<select disabled><option>unavailable</option></select>'
            : '<select id="gen-upscaler" data-tip="' + esc(methodTip) + '">' +
              ((upscale && upscale.methods) || ["none", "esrgan", "seedvr2", "lanczos"]).map(function (m) {
                return '<option value="' + esc(m) + '"' + (m === method ? " selected" : "") + ">" +
                  esc(m === "none" ? "Off" : m) + "</option>";
              }).join("") + "</select>") +
        "</label>" +
      "</div>";
  }

  /* ----------------------------------------------------------------- VRAM */
  function vramStatus() {
    const s = (vramInfo && vramInfo.settings) || { auto_offload: true, idle_minutes: 8 };
    const st = (vramInfo && vramInfo.state) || {};
    const farms = (vramInfo && vramInfo.farms) || [];
    if (!vramInfo) return "status unavailable";
    if (farms.some(function (f) { return f.busy; })) return "rendering, the model stays loaded";
    if (st.released) return "models unloaded, the next render loads them";
    if (!s.auto_offload) return "idle, the models stay loaded";
    if (st.release_in_s != null) return "idle, unloads in " + Math.ceil(st.release_in_s / 60) + " min";
    return "idle";
  }

  function vramBody() {
    const s = (vramInfo && vramInfo.settings) || { auto_offload: true, idle_minutes: 8 };
    const st = (vramInfo && vramInfo.state) || {};
    const farms = (vramInfo && vramInfo.farms) || [];
    const farmTip = farms.map(function (f) {
      const warm = f.state ? (", " + f.state + (f.model ? " " + f.model : "") +
        (f.warm_remaining_s != null ? " " + Math.ceil(f.warm_remaining_s / 60) + "m" : "")) : "";
      return ":" + f.port + (f.busy ? " busy" : " idle") + warm +
        (f.memory ? " (" + f.memory.free_mb + " MB free)" : "");
    }).join("\n") + (st.last_release ? "\nlast release " + st.last_release : "");
    return '<div class="gen-row"' + (farmTip ? ' data-tip="' + esc(farmTip) + '"' : "") + ">" +
        sw("vram-auto", s.auto_offload, { label: "Unload the models after", tip: "Free the GPU memory after this long without a render" }) +
        '<input id="vram-idle" type="number" min="1" max="120" value="' + esc(s.idle_minutes) + '" class="gen-num">' +
        '<span class="muted small">minutes idle</span>' +
      "</div>" +
      '<div class="gen-row">' +
        '<button class="btn small" type="button" id="vram-offload" data-tip="Unload the render models now">' +
          Icon.get("stop") + "Unload now</button>" +
        '<button class="btn small ghost" type="button" id="vram-free-apps" data-tip="Close the GPU-heavy apps listed in settings (render_mode.kill_gpu_apps)">' +
          Icon.get("x") + "Close GPU apps</button>" +
      "</div>";
  }

  function queueMeta(shots) {
    const by = {};
    shots.forEach(function (s) { by[s.status || "pending"] = (by[s.status || "pending"] || 0) + 1; });
    const order = ["passed", "rendering", "queued", "review", "failed", "prompt", "pending"];
    const word = { passed: "passed", rendering: "rendering", queued: "queued", review: "to review", failed: "failed",
      prompt: "waiting for a prompt", pending: "not started" };
    return shots.length + " shots" + (shots.length ? ": " + order.filter(function (k) { return by[k]; })
      .map(function (k) { return by[k] + " " + word[k]; }).join(", ") : "");
  }

  function render(el) {
    if (!State.pid) { el.innerHTML = noProject(); el.__sig = null; return; }
    const shots = State.shots || [];
    const html =
      intro() +
      videoModelCard() +
      '<section class="render-more">' +
        fold("gen-queue", "The shot queue", esc(queueMeta(shots)),
          shots.length ? queueTable(shots) : empty("No shots yet", "The story's shot plan fills the queue.")) +
        fold("gen-post", "Finishing", (State.project && State.project.post_processing) ? "On: upscale and smoothing" : "Off", postBody()) +
        fold("gen-vram", "GPU memory", esc(vramStatus()), vramBody()) +
        fold("gen-loras", "Wan 2.2 LoRAs", "Legacy. Only the old Wan 2.2 model uses them; " + (State.loras || []).length + " in the registry",
          '<p class="gen-hint">These apply only when a shot renders on Wan 2.2. LTX-2.5 and MiniMax H3 ignore them and use the cast pictures instead.</p>' +
          '<div class="ctl-row gen-lora-bar">' +
            '<select id="gen-lora-cat" class="small-sel"><option value="">All categories</option>' +
              loraCats().map(function (c) {
                return "<option" + (loraCat === c ? " selected" : "") + ">" + esc(c) + "</option>";
              }).join("") +
            "</select>" +
            '<button class="btn small" type="button" id="gen-lora-rescan">' + Icon.get("refresh") + "Rescan</button>" +
          "</div>" +
          '<div class="table-wrap">' + loraTable() + "</div>", "legacy") +
      "</section>";
    // re-render only on a visible change: the status poll ticks every 5 s and
    // a rebuild would close an open select
    if (el.__sig !== html) {
      el.__sig = html;
      el.innerHTML = html;
      bind(el);
    }
    // VRAM status is async; re-render once when fresh data actually arrived
    refreshVram(false).then(function (updated) {
      if (updated && el.isConnected) render(el);
    });
  }

  function queueTable(shots) {
    const rows = shots.slice().sort(function (a, b) {
      return (a.scene || "").localeCompare(b.scene || "") ||
        (a.shot_id || "").localeCompare(b.shot_id || "");
    });
    return '<div class="table-wrap"><table class="table">' +
      "<thead><tr><th>Shot</th><th>Scene</th><th>Kind</th><th>Status</th><th>Tries</th>" +
      '<th data-tip="How well the clip matches its prompt (CLIP similarity)">Prompt match</th><th data-tip="How well the face matches the cast look">Face</th>' +
      '<th data-tip="How much it moves">Motion</th><th>Render time</th><th>Profile</th><th></th></tr></thead>' +
      "<tbody>" + rows.map(function (r) {
        const j = r.json || {};
        return "<tr data-sid=\"" + esc(r.shot_id) + '">' +
          '<td class="mono">' + esc(r.shot_id) + "</td>" +
          "<td>" + esc(r.scene || "") + "</td>" +
          '<td><span class="badge ' + tierClass(r.tier) + '">' + esc(r.tier || "—") + "</span></td>" +
          '<td><span class="badge ' + statusClass(r.status) + '">' + esc(statusWord(r.status)) + "</span></td>" +
          "<td>" + esc(r.attempts || 0) + "</td>" +
          '<td class="' + clsNum(r.qa_clip) + '">' + (r.qa_clip == null ? "—" : Number(r.qa_clip).toFixed(2)) + "</td>" +
          '<td class="' + clsNum(r.qa_face) + '">' + (r.qa_face == null ? "—" : Number(r.qa_face).toFixed(2)) + "</td>" +
          '<td class="' + clsNum(r.qa_motion) + '">' + (r.qa_motion == null ? "—" : Number(r.qa_motion).toFixed(2)) + "</td>" +
          "<td>" + (r.render_time_s ? fmt.s(r.render_time_s) : "—") + "</td>" +
          '<td><select class="prof-sel">' +
            ["", "preview", "production_fast", "effect_quality"].map(function (pr) {
              return "<option" + ((j.render_profile || "") === pr ? " selected" : "") + ">" +
                (pr || "auto") + "</option>";
            }).join("") +
          "</select></td>" +
          '<td><button class="btn small ghost" type="button" data-edit>Edit</button></td>' +
        "</tr>";
      }).join("") + "</tbody></table></div>";
  }

  function loraCats() {
    const loras = State.loras || [];
    const set = {};
    loras.forEach(function (e) {
      const c = e.category || "other";
      set[c] = true;
    });
    return Object.keys(set).sort();
  }

  function loraTable() {
    const loras = (State.loras || []).filter(function (e) {
      return !loraCat || (e.category || "other") === loraCat;
    });
    if (!loras.length) {
      return empty("No Wan 2.2 LoRAs", "The registry is empty. Rescan if you added files.");
    }
    return '<table class="table"><thead><tr><th>Entry</th><th>Category</th><th>Trigger</th>' +
      "<th>Strength</th><th>Steps</th><th>CFG</th><th>Status</th><th>Enabled</th></tr></thead><tbody>" +
      loras.map(function (e) {
        const ident = e.id || e.file || "";
        return '<tr data-lora="' + esc(ident) + '">' +
          "<td><b>" + esc(e.display || ident) + "</b>" +
          (e.conflicts ? '<div class="muted small">' + Icon.get("warning") + " conflicts: " + esc(e.conflicts) + "</div>" : "") +
          "</td>" +
          "<td>" + esc(e.category || "—") + "</td>" +
          '<td class="mono small">' + esc(e.trigger || "—") + "</td>" +
          "<td>" + (e.strength != null ? esc(e.strength) : "—") + "</td>" +
          "<td>" + (e.steps != null ? esc(e.steps) : "—") + "</td>" +
          "<td>" + (e.cfg != null ? esc(e.cfg) : "—") + "</td>" +
          '<td><span class="badge ' + ((e.status || "") === "trained" ? "st-green" : "st-gray") + '">' +
            esc(e.status || "—") + "</span></td>" +
          '<td><label class="toggle"><input type="checkbox" data-lora-enabled' +
            (e.enabled === false ? "" : " checked") + "><span></span></label></td>" +
        "</tr>";
      }).join("") + "</tbody></table>";
  }

  /* Save one project field; re-render from the stored state either way, so
     a rejected change never shows as applied. */
  async function saveProject(el, ctl, body, okMsg, failMsg) {
    if (ctl) ctl.disabled = true;
    const r = await post("/api/projects/" + encodeURIComponent(State.pid), body, 30000);
    if (ctl) ctl.disabled = false;
    if (r && r.ok) {
      toast(okMsg);
      await State.refreshProject();
    } else {
      toast((r && r.error) || failMsg, true);
    }
    el.__sig = null;
    render(el);
  }

  function bind(el) {
    const orchAct = function (id, verb) {
      const b = el.querySelector("#" + id);
      if (!b) return;
      b.onclick = async function () {
        b.disabled = true;
        // Start is SCOPED to this project: without the pid the orchestrator
        // renders every project's leftover queue (an old story generated
        // while the operator watched a new one).
        const body = verb === "start" ? { project: State.pid } : {};
        const r = await post("/api/orchestrator/" + verb, body, 30000);
        b.disabled = false;
        const said = { start: "Rendering the queue", pause: "Pausing after this shot", resume: "Rendering again", stop: "Renderer stopped" };
        if (r) toast(r.ok ? said[verb] : (r.error || "The renderer did not answer"), !r.ok);
        State.refreshStatus();
      };
    };
    orchAct("gen-orch-start", "start");
    orchAct("gen-orch-pause", "pause");
    orchAct("gen-orch-resume", "resume");
    orchAct("gen-orch-stop", "stop");

    // fold states survive the re-renders
    el.querySelectorAll("details.fold[id]").forEach(function (d) {
      d.addEventListener("toggle", function () { folds[d.id] = d.open; });
    });

    const modelName = function (id) {
      const m = ModelCatalog.find(videoModels, id || (videoModels || {}).default_model);
      return m ? ((m.catalog || {}).label || m.display_name || m.id) : "the studio default";
    };
    el.querySelectorAll(".vm-card[data-model]").forEach(function (card) {
      card.onclick = function () {
        if (card.classList.contains("on")) return;
        const id = card.dataset.model;
        saveProject(el, card, { video_family: id }, "Video model: " + modelName(id), "Could not change the video model");
      };
    });
    const vmsel = el.querySelector("#gen-video-model");
    if (vmsel) vmsel.onchange = function () {
      saveProject(el, vmsel, { video_family: vmsel.value },
        "Video model: " + modelName(vmsel.value), "Could not change the video model");
    };

    // Per-tier video model overrides (hero / standard / volume): each may run
    // a different model — e.g. H3 for hero shots, LTX for the rest. Saved as
    // one object; empty values fall back to the movie model.
    ["hero", "standard", "volume"].forEach(function (tier) {
      const sel = el.querySelector("#gen-vm-" + tier);
      if (!sel) return;
      sel.onchange = function () {
        const cur = (State.project && State.project.video_models) || {};
        const next = { hero: cur.hero || "", standard: cur.standard || "", volume: cur.volume || "" };
        next[tier] = sel.value;
        saveProject(el, sel, { video_models: next },
          (tier === "hero" ? "Hero" : tier === "standard" ? "Standard" : "Crowd and filler") + " shots: " +
            (sel.value ? modelName(sel.value) : "same as the movie"),
          "Could not set the model for those shots");
      };
    });

    const takes = el.querySelector("#gen-takes-on");
    if (takes) takes.onchange = function () {
      saveProject(el, takes, { seamless_takes: takes.checked },
        "Seamless takes " + (takes.checked ? "on" : "off"), "Could not change seamless takes");
    };
    const takeLen = el.querySelector("#gen-take-len");
    if (takeLen) takeLen.onchange = function () {
      const raw = takeLen.value.trim();
      const n = raw === "" ? 0 : Number(raw);
      const top = Number(takeLen.max) || 50;
      if (!Number.isInteger(n) || n < 0 || n > top) {
        toast("Shots per take: a whole number from 1 to " + top + ", or empty for the studio's", true);
        takeLen.value = (State.project && State.project.take_max_shots) || "";
        return;
      }
      saveProject(el, takeLen, { take_max_shots: n },
        n ? "Takes hold up to " + n + " shot" + (n === 1 ? "" : "s") : "Takes use the studio's length",
        "Could not change the take length");
    };

    const postOn = el.querySelector("#gen-post-on");
    if (postOn) postOn.onchange = function () {
      saveProject(el, postOn, { post_processing: postOn.checked },
        "Finishing " + (postOn.checked ? "on" : "off"), "Could not change finishing");
    };

    // Post worker lane picker: "" = local, or a worker id with the upscale
    // role. Persisted per project; workers without the role / blocked /
    // offline are disabled in the dropdown.
    const pws = el.querySelector("#gen-post-worker");
    if (pws) pws.onchange = function () {
      saveProject(el, pws, { post_worker: pws.value },
        "Finishing runs on " + (pws.value ? pws.options[pws.selectedIndex].text : "this PC"),
        "Could not change where finishing runs");
    };

    // the studio-wide upscale method (settings tiers.upscale, every tier)
    const ups = el.querySelector("#gen-upscaler");
    if (ups) ups.onchange = async function () {
      ups.disabled = true;
      const on = ups.value !== "none";
      const r = await post("/api/upscale/state", { enabled: on, method: on ? ups.value : "esrgan" }, 30000);
      ups.disabled = false;
      toast(r && r.ok ? (on ? "Upscaler: " + ups.value : "Upscaler off") : "Could not change the upscaler", !(r && r.ok));
      await loadUpscale();
      el.__sig = null;
      render(el);
    };

    // VRAM card: auto-offload toggle, idle minutes, direct offload, and
    // closing the GPU apps settings lists
    const vramAuto = el.querySelector("#vram-auto");
    if (vramAuto) vramAuto.onchange = async function () {
      vramAuto.disabled = true;
      const r = await post("/api/vram", { auto_offload: vramAuto.checked }, 30000);
      vramAuto.disabled = false;
      if (r && r.ok) {
        toast(vramAuto.checked ? "The models unload when idle" : "The models stay loaded");
        await refreshVram(true);
      } else {
        toast((r && r.error) || "Could not change it", true);
      }
      el.__sig = null;
      render(el);
    };
    const vramIdle = el.querySelector("#vram-idle");
    if (vramIdle) vramIdle.onchange = async function () {
      const v = parseInt(vramIdle.value, 10);
      const r = await post("/api/vram", { idle_minutes: isNaN(v) ? 8 : v }, 30000);
      if (r && r.ok) {
        toast("The models unload after " + r.settings.idle_minutes + " minutes idle");
        await refreshVram(true);
      }
      el.__sig = null;
      render(el);
    };
    const vramBtn = el.querySelector("#vram-offload");
    if (vramBtn) vramBtn.onclick = async function () {
      vramBtn.disabled = true;
      const r = await post("/api/vram/offload", {}, 90000);
      vramBtn.disabled = false;
      if (r && r.ok) {
        const parts = Object.keys(r.farms || []).map(function (k) {
          return k + ": " + r.farms[k];
        }).join(", ");
        toast("Unloaded" + (parts ? ": " + parts : ""));
        await refreshVram(true);
        el.__sig = null;
        render(el);
      } else {
        toast((r && r.error) || "Could not unload the models", true);
      }
    };
    const freeBtn = el.querySelector("#vram-free-apps");
    if (freeBtn) freeBtn.onclick = async function () {
      const ok = await Dlg.confirm({
        title: "Close the GPU apps?",
        message: "Close the GPU-heavy apps listed in settings (render_mode.kill_gpu_apps)? " +
          "Unsaved work in them is lost.",
        okLabel: "Close them", danger: true,
      });
      if (!ok) return;
      freeBtn.disabled = true;
      const r = await post("/api/render_mode/free", {}, 60000);
      freeBtn.disabled = false;
      const killed = (r && r.killed) || [];
      toast(killed.length ? "Closed " + killed.join(", ") : "None of the listed GPU apps was running");
    };

    const cat = el.querySelector("#gen-lora-cat");
    if (cat) cat.onchange = function () {
      loraCat = cat.value;
      State.refreshLoras(false).then(function () { render(el); });
    };
    const rescan = el.querySelector("#gen-lora-rescan");
    if (rescan) rescan.onclick = async function () {
      rescan.disabled = true;
      const r = await post("/api/lora/video/rescan", {}, 60000);
      rescan.disabled = false;
      if (r) toast(r.ok !== false ? "Rescanned" : "Could not rescan", r.ok === false);
      await State.refreshLoras(true);
      render(el);
    };

    el.querySelectorAll("tr[data-sid]").forEach(function (tr) {
      const sid = tr.dataset.sid;
      const prof = tr.querySelector(".prof-sel");
      prof.onchange = async function () {
        const r = await post("/api/projects/" + encodeURIComponent(State.pid) +
          "/shots/" + encodeURIComponent(sid) + "/rerender",
          { profile: prof.value }, 30000);
        if (r) toast(r.ok ? sid + " renders again with the " + (prof.value || "automatic") + " profile" : (r.error || "Could not change the profile"), !r.ok);
        Bus.emit("shots:changed");
      };
      tr.querySelector("[data-edit]").onclick = function () {
        const row = (State.shots || []).find(function (s) { return s.shot_id === sid; });
        if (!row) return;
        const veil = openModal('<div class="adv-open"></div>', {
          title: "Edit " + sid,
          wide: true,
        });
        veil.querySelector(".adv-open").appendChild(UI.advSection(sid, row));
      };
    });

    el.querySelectorAll("tr[data-lora]").forEach(function (tr) {
      const ident = tr.dataset.lora;
      tr.querySelector("[data-lora-enabled]").onchange = async function (e) {
        const r = await post("/api/lora/video/" + encodeURIComponent(ident),
          { enabled: e.target.checked }, 30000);
        if (r) toast(r.ok ? ident + (e.target.checked ? " on" : " off") : (r.error || "Could not change it"), !r.ok);
        if (r && r.ok) {
          // update the CACHED registry entry — otherwise the next render
          // re-reads stale state and the checkbox flips back on
          (State.loras || []).forEach(function (x) {
            if ((x.id || x.file) === ident) x.enabled = e.target.checked;
          });
          render(el);
        } else {
          e.target.checked = !e.target.checked; // revert on failure
        }
      };
    });
  }

  Router.register("render", {
    polls: ["status", "shots"],
    subs: [],
    mount: function (el) {
      const page = this;
      page.el = el;
      page.render = function () { render(el); };
      page.subs.push(Bus.on("shots", page.render));
      page.subs.push(Bus.on("status", page.render));
      page.subs.push(Bus.on("loras", page.render));
      page.subs.push(Bus.on("project", page.render));
      page.render();
      State.refreshLoras(false);
      // async payloads (models, post workers, upscaler); re-render as they land
      [loadVideoModels(), loadPostWorkers(), loadUpscale()].forEach(function (p) {
        p.then(function () { if (page.el === el) page.render(); });
      });
    },
    unmount: function () {
      this.subs.forEach(function (off) { off(); });
      this.subs = [];
      if (this.el) { this.el.innerHTML = ""; this.el.__sig = null; }
    },
  });
})();
