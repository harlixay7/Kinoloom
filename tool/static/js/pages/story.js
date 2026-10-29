/* story.js — the idea, the two ways to write the story (the AI writer, or
   any chat AI by copy and paste), and the five versioned parts of the story
   with regenerate / import / rollback per part. */
(function () {
  "use strict";
  const STAGES = ["treatment", "bible", "beats", "scenes", "shots"];
  const STAGE_LABEL = { treatment: "Treatment", bible: "Story bible", beats: "Beats", scenes: "Scenes", shots: "Shot plan" };
  const STAGE_HINT = {
    treatment: "The logline, the synopsis, the themes and the ending.",
    bible: "Who and where: the characters, places and world rules the story uses. They merge into Cast & World, and your cast there wins.",
    beats: "The story scene by scene: where, when, the goal and the turn.",
    scenes: "Each scene's mood, music and shot count.",
    shots: "Every shot: its action, camera and size. These become the Shots.",
  };
  let activeStage = "treatment";

  function render(el) {
    const p = State.project;
    if (!p || !State.pid) { el.innerHTML = noProject(); return; }
    const story = p.story || {};
    el.innerHTML =
      '<header class="page-intro">' +
        '<div class="pi-text"><h1>Story</h1>' +
          "<p>Your idea, written out as a script: the treatment, the story bible, the beats, the scenes and " +
          "the shot plan. It uses your cast, and invents one if Cast &amp; World is empty.</p></div>" +
        '<div class="pi-acts">' +
          (STAGES.some(function (s) { return story[s] && Object.keys(story[s]).length; })
            ? '<button class="btn danger quiet" type="button" id="st-clear" ' +
                'data-tip="Delete the whole story (all five parts), its history and the shots, so you can write it again. The cast is not touched.">' +
                Icon.get("trash") + "Clear story</button>"
            : "") +
        "</div>" +
      "</header>" +
      '<section class="story-top">' +
        '<div class="idea-card">' +
          '<label class="f-label" for="st-idea" data-tip="The premise the story writer gets. Your cast stays in charge: if the idea names someone who is not in the cast, the AI adapts it.">The idea</label>' +
          '<textarea id="st-idea" class="script" rows="4" ' +
            'placeholder="One paragraph: who wants what, what stands in the way, how it ends.">' + esc(p.idea || "") + "</textarea>" +
          '<div class="idea-row">' +
            '<button class="btn small" type="button" id="st-idea-save" ' +
              'data-tip="Save the idea: the story writer uses it from now on">Save idea</button>' +
            '<button class="btn small ghost" type="button" id="st-idea-rewrite" ' +
              'data-tip="Rewrite the idea around your current cast with the AI writer. Review it, then Save. Useful when it still names characters you replaced.">' +
              Icon.get("wand") + "Rewrite from the cast</button>" +
            '<span class="spacer"></span>' +
            '<label class="idea-len" data-tip="The film&apos;s length: the number of scenes and shots follows it">Length ' +
              '<input id="st-dur" type="number" min="10" max="7200"> seconds</label>' +
          "</div>" +
        "</div>" +
        '<div class="write-paths">' +
          '<div class="wpath">' +
            '<div class="wpath-h">' + Icon.get("spark") + "<h3>With the AI writer</h3></div>" +
            "<p>Writes all five parts down to the shot list, scene by scene, and checks each scene against the story bible.</p>" +
            '<div class="wpath-acts">' +
              '<button class="btn primary" type="button" id="st-aigen">' + Icon.get("wand") + "Write the story</button>" +
              '<button class="btn ghost" type="button" id="st-aicfg">' + Icon.get("sliders") + "AI writer</button>" +
            "</div>" +
          "</div>" +
          '<div class="wpath">' +
            '<div class="wpath-h">' + Icon.get("copy") + "<h3>With any chat AI</h3></div>" +
            '<ol class="wpath-steps"><li>Copy the prompt.</li><li>Paste it into a chat AI.</li><li>Paste its whole reply back here.</li></ol>' +
            '<div class="wpath-acts">' +
              '<button class="btn" type="button" id="st-aiprompt" data-tip="Copy the story prompt for any chat AI">' + Icon.get("copy") + "Copy prompt</button>" +
              '<button class="btn" type="button" id="st-pastepkg" data-tip="Paste the AI&apos;s reply: the parts are filled in and the shots checked and queued">' + Icon.get("import") + "Paste reply</button>" +
            "</div>" +
            '<button class="linkish" type="button" id="st-spine" data-tip="For a long film (an hour): the treatment, story bible and beats only. You then write the shots scene by scene.">' +
              "Long film? Copy the outline prompt instead</button>" +
          "</div>" +
        "</div>" +
      "</section>" +
      '<div id="st-aigen-box" class="aigen-box hidden"></div>' +
      '<div id="st-scenebox" class="aigen-box hidden"></div>' +
      '<div class="story-bar">' +
        '<div class="tabs" id="story-tabs" role="tablist">' +
          STAGES.map(function (s) {
            const filled = story[s] && Object.keys(story[s] || {}).length;
            return '<button class="tab' + (s === activeStage ? " active" : "") + (filled ? " filled" : "") +
              '" type="button" role="tab" data-stage="' + s + '">' +
              '<span class="tab-dot"></span>' + STAGE_LABEL[s] + "</button>";
          }).join("") +
        "</div>" +
        '<div class="story-tools">' +
          '<button class="btn small ghost" type="button" id="st-regen" data-tip="Write this part again with the AI writer. The current version stays in its history.">' + Icon.get("refresh") + "Rewrite part</button>" +
          '<button class="btn small ghost" type="button" id="st-import" data-tip="Import this part from JSON">' + Icon.get("import") + "Import</button>" +
          '<select id="st-ver" title="This part&apos;s earlier versions" class="small-sel"><option value="">History</option></select>' +
          '<button class="btn small" type="button" id="st-rollback" disabled data-tip="Go back to the selected version">Restore</button>' +
          '<button class="btn small ghost icon" type="button" id="st-ver-del" disabled ' +
            'data-tip="Delete the selected version from the history (the current story stays)">' + Icon.get("trash") + "</button>" +
        "</div>" +
      "</div>" +
      '<p class="stage-hint" id="st-stage-hint">' + esc(STAGE_HINT[activeStage]) + "</p>" +
      '<div id="story-body"></div>' +
      '<div class="story-import-modal hidden" id="st-pkg-modal">' +
        '<div class="card">' +
          '<div class="card-head"><h2>Paste the chat AI&apos;s reply</h2><span class="spacer"></span>' +
            '<button class="btn ghost small icon" type="button" id="st-pkg-close">' + Icon.get("x") + "</button></div>" +
          '<p class="sec-sub">Its whole reply, as it came. The parts it contains are filled in and its shots checked and queued.</p>' +
          '<textarea id="st-pkg-ta" class="mono" rows="14" spellcheck="false" ' +
            'placeholder=\'{"treatment": {…}, "bible": {…}, "beats": {…}, "shots": {"shots": [ … ]}}\'></textarea>' +
          '<div class="ctl-row" style="margin-top:12px">' +
            '<button class="btn primary" type="button" id="st-pkg-go">Import reply</button>' +
            '<span class="spacer"></span>' +
            '<input id="st-pkg-file" class="mono" style="flex:0 1 320px" placeholder="or a file on disk: C:\\…\\story_package.txt">' +
            '<button class="btn" type="button" id="st-pkg-file-go" data-tip="Read the reply from a file on disk: avoids a clipboard that cut it short">Import file</button>' +
          "</div>" +
        "</div>" +
      "</div>" +
      '<div class="story-import-modal hidden" id="st-import-modal">' +
        '<div class="card">' +
          '<div class="card-head"><h2>Import <span id="st-import-stage"></span> as JSON</h2><span class="spacer"></span>' +
            '<button class="btn ghost small icon" type="button" id="st-import-close">' + Icon.get("x") + "</button></div>" +
          '<textarea id="st-import-ta" class="mono" rows="14" spellcheck="false" ' +
            'placeholder=\'{"logline": "…", …}\'></textarea>' +
          '<div class="ctl-row" style="margin-top:12px"><button class="btn primary" type="button" id="st-import-go">Import</button></div>' +
        "</div>" +
      "</div>";

    el.querySelectorAll("#story-tabs .tab").forEach(function (t) {
      t.onclick = function () {
        activeStage = t.dataset.stage;
        el.querySelector("#st-stage-hint").textContent = STAGE_HINT[activeStage];
        renderStage(el);
        refreshTabs(el);
      };
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
      toast(r && r.ok ? "Idea saved" : "Could not save the idea", !(r && r.ok));
      if (r && r.ok && State.refreshProject) State.refreshProject();
    };
    const ideaRe = el.querySelector("#st-idea-rewrite");
    if (ideaRe) ideaRe.onclick = async function () {
      toast("Rewriting the idea around your cast…");
      const r = await post("/api/projects/" + encodeURIComponent(State.pid) +
        "/story/rewrite-idea", {}, 120000);
      if (!r || !r.ok) return toast((r && r.error) || "Could not rewrite the idea", true);
      ideaTa.value = r.idea;
      toast("Idea rewritten. Read it, then Save idea.");
    };
    // Clear the whole story (a wrong generation must be removable before a
    // new package is pasted — the old UI only offered rollback).
    const clearBtn = el.querySelector("#st-clear");
    if (clearBtn) clearBtn.onclick = function () {
      Dlg.confirm({
        title: "Clear the story?",
        message: "This deletes all five parts of the story, their history and the shots. "
          + "Your cast and places in Cast &amp; World stay. It cannot be undone.",
        okLabel: "Clear story",
        danger: true,
      }).then(async function (yes) {
        if (!yes) return;
        const r = await post("/api/projects/" + encodeURIComponent(State.pid) +
          "/story/clear", { also_shots: true, keep_history: false }, 120000);
        if (r && r.ok) {
          toast("Story cleared");
          await State.refreshProject();
          await State.refreshShots();
          renderStage(el);
          refreshTabs(el);
        } else {
          toast((r && r.error) || "Could not clear the story", true);
        }
      });
    };
    const verDel = el.querySelector("#st-ver-del");
    if (verDel) verDel.onclick = async function () {
      const sel = el.querySelector("#st-ver");
      if (!sel || !sel.value) { toast("Pick a version in History first", true); return; }
      const v = parseInt(sel.value, 10);
      const r = await post("/api/projects/" + encodeURIComponent(State.pid) +
        "/story/delete-version", { stage: activeStage, version: v }, 30000);
      if (r && r.ok) {
        toast("Version " + v + " deleted");
        loadVersions(el);
      } else {
        toast((r && r.error) || "Could not delete the version", true);
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
          toast("Length set to " + Math.min(7200, Math.max(10, v)) + " seconds. "
            + "Copy the prompt again so it asks for the new number of shots.");
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
      if (!path) { toast("Type the file's path first", true); return; }
      toast("Importing the file…");
      const r = await post("/api/projects/" + encodeURIComponent(State.pid) +
                           "/story/import-file", { path: path }, 300000);
      if (!r) return;
      if (r.ok) toast("Reply imported from the file");
      else toast("Could not import it: " + (r.error || "unknown error"), true);
      el.querySelector("#st-pkg-modal").classList.add("hidden");
      await State.refreshProject();
      await State.refreshShots();
      Bus.emit("project", State.project);
      render(el);
    };
    el.querySelector("#st-import").onclick = function () {
      el.querySelector("#st-import-stage").textContent = STAGE_LABEL[activeStage].toLowerCase();
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
    const done = beats.filter(function (b) { return written[b.scene]; }).length;
    box.innerHTML =
      '<details class="fold scene-fold"' + (done < beats.length ? " open" : "") + ">" +
        "<summary><span class=\"fold-t\">Shots scene by scene</span>" +
          '<span class="fold-d">' + done + " of " + beats.length + " scenes have their shots. " +
          "For a long film or a chat AI: copy a scene&apos;s prompt into the same chat and paste its reply back.</span></summary>" +
        '<div class="scene-rows"></div>' +
      "</details>";
    const wrap = box.querySelector(".scene-rows");
    beats.forEach(function (b) {
      const row = document.createElement("div");
      row.className = "scene-row";
      const n = written[b.scene] || 0;
      const badge = n
        ? '<span class="badge st-green" data-tip="This scene&apos;s shots are in the shot list">' +
          Icon.get("check") + n + " shot" + (n === 1 ? "" : "s") + "</span>"
        : '<span class="badge st-gray" data-tip="Paste this scene&apos;s reply to fill it">No shots yet</span>';
      row.innerHTML =
        '<span class="sr-id">' + esc(b.scene) + "</span>" +
        '<span class="sr-slug">' + esc(slug(b)) + "</span>" +
        badge +
        '<span class="spacer"></span>' +
        '<button class="btn small ghost" type="button" data-cp="' + esc(b.scene) +
          '" data-tip="Copy this scene&apos;s prompt, with what happened so far">' + Icon.get("copy") + "Copy prompt</button>" +
        '<button class="btn small" type="button" data-ps="' + esc(b.scene) +
          '" data-tip="Paste the chat AI&apos;s reply for this scene">' + Icon.get("import") + "Paste reply</button>";
      wrap.appendChild(row);
    });
    wrap.querySelectorAll("[data-cp]").forEach(function (btn) {
      btn.onclick = async function () {
        const scene = btn.dataset.cp;
        toast("Building the scene prompt…");
        const r = await get("/api/projects/" + encodeURIComponent(State.pid) +
          "/story/scene-prompt?scene=" + encodeURIComponent(scene), 30000);
        if (!r || !r.prompt) { toast("Could not build the scene prompt", true); return; }
        const v = openModal('<textarea class="mono" rows="20" style="width:100%" ' +
          'id="sp-ta" spellcheck="false">' + esc(r.prompt) + "</textarea>",
          { title: "Prompt for " + scene + ": paste it into the same chat", wide: true });
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
          '<div class="ctl-row" style="margin-top:12px"><button class="btn primary" type="button" ' +
          'id="ps-go">Import the shots</button></div>',
          { title: "Paste the reply for " + scene, wide: true });
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
          else toast("Could not import it: " + (r.error || "unknown error"), true);
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
      { title: "AI writer", wide: true });
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
    box.innerHTML = '<span class="leader small"></span><span class="aigen-status">Starting the AI writer…</span>';
    const r = await post("/api/projects/" + encodeURIComponent(State.pid) +
                         "/story/generate-ai", {}, 60000);
    if (!r) { box.classList.add("hidden"); return; }
    if (!r.ok) {
      box.innerHTML = '<span class="aigen-status err">' +
        esc(r.error || "The AI writer did not start") + "</span>";
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
          esc(a.message || "The story is written") + "</span>";
        await State.refreshProject();
        await State.refreshShots();
        Bus.emit("project", State.project);
        render(el);
        return;
      }
      box2.innerHTML =
        '<span class="leader small"></span>' +
        '<span class="aigen-status">' + esc(a.stage || "Writing") + "</span>" +
        '<span class="aigen-bar"><span class="aigen-fill" style="width:' +
          (a.total ? Math.round((a.done / a.total) * 100) : 5) + '%"></span></span>' +
        '<span class="muted small">' + (a.total ? (a.done || 0) + " of " + a.total + " scenes. " : "") +
          esc(a.message || "") + "</span>" +
        '<span class="spacer"></span>' +
        '<button class="btn small warn" type="button" id="st-aistop">' + Icon.get("stop") + "Stop</button>";
      const sb = box2.querySelector("#st-aistop");
      if (sb) sb.onclick = async function () {
        await post("/api/projects/" + encodeURIComponent(State.pid) +
                   "/story/generate-ai/stop", {}, 30000);
        toast("Stopping after the AI's current answer…");
      };
    }, 3000);
  }

  async function copyAiPrompt(kind) {
    toast("Building the prompt…");
    const q = kind === "spine" ? "?kind=spine" : "";
    const r = await get("/api/projects/" + encodeURIComponent(State.pid) +
                         "/story/architect-prompt" + q, 30000);
    if (!r || !r.prompt) { toast("Could not build the prompt", true); return; }
    // stale-premise warning: names the idea mentions that are not in the
    // cast, surfaced with the copy confirmation so it is never missed
    const staleNames = (r.idea_warnings || []).filter(Boolean);
    const size = r.shot_count
      ? " It asks for " + r.shot_count + " shots" + (kind === "spine" && r.scene_count ? " over " + r.scene_count + " scenes" : "") + "."
      : "";
    function copy(text) {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () {
          if (staleNames.length) {
            toast("Prompt copied. Your idea names " + staleNames.join(", ") + ", who is not in the cast: " +
              "the AI is told to adapt, or use Rewrite from the cast.", true);
          } else {
            toast("Prompt copied. Paste it into a chat AI." + size);
          }
        }).catch(function () { showCopyModal(text); });
      } else { showCopyModal(text); }
    }
    function showCopyModal(text) {
      const v = openModal('<textarea class="mono" rows="20" style="width:100%" id="cp-ta" spellcheck="false">' +
        esc(text) + "</textarea>", { title: "The story prompt: copy it", wide: true });
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
    toast("Importing the reply and checking its shots…");
    const r = await post("/api/projects/" + encodeURIComponent(State.pid) +
                         "/story/import-all", payload, 300000);
    if (!r) return;
    if (r.ok) {
      const n = r.stages && r.stages.shots && r.stages.shots.ok;
      toast("Reply imported" + (n ? ". Its shots are in the shot list." : ""));
    } else {
      const bad = (r.stages && Object.keys(r.stages)
        .filter(function (k) { return !r.stages[k].ok; })
        .map(function (k) { return STAGE_LABEL[k] || k; }).join(", ")) || "";
      toast("Could not import it: " + (r.error || "problems in " + (bad || "the reply")),
            true);
    }
    el.querySelector("#st-pkg-modal").classList.add("hidden");
    await State.refreshProject();
    await State.refreshShots();
    Bus.emit("project", State.project);
    render(el);
  }

  async function regenerate(stage) {
    const label = STAGE_LABEL[stage] || stage;
    const okGo = await Dlg.confirm({
      title: "Rewrite the " + label.toLowerCase() + "?",
      message: "The AI writer writes the <b>" + esc(label.toLowerCase()) + "</b> again. " +
        "The current version stays in its History.",
      okLabel: "Rewrite",
    });
    if (!okGo) return;
    toast("Rewriting the " + label.toLowerCase() + ". This can take a few minutes…");
    const r = await post("/api/projects/" + encodeURIComponent(State.pid) + "/story/generate",
      { stages: [stage] }, 900000);
    if (!r) return;
    if (r.ok) {
      toast(label + " rewritten" + (r.shots_ingested ? ". " + r.shots_ingested + " shots added to the shot list." : ""));
    } else {
      toast("Could not rewrite it: " + (r.error || "unknown error"), true);
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
        toast("Imported: " + (done.map(function (k) { return STAGE_LABEL[k] || k; }).join(", ") || "nothing"));
      } else {
        toast(STAGE_LABEL[activeStage] + " imported");
      }
    } else {
      const firstErr = (function () {
        const st = r.stages || {};
        for (const k of Object.keys(st)) {
          if (st[k] && st[k].ok === false) return (STAGE_LABEL[k] || k) + ": " + (st[k].error || "unknown error");
        }
        return r.error || "unknown error";
      })();
      toast("Could not import it: " + firstErr, true);
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
    sel.innerHTML = '<option value="">' + (versions.length ? "History (" + versions.length + ")" : "No history") + "</option>" +
      versions.map(function (v) {
        return '<option value="' + esc(v.version) + '">Version ' + esc(v.version) + ", " +
          esc(fmt.ts(v.created)) + "</option>";
      }).join("");
    sel.disabled = !versions.length;
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
    if (!sel.value) { toast("Pick a version in History first", true); return; }
    const r = await post("/api/projects/" + encodeURIComponent(State.pid) + "/story/rollback",
      { stage: activeStage, version: Number(sel.value) }, 30000);
    if (r) toast(r.ok ? "Restored version " + sel.value : "Could not restore it", !r.ok);
    await State.refreshProject();
    Bus.emit("project", State.project);
    render(el);
  }

  /* ------------------------------------------------------------- stage view */
  const STAGE_EMPTY = {
    treatment: "The treatment is the film on one page. Write the story with the AI writer, or paste a chat AI's reply.",
    bible: "The story bible lists who and where. The AI writer fills it from your cast, or invents one.",
    beats: "The beats come after the treatment: one per scene.",
    scenes: "The scenes come from the beats: their mood, music and shot count.",
    shots: "The shot plan is the last part. Once it is written, its shots appear on the Shots page.",
  };

  /* a screenplay scene heading: "ROOFTOP - NIGHT" */
  function slug(b) {
    return [b.location, b.time].filter(Boolean).map(function (x) {
      return String(x).replace(/_/g, " ").toUpperCase();
    }).join(" - ");
  }

  function renderStage(el) {
    const story = (State.project && State.project.story) || {};
    const c = story[activeStage];
    const body = el.querySelector("#story-body");
    if (!c || !Object.keys(c || {}).length) {
      body.innerHTML = empty("No " + STAGE_LABEL[activeStage].toLowerCase() + " yet", STAGE_EMPTY[activeStage]);
      loadVersions(el);
      return;
    }
    if (activeStage === "treatment") body.innerHTML = renderTreatment(c);
    else if (activeStage === "bible") body.innerHTML = renderBible(c);
    else if (activeStage === "beats") body.innerHTML = renderBeats(c);
    else if (activeStage === "scenes") body.innerHTML = renderScenes(c);
    else if (activeStage === "shots") body.innerHTML = renderShots(c);
    loadVersions(el);
  }

  function list(items, tag) {
    items = (items || []).filter(Boolean);
    if (!items.length) return "";
    return "<" + tag + ">" + items.map(function (x) { return "<li>" + esc(x) + "</li>"; }).join("") + "</" + tag + ">";
  }

  function renderTreatment(c) {
    const themes = c.themes || [];
    const side = function (h, inner) { return inner ? '<div class="tr-block"><h3>' + h + "</h3>" + inner + "</div>" : ""; };
    return '<section class="treat">' +
      '<div class="treat-page">' +
        (c.logline ? '<p class="treat-logline">' + esc(c.logline) + "</p>" : "") +
        (c.synopsis ? '<div class="treat-syn">' + esc(c.synopsis).split(/\n{2,}/).map(function (para) {
          return "<p>" + para + "</p>";
        }).join("") + "</div>" : "") +
        side("How it escalates", list(c.escalation, "ol")) +
        side("How it ends", c.ending ? "<p>" + esc(c.ending) + "</p>" : "") +
      "</div>" +
      '<aside class="treat-side">' +
        side("Protagonist", c.protagonist ? "<p>" + esc(c.protagonist) + "</p>" : "") +
        side("Antagonist", c.antagonist ? "<p>" + esc(c.antagonist) + "</p>" : "") +
        side("Themes", themes.length ? '<div class="chips">' + themes.map(function (t) {
          return '<span class="chip">' + esc(t) + "</span>";
        }).join("") + "</div>" : "") +
        side("Pacing", c.pacing ? "<p>" + esc(c.pacing) + "</p>" : "") +
        side("Moments worth a clip", list(c.clipable_moments, "ul")) +
      "</aside>" +
    "</section>";
  }

  function renderBible(c) {
    const chars = asList(c.characters);
    const locs = asList(c.locations);
    const crowd = c.crowd_types || {};
    const uni = c.universe || {};
    const world = [["Genre", uni.genre], ["Tone", uni.tone], ["Colour grade", uni.grade]]
      .concat(Object.keys(uni.cinematography || {}).map(function (k) {
        return [k.charAt(0).toUpperCase() + k.slice(1).replace(/_/g, " "), uni.cinematography[k]];
      }))
      .filter(function (r) { return r[1]; });
    return '<p class="stage-note">' + Icon.get("info") +
        "These merge into Cast &amp; World. Anything you set there wins.</p>" +
      '<section class="bible-chars">' + chars.map(function (ch) {
        const powers = ch.powers || {};
        return '<article class="bible-char">' +
          '<header><h3>' + esc(ch.name || ch.id || "Unnamed") + "</h3>" +
            (ch.tier ? '<span class="badge tier-' + esc(ch.tier) + '">' + esc(ch.tier) + "</span>" : "") + "</header>" +
          (ch.body ? '<p class="bc-look">' + esc(ch.body) + "</p>" : "") +
          (ch.outfit ? '<p class="bc-outfit">' + esc(ch.outfit) + "</p>" : "") +
          (Object.keys(powers).length ? '<div class="bc-powers">' + Object.keys(powers).map(function (pn) {
            const pd = powers[pn] || {};
            return '<div class="power-row"><span class="swatch" style="background:' +
              esc(pd.color || "var(--accent)") + '"></span><b>' + esc(pn) + "</b>" +
              ((pd.descriptor || pd.motion) ? "<span>" + esc(pd.descriptor || pd.motion) + "</span>" : "") + "</div>";
          }).join("") + "</div>" : "") +
          ((ch.emotion_palette || []).length
            ? '<div class="chips">' + ch.emotion_palette.map(function (em) {
                return '<span class="chip emo">' + esc(em) + "</span>";
              }).join("") + "</div>"
            : "") +
          (ch.arc_summary ? '<p class="bc-arc">' + esc(ch.arc_summary) + "</p>" : "") +
        "</article>";
      }).join("") + "</section>" +
      '<section class="bible-two">' +
        '<div class="card"><div class="card-head"><h2>Places</h2></div>' +
          (locs.length ? '<div class="loc-list">' + locs.map(function (l) {
            return '<div class="loc-card"><b class="script-slug">' + esc(String(l.id || l.name || "").replace(/_/g, " ").toUpperCase()) + "</b>" +
              (l.descriptor ? "<p>" + esc(l.descriptor) + "</p>" : "") +
              (l.lighting ? '<p class="muted small">Light: ' + esc(l.lighting) + "</p>" : "") +
            "</div>";
          }).join("") + "</div>" : '<p class="muted small">No places yet.</p>') +
        "</div>" +
        '<div class="card"><div class="card-head"><h2>The world</h2></div>' +
          (world.length ? '<dl class="kv">' + world.map(function (r) {
            return "<dt>" + esc(r[0]) + "</dt><dd>" + esc(r[1]) + "</dd>";
          }).join("") + "</dl>" : "") +
          (Object.keys(crowd).length ? '<h3 class="kv-h">Crowds</h3><div class="chips">' + Object.keys(crowd).map(function (k) {
            return '<span class="chip">' + esc(k.replace(/_/g, " ")) + "</span>";
          }).join("") + "</div>" : "") +
        "</div>" +
      "</section>";
  }

  /* the beats as a screenplay page: a scene heading, then the action */
  function renderBeats(c) {
    const beats = c.beats || [];
    return '<section class="script-page">' + beats.map(function (b, i) {
      return '<div class="beat">' +
        '<div class="beat-slug"><span class="beat-n">' + (i + 1) + "</span>" +
          esc(slug(b) || b.scene || "") + "</div>" +
        '<div class="beat-meta">' + esc(b.scene || "") +
          (b.mood ? ", " + esc(b.mood) : "") + (b.est_shots ? ", about " + esc(b.est_shots) + " shots" : "") + "</div>" +
        (b.goal ? '<p class="beat-line">' + esc(b.goal) + "</p>" : "") +
        (b.turn ? '<p class="beat-line turn">' + esc(b.turn) + "</p>" : "") +
        (b.hook ? '<p class="beat-hook">' + esc(b.hook) + "</p>" : "") +
      "</div>";
    }).join("") + "</section>";
  }

  /* the scenes as index cards on a board */
  function renderScenes(c) {
    const scenes = c.scenes || [];
    return '<section class="index-board">' + scenes.map(function (s) {
      return '<article class="index-card">' +
        '<header><span class="ic-id">' + esc(s.id || "") + "</span>" +
          (s.shot_count ? '<span class="ic-n">' + esc(s.shot_count) + " shots</span>" : "") + "</header>" +
        '<div class="ic-slug">' + esc(slug(s)) + "</div>" +
        (s.emotion_arc ? '<p class="ic-line">' + esc(s.emotion_arc) + "</p>" : "") +
        (s.music_mood ? '<p class="ic-line music">' + Icon.get("music") + esc(s.music_mood) + "</p>" : "") +
        ((s.beats || []).length ? '<div class="chips">' + s.beats.map(function (b) {
          return '<span class="chip">' + esc(b) + "</span>";
        }).join("") + "</div>" : "") +
      "</article>";
    }).join("") + "</section>";
  }

  function renderShots(c) {
    const shots = c.shots || [];
    return '<section class="shot-plan">' +
      '<div class="plan-head"><span>Shot</span><span>Size and camera</span><span>Action</span></div>' +
      shots.map(function (s) {
        return '<div class="plan-row">' +
          '<span class="plan-id">' + esc(s.shot_id || "") +
            (s.tier ? ' <span class="badge ' + tierClass(s.tier) + '">' + esc(s.tier) + "</span>" : "") + "</span>" +
          '<span class="plan-cam">' + esc([s.shot_size, s.camera].filter(Boolean).join(", ").replace(/_/g, " ")) + "</span>" +
          '<span class="plan-act">' + esc(s.primary_action || "") + "</span>" +
        "</div>";
      }).join("") +
      '<p class="plan-foot">' + shots.length + " shots. Their video prompts and renders are on the Shots page.</p>" +
    "</section>";
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
