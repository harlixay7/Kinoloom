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
    const veil = openModal(
      '<div class="ids-strip">' +
        (identity.length
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
          : "") +
      "</div>" +
      '<div class="studio-row">' +
        '<label class="studio-field">Model' +
          '<select id="sfx-model" data-tip="Image model to render with">' +
            models.map(function (m) {
              return '<option value="' + esc(m.id || "") + '">' +
                esc(m.name || m.id || "?") + "</option>";
            }).join("") +
          "</select></label>" +
        '<label class="studio-field">Count' +
          '<select id="sfx-count" data-tip="Images to generate">' +
            [1, 2, 3, 4].map(function (n) {
              return '<option value="' + n + '"' + (n === 1 ? " selected" : "") + ">" + n + "</option>";
            }).join("") +
          "</select></label>" +
      "</div>" +
      '<div class="studio-section-head">LoRAs <span class="muted small">up to 6</span></div>' +
      '<div id="sfx-loras">' + studioLoraRow(planLoras, null) + "</div>" +
      '<button class="btn small" type="button" id="sfx-lora-add" ' +
        'data-tip="Add another LoRA row (max 6)">+ add LoRA</button>' +
      '<div class="studio-field"><label>Prompt</label>' +
        '<textarea id="sfx-prompt" rows="4" spellcheck="false">' + esc(auto.prompt || "") +
        "</textarea></div>" +
      '<div class="studio-field"><label>Negative</label>' +
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
        model: veil.querySelector("#sfx-model").value,
        count: Number(veil.querySelector("#sfx-count").value) || 1,
        loras: loras,
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
    el.innerHTML =
      '<div class="page-slate">' +
        "<h1>Shots</h1>" +
        '<span class="slate-meta">' + shots.length + " shots · " + Object.keys(allScenes).length +
          " scenes · " + passedAll + " passed</span>" +
        '<span class="spacer"></span>' +
        '<div class="slate-acts">' +
          '<button class="btn small" type="button" data-batchrefs data-tip="Generate start images for many shots with one config and assign the sharpest to each">' +
            Icon.get("image") + "Batch refs</button>" +
        "</div>" +
      "</div>" +
      Object.keys(allScenes).sort().map(function (sceneId) {
      const rows = allScenes[sceneId];
      const meta = Object.assign({}, scenes[sceneId] || {},
                                 beatsMap[sceneId] || {});
      const passed = rows.filter(function (r) { return r.status === "passed"; }).length;
      const body = rows.length
        ? '<div class="shot-grid"></div>'
        : '<div class="ws-empty"><div class="ws-empty-mark">' + Icon.get("film") + "</div><div>" +
            'scene ' + esc(sceneId) + " — script not written yet" +
          "</div>" +
          '<div class="ws-empty-hint">Generate it in the Story tab (scene-by-scene panel) or paste this scene\'s AI reply</div></div>';
      return '<section class="scene-block" data-scene="' + esc(sceneId) + '">' +
        '<header class="scene-head">' +
          '<h2 class="scene-title">' + esc(sceneId) + "</h2>" +
          '<span class="scene-meta">' + esc(meta.location || "") +
            (meta.time ? " · " + esc(meta.time) : "") +
            (meta.mood ? " · " + esc(meta.mood) : "") + "</span>" +
          '<span class="spacer"></span>' +
          '<span class="scene-count">' + (rows.length ? passed + "/" + rows.length + " passed" : "no shots") + "</span>" +
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
            toast("scene deleted — " + (r.removed_shots || 0) + " shots removed");
            await State.refreshProject();
            await State.refreshShots();
            Bus.emit("project", State.project);
            render(el);
          } else if (r) {
            toast("delete failed: " + (r.error || "?"), true);
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
      });
    });
    bindTips(el);
  }

  /* extend a built shot card with reference-image + location controls */
  function extendShotCard(card, row) {
    const sid = row.shot_id;
    const j = row.json || {};
    if (row.ref_image) {
      const frame = card.querySelector(".sc-media .sc-frame");
      if (frame) {
        frame.innerHTML =
          '<img loading="lazy" src="' + esc(refUrl(sid, row.ref_image)) + '" alt="reference">' +
          '<div class="sc-frame-tag ref">REF</div>' +
          '<div class="sc-frame-tag start">START</div>';
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
        ": CONTINUE / MATCH_ACTION opens this shot on the previous shot's last frame (H3 frame-0 anchor / " +
        "LTX reference slot); CUT starts fresh. A scene or location change is always a CUT. Changing it requeues the shot." + '">' +
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
        ? '<label class="sc-lvl" data-tip="Power intensity: the renderer passes the reference image whose level is the highest at or below it (floor rule). Blank = story curve.">' +
            "lvl" +
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
        const chips = refs.map(function (r) {
          const src = String(r.source || "?").replace(/^character:/, "")
            .replace(/^location:/, "env:");
          return '<span class="lora-chip refchip" data-tip="' + esc(src) + '">' +
            esc(String(r.slot || "?")) + ": " + esc(src) + "</span>";
        }).join("");
        // the warnings themselves already show on the card (refs: …)
        return chips ? '<div class="sc-refplan" data-tip="References applied to the last render">' + chips + "</div>" : "";
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
      const r = contSel.value === "CONTINUE"
        ? await post(base + "/continue", {}, 30000)
        : await post(base + "/continuity", { continuity: contSel.value }, 30000);
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
      page.subs.push(Bus.on("shots", page.render));
      page.subs.push(Bus.on("project", page.render));
      page.render();
    },
    unmount: function () {
      closeStudio();
      this.subs.forEach(function (off) { off(); });
      this.subs = [];
      if (this.el) this.el.innerHTML = "";
    },
  });
})();
