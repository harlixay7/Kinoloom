/* shots.js — Scenes & Shots: storyboard grouped by scene, each shot card
   shows START keyframe → 5s clip poster → TAIL frame with actions, plus
   per-shot reference image (upload/clear) and bible location assignment. */
(function () {
  "use strict";

  function normPath(p) { return String(p || "").replace(/\\/g, "/"); }
  function filePart(p) {
    p = normPath(p);
    const i = p.lastIndexOf("/");
    return i >= 0 ? p.slice(i + 1) : p;
  }
  function refUrl(sid, path) {
    const segs = normPath(path).split("/").map(function (s) { return encodeURIComponent(s); });
    return "/api/shotrefs/" + encodeURIComponent(State.pid) + "/" +
      encodeURIComponent(sid) + "/" + segs.join("/");
  }

  /* ============================================== reference image studio */
  let curStudio = null; /* { veil, timer, sid } */

  function imgRefUrl(sid, file) {
    return "/api/keyframes/" + encodeURIComponent(State.pid) + "/" +
      encodeURIComponent(sid) + "/" + encodeURIComponent(filePart(file));
  }

  function stopStudioPoll() {
    if (curStudio && curStudio.timer) {
      clearInterval(curStudio.timer);
      curStudio.timer = null;
    }
  }

  function closeStudio() {
    stopStudioPoll();
    if (curStudio && curStudio.veil) closeModal(curStudio.veil);
    curStudio = null;
  }

  function studioLoraRow(planLoras, lr) {
    const keys = Object.keys(planLoras || {});
    return '<div class="lr-row">' +
      '<select class="lr-sel" data-tip="LoRA to boost with">' +
        '<option value="">none</option>' +
        keys.map(function (k) {
          const e = planLoras[k] || {};
          const lab = e.label || e.name || k;
          return '<option value="' + esc(k) + '"' +
            (lr && lr.file === k ? " selected" : "") + ">" + esc(lab) + "</option>";
        }).join("") +
      "</select>" +
      '<input type="range" class="lr-str" min="0.05" max="1.5" step="0.05" value="' +
        (lr && lr.strength != null ? Number(lr.strength) : 0.7) +
        '" data-tip="LoRA strength 0.05–1.5">' +
      '<output class="lr-out"></output>' +
      '<button class="btn small" type="button" data-act="lr-rm" data-tip="Remove this LoRA">' + Icon.get("x") + "</button>" +
    "</div>";
  }

  function wireStudioLoraRow(rowEl) {
    const range = rowEl.querySelector(".lr-str");
    const out = rowEl.querySelector(".lr-out");
    const rm = rowEl.querySelector('[data-act="lr-rm"]');
    const sync = function () { out.textContent = Number(range.value).toFixed(2); };
    range.oninput = sync;
    sync();
    if (rm) rm.onclick = function () {
      if (rowEl.parentNode) rowEl.parentNode.removeChild(rowEl);
    };
  }

  async function openImgStudio(sid) {
    if (curStudio) closeStudio();
    const base = "/api/projects/" + encodeURIComponent(State.pid) +
      "/shots/" + encodeURIComponent(sid) + "/img";
    const plan = await get(base + "/plan", 20000);
    if (!plan || !Array.isArray(plan.models) || !plan.models.length) {
      toast("image studio unavailable", true);
      return;
    }
    const models = plan.models;
    const planLoras = plan.loras || {};
    const auto = plan.auto || {};
    const identity = Array.isArray(plan.identity) ? plan.identity : [];
    const envRef = plan.env_ref || null;
    const nativeRefs = Array.isArray(plan.native_refs) ? plan.native_refs : [];
    function modelInfo(id) {
      return models.filter(function (m) { return m.id === id; })[0] || null;
    }
    const firstModel = modelInfo(plan.default_model) || models[0];
    /* the pictures the chosen model draws the shot from: Qwen-Image takes
       each person's still and the place natively, SDXL locks faces with
       IP-Adapter, the others take none */
    function stripHtml(mi) {
      if (mi && mi.family === "qwen21") {
        return nativeRefs.length
          ? nativeRefs.map(function (r) {
              const place = r.kind === "location";
              return '<div class="ids-chip' + (place ? " env" : "") + '" data-tip="' +
                (place ? "The place: the set is drawn from this picture"
                       : "This person is drawn from this picture, outfit included") + '">' +
                (r.url ? '<img src="' + esc(r.url) + '" alt="' + esc(r.id || "") + '">' : "") +
                '<span class="ids-name">' + esc(r.label || r.id || "?") + "</span>" +
                '<span class="ids-lock' + (place ? " env" : "") + '">' +
                  (place ? "ENV" : "reference") + "</span>" +
              "</div>";
            }).join("")
          : '<span class="muted small">no reference pictures on this shot</span>';
      }
      if (!(mi && mi.capabilities && mi.capabilities.refs)) {
        return '<span class="muted small">No reference pictures with this model</span>';
      }
      return (identity.length
          ? identity.map(function (i) {
              return '<div class="ids-chip" ' +
                'data-tip="Identity lock — this character is locked via IP-Adapter">' +
                (i.url ? '<img src="' + esc(i.url) + '" alt="' + esc(i.char_id || "") + '">' : "") +
                '<span class="ids-name">' + esc(i.char_id || "?") + "</span>" +
                '<span class="ids-lock">identity lock</span>' +
              "</div>";
            }).join("")
          : '<span class="muted small">no identity references on this shot</span>') +
        (envRef && envRef.url
          ? '<div class="ids-chip env" ' +
              'data-tip="Environment reference — IP-Adapter style guidance for this shot">' +
              '<img src="' + esc(envRef.url) + '" alt="environment reference">' +
              '<span class="ids-lock env">ENV</span>' +
            "</div>"
          : "");
    }
    const veil = openModal(
      '<div class="ids-strip" id="sfx-strip">' + stripHtml(firstModel) + "</div>" +
      '<div class="studio-row">' +
        '<label class="studio-field">Model' +
          '<select id="sfx-model" data-tip="Image model to render with">' +
            models.map(function (m) {
              return '<option value="' + esc(m.id || "") + '"' +
                (firstModel && m.id === firstModel.id ? " selected" : "") + (m.gpu ? " disabled" : "") +
                (m.gpu ? ' title="' + esc(m.gpu) + '"' : "") + ">" +
                esc(m.name || m.id || "?") + (m.gpu ? " (not on this graphics card)" : "") + "</option>";
            }).join("") +
          "</select></label>" +
        '<label class="studio-field">Count' +
          '<select id="sfx-count" data-tip="Images to generate">' +
            [1, 2, 3, 4].map(function (n) {
              return '<option value="' + n + '"' + (n === 1 ? " selected" : "") + ">" + n + "</option>";
            }).join("") +
          "</select></label>" +
      "</div>" +
      '<div id="sfx-lora-box">' +
        '<div class="studio-section-head">LoRAs <span class="muted small">up to 6</span></div>' +
        '<div id="sfx-loras">' + studioLoraRow(planLoras, null) + "</div>" +
        '<button class="btn small" type="button" id="sfx-lora-add" ' +
          'data-tip="Add another LoRA row (max 6)">+ add LoRA</button>' +
      "</div>" +
      '<div class="studio-field"><label>Prompt</label>' +
        '<textarea id="sfx-prompt" rows="4" spellcheck="false">' + esc(auto.prompt || "") +
        "</textarea></div>" +
      '<div class="studio-field" id="sfx-neg-field"><label>Negative</label>' +
        '<textarea id="sfx-negative" rows="3" spellcheck="false">' + esc(auto.negative || "") +
        "</textarea></div>" +
      '<button class="btn small" type="button" id="sfx-adv-toggle" ' +
        'data-tip="Fine-tune steps, CFG and seed">' + Icon.get("chevron-down") + "Advanced</button>" +
      '<div class="adv-collapse hidden">' +
        '<div class="studio-row">' +
          '<label class="studio-field">Steps<input id="sfx-steps" type="number" min="1" max="60" placeholder="auto"></label>' +
          '<label class="studio-field">CFG<input id="sfx-cfg" type="number" min="0" max="20" step="0.5" placeholder="auto"></label>' +
          '<label class="studio-field">Seed<input id="sfx-seed" type="number" placeholder="random"></label>' +
        "</div>" +
      "</div>" +
      '<div id="sfx-progress" class="studio-progress"></div>' +
      '<div id="sfx-results" class="studio-results"></div>' +
      '<div class="studio-actions">' +
        '<button class="btn primary" type="button" id="sfx-generate">' + Icon.get("wand") + "Generate</button>" +
        '<button class="btn danger" type="button" id="sfx-stop">' + Icon.get("stop") + "Stop</button>" +
        '<button class="btn" type="button" id="sfx-close">Close</button>' +
      "</div>",
      { title: sid + " — reference image studio", wide: true });
    if (!veil) return;
    curStudio = { veil: veil, timer: null, sid: sid };
    const cs = curStudio;

    veil.querySelectorAll(".lr-row").forEach(wireStudioLoraRow);
    const loraBox = veil.querySelector("#sfx-loras");
    const addBtn = veil.querySelector("#sfx-lora-add");
    if (loraBox && addBtn) addBtn.onclick = function () {
      if (loraBox.querySelectorAll(".lr-row").length >= 6) {
        toast("max 6 LoRAs", true);
        return;
      }
      const holder = document.createElement("div");
      holder.innerHTML = studioLoraRow(planLoras, null);
      const rowEl = holder.firstElementChild;
      if (!rowEl) return;
      loraBox.appendChild(rowEl);
      wireStudioLoraRow(rowEl);
    };

    /* the strip, LoRAs and negative follow the chosen model */
    const modelSel = veil.querySelector("#sfx-model");
    const loraWrap = veil.querySelector("#sfx-lora-box");
    const negField = veil.querySelector("#sfx-neg-field");
    function syncModel() {
      const mi = modelInfo(modelSel.value);
      const caps = (mi && mi.capabilities) || {};
      veil.querySelector("#sfx-strip").innerHTML = stripHtml(mi);
      loraWrap.classList.toggle("hidden", !caps.loras);
      negField.classList.toggle("hidden", caps.negative_override === false);
    }
    modelSel.onchange = syncModel;
    syncModel();

    const advToggle = veil.querySelector("#sfx-adv-toggle");
    const advBox = veil.querySelector(".adv-collapse");
    if (advToggle && advBox) advToggle.onclick = function () {
      advBox.classList.toggle("hidden");
    };

    const closeBtn = veil.querySelector("#sfx-close");
    if (closeBtn) closeBtn.onclick = function () { closeStudio(); };
    const x = veil.querySelector(".ws-modal-x");
    if (x) x.onclick = function () { closeStudio(); };
    veil.addEventListener("click", function (e) {
      if (e.target === veil) closeStudio();
    });

    const genBtn = veil.querySelector("#sfx-generate");
    if (genBtn) genBtn.onclick = async function () {
      const loras = [];
      veil.querySelectorAll(".lr-row").forEach(function (lr) {
        const sel = lr.querySelector(".lr-sel");
        if (sel && sel.value) {
          loras.push({
            file: sel.value,
            strength: Number(lr.querySelector(".lr-str").value) || 0.7,
          });
        }
      });
      const body = {
        model: modelSel.value,
        count: Number(veil.querySelector("#sfx-count").value) || 1,
        /* hidden rows (a model without LoRAs) are not sent */
        loras: loraWrap.classList.contains("hidden") ? [] : loras,
        prompt: veil.querySelector("#sfx-prompt").value,
        negative: veil.querySelector("#sfx-negative").value,
      };
      const steps = veil.querySelector("#sfx-steps").value;
      if (steps !== "") body.steps = Number(steps);
      const cfg = veil.querySelector("#sfx-cfg").value;
      if (cfg !== "") body.cfg = Number(cfg);
      const seed = veil.querySelector("#sfx-seed").value;
      if (seed !== "") body.seed = Number(seed);
      genBtn.disabled = true;
      const r = await post(base + "/generate", body, 120000);
      genBtn.disabled = false;
      if (!r || !veil.isConnected) return;
      if (!r.ok) { toast(r.error || "generation failed", true); return; }
      toast("reference image generation started");
      const prog = veil.querySelector("#sfx-progress");
      if (prog) prog.textContent = "starting…";
      stopStudioPoll();
      cs.timer = setInterval(function () { studioTick(cs); }, 3000);
    };

    const stopBtn = veil.querySelector("#sfx-stop");
    if (stopBtn) stopBtn.onclick = async function () {
      const r = await post(base + "/stop", {}, 30000);
      stopStudioPoll();
      const prog = veil.querySelector("#sfx-progress");
      if (prog) prog.textContent = "stopped";
      if (r) toast(r.ok ? "generation stopped" : (r.error || "failed"), !r.ok);
    };

    bindTips(veil);
  }

  async function studioTick(cs) {
    if (!cs || !cs.veil || !cs.veil.isConnected) {
      stopStudioPoll();
      if (curStudio === cs) curStudio = null;
      return;
    }
    const veil = cs.veil;
    const r = await window.API.get("/api/projects/" + encodeURIComponent(State.pid) +
      "/shots/" + encodeURIComponent(cs.sid) + "/img/status", 15000, true);
    if (!r) return; /* transient — keep polling */
    const prog = veil.querySelector("#sfx-progress");
    if (r.active) {
      if (prog) prog.textContent = "generating… " +
        (r.active.done != null && r.active.total != null
          ? r.active.done + "/" + r.active.total : "");
    } else {
      stopStudioPoll();
      if (r.files && r.files.length) {
        showStudioResults(veil, cs.sid, r.files);
        if (prog) prog.textContent = "done — " + r.files.length + " image(s)";
      } else if (prog) {
        prog.textContent = r.error || "no images generated";
      }
    }
  }

  function showStudioResults(veil, sid, files) {
    const box = veil.querySelector("#sfx-results");
    if (!box) return;
    box.innerHTML = files.map(function (f) {
      return '<div class="gen-thumb">' +
        '<img loading="lazy" src="' + esc(imgRefUrl(sid, f)) + '" alt="generated reference">' +
        '<div class="gen-act">' +
          '<button class="btn small primary" type="button" data-use="' + esc(f) + '">Use as start</button>' +
        "</div>" +
      "</div>";
    }).join("");
    box.querySelectorAll("[data-use]").forEach(function (b) {
      b.onclick = async function () {
        const r = await post("/api/projects/" + encodeURIComponent(State.pid) +
          "/shots/" + encodeURIComponent(sid) + "/img/use",
          { file: b.dataset.use }, 30000);
        if (!r) return;
        if (r.ok) {
          toast("reference set — shot requeued");
          closeStudio();
          State.refreshShots();
        } else {
          toast(r.error || "failed", true);
        }
      };
    });
  }

  function render(el) {
    if (!State.pid) { el.innerHTML = noProject(); return; }
    const shots = State.shots || [];
    const scenes = (State.bible && State.bible.scenes) || {};
    if (!shots.length) {
      el.innerHTML = empty("No shots in this project yet",
        "Generate the story (Story page) or import a shot plan — the queue appears here.");
      return;
    }
    const groups = {};
    shots.slice().sort(function (a, b) {
      return (a.scene || "").localeCompare(b.scene || "") ||
        (a.shot_id || "").localeCompare(b.shot_id || "");
    }).forEach(function (r) {
      (groups[r.scene || "?"] = groups[r.scene || "?"] || []).push(r);
    });
    /* scene meta: beats carry location/time/mood (the spine only imports
       beats; bible scene YAMLs are optional) — beats win, bible overrides */
    const beatsMap = {};
    (State.project && State.project.story && State.project.story.beats &&
      Array.isArray(State.project.story.beats.beats)
      ? State.project.story.beats.beats : []).forEach(function (b) {
        if (b && b.scene) beatsMap[b.scene] = b;
      });
    /* render EVERY scene from the spine (with its real environment),
       plus any shot groups not in the beats — unwritten scenes show a
       "script not written yet" placeholder */
    const allScenes = {};
    Object.keys(beatsMap).forEach(function (s) { allScenes[s] = groups[s] || []; });
    Object.keys(groups).forEach(function (s) {
      if (!(s in allScenes)) allScenes[s] = groups[s];
    });
    const passedAll = shots.filter(function (r) { return r.status === "passed"; }).length;
    const pv = State.prompts;
    const movie = !!(pv && pv.shots && Object.keys(pv.shots).length);
    /* after a first batch from a chat AI, the same button gives the next
       batch's brief: say so; without an AI writer it is the way to go on */
    const prog = State.promptProgress();
    const chatFirst = !!(movie && !pv.ai && prog.todo);
    el.innerHTML =
      '<header class="page-intro">' +
        '<div class="pi-text"><h1>Shots</h1>' +
          "<p>Every shot of the shot plan, scene by scene. A shot renders once it has a video prompt: the AI writer " +
          "writes them in story order, or you write one by hand on the shot.</p></div>" +
        '<div class="pi-acts">' +
          '<span class="pi-stat"><b>' + shots.length + "</b> shots in <b>" + Object.keys(allScenes).length +
            "</b> scenes, <b>" + passedAll + "</b> passed</span>" +
          '<button class="btn small ghost" type="button" data-batchrefs data-tip="Make start pictures for many shots with one setup, and give each shot the sharpest">' +
            Icon.get("image") + "Start pictures</button>" +
        "</div>" +
      "</header>" +
      (movie
        ? '<div class="prompt-bar">' +
            '<span class="pb-mark">' + Icon.get("pen") + "</span>" +
            '<div class="pbar-text"><b>Video prompts</b>' + promptSummary() + "</div>" +
            '<span class="spacer"></span>' +
            '<button class="btn small ' + (chatFirst ? "primary" : "ghost") + '" type="button" data-chat-brief data-tip="' +
              (prog.next
                ? prog.todo + (prog.todo === 1 ? " shot still needs" : " shots still need") + " a video prompt. Copy the brief " +
                  "for the next batch into the same chat AI and paste its reply back here"
                : "Copy a brief for the next shots (one scene, a few prompts at a time; a longer take in parts) " +
                  "into any chat AI and paste its reply back, batch by batch") + '">' +
              Icon.get("copy") + (prog.next ? "Use a chat AI for the next batch" : "Use a chat AI") + "</button>" +
            '<button class="btn small ghost" type="button" data-ai-settings data-tip="The AI that writes the story and the video prompts">' +
              Icon.get("spark") + (pv.ai ? "AI writer" : "Connect an AI") + "</button>" +
            '<button class="btn small ' + (chatFirst ? "ghost" : "primary") + '" type="button" data-write-prompts data-tip="' +
              (pv.ai ? "The AI writer writes every missing and outdated prompt, take by take in story order, with the story so far, the cast and the rules of the video model"
                     : "Connect an AI writer first, or use a chat AI") + '"' +
              (pv.ai ? "" : " disabled") + ">" + Icon.get("wand") + "Write the prompts</button>" +
          "</div>"
        : "") +
      '<div id="sp-writer" class="aigen-box hidden"></div>' +
      Object.keys(allScenes).sort().map(function (sceneId) {
      const rows = allScenes[sceneId];
      const meta = Object.assign({}, scenes[sceneId] || {},
                                 beatsMap[sceneId] || {});
      const passed = rows.filter(function (r) { return r.status === "passed"; }).length;
      const body = rows.length
        ? '<div class="shot-grid"></div>'
        : '<div class="ws-empty slim"><div>This scene has no shots yet.</div>' +
          '<div class="ws-empty-hint">Write them on the Story page: with the AI writer, or paste a chat AI&apos;s reply for this scene.</div></div>';
      const slugline = [meta.location, meta.time].filter(Boolean).map(function (x) {
        return String(x).replace(/_/g, " ").toUpperCase();
      }).join(" - ");
      return '<section class="scene-block" data-scene="' + esc(sceneId) + '">' +
        '<header class="scene-head">' +
          '<h2 class="scene-title">' + esc(sceneId) + "</h2>" +
          (slugline ? '<span class="scene-slug">' + esc(slugline) + "</span>" : "") +
          (meta.mood ? '<span class="scene-meta">' + esc(meta.mood) + "</span>" : "") +
          '<span class="spacer"></span>' +
          '<span class="scene-count">' + (rows.length ? passed + " of " + rows.length + " passed" : "No shots") + "</span>" +
          '<div class="scene-prog"><div class="scene-prog-fill" style="width:' +
            (rows.length ? Math.round((passed / rows.length) * 100) : 0) + '%"></div></div>' +
          (rows.length
            ? '<button class="btn small ghost icon" type="button" data-del-scene="' +
                esc(sceneId) + '" data-tip="Delete scene: removes its shots from the queue; files stay on disk">' +
                Icon.get("trash") + "</button>"
            : "") +
        "</header>" +
        body +
      "</section>";
    }).join("");
    el.querySelectorAll("[data-del-scene]").forEach(function (b) {
      b.onclick = async function () {
        const sceneId = b.dataset.delScene;
        const v = openModal(
          '<p>Delete scene <b>' + esc(sceneId) + "</b>? Its shots are removed " +
          "from the queue (rendered clips stay on disk).</p>" +
          '<div class="ctl-row"><button class="btn" type="button" id="sd-cancel">Cancel</button>' +
          '<button class="btn warn" type="button" id="sd-go">Delete scene</button></div>',
          { title: "Delete scene" });
        if (!v) return;
        v.querySelector("#sd-cancel").onclick = function () { closeModal(v); };
        v.querySelector("#sd-go").onclick = async function () {
          closeModal(v);
          const r = await post("/api/projects/" + encodeURIComponent(State.pid) +
            "/cast/scenes/" + encodeURIComponent(sceneId) + "/delete", {}, 60000);
          if (r && r.ok) {
            toast("Scene deleted, with its " + (r.removed_shots || 0) + " shots");
            await State.refreshProject();
            await State.refreshShots();
            Bus.emit("project", State.project);
            render(el);
          } else if (r) {
            toast("Could not delete the scene: " + (r.error || "unknown error"), true);
          }
        };
      };
    });
    /* build cards as real DOM (events) */
    Object.keys(groups).forEach(function (sceneId) {
      const wrap = el.querySelector('[data-scene="' + CSS.escape(sceneId) + '"] .shot-grid');
      if (!wrap) return;
      groups[sceneId].forEach(function (r) {
        const card = UI.shotCard(r, {});
        wrap.appendChild(card);
        extendShotCard(card, r);
        promptRow(card, r);
      });
    });
    const aiBtn = el.querySelector("[data-ai-settings]");
    if (aiBtn) aiBtn.onclick = function () { AiSettings.open(function () { State.refreshPrompts(); }); };
    const briefBtn = el.querySelector("[data-chat-brief]");
    if (briefBtn) briefBtn.onclick = function () { PromptEditor.brief(); };
    const writeBtn = el.querySelector("[data-write-prompts]");
    if (writeBtn) writeBtn.onclick = async function () {
      writeBtn.disabled = true;
      const r = await post("/api/projects/" + encodeURIComponent(State.pid) + "/prompts/write", {}, 30000);
      writeBtn.disabled = false;
      if (r && r.ok) startedWriting(el);
    };
    // the polled status is fresher than the view's, unless the view shows a
    // newer job (one the director started)
    const live = lastJob && lastJob.pid === State.pid ? lastJob.job : null;
    const snap = pv && pv.writer;
    const job = !live ? snap : !snap ? live : ((snap.started || 0) > (live.started || 0) ? snap : live);
    showWriter(el, job);
    if (job && job.running) watchWriter(el);
    markWriter(el);
    bindTips(el);
  }

  /* an AI writer that is set up but does not answer: the chat AI becomes the
     primary way (asked at most once a minute, apart from the page load) */
  let reach = null;          /* {at, up, base} */
  async function markWriter(el) {
    const pv = State.prompts;
    if (!pv || !pv.ai) return;
    if (!reach || Date.now() - reach.at > 60000) {
      const r = await API.get("/api/ai/reachable", 20000, true);
      if (!r) return;
      reach = { at: Date.now(), up: !!r.reachable, base: r.base_url || "" };
    }
    if (reach.up) return;
    const chatBtn = el.querySelector("[data-chat-brief]");
    const writeBtn = el.querySelector("[data-write-prompts]");
    if (chatBtn) { chatBtn.classList.remove("ghost"); chatBtn.classList.add("primary"); }
    if (writeBtn) {
      writeBtn.classList.remove("primary");
      writeBtn.classList.add("ghost");
      writeBtn.dataset.tip = "The AI writer" + (reach.base ? " at " + reach.base : "") +
        " is not answering: start it, pick another under AI writer, or use a chat AI";
    }
  }

  /* ====================================================== video prompts */
  let writerTimer = null;
  let lastJob = null;        /* {pid, job}: the writer's latest status */
  let hiddenJob = null;      /* the finished job whose summary was closed */

  function promptSummary() {
    const pv = State.prompts;
    if (!pv || !pv.shots || !Object.keys(pv.shots).length) return "";
    const c = pv.counts || {};
    const parts = [];
    if (c.ready) parts.push('<span class="pb-n ok">' + c.ready + " ready</span>");
    if (c.missing) parts.push('<span class="pb-n bad">' + c.missing + " without one</span>");
    if (c.stale) parts.push('<span class="pb-n warn">' + c.stale + " outdated</span>");
    if (!parts.length) return '<span class="pb-n">Nothing left to render</span>';
    return '<span class="pb-counts" data-tip="The prompts of the shots still to render. A shot renders only ' +
      'with a prompt written for it, by the AI or by hand; one without waits.">' + parts.join("") + "</span>";
  }

  function showWriter(el, job) {
    const box = el.querySelector("#sp-writer");
    if (!box) return;
    if (!job || (!job.running && job.started === hiddenJob)) { box.classList.add("hidden"); return; }
    const failed = Object.keys(job.failed || {});
    const pct = job.total ? Math.round(100 * (job.done || 0) / job.total) : 0;
    box.classList.remove("hidden");
    box.innerHTML =
      '<span class="aigen-status' + (job.running ? "" : failed.length ? " err" : " ok") + '">' +
        (job.running ? "writing prompts"
          : !failed.length ? "prompts written"
          : (job.written || []).length ? "prompts written, some not" : "prompts not written") +
      "</span>" +
      (job.running ? '<span class="aigen-bar"><span class="aigen-fill" style="width:' + pct + '%"></span></span>' : "") +
      '<span class="muted small">' + esc(job.message || "") +
        (job.total ? " (" + (job.done || 0) + "/" + job.total + ")" : "") + "</span>" +
      (failed.length && !job.running
        ? '<span class="small st-red">' + esc(failed.slice(0, 4).join(", ")) + (failed.length > 4 ? "…" : "") +
          ": " + esc(job.failed[failed[0]]) + "</span>"
        : "") +
      '<span class="spacer"></span>' +
      (job.running
        ? '<button class="btn small" type="button" data-writer-stop>' + Icon.get("stop") + "Stop</button>"
        : '<button class="btn small ghost icon" type="button" data-writer-hide data-tip="Hide">' + Icon.get("x") + "</button>");
    const stop = box.querySelector("[data-writer-stop]");
    if (stop) stop.onclick = function () {
      post("/api/projects/" + encodeURIComponent(State.pid) + "/prompts/write/stop", {}, 30000);
    };
    const hide = box.querySelector("[data-writer-hide]");
    if (hide) hide.onclick = function () { hiddenJob = job.started; box.classList.add("hidden"); };
    bindTips(box);
  }

  /* a job was just started: show it before the first status poll (any
     status the server sends replaces this one) */
  function startedWriting(el) {
    lastJob = { pid: State.pid, job: { running: true, message: "starting" } };
    showWriter(el, lastJob.job);
    watchWriter(el);
  }

  function stopWatching() {
    if (writerTimer) { clearInterval(writerTimer); writerTimer = null; }
  }

  function watchWriter(el) {
    if (writerTimer) return;
    const pid = State.pid;
    writerTimer = setInterval(async function () {
      if (!el.isConnected || pid !== State.pid) { stopWatching(); return; }
      const r = await API.get("/api/projects/" + encodeURIComponent(pid) + "/prompts/write/status", 15000, true);
      if (!r) return;
      lastJob = { pid: pid, job: r.active };
      showWriter(el, r.active);
      if (!r.active || !r.active.running) {
        stopWatching();
        await State.refreshPrompts();
        await State.refreshShots();
      }
    }, 2500);
  }

  /* the card's prompt row: its state, the start of the prompt, the editor */
  function promptRow(card, row) {
    const e = State.prompts && State.prompts.shots && State.prompts.shots[row.shot_id];
    const foot = card.querySelector(".sc-foot");
    if (!e || !foot) return;
    const text = String(e.text || "");
    const div = document.createElement("div");
    div.className = "sc-prompt";
    div.innerHTML = PromptEditor.chip(e) +
      '<span class="sc-prompt-text"' + (text ? ' title="' + esc(text) + '"' : "") + ">" +
        esc(text || (e.state === "missing" ? "" : e.why) || "") + "</span>" +
      '<button class="btn small ghost" type="button" data-tip="Read, write or check this shot\'s video prompt">' +
        Icon.get("pen") + "Prompt</button>";
    div.querySelector("button").onclick = function () { PromptEditor.open(row.shot_id); };
    foot.insertAdjacentElement("beforebegin", div);
    // an LTX-2.5 / H3 shot opens on no keyframe: say what it opens on
    const ph = card.querySelector(".sc-media .ph-kf span");
    if (ph) ph.textContent = e.wants_image ? "Made first"
      : OPENS[e.start] || "None";
    if (e.frame) openingFrame(card, row.shot_id, e.frame);
  }

  /* the opening frame the shot's take starts from, how much the faces in it
     look like the cast's pictures, and Make again */
  function openingFrame(card, sid, f) {
    const slot = card.querySelector(".sc-media .sc-frame");
    if (!slot || slot.querySelector("img")) return;           // the shot's own start picture wins
    const like = f.likeness == null ? null : Number(f.likeness);
    const who = Object.keys(f.people || {}).map(function (n) {
      return n + " " + (f.people[n] == null ? "not seen" : Number(f.people[n]).toFixed(2));
    }).join(", ");
    const tip = !f.scored ? "Not scored: install InsightFace to compare the faces with the cast's pictures"
      : like == null ? "No face in it to compare with the cast's pictures"
      : "Likeness to the cast's pictures (ArcFace, the least-alike person counts): " + who +
        (f.weak ? ". Under the floor of " + f.min_likeness + " (movie.key_image_min_likeness): make it again" : "");
    slot.innerHTML =
      '<img loading="lazy" src="/api/projects/' + encodeURIComponent(State.pid) + "/shots/" +
        encodeURIComponent(sid) + "/opening-frame?v=" + encodeURIComponent(f.v) + '" alt="opening frame">' +
      '<div class="sc-frame-tag">Opening frame</div>' +
      (f.scored && like != null
        ? '<div class="sc-frame-tag sc-like' + (f.weak ? " weak" : "") + '" data-tip="' + esc(tip) + '">' +
          like.toFixed(2) + "</div>"
        : "") +
      '<button class="sc-raw sc-again" type="button" data-tip="Make the opening frame again with new seeds. ' +
        esc(tip) + '">' + Icon.get("refresh") + "</button>";
    const btn = slot.querySelector(".sc-again");
    btn.onclick = async function (ev) {
      ev.stopPropagation();                // not the frame's own click
      btn.disabled = true;
      const r = await post("/api/projects/" + encodeURIComponent(State.pid) + "/shots/" +
        encodeURIComponent(sid) + "/opening-frame/again", {}, 30000);
      if (!r || !r.ok) { btn.disabled = false; return; }
      toast("making the opening frame of " + sid + " again");
      waitFrame(sid, f.v);
    };
    bindTips(slot);
  }

  /* refresh the prompts until the frame made again is in (at most 20 minutes) */
  function waitFrame(sid, v) {
    const pid = State.pid;
    const t0 = Date.now();
    const timer = setInterval(async function () {
      if (pid !== State.pid || Date.now() - t0 > 20 * 60000) { clearInterval(timer); return; }
      await State.refreshPrompts();
      const e = State.prompts && State.prompts.shots && State.prompts.shots[sid];
      if (e && e.frame && e.frame.v !== v) {
        clearInterval(timer);
        await State.refreshShots();
      }
    }, 10000);
  }
  const OPENS = {
    "": "From the prompt",
    "opening frame": "Opening frame",
    "previous clip": "Previous clip",
    take: "Same take",
  };

  /* extend a built shot card with reference-image + location controls */
  function extendShotCard(card, row) {
    const sid = row.shot_id;
    const j = row.json || {};
    if (row.ref_image) {
      const frame = card.querySelector(".sc-media .sc-frame");
      if (frame) {
        frame.innerHTML =
          '<img loading="lazy" src="' + esc(refUrl(sid, row.ref_image)) + '" alt="reference">' +
          '<div class="sc-frame-tag ref">Ref</div>' +
          '<div class="sc-frame-tag start">Start</div>';
      }
    }
    const foot = card.querySelector(".sc-foot");
    if (!foot) return;
    const ctl = document.createElement("div");
    ctl.className = "sc-ctl";
    const locs = asList((State.bible || {}).locations);
    const loc = locs.filter(function (l) { return (l.id || "") === (j.location || ""); })[0] || {};
    const tags = [];
    (loc.reference_images || []).forEach(function (r) {
      const t = r && r.tag ? String(r.tag) : "";
      if (t && tags.indexOf(t) < 0) tags.push(t);
    });
    const imgRef = row.ref_image &&
      filePart(row.ref_image).indexOf("img_") === 0;
    const prev = j.prev_shot || "";
    // one row of per-shot settings: continuity, location (+ place variant,
    // power level), and the start-reference tools as icon buttons
    ctl.innerHTML =
      '<select class="sc-cont-sel" data-tip="Continuity' + (prev ? " after " + esc(prev) : "") +
        ": CUT starts a new shot. CONTINUE carries the same camera take on from the previous shot's last frame " +
        "(on LTX-2.5 / H3 a run renders as one seamless take). MATCH_ACTION cuts to a new angle while the action " +
        "carries on. A scene or location change is always a CUT. Changing it requeues the shot." + '">' +
        ["CUT", "CONTINUE", "MATCH_ACTION"].map(function (c) {
          return '<option value="' + c + '"' +
            (String(j.continuity || "CUT") === c ? " selected" : "") + ">" + c.replace("_", " ").toLowerCase() + "</option>";
        }).join("") +
      "</select>" +
      '<select class="sc-loc-sel" data-tip="Bible location of this shot">' +
        '<option value="">no location</option>' +
        locs.map(function (l) {
          const id = l.id || "";
          return '<option value="' + esc(id) + '"' + (j.location === id ? " selected" : "") + ">" +
            esc(id || "?") + "</option>";
        }).join("") +
      "</select>" +
      (tags.length
        ? '<select class="sc-place-sel" data-tip="Environment variant: the place whose reference image is passed (auto = scene map)">' +
            '<option value="">place: auto</option>' +
            tags.map(function (t) {
              return '<option value="' + esc(t) + '"' +
                (String(j.location_tag || "") === t ? " selected" : "") + ">" +
                esc(t) + "</option>";
            }).join("") +
          "</select>"
        : "") +
      (j.power
        ? '<label class="sc-lvl" data-tip="How strong the power looks in this shot. The power picture with the closest level at or below it is used. Leave it blank to follow the story.">' +
            "Power" +
            '<input type="number" class="sc-lvl-in" min="0" max="99" step="1" ' +
              'value="' + (j.power_level != null ? j.power_level : "") + '" placeholder="auto">' +
          "</label>"
        : "") +
      '<span class="spacer"></span>' +
      (imgRef
        ? '<span class="lora-chip imgref" data-tip="Start reference generated by the image studio">img ref</span>'
        : "") +
      '<button class="btn small ghost icon sc-img-btn" type="button" ' +
        'data-tip="Generate a start reference with the image studio">' + Icon.get("wand") + "</button>" +
      '<button class="btn small ghost icon sc-ref-btn" type="button" ' +
        'data-tip="Upload a start reference: the keyframe generates from it (I2V)">' + Icon.get("upload") + "</button>" +
      '<input type="file" class="sc-ref-file" accept="image/png,image/jpeg,image/webp" hidden>' +
      (row.ref_image
        ? '<button class="btn small ghost icon sc-ref-clear" type="button" ' +
            'data-tip="Remove the start reference">' + Icon.get("x") + "</button>"
        : "") +
      (function () {
        const refs = j.applied_references || [];
        const warns = j.reference_warnings || [];
        if (!refs.length && !warns.length) return "";
        // "character:Sara:royal gown" reads "Sara (royal gown)",
        // "location:Palace:the sky docks" reads "Palace, the sky docks"
        const chips = refs.map(function (r) {
          const raw = String(r.source || "?");
          const parts = raw.replace(/^(character|location):/, "").split(":");
          const label = /^location:/.test(raw) ? parts.join(", ")
            : parts[0] + (parts.length > 1 ? " (" + parts.slice(1).join(", ") + ")" : "");
          return '<span class="lora-chip refchip" data-tip="Picture ' + esc(String(r.slot || "?")) +
            ' of the last render">' + Icon.get(/^location:/.test(raw) ? "pin" : "users") + esc(label) + "</span>";
        }).join("");
        // the warnings themselves already show on the card (refs: …)
        return chips ? '<div class="sc-refplan">' + chips + "</div>" : "";
      })();
    foot.insertAdjacentElement("beforebegin", ctl);

    const btn = ctl.querySelector(".sc-ref-btn");
    const imgBtn = ctl.querySelector(".sc-img-btn");
    const fileIn = ctl.querySelector(".sc-ref-file");
    const clearBtn = ctl.querySelector(".sc-ref-clear");
    const locSel = ctl.querySelector(".sc-loc-sel");
    if (imgBtn) imgBtn.onclick = function () { openImgStudio(sid); };
    if (btn && fileIn) btn.onclick = function () { fileIn.click(); };
    if (fileIn) fileIn.onchange = async function () {
      const files = Array.prototype.slice.call(fileIn.files || []);
      if (!files.length) return;
      const r = await uploadImage("/api/projects/" + encodeURIComponent(State.pid) +
        "/shots/" + encodeURIComponent(sid) + "/refs/upload", files);
      fileIn.value = "";
      if (!r) return;
      if (r.ok) { toast("reference set for " + sid); State.refreshShots(); }
      else toast(r.error || "upload failed", true);
    };
    if (clearBtn) clearBtn.onclick = async function () {
      const r = await post("/api/projects/" + encodeURIComponent(State.pid) +
        "/shots/" + encodeURIComponent(sid) + "/refs/clear", {}, 30000);
      if (!r) return;
      toast(r.ok ? "reference cleared" : (r.error || "failed"), !r.ok);
      if (r.ok) State.refreshShots();
    };
    if (locSel) locSel.onchange = async function () {
      const r = await post("/api/projects/" + encodeURIComponent(State.pid) +
        "/shots/" + encodeURIComponent(sid) + "/location", { location: locSel.value }, 30000);
      if (!r) return;
      toast(r.ok ? "location set" : (r.error || "failed"), !r.ok);
      if (r.ok) State.refreshShots();
    };
    const placeSel = ctl.querySelector(".sc-place-sel");
    if (placeSel) placeSel.onchange = async function () {
      const r = await post("/api/projects/" + encodeURIComponent(State.pid) +
        "/shots/" + encodeURIComponent(sid) + "/reference-choices",
        { location_tag: placeSel.value }, 30000);
      if (!r) return;
      toast(r.ok ? "place variant set" : (r.error || "failed"), !r.ok);
      if (r.ok) State.refreshShots();
    };
    const contSel = ctl.querySelector(".sc-cont-sel");
    if (contSel) contSel.onchange = async function () {
      // CONTINUE also resets the QA stage (the old Continue button's route)
      const base = "/api/projects/" + encodeURIComponent(State.pid) + "/shots/" + encodeURIComponent(sid);
      const send = function (dry) {
        return contSel.value === "CONTINUE"
          ? post(base + "/continue", { dry_run: dry }, 30000)
          : post(base + "/continuity", { continuity: contSel.value, dry_run: dry }, 30000);
      };
      // a take renders whole: say which rendered shots the change renders again
      const ask = await send(true);
      if (!ask || !ask.ok) { contSel.value = String(j.continuity || "CUT"); return; }  // the error is shown
      const again = ask.rerenders || [];
      if (again.length) {
        const one = again.length === 1;
        const ok = await Dlg.confirm({
          title: "Render " + again.length + " more shot" + (one ? "" : "s") + " again?",
          message: "After this change " + again.join(", ") + (one ? " renders" : " render") + " again: a take " +
            "renders whole, and a shot that opens on the last frame of a shot rendered again renders again too. " +
            (one ? "The clip it has now is replaced." : "The clips they have now are replaced."),
          okLabel: "Change and render again",
        });
        if (!ok) { contSel.value = String(j.continuity || "CUT"); return; }
      }
      const r = await send(false);
      if (!r) return;
      toast(r.ok ? sid + ": " + contSel.value.toLowerCase().replace("_", " ") + ", requeued" : (r.error || "failed"), !r.ok);
      if (r.ok) State.refreshShots();
    };
    const lvlIn = ctl.querySelector(".sc-lvl-in");
    if (lvlIn) lvlIn.onchange = async function () {
      const raw = String(lvlIn.value || "").trim();
      const v = raw === "" ? 0 : parseInt(raw, 10);
      if (isNaN(v) || v < 0 || v > 99) { toast("power level must be 1–99 (blank = auto)", true); return; }
      const r = await post("/api/projects/" + encodeURIComponent(State.pid) +
        "/shots/" + encodeURIComponent(sid) + "/reference-choices",
        { power_level: v }, 30000);
      if (!r) return;
      toast(r.ok ? (v ? "power level set to " + v : "power level reset to auto") : (r.error || "failed"), !r.ok);
      if (r.ok) State.refreshShots();
    };
  }

  Router.register("shots", {
    polls: ["status", "shots"],
    subs: [],
    mount: function (el) {
      const page = this;
      page.el = el;
      page.render = function () { render(el); };
      // shot statuses decide which prompts are still needed: the view
      // follows them, at most every 10 s
      let promptsAt = 0;
      const onShots = function () {
        page.render();
        if (Date.now() - promptsAt > 10000) { promptsAt = Date.now(); State.refreshPrompts(); }
      };
      page.subs.push(Bus.on("shots", onShots));
      page.subs.push(Bus.on("project", page.render));
      page.subs.push(Bus.on("prompts", page.render));
      page.subs.push(Bus.on("prompts:writing", function () { startedWriting(el); }));
      page.render();
      promptsAt = Date.now();
      State.refreshPrompts();
    },
    unmount: function () {
      stopWatching();
      closeStudio();
      this.subs.forEach(function (off) { off(); });
      this.subs = [];
      if (this.el) this.el.innerHTML = "";
    },
  });
})();
