/* generate.js — how this movie renders: the renderer (queue) controls, the
   video model (movie model, per-tier overrides, seamless takes), post-
   processing (per-project switch, worker lane, the studio-wide upscaler),
   VRAM, the render queue with per-shot profiles, and the LoRA registry
   (enable/category/rescan). The director transport lives in the topbar;
   the renderer controls here render the queue without the story stages. */
(function () {
  "use strict";
  let loraCat = "";
  let loraOpen = false;     // the LoRA registry fold survives re-renders
  let tiersOpen = false;    // so does the per-tier models fold
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

  /* --------------------------------------------------------------- header */
  function renderer() {
    const orch = State.orch || {};
    const state = orch.running ? (orch.paused ? "paused" : "running") : "stopped";
    const stats = orch.stats || {};
    const statsTip = stats.total
      ? "Queue, all projects: " + stats.total + " shots · " +
        Object.keys(stats).filter(function (k) { return k !== "total" && k !== "hero_ratio"; })
          .map(function (k) { return k + " " + stats[k]; }).join(" · ")
      : "The queue is empty";
    const btn = function (id, icon, label, cls, tip) {
      return '<button class="btn small' + (cls ? " " + cls : "") + '" type="button" id="' + id + '" data-tip="' +
        esc(tip) + '">' + Icon.get(icon) + label + "</button>";
    };
    const acts = state === "stopped"
      ? btn("gen-orch-start", "play", "Render queue", "", "Render this project's queued shots now, without the director's story stages")
      : (state === "paused"
        ? btn("gen-orch-resume", "resume", "Resume", "", "Resume rendering")
        : btn("gen-orch-pause", "pause", "Pause", "", "Pause after the current shot")) +
        btn("gen-orch-stop", "stop", "Stop", "danger", "Stop the renderer");
    const scope = orch.running && !orch.project
      ? '<div class="gen-note warn">The renderer runs every project\'s queue (started without a project). ' +
        "Stop it and press Render queue here to scope it to this movie.</div>"
      : (orch.running && orch.project && orch.project !== State.pid
        ? '<div class="gen-note">The renderer is busy with project <b>' + esc(orch.project) + "</b>.</div>" : "");
    return '<div class="page-slate">' +
        "<h1>Generate</h1>" +
        '<span class="slate-meta" data-tip="' + esc(statsTip) + '">' + (State.shots || []).length + " shots</span>" +
        '<span class="spacer"></span>' +
        '<div class="slate-acts">' +
          '<span class="pill' + (state === "running" ? " run" : state === "paused" ? " st-amber" : "") +
            '" data-tip="The renderer works through the shot queue">renderer ' + state + "</span>" +
          acts +
        "</div>" +
      "</div>" + scope +
      (orch.last_error ? '<div class="gen-note bad" data-tip="' + esc(orch.last_error) + '">Last renderer error: ' +
        esc(String(orch.last_error).split("\n")[0]) + "</div>" : "");
  }

  /* ---------------------------------------------------------- video model */
  /* the "" choice is the studio default (LTX-2.5), never Wan 2.2 (legacy) */
  function defaultModelLabel() {
    const m = ModelCatalog.find(videoModels, (videoModels || {}).default_model);
    return "Studio default: " + (m ? ((m.catalog || {}).label || m.display_name || m.id)
      : "no LTX-2.5 or H3 model installed");
  }

  function videoModelCard() {
    const cur = (State.project && State.project.video_family) || "";
    const perTier = (State.project && State.project.video_models) || {};
    const takesOn = !(State.project && State.project.seamless_takes === 0);
    const curModel = ModelCatalog.find(videoModels, cur) || {};
    const fam = String(curModel.family || "");
    // the closed select shows the config's short label; its family is the group
    const group = ((videoModels && videoModels.groups) || []).find(function (g) {
      return g.id === (curModel.catalog || {}).group;
    });
    const groupLabel = group ? group.label : "";
    // the models this project actually renders with (movie model + tier overrides)
    const used = [cur, perTier.hero, perTier.standard, perTier.volume].filter(Boolean);
    const seamlessUsed = used.filter(function (id) {
      return (ModelCatalog.find(videoModels, id) || {}).seamless;
    });
    const bible = State.bible || {};
    const withRefs = function (o) {
      return Object.keys(o || {}).filter(function (k) { return ((o[k] || {}).reference_images || []).length > 0; }).length;
    };
    const chars = Object.keys(bible.characters || {}).length;
    const locs = Object.keys(bible.locations || {}).length;
    const opt = function (sel) {
      return ModelCatalog.optionsHtml(videoModels, sel, { empty: "follow the movie model" });
    };
    const familyTip = fam.indexOf("ltx") === 0
      ? "LTX-2.5 renders from the cast reference images (Cast & World → references) at 1080p with native audio. " +
        "Shots whose cast has no references render as plain text-to-video."
      : fam.indexOf("h3") === 0
        ? "MiniMax H3 renders from the cast reference images in slot order (<Picture 1> = lead cast, also the " +
          "frame-0 identity anchor) with generated audio: keep post-processing off. The studio writes " +
          "the prompts in H3's Ref2VA layout. Per 5.2 s clip on my RTX 3070: 1080p tiers ≈ 15–25 min, drafts ≈ 10–17 min."
        : fam.indexOf("wan") === 0
          ? "Wan 2.2 is the studio's legacy model, kept for old projects. It is not as good as LTX-2.5 " +
            "or MiniMax H3: it renders 832x480 (upscaled in post), with softer faces and detail, and " +
            "takes no cast references. Pick an LTX-2.5 or H3 model for new work."
          : "";
    const tierNames = ["hero", "standard", "volume"].filter(function (t) { return perTier[t]; });
    const takesTip = !seamlessUsed.length
      ? "None of this project's models renders seamless takes, so every shot renders on its own (a CONTINUE " +
        "shot opens on the previous shot's last frame). Pick a ⛓ model to use them."
      : "On: a run of CONTINUE shots in one scene and location renders as one continuous take (latent carry " +
        "between shots, one cast, seam QA; experimental), then is cut back into shots for review and the edit. CUT / " +
        "MATCH_ACTION shots and QA retakes render on their own. Off: every shot renders on its own.";
    return '<section class="card">' +
      '<div class="card-head"><h2>Video model</h2>' +
        help(ModelCatalog.SEAMLESS_NOTE + " · ★ = recommended config") + "</div>" +
      '<select id="gen-video-model" class="gen-wide">' +
        ModelCatalog.optionsHtml(videoModels, cur, { empty: defaultModelLabel() }) +
      "</select>" +
      '<div class="gen-meta">' +
        (groupLabel ? "<span>" + esc(groupLabel) + "</span><span>·</span>" : "") +
        '<span data-tip="Characters / locations with reference images">references: characters ' +
          withRefs(bible.characters) + "/" + chars + " · locations " + withRefs(bible.locations) + "/" + locs + "</span>" +
        (familyTip ? help(familyTip) : "") +
      "</div>" +
      '<div class="gen-row">' +
        sw("gen-takes-on", takesOn && !!seamlessUsed.length, { disabled: !seamlessUsed.length, label: "Seamless takes", tip: takesTip }) +
        '<span class="gen-state">' + (!seamlessUsed.length ? "needs a ⛓ model" : takesOn ? "on" : "off") + "</span>" +
      "</div>" +
      '<details class="fold" id="gen-tiers"' + (tiersOpen ? " open" : "") + ">" +
        "<summary>Per-tier models" +
          '<span class="fold-meta">' + (tierNames.length ? esc(tierNames.join(" · ")) + " overridden" : "all follow the movie model") +
        "</span></summary>" +
        '<div class="fold-body">' +
          '<div class="gen-tiers">' +
            ["hero", "standard", "volume"].map(function (t) {
              return '<label class="gen-tier"><span>' + t + '</span><select id="gen-vm-' + t + '">' +
                opt(perTier[t] || "") + "</select></label>";
            }).join("") +
          "</div>" +
          '<div class="gen-hint">Hero = close-ups and power reveals. E.g. H3 for hero shots and LTX-2.5 for the rest (about 6 min against 20–22 min per 5 s clip, warm).</div>' +
        "</div>" +
      "</details>" +
    "</section>";
  }

  /* ------------------------------------------------------ post-processing */
  function postCard() {
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
        if (caps.blocked) bits.push("blocked: missing models");
        if (roles.indexOf("upscale") < 0) bits.push("no upscale role");
        return '<option value="' + esc(w.id) + '"' + (pw === w.id ? " selected" : "") +
          (canPost ? "" : " disabled") + ">" + esc(w.name || w.id) +
          (bits.length ? " (" + esc(bits.join(", ")) + ")" : "") + "</option>";
      }).join("");
    const method = upscale ? (upscale.enabled ? upscale.method : "none") : null;
    const costs = (upscale && upscale.cost_note) || {};
    const methodTip = "Studio-wide (every project): the upscale method of the post stage. " +
      Object.keys(costs).map(function (k) { return k + ": " + costs[k]; }).join(" · ");
    return '<section class="card">' +
      '<div class="card-head"><h2>Post-processing</h2>' +
        help("Upscale · face-fix · interpolation · grade. Applies to this project only.") + "</div>" +
      '<div class="gen-row">' +
        sw("gen-post-on", on, { label: "Post-process finished clips", tip: on
          ? "On: every finished clip goes to the machine below for the upscale (RIFE 2× + RealESRGAN ×4) in its own lane. Rendering continues meanwhile; the next shot starts from the raw render, so continuity never waits."
          : "Off: shots are delivered exactly as the model rendered them." }) +
      "</div>" +
      '<div class="gen-grid2">' +
        '<label class="gen-field"><span>Run on</span><select id="gen-post-worker"' +
          ' data-tip="Offline or blocked workers fall back to this PC">' + laneOptions + "</select></label>" +
        '<label class="gen-field"><span>Upscaler <em>studio-wide</em></span>' +
          (method == null
            ? '<select disabled><option>unavailable</option></select>'
            : '<select id="gen-upscaler" data-tip="' + esc(methodTip) + '">' +
              ((upscale && upscale.methods) || ["none", "esrgan", "seedvr2", "lanczos"]).map(function (m) {
                return '<option value="' + esc(m) + '"' + (m === method ? " selected" : "") + ">" +
                  esc(m === "none" ? "off" : m) + "</option>";
              }).join("") + "</select>") +
        "</label>" +
      "</div>" +
    "</section>";
  }

  /* ----------------------------------------------------------------- VRAM */
  function vramCard() {
    const s = (vramInfo && vramInfo.settings) || { auto_offload: true, idle_minutes: 8 };
    const st = (vramInfo && vramInfo.state) || {};
    const farms = (vramInfo && vramInfo.farms) || [];
    const busy = farms.some(function (f) { return f.busy; });
    let status;
    if (!vramInfo) status = "status unavailable";
    else if (busy) status = "rendering · model held warm";
    else if (st.released) status = "models released · the next render reloads";
    else if (!s.auto_offload) status = "idle · models stay loaded";
    else if (st.release_in_s != null) status = "idle · offload in " + Math.ceil(st.release_in_s / 60) + " min";
    else status = "idle";
    const farmTip = farms.map(function (f) {
      const warm = f.state ? (", " + f.state + (f.model ? " " + f.model : "") +
        (f.warm_remaining_s != null ? " " + Math.ceil(f.warm_remaining_s / 60) + "m" : "")) : "";
      return ":" + f.port + (f.busy ? " busy" : " idle") + warm +
        (f.memory ? " (" + f.memory.free_mb + " MB free)" : "");
    }).join("\n") + (st.last_release ? "\nlast release " + st.last_release : "");
    return '<section class="card">' +
      '<div class="card-head"><h2>VRAM</h2>' +
        '<span class="gen-state"' + (farmTip ? ' data-tip="' + esc(farmTip) + '"' : "") + ">" + esc(status) + "</span></div>" +
      '<div class="gen-row">' +
        sw("vram-auto", s.auto_offload, { label: "Release models after", tip: "Unload the render models after this long without a render" }) +
        '<input id="vram-idle" type="number" min="1" max="120" value="' + esc(s.idle_minutes) + '" class="gen-num">' +
        '<span class="muted small">min idle</span>' +
      "</div>" +
      '<div class="gen-row">' +
        '<button class="btn small" type="button" id="vram-offload" data-tip="Unload the render models now">' +
          Icon.get("stop") + "Offload now</button>" +
        '<button class="btn small ghost" type="button" id="vram-free-apps" data-tip="Close the GPU-heavy apps listed in settings (render_mode.kill_gpu_apps)">' +
          Icon.get("x") + "Close GPU apps</button>" +
      "</div>" +
    "</section>";
  }

  function render(el) {
    if (!State.pid) { el.innerHTML = noProject(); el.__sig = null; return; }
    const shots = State.shots || [];
    const html =
      renderer() +
      '<div class="gen-grid">' +
        videoModelCard() +
        '<div class="gen-col">' + postCard() + vramCard() + "</div>" +
      "</div>" +
      '<section class="card">' +
        '<div class="card-head"><h2>Render queue</h2>' +
          '<span class="muted small">' + shots.length + " shots</span></div>" +
        (shots.length ? queueTable(shots) : empty("Queue is empty", "Generate the shot plan first (Story page).")) +
      "</section>" +
      '<section class="card">' +
        '<details class="fold flush" id="gen-loras"' + (loraOpen ? " open" : "") + ">" +
          "<summary>LoRA registry" +
            '<span class="fold-meta">' + (State.loras || []).length + " entries · legacy Wan 2.2 only</span></summary>" +
          '<div class="fold-body">' +
            '<div class="gen-hint">These video LoRAs apply only when a shot renders on the legacy Wan 2.2 model. ' +
              "LTX-2.5 and MiniMax H3 ignore them and take the cast reference images instead.</div>" +
            '<div class="ctl-row gen-lora-bar">' +
              '<select id="gen-lora-cat" class="small-sel"><option value="">all categories</option>' +
                loraCats().map(function (c) {
                  return "<option" + (loraCat === c ? " selected" : "") + ">" + esc(c) + "</option>";
                }).join("") +
              "</select>" +
              '<button class="btn small" type="button" id="gen-lora-rescan">' + Icon.get("refresh") + "Rescan</button>" +
            "</div>" +
            '<div class="table-wrap">' + loraTable() + "</div>" +
          "</div>" +
        "</details>" +
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
      "<thead><tr><th>Shot</th><th>Scene</th><th>Tier</th><th>Status</th><th>Try</th>" +
      '<th data-tip="CLIP similarity to the prompt">Clip</th><th data-tip="Face identity">Face</th>' +
      '<th data-tip="Motion score">Motion</th><th>Time</th><th>Profile</th><th></th></tr></thead>' +
      "<tbody>" + rows.map(function (r) {
        const j = r.json || {};
        return "<tr data-sid=\"" + esc(r.shot_id) + '">' +
          '<td class="mono">' + esc(r.shot_id) + "</td>" +
          "<td>" + esc(r.scene || "") + "</td>" +
          '<td><span class="badge ' + tierClass(r.tier) + '">' + esc(r.tier || "—") + "</span></td>" +
          '<td><span class="badge ' + statusClass(r.status) + '">' + esc(r.status || "—") + "</span></td>" +
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
      return empty("No LoRA entries", "Registry is empty — rescan, or check /api/lora/video.");
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
        if (r) toast(r.ok ? "renderer: " + verb : (r.error || "failed"), !r.ok);
        State.refreshStatus();
      };
    };
    orchAct("gen-orch-start", "start");
    orchAct("gen-orch-pause", "pause");
    orchAct("gen-orch-resume", "resume");
    orchAct("gen-orch-stop", "stop");

    // fold states survive the re-renders
    const tiers = el.querySelector("#gen-tiers");
    if (tiers) tiers.addEventListener("toggle", function () { tiersOpen = tiers.open; });
    const loras = el.querySelector("#gen-loras");
    if (loras) loras.addEventListener("toggle", function () { loraOpen = loras.open; });

    const vmsel = el.querySelector("#gen-video-model");
    if (vmsel) vmsel.onchange = function () {
      saveProject(el, vmsel, { video_family: vmsel.value },
        "video model: " + (vmsel.value ? vmsel.options[vmsel.selectedIndex].text.replace(/^★\s*/, "") : defaultModelLabel()),
        "could not change the video model");
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
          tier + " shots: " + (sel.value ? sel.options[sel.selectedIndex].text.replace(/^★\s*/, "") : "movie model"),
          "could not set the tier model");
      };
    });

    const takes = el.querySelector("#gen-takes-on");
    if (takes) takes.onchange = function () {
      saveProject(el, takes, { seamless_takes: takes.checked },
        "seamless takes " + (takes.checked ? "on" : "off"), "could not change seamless takes");
    };

    const postOn = el.querySelector("#gen-post-on");
    if (postOn) postOn.onchange = function () {
      saveProject(el, postOn, { post_processing: postOn.checked },
        "post-processing " + (postOn.checked ? "on" : "off"), "could not change post-processing");
    };

    // Post worker lane picker: "" = local, or a worker id with the upscale
    // role. Persisted per project; workers without the role / blocked /
    // offline are disabled in the dropdown.
    const pws = el.querySelector("#gen-post-worker");
    if (pws) pws.onchange = function () {
      saveProject(el, pws, { post_worker: pws.value },
        pws.value ? "post runs on " + pws.options[pws.selectedIndex].text : "post runs on this PC",
        "could not change the post worker");
    };

    // the studio-wide upscale method (settings tiers.upscale, every tier)
    const ups = el.querySelector("#gen-upscaler");
    if (ups) ups.onchange = async function () {
      ups.disabled = true;
      const on = ups.value !== "none";
      const r = await post("/api/upscale/state", { enabled: on, method: on ? ups.value : "esrgan" }, 30000);
      ups.disabled = false;
      toast(r && r.ok ? (on ? "upscaler: " + ups.value : "upscaler off") : "could not change the upscaler", !(r && r.ok));
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
        toast("auto-offload " + (vramAuto.checked ? "on" : "off"));
        await refreshVram(true);
      } else {
        toast((r && r.error) || "could not change auto-offload", true);
      }
      el.__sig = null;
      render(el);
    };
    const vramIdle = el.querySelector("#vram-idle");
    if (vramIdle) vramIdle.onchange = async function () {
      const v = parseInt(vramIdle.value, 10);
      const r = await post("/api/vram", { idle_minutes: isNaN(v) ? 8 : v }, 30000);
      if (r && r.ok) {
        toast("models release after " + r.settings.idle_minutes + " min idle");
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
        toast("offload — " + parts);
        await refreshVram(true);
        el.__sig = null;
        render(el);
      } else {
        toast((r && r.error) || "offload failed", true);
      }
    };
    const freeBtn = el.querySelector("#vram-free-apps");
    if (freeBtn) freeBtn.onclick = async function () {
      const ok = await Dlg.confirm({
        title: "Close GPU apps",
        message: "Close the GPU-heavy apps listed in settings (render_mode.kill_gpu_apps)? " +
          "Unsaved work in them is lost.",
        okLabel: "Close them", danger: true,
      });
      if (!ok) return;
      freeBtn.disabled = true;
      const r = await post("/api/render_mode/free", {}, 60000);
      freeBtn.disabled = false;
      const killed = (r && r.killed) || [];
      toast(killed.length ? "closed: " + killed.join(", ") : "no listed GPU apps were running");
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
      if (r) toast(r.ok !== false ? "rescan done" : "rescan failed", !r);
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
        if (r) toast(r.ok ? sid + " → profile " + (prof.value || "auto") + " (requeued)" : "failed", !r.ok);
        Bus.emit("shots:changed");
      };
      tr.querySelector("[data-edit]").onclick = function () {
        const row = (State.shots || []).find(function (s) { return s.shot_id === sid; });
        if (!row) return;
        const veil = openModal('<div class="adv-open"></div>', {
          title: "Edit shot — " + sid,
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
        if (r) toast(r.ok ? ident + (e.target.checked ? " enabled" : " disabled") : "failed", !r.ok);
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

  Router.register("generate", {
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
