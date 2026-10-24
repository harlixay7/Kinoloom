/* story.js — five versioned story stages with regenerate / import / rollback. */
(function () {
  "use strict";
  const STAGES = ["treatment", "bible", "beats", "scenes", "shots"];
  let activeStage = "treatment";

  function render(el) {
    const p = State.project;
    if (!p || !State.pid) { el.innerHTML = noProject(); return; }
    const story = p.story || {};
    el.innerHTML =
      '<div class="idea-row" style="display:flex;gap:8px;align-items:flex-start;margin-bottom:8px">' +
        '<label class="muted small" style="min-width:52px;padding-top:6px" ' +
          'data-tip="The premise the Architect prompt carries. The CAST / LOCATIONS / POWERS lists stay authoritative — if the idea names someone who is not in the cast, the AI adapts it.">Idea</label>' +
        '<textarea id="st-idea" class="mono" rows="2" style="flex:1" ' +
          'placeholder="one paragraph — the seed of the movie">' + esc(p.idea || "") + "</textarea>" +
        '<div style="display:flex;flex-direction:column;gap:4px">' +
          '<button class="btn small" type="button" id="st-idea-save" ' +
            'data-tip="Save the premise — the architect prompt uses it immediately">Save</button>' +
          '<button class="btn small" type="button" id="st-idea-rewrite" ' +
            'data-tip="Rewrite the premise from the CURRENT cast with the architect AI — review it, then Save. Fixes premises that still name characters you replaced.">' + Icon.get("wand") + "Rewrite with AI</button>" +
        "</div>" +
      "</div>" +
      '<div class="tabs" id="story-tabs">' +
        STAGES.map(function (s) {
          const filled = story[s] && Object.keys(story[s] || {}).length;
          return '<button class="tab' + (s === activeStage ? " active" : "") + '" data-stage="' + s + '">' +
            s.charAt(0).toUpperCase() + s.slice(1) +
            (filled ? Icon.get("check") : "") + "</button>";
        }).join("") +
      "</div>" +
      '<div class="story-toolbar">' +
        '<button class="btn primary" type="button" id="st-aigen" data-tip="One click: prompts the AI and writes every stage down to shots — continuity-checked">' + Icon.get("wand") + "Generate full script</button>" +
        '<button class="btn" type="button" id="st-aicfg" data-tip="AI endpoint — OpenAI/Claude/Gemini-compatible, or local via Ollama / LM Studio">' + Icon.get("sliders") + "AI config</button>" +
        '<button class="btn" type="button" id="st-aiprompt" data-tip="Copy the Story Architect prompt for any chat AI">' + Icon.get("copy") + "Copy AI prompt</button>" +
        '<button class="btn" type="button" id="st-spine" data-tip="1-hour movies: treatment + bible + beats only">' + Icon.get("copy") + "Spine prompt (1hr)</button>" +
        '<label class="muted small tb-labeled" data-tip="Movie length — scene/shot counts scale from this">Duration (s) <input id="st-dur" type="number" min="10" max="7200" style="width:80px"></label>' +
        '<button class="btn" type="button" id="st-pastepkg" data-tip="Paste the AI\'s JSON package — stages distributed, shots validated and queued">' + Icon.get("import") + "Paste package</button>" +
        '<span class="spacer"></span>' +
        '<button class="btn" type="button" id="st-regen" data-tip="Rewrite the current stage — the old version is kept in history">' + Icon.get("refresh") + "Regenerate stage</button>" +
        '<button class="btn" type="button" id="st-import" data-tip="Import this stage from JSON">' + Icon.get("import") + "Import JSON</button>" +
        '<select id="st-ver" title="version history"><option value="">versions…</option></select>' +
        '<button class="btn small" type="button" id="st-rollback" disabled>Rollback</button>' +
        '<button class="btn small danger" type="button" id="st-ver-del" disabled ' +
          'data-tip="Delete the selected version from history (the live story stays)">Delete version</button>' +
        '<button class="btn danger" type="button" id="st-clear" ' +
          'data-tip="Delete the whole live story (treatment/bible/beats/scenes/shots) and its history + shot rows, so a fresh package can be pasted. The cast is NOT touched.">' + Icon.get("trash") + "Clear story</button>" +
      "</div>" +
      '<div id="st-aigen-box" class="aigen-box hidden"></div>' +
      '<div id="st-scenebox" class="aigen-box hidden"></div>' +
      '<div id="story-body"></div>' +
      '<div class="story-import-modal hidden" id="st-pkg-modal">' +
        '<div class="card">' +
          '<div class="card-head"><h2>Paste full package (the AI\'s whole JSON reply)</h2>' +
            '<button class="btn ghost small" type="button" id="st-pkg-close">' + Icon.get("x") + "</button></div>" +
          '<textarea id="st-pkg-ta" class="mono" rows="16" spellcheck="false" ' +
            'placeholder=\'{"treatment": {…}, "bible": {…}, "beats": {…}, "shots": {"shots": [ … ]}}\'></textarea>' +
          '<div class="ctl-row">' +
            '<button class="btn primary" type="button" id="st-pkg-go">Import package</button>' +
            '<span class="muted small">or import from a file:</span>' +
            '<input id="st-pkg-file" class="mono" style="flex:1" placeholder="story_package.txt">' +
            '<button class="btn" type="button" id="st-pkg-file-go" data-tip="Read the AI reply straight from a file on disk — avoids clipboard truncation">Import file</button>' +
          "</div>" +
        "</div>" +
      "</div>" +
      '<div class="story-import-modal hidden" id="st-import-modal">' +
        '<div class="card">' +
          '<div class="card-head"><h2>Import JSON — <span id="st-import-stage"></span></h2>' +
            '<button class="btn ghost small" type="button" id="st-import-close">' + Icon.get("x") + "</button></div>" +
          '<textarea id="st-import-ta" class="mono" rows="16" spellcheck="false" ' +
            'placeholder=\'{"logline": "…", …}\'></textarea>' +
          '<div class="ctl-row"><button class="btn primary" type="button" id="st-import-go">Import</button></div>' +
        "</div>" +
      "</div>";

    el.querySelectorAll("#story-tabs .tab").forEach(function (t) {
      t.onclick = function () { activeStage = t.dataset.stage; renderStage(el); refreshTabs(el); };
    });
    el.querySelector("#st-regen").onclick = function () { regenerate(activeStage); };
    // Idea editor: the premise is carried by the Architect prompt, so it must
    // be editable AFTER project creation (it used to be frozen at creation —
    // a cast change left the old premise referencing the old characters).
    const ideaTa = el.querySelector("#st-idea");
    const ideaSave = el.querySelector("#st-idea-save");
    if (ideaSave) ideaSave.onclick = async function () {
      const r = await post("/api/projects/" + encodeURIComponent(State.pid),
        { idea: ideaTa.value }, 30000);
      toast(r && r.ok ? "idea saved — the architect prompt uses it now"
                      : "save failed", !(r && r.ok));
      if (r && r.ok && State.refreshProject) State.refreshProject();
    };
    const ideaRe = el.querySelector("#st-idea-rewrite");
    if (ideaRe) ideaRe.onclick = async function () {
      toast("rewriting the premise from the current cast…");
      const r = await post("/api/projects/" + encodeURIComponent(State.pid) +
        "/story/rewrite-idea", {}, 120000);
      if (!r || !r.ok) return toast((r && r.error) || "rewrite failed", true);
      ideaTa.value = r.idea;
      toast("premise rewritten — review it, then press Save");
    };
    // Clear the whole story (a wrong generation must be removable before a
    // new package is pasted — the old UI only offered rollback).
    const clearBtn = el.querySelector("#st-clear");
    if (clearBtn) clearBtn.onclick = function () {
      Dlg.confirm({
        title: "Clear story",
        message: "Delete the live story (treatment, bible, beats, scenes, shots), "
          + "its version history and the shot rows? The CAST (characters/locations) "
          + "is not touched. This cannot be undone.",
        okLabel: "Clear story",
        danger: true,
      }).then(async function (yes) {
        if (!yes) return;
        const r = await post("/api/projects/" + encodeURIComponent(State.pid) +
          "/story/clear", { also_shots: true, keep_history: false }, 120000);
        if (r && r.ok) {
          toast("story cleared — paste a new package or generate again");
          await State.refreshProject();
          await State.refreshShots();
          renderStage(el);
          refreshTabs(el);
        } else {
          toast((r && r.error) || "clear failed", true);
        }
      });
    };
    const verDel = el.querySelector("#st-ver-del");
    if (verDel) verDel.onclick = async function () {
      const sel = el.querySelector("#st-ver");
      if (!sel || !sel.value) { toast("pick a version first", true); return; }
      const v = parseInt(sel.value, 10);
      const r = await post("/api/projects/" + encodeURIComponent(State.pid) +
        "/story/delete-version", { stage: activeStage, version: v }, 30000);
      if (r && r.ok) {
        toast("version " + v + " deleted from history");
        refreshTabs(el);
      } else {
        toast((r && r.error) || "delete failed", true);
      }
    };
    el.querySelector("#st-aiprompt").onclick = function () { copyAiPrompt("full"); };
    el.querySelector("#st-spine").onclick = function () { copyAiPrompt("spine"); };
    el.querySelector("#st-aigen").onclick = function () { startAiGen(el); };
    el.querySelector("#st-aicfg").onclick = function () { openAiConfig(el); };
    const durIn = el.querySelector("#st-dur");
    if (durIn) {
      durIn.value = (State.project && State.project.duration_target) || 60;
      durIn.onchange = async function () {
        const v = Number(durIn.value) || 60;
        const r = await post("/api/projects/" + encodeURIComponent(State.pid),
          { duration_target: Math.min(7200, Math.max(10, v)) }, 30000);
        if (r && r.ok) {
          toast("duration → " + Math.min(7200, Math.max(10, v)) + "s — "
            + "copy the spine prompt again for the new scene count");
          await State.refreshProject();
        }
      };
    }
    renderSceneBox(el);
    el.querySelector("#st-pastepkg").onclick = function () {
      el.querySelector("#st-pkg-ta").value = "";
      el.querySelector("#st-pkg-modal").classList.remove("hidden");
    };
    el.querySelector("#st-pkg-close").onclick = function () {
      el.querySelector("#st-pkg-modal").classList.add("hidden");
    };
    el.querySelector("#st-pkg-go").onclick = function () { doPastePackage(el); };
    el.querySelector("#st-pkg-file-go").onclick = async function () {
      const path = el.querySelector("#st-pkg-file").value.trim();
      if (!path) { toast("enter a file path", true); return; }
      toast("importing from file…");
      const r = await post("/api/projects/" + encodeURIComponent(State.pid) +
                           "/story/import-file", { path: path }, 300000);
      if (!r) return;
      if (r.ok) toast("package imported from file");
      else toast("import failed: " + (r.error || "?"), true);
      el.querySelector("#st-pkg-modal").classList.add("hidden");
      await State.refreshProject();
      await State.refreshShots();
      Bus.emit("project", State.project);
      render(el);
    };
    el.querySelector("#st-import").onclick = function () {
      el.querySelector("#st-import-stage").textContent = activeStage;
      el.querySelector("#st-import-ta").value = "";
      el.querySelector("#st-import-modal").classList.remove("hidden");
    };
    el.querySelector("#st-import-close").onclick = function () {
      el.querySelector("#st-import-modal").classList.add("hidden");
    };
    el.querySelector("#st-import-go").onclick = function () { doImport(el); };
    el.querySelector("#st-rollback").onclick = function () { rollback(el); };
    renderStage(el);
    loadVersions(el);
  }

  function refreshTabs(el) {
    el.querySelectorAll("#story-tabs .tab").forEach(function (t) {
      t.classList.toggle("active", t.dataset.stage === activeStage);
    });
  }

  let aiPollTimer = null;

  function renderSceneBox(el) {
    const box = el.querySelector("#st-scenebox");
    if (!box) return;
    const story = (State.project && State.project.story) || {};
    const beats = (story.beats && story.beats.beats) || [];
    if (!beats.length) { box.classList.add("hidden"); return; }
    /* shots per scene -> "written" markers */
    const written = {};
    (State.shots || []).forEach(function (s) {
      written[s.scene] = (written[s.scene] || 0) + 1;
    });
    box.classList.remove("hidden");
    box.innerHTML =
      '<span class="aigen-status">Long film — scene by scene</span>' +
      '<span class="muted small">copy one scene prompt, paste the reply ' +
      'back; repeat. Each reply can cover 2-4 scenes (compact format).</span>';
    const wrap = document.createElement("div");
    wrap.className = "scene-rows";
    beats.forEach(function (b) {
      const row = document.createElement("div");
      row.className = "scene-row";
      const n = written[b.scene] || 0;
      const badge = n
        ? '<span class="badge st-green" data-tip="This scene\'s shots are in the queue">' +
          Icon.get("check") + " written · " + n + " shot" + (n === 1 ? "" : "s") + "</span>"
        : '<span class="badge st-gray" data-tip="Paste this scene\'s AI reply to fill it">not written</span>';
      row.innerHTML =
        '<span class="sr-id">' + esc(b.scene) + "</span>" +
        badge +
        '<span class="muted small">' + esc(b.location || "") + " · " +
          esc(b.time || "") + " · " + esc(String(b.est_shots || "")) +
          " shots</span>" +
        '<span class="spacer"></span>' +
        '<button class="btn small" type="button" data-cp="' + esc(b.scene) +
          '" data-tip="Copy this scene\'s prompt (running continuity included)">' + Icon.get("copy") + "Copy</button>" +
        '<button class="btn small" type="button" data-ps="' + esc(b.scene) +
          '" data-tip="Paste the AI reply for this scene">' + Icon.get("import") + "Paste</button>";
      wrap.appendChild(row);
    });
    box.appendChild(wrap);
    wrap.querySelectorAll("[data-cp]").forEach(function (btn) {
      btn.onclick = async function () {
        const scene = btn.dataset.cp;
        toast("building scene prompt…");
        const r = await get("/api/projects/" + encodeURIComponent(State.pid) +
          "/story/scene-prompt?scene=" + encodeURIComponent(scene), 30000);
        if (!r || !r.prompt) { toast("scene prompt unavailable", true); return; }
        const v = openModal('<textarea class="mono" rows="20" style="width:100%" ' +
          'id="sp-ta" spellcheck="false">' + esc(r.prompt) + "</textarea>",
          { title: "Scene prompt — copy it into the SAME chat", wide: true });
        if (v) {
          const ta = v.querySelector("#sp-ta");
          ta.onfocus = function () { ta.select(); };
        }
      };
    });
    wrap.querySelectorAll("[data-ps]").forEach(function (btn) {
      btn.onclick = function () {
        const scene = btn.dataset.ps;
        const v = openModal(
          '<textarea id="ps-ta" class="mono" rows="16" spellcheck="false" ' +
          'placeholder=\'{"shots": {"shots": [{"scene": "' + esc(scene) +
          '", "location": "...", "time": "...", "shots": [...]}]}}\'></textarea>' +
          '<div class="ctl-row"><button class="btn primary" type="button" ' +
          'id="ps-go">Import scene shots</button></div>',
          { title: "Paste scene " + scene + " reply", wide: true });
        if (!v) return;
        v.querySelector("#ps-go").onclick = async function () {
          const rawText = v.querySelector("#ps-ta").value;
          let data = null;
          try { data = JSON.parse(rawText); } catch (e) { data = null; }
          let shots = null;
          if (data && data.shots && Array.isArray(data.shots)) shots = data.shots;
          else if (data && data.shots && data.shots.shots) shots = data.shots.shots;
          else if (Array.isArray(data)) shots = data;
          const r = await post("/api/projects/" + encodeURIComponent(State.pid) +
            "/story/import-scene-shots",
            shots ? { scene: scene, shots: shots }
                  : { scene: scene, text: rawText }, 120000);
          if (!r) return;
          if (r.ok) toast(scene + ": " + r.added + " shots imported");
          else toast("import rejected: " + (r.error || "?"), true);
          closeModal(v);
          await State.refreshProject();
          await State.refreshShots();
          Bus.emit("project", State.project);
          render(el);
        };
      };
    });
    bindTips(el);
  }

  function aiBox(el) { return el.querySelector("#st-aigen-box"); }

  async function openAiConfig(el) {
    let cfg = null;
    try {
      cfg = await get("/api/projects/" + encodeURIComponent(State.pid) +
                      "/story/ai-config", 20000);
    } catch (e) { /* degrade */ }
    const a = (cfg && cfg.architect) || {};
    const v = openModal(
      '<p class="muted small">OpenAI-compatible endpoint. Local options: ' +
      'Ollama <code>http://127.0.0.1:11434/v1</code> (key "ollama") or LM Studio ' +
      '<code>http://127.0.0.1:1234/v1</code>. Cloud: OpenAI/DeepSeek/Gemini-compat.</p>' +
      '<label class="np-label">Base URL <input id="ai-base" value="' +
        esc(a.base_url || "http://127.0.0.1:11434/v1") + '"></label>' +
      '<label class="np-label">API key <input id="ai-key" value="' +
        esc(a.api_key || "ollama") + '" autocomplete="off"></label>' +
      '<label class="np-label">Model <input id="ai-model" value="' +
        esc(a.model || "llama3.1:70b") + '"></label>' +
      '<div class="ctl-row"><button class="btn" type="button" id="ai-test">Test connection</button>' +
      '<button class="btn primary" type="button" id="ai-save">Save</button></div>',
      { title: "Story Architect AI config", wide: true });
    if (!v) return;
    v.querySelector("#ai-test").onclick = async function () {
      toast("testing…");
      const r = await post("/api/architect/test", {}, 60000);
      if (r) toast(r.ok ? "AI reachable: " + (r.reply || "OK") : (r.error || "failed"), !r.ok);
    };
    v.querySelector("#ai-save").onclick = async function () {
      const r = await post("/api/projects/" + encodeURIComponent(State.pid) +
        "/story/ai-config", {
          base_url: v.querySelector("#ai-base").value.trim(),
          api_key: v.querySelector("#ai-key").value.trim(),
          model: v.querySelector("#ai-model").value.trim(),
        }, 30000);
      if (r && r.ok) { toast("AI config saved"); closeModal(v); }
      else if (r) toast("save failed: " + (r.error || "?"), true);
    };
  }

  async function startAiGen(el) {
    const box = aiBox(el);
    box.classList.remove("hidden");
    box.innerHTML = '<span class="aigen-status">starting…</span>';
    const r = await post("/api/projects/" + encodeURIComponent(State.pid) +
                         "/story/generate-ai", {}, 60000);
    if (!r) { box.classList.add("hidden"); return; }
    if (!r.ok) {
      box.innerHTML = '<span class="aigen-status err">' +
        esc(r.error || "failed") + "</span>";
      if ((r.error || "").indexOf("not configured") >= 0) openAiConfig(el);
      return;
    }
    clearInterval(aiPollTimer);
    aiPollTimer = setInterval(async function () {
      const st = await get("/api/projects/" + encodeURIComponent(State.pid) +
                           "/story/generate-ai/status", 20000);
      const a = st && st.active;
      if (!a) return;
      const box2 = aiBox(el);
      if (!box2) { clearInterval(aiPollTimer); return; }
      if (!a.running) {
        clearInterval(aiPollTimer);
        box2.innerHTML = '<span class="aigen-status ' +
          ((a.message || "").indexOf("failed") >= 0 ? "err" : "ok") + '">' +
          esc(a.message || "complete") + "</span>" +
          ' <button class="btn small" type="button" id="st-aistop2" style="display:none"></button>';
        await State.refreshProject();
        await State.refreshShots();
        Bus.emit("project", State.project);
        render(el);
        return;
      }
      box2.innerHTML =
        '<span class="aigen-status">' + esc(a.stage || "working") + "</span>" +
        '<span class="aigen-bar"><span class="aigen-fill" style="width:' +
          (a.total ? Math.round((a.done / a.total) * 100) : 5) + '%"></span></span>' +
        '<span class="muted small">' + (a.done || 0) + "/" + (a.total || "?") +
        " scenes · " + esc(a.message || "") + "</span>" +
        ' <button class="btn small warn" type="button" id="st-aistop">' + Icon.get("stop") + "Stop</button>";
      const sb = box2.querySelector("#st-aistop");
      if (sb) sb.onclick = async function () {
        await post("/api/projects/" + encodeURIComponent(State.pid) +
                   "/story/generate-ai/stop", {}, 30000);
        toast("stopping after the current AI call…");
      };
    }, 3000);
  }

  async function copyAiPrompt(kind) {
    toast("building the architect prompt…");
    const q = kind === "spine" ? "?kind=spine" : "";
    const r = await get("/api/projects/" + encodeURIComponent(State.pid) +
                         "/story/architect-prompt" + q, 30000);
    if (!r || !r.prompt) { toast("prompt unavailable", true); return; }
    // stale-premise warning: names the idea mentions that are not in the
    // cast — surfaced with the copy confirmation so it is never missed
    const staleNames = (r.idea_warnings || []).filter(Boolean);
    const warnTxt = staleNames.length
      ? " — note: the IDEA mentions " + staleNames.join(", ") +
        " (not in the cast; the AI is told to adapt, or press Rewrite with AI)"
      : "";
    if (r.shot_count && !staleNames.length) {
      toast((kind === "spine" ? "spine prompt copied — " + r.shot_count + " shots over "
             + r.scene_count + " scenes" : "prompt copied — " + r.shot_count + " shots"));
    }
    function copy(text) {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () {
          if (staleNames.length) {
            toast("prompt copied" + warnTxt, true);
          } else {
            toast("prompt copied — paste it into any chat AI");
          }
        }).catch(function () { showCopyModal(text); });
      } else { showCopyModal(text); }
    }
    function showCopyModal(text) {
      const v = openModal('<textarea class="mono" rows="20" style="width:100%" id="cp-ta" spellcheck="false">' +
        esc(text) + "</textarea>", { title: "Architect prompt — copy it", wide: true });
      if (v) {
        const ta = v.querySelector("#cp-ta");
        ta.onfocus = function () { ta.select(); };
      }
    }
    copy(r.prompt);
  }

  async function doPastePackage(el) {
    const rawText = el.querySelector("#st-pkg-ta").value;
    let pkg;
    try { pkg = JSON.parse(rawText); }
    catch (e) { pkg = null; }
    const payload = pkg !== null ? { package: pkg } : { text: rawText };
    toast("importing package — validating shots…");
    const r = await post("/api/projects/" + encodeURIComponent(State.pid) +
                         "/story/import-all", payload, 300000);
    if (!r) return;
    if (r.ok) {
      const n = r.stages && r.stages.shots && r.stages.shots.ok;
      toast("package imported" + (n ? " — shots queued" : ""));
    } else {
      const bad = (r.stages && Object.keys(r.stages)
        .filter(function (k) { return !r.stages[k].ok; }).join(", ")) || "";
      toast("import failed: " + (r.error || "issues in: " + (bad || "?")),
            true);
    }
    el.querySelector("#st-pkg-modal").classList.add("hidden");
    await State.refreshProject();
    await State.refreshShots();
    Bus.emit("project", State.project);
    render(el);
  }

  async function regenerate(stage) {
    const okGo = await Dlg.confirm({
      title: "Regenerate stage",
      message: "Regenerate the <b>" + esc(stage) + "</b> stage? Existing content is kept in version history.",
      okLabel: "Regenerate",
    });
    if (!okGo) return;
    toast("generating " + stage + " — this can take minutes…");
    const r = await post("/api/projects/" + encodeURIComponent(State.pid) + "/story/generate",
      { stages: [stage] }, 900000);
    if (!r) return;
    if (r.ok) {
      toast(stage + " done" + (r.shots_ingested ? " — " + r.shots_ingested + " shots ingested" : ""));
    } else {
      toast("generation failed: " + (r.error || "?"), true);
    }
    await State.refreshProject();
    await State.refreshShots();
    Bus.emit("project", State.project);
    const el = document.getElementById("page-story");
    if (el && el.classList.contains("active")) render(el);
  }

  async function doImport(el) {
    const rawText = el.querySelector("#st-import-ta").value;
    let content = null;
    try { content = JSON.parse(rawText); } catch (e) { content = null; }
    // SMART ROUTING: a FULL package (any of treatment/bible/beats/scenes/
    // shots keys) is distributed across stages via import-all. Importing it
    // into the active stage alone was why a pasted spine never produced its
    // treatment/beats ("import JSON" on the Treatment tab swallowed it).
    const isPackage = content && typeof content === "object" &&
      ["treatment", "bible", "beats", "scenes", "shots"].some(function (k) {
        return content[k] !== undefined;
      });
    const r = isPackage
      ? await post("/api/projects/" + encodeURIComponent(State.pid) + "/story/import-all",
                   { package: content }, 300000)
      : await post("/api/projects/" + encodeURIComponent(State.pid) + "/story/import",
                   content !== null ? { stage: activeStage, content: content }
                                    : { stage: activeStage, text: rawText }, 120000);
    if (!r) return;
    if (r.ok) {
      if (isPackage) {
        const done = Object.keys(r.stages || {}).filter(function (k) {
          return (r.stages[k] || {}).ok;
        });
        toast("package imported — stages: " + (done.join(", ") || "none"));
      } else {
        toast(activeStage + " imported");
      }
    } else {
      const firstErr = (function () {
        const st = r.stages || {};
        for (const k of Object.keys(st)) {
          if (st[k] && st[k].ok === false) return k + ": " + (st[k].error || "?");
        }
        return r.error || "?";
      })();
      toast("import failed: " + firstErr, true);
    }
    el.querySelector("#st-import-modal").classList.add("hidden");
    await State.refreshProject();
    Bus.emit("project", State.project);
    render(el);
  }

  async function loadVersions(el) {
    const sel = el.querySelector("#st-ver");
    const r = await get("/api/projects/" + encodeURIComponent(State.pid) +
      "/story/versions?stage=" + encodeURIComponent(activeStage), 20000);
    const versions = (r && r.versions) || [];
    sel.innerHTML = '<option value="">versions…</option>' + versions.map(function (v) {
      return '<option value="' + esc(v.version) + '">v' + esc(v.version) + " · " +
        esc(fmt.ts(v.created)) + "</option>";
    }).join("");
    // enable Rollback + Delete-version only with a selection
    const rb = el.querySelector("#st-rollback");
    const vd = el.querySelector("#st-ver-del");
    const sync = function () {
      const has = !!sel.value;
      if (rb) rb.disabled = !has;
      if (vd) vd.disabled = !has;
    };
    sel.onchange = sync;
    sync();
  }

  async function rollback(el) {
    const sel = el.querySelector("#st-ver");
    if (!sel.value) { toast("pick a version first", true); return; }
    const r = await post("/api/projects/" + encodeURIComponent(State.pid) + "/story/rollback",
      { stage: activeStage, version: Number(sel.value) }, 30000);
    if (r) toast(r.ok ? "rolled back to v" + sel.value : "rollback failed", !r.ok);
    await State.refreshProject();
    Bus.emit("project", State.project);
    render(el);
  }

  /* ------------------------------------------------------------- stage view */
  function renderStage(el) {
    const story = (State.project && State.project.story) || {};
    const c = story[activeStage];
    const body = el.querySelector("#story-body");
    if (!c || !Object.keys(c || {}).length) {
      body.innerHTML = empty("No " + activeStage + " yet",
        "Hit “Regenerate this stage” — or import your own JSON.");
      return;
    }
    if (activeStage === "treatment") body.innerHTML = renderTreatment(c);
    else if (activeStage === "bible") body.innerHTML = renderBible(c);
    else if (activeStage === "beats") body.innerHTML = renderBeats(c);
    else if (activeStage === "scenes") body.innerHTML = renderScenes(c);
    else if (activeStage === "shots") body.innerHTML = renderShots(c);
    loadVersions(el);
  }

  function renderTreatment(c) {
    const themes = c.themes || [];
    return '<section class="treat-grid">' +
      '<div class="card treat-logline"><div class="card-head"><h2>Logline</h2></div>' +
        '<p class="treat-big">' + esc(c.logline || "") + "</p></div>" +
      '<div class="card"><div class="card-head"><h2>Synopsis</h2></div>' +
        '<p>' + esc(c.synopsis || "") + "</p></div>" +
      '<div class="card"><div class="card-head"><h2>Themes</h2></div>' +
        '<div class="chips">' + themes.map(function (t) {
          return '<span class="chip">' + esc(t) + "</span>";
        }).join("") + "</div></div>" +
      '<div class="card"><div class="card-head"><h2>Characters</h2></div>' +
        '<div class="treat-two"><div><span class="muted small">Protagonist</span><div class="treat-char">' +
          esc(c.protagonist || "") + "</div></div>" +
        '<div><span class="muted small">Antagonist</span><div class="treat-char">' +
          esc(c.antagonist || "") + "</div></div></div></div>" +
      '<div class="card"><div class="card-head"><h2>Escalation</h2></div><ol class="ov-escalation">' +
        (c.escalation || []).map(function (x) { return "<li>" + esc(x) + "</li>"; }).join("") +
        "</ol></div>" +
      '<div class="card"><div class="card-head"><h2>Ending &amp; pacing</h2></div>' +
        '<p>' + esc(c.ending || "") + "</p>" +
        '<div class="muted small">pacing: ' + esc(c.pacing || "") + "</div></div>" +
      '<div class="card"><div class="card-head"><h2>Clipable moments</h2></div><ul class="ulist">' +
        (c.clipable_moments || []).map(function (x) { return "<li>" + esc(x) + "</li>"; }).join("") +
        "</ul></div>" +
    "</section>";
  }

  function renderBible(c) {
    const chars = asList(c.characters);
    const locs = asList(c.locations);
    const crowd = c.crowd_types || {};
    const uni = c.universe || {};
    return '<section class="card"><div class="card-head"><h2>Characters</h2></div>' +
      '<div class="char-grid">' + chars.map(function (ch) {
        const powers = ch.powers || {};
        return '<div class="char-card">' +
          '<div class="char-head"><span class="char-name">' + esc(ch.name || ch.id || "?") + "</span>" +
          '<span class="badge tier-' + esc(ch.tier || "supporting") + '">' + esc(ch.tier || "") + "</span></div>" +
          '<div class="char-body">' + esc(ch.body || "") + "</div>" +
          '<div class="char-outfit">' + esc(ch.outfit || "") + "</div>" +
          '<div class="char-powers">' + Object.keys(powers).map(function (pn) {
            const pd = powers[pn] || {};
            return '<div class="power-row"><span class="swatch" style="background:' +
              esc(pd.color || "#e8b04b") + '"></span><b>' + esc(pn) + "</b>" +
              '<div class="muted small">' + esc(pd.descriptor || pd.motion || "") + "</div></div>";
          }).join("") + "</div>" +
          (ch.emotion_palette
            ? '<div class="chips">' + ch.emotion_palette.map(function (em) {
                return '<span class="chip emo">' + esc(em) + "</span>";
              }).join("") + "</div>"
            : "") +
          (ch.voice
            ? '<div class="muted small">voice: ' + esc(ch.voice.engine || "") + " · " +
              esc(ch.voice.style_tag || "") + "</div>" : "") +
          (ch.arc_summary ? '<div class="muted small">' + esc(ch.arc_summary) + "</div>" : "") +
        "</div>";
      }).join("") + "</div></section>" +
      '<section class="grid two">' +
        '<div class="card"><div class="card-head"><h2>Locations</h2></div><div class="loc-list">' +
          (locs.length ? locs.map(function (l) {
            return '<div class="loc-card"><b>' + esc(l.id || l.name || "?") + "</b>" +
              '<div class="muted small">' + esc(l.descriptor || "") + "</div>" +
              (l.lighting ? '<div class="muted small">lighting: ' + esc(l.lighting) + "</div>" : "") +
            "</div>";
          }).join("") : '<div class="muted small">none</div>') +
        "</div></div>" +
        '<div class="card"><div class="card-head"><h2>World</h2></div>' +
          '<div class="chips">' + Object.keys(crowd).map(function (k) {
            return '<span class="chip">' + esc(k) + "</span>";
          }).join("") + "</div>" +
          '<div class="muted small" style="margin-top:10px">genre ' + esc(uni.genre || "—") +
            " · tone " + esc(uni.tone || "—") + " · grade " + esc(uni.grade || "—") + "</div>" +
          (uni.cinematography
            ? '<div class="muted small">' + Object.keys(uni.cinematography).map(function (k) {
                return esc(k) + ": " + esc(uni.cinematography[k]);
              }).join(" · ") + "</div>" : "") +
        "</div>" +
      "</section>";
  }

  function renderBeats(c) {
    const beats = c.beats || [];
    return '<section class="card"><div class="card-head"><h2>Beats</h2></div>' +
      '<div class="beat-list">' + beats.map(function (b, i) {
        return '<div class="beat-row">' +
          '<span class="beat-num">' + (i + 1) + "</span>" +
          '<div class="beat-main"><b>' + esc(b.scene || "") + "</b>" +
          '<span class="muted small"> · ' + esc(b.location || "") + " · " + esc(b.time || "") +
            " · mood " + esc(b.mood || "") + " · ~" + esc(b.est_shots || 0) + " shots</span>" +
          '<div class="muted">goal: ' + esc(b.goal || "") + "</div>" +
          '<div class="muted">turn: ' + esc(b.turn || "") + "</div>" +
          (b.hook ? '<div class="muted small">hook: ' + esc(b.hook) + "</div>" : "") +
          "</div></div>";
      }).join("") + "</div></section>";
  }

  function renderScenes(c) {
    const scenes = c.scenes || [];
    return '<section class="card"><div class="card-head"><h2>Scenes</h2></div>' +
      '<div class="scene-grid">' + scenes.map(function (s) {
        return '<div class="scene-card"><div class="scene-id">' + esc(s.id || "?") + "</div>" +
          '<div class="muted small">' + esc(s.location || "") + " · " + esc(s.time || "") + "</div>" +
          '<div class="muted small">arc: ' + esc(s.emotion_arc || "") + "</div>" +
          '<div class="muted small">music: ' + esc(s.music_mood || "") + " · " +
            esc(s.shot_count || 0) + " shots</div>" +
          '<div class="chips">' + (s.beats || []).map(function (b) {
            return '<span class="chip">' + esc(b) + "</span>";
          }).join("") + "</div></div>";
      }).join("") + "</div></section>";
  }

  function renderShots(c) {
    const shots = c.shots || [];
    return '<section class="card"><div class="card-head"><h2>Shot plan — ' +
      shots.length + " shots</h2></div>" +
      '<div class="shot-plan">' + shots.map(function (s) {
        return '<div class="plan-row">' +
          '<span class="plan-id">' + esc(s.shot_id || "?") + "</span>" +
          '<span class="badge ' + tierClass(s.tier) + '">' + esc(s.tier || "") + "</span>" +
          '<span class="muted small">' + esc(s.camera || "") + " · " + esc(s.shot_size || "") + "</span>" +
          '<span class="plan-act">' + esc(s.primary_action || "") + "</span>" +
        "</div>";
      }).join("") + "</div></section>";
  }

  Router.register("story", {
    polls: ["status"],
    subs: [],
    mount: function (el) {
      const page = this;
      page.el = el;
      page.render = function () { render(el); };
      page.subs.push(Bus.on("project", page.render));
      page.render();
    },
    unmount: function () {
      this.subs.forEach(function (off) { off(); });
      this.subs = [];
      if (aiPollTimer) { clearInterval(aiPollTimer); aiPollTimer = null; }
      if (this.el) this.el.innerHTML = "";
    },
  });
})();
