/* prompt-editor.js — the video prompt of an LTX-2.5 / H3 shot: the editor
   (any page: Bus "prompt:edit" <shot id>) and the briefs for a chat AI, batch
   by batch or for one shot, with the replies pasted back. Prompts are written for the exact request the shot
   renders with; the studio checks every one before it saves it. */
(function () {
  "use strict";

  const STATE = {
    ready: ["ready", "st-green", "Ready: written for how this shot renders now"],
    missing: ["no prompt", "st-amber", "No prompt yet: write one, or let the AI write it. The shot waits for it before it renders."],
    stale: ["outdated", "st-amber", "Written for an earlier version of the shot or of how it opens"],
    rendered: ["rendered", "st-gray", "Rendered: a re-render writes or checks its prompt first"],
  };
  const SOURCE = { ai: "written by the AI", hand: "written by hand", chat: "pasted from a chat AI" };
  const OPENS = {
    cut: "opens fresh (a cut)",
    match_action: "a match cut: a new angle on the running action",
    continue: "carries on from the shot before, no cut",
  };

  function base() { return "/api/projects/" + encodeURIComponent(State.pid); }

  function chip(entry) {
    const s = STATE[(entry && entry.state) || "missing"] || STATE.missing;
    return '<span class="pchip ' + s[1] + '" data-tip="' + esc(s[2]) + '">' + esc(s[0]) + "</span>";
  }

  async function refresh() {
    await State.refreshPrompts();
    await State.refreshShots();
  }

  function issuesHtml(issues) {
    const list = (issues || []).filter(function (i) { return i && i.message; });
    if (!list.length) return "";
    return '<ul class="pe-issues">' + list.map(function (i) {
      return '<li class="' + (i.level === "error" ? "st-red" : "st-amber") + '">' + esc(i.message) + "</li>";
    }).join("") + "</ul>";
  }

  /* ----------------------------------------------------------- the editor */
  async function open(sid) {
    if (!State.pid) return;
    if (!State.prompts) await State.refreshPrompts();
    const all = (State.prompts && State.prompts.shots) || {};
    const e = all[sid];
    if (!e) { toast(sid + " renders on the legacy Wan model: edit its prompt under Advanced", true); return; }
    const unit = e.unit || [sid];
    const head = unit[0] === sid;
    const where = unit.length > 1
      ? "Renders in one seamless take: " + unit.join(" → ") + " (segment " + (unit.indexOf(sid) + 1) +
        " of " + unit.length + ")."
      : "Renders as one clip.";
    const opens = head ? OPENS[e.opening] || "" : "carries on from the segment before, no cut";
    const image = head && (e.wants_image || e.image);
    const ai = !!(State.prompts && State.prompts.ai);
    const v = openModal(
      '<div class="pe-meta">' + chip(e) +
        (e.source ? '<span class="muted small">' + esc(SOURCE[e.source] || e.source) + "</span>" : "") +
        (e.updated ? '<span class="muted small">' + esc(fmt.ago(e.updated)) + "</span>" : "") +
        '<span class="muted small">' + esc(e.model || "") + "</span>" +
      "</div>" +
      '<p class="muted small">' + esc(where) + (opens ? " It " + esc(opens) + "." : "") + "</p>" +
      (e.why ? '<p class="small st-amber">' + esc(e.why) + "</p>" : "") +
      '<label class="np-label">Video prompt' +
        '<textarea id="pe-text" class="mono" rows="9" spellcheck="false" placeholder="What is seen and heard, ' +
          'in the present tense: the camera, the action from start to end, the light, the sound.">' +
          esc(e.text || "") + "</textarea></label>" +
      (image
        ? '<label class="np-label">Opening image' +
            '<textarea id="pe-image" class="mono" rows="3" spellcheck="false" placeholder="The first frame as a still: ' +
              'shot size and angle, the setting and its light, who is where and how they look. No motion.">' +
              esc(e.image || "") + "</textarea>" +
            '<span class="muted small">Made as a still before the render, from the same reference pictures; ' +
              "the clip starts from it. Leave it empty to render from the references alone.</span></label>"
        : "") +
      '<div id="pe-issues">' + issuesHtml(e.issues) + "</div>" +
      '<div class="ctl-row">' +
        (ai ? '<button class="btn" type="button" id="pe-ai" data-tip="Let the AI write this ' +
                (unit.length > 1 ? "take's prompts again (hand-written ones are kept)" : "prompt again") + '">' +
                Icon.get("wand") + "Write with AI</button>" : "") +
        '<button class="btn" type="button" id="pe-chat" data-tip="Copy a brief for this ' +
          (unit.length > 1 ? "take" : "shot") + ' into any chat AI and paste its reply back">' +
          Icon.get("copy") + "Use a chat AI</button>" +
        '<button class="btn" type="button" id="pe-clear" data-tip="Remove this prompt' +
          (ai ? ": the AI writes a new one" : "") + '">Clear</button>' +
        '<span class="spacer"></span>' +
        '<button class="btn" type="button" id="pe-close">Close</button>' +
        '<button class="btn primary" type="button" id="pe-save">Save</button>' +
      "</div>",
      { title: sid + " — video prompt", wide: true });
    if (!v) return;
    const url = base() + "/shots/" + encodeURIComponent(sid) + "/prompt";
    v.querySelector("#pe-close").onclick = function () { closeModal(v); };
    v.querySelector("#pe-chat").onclick = function () { closeModal(v); brief(unit); };
    v.querySelector("#pe-save").onclick = async function () {
      const body = { prompt: v.querySelector("#pe-text").value };
      const img = v.querySelector("#pe-image");
      if (img) body.image = img.value;
      const r = await post(url, body, 60000);
      if (!r) return;
      if (!r.ok) {
        v.querySelector("#pe-issues").innerHTML = issuesHtml(r.issues || [{ level: "error", message: r.error }]);
        return;
      }
      toast(r.cleared ? "prompt cleared" : r.fixed ? "saved, with the check's fixes applied" : "prompt saved");
      closeModal(v);
      refresh();
    };
    v.querySelector("#pe-clear").onclick = async function () {
      const r = await post(url, { prompt: "" }, 30000);
      if (r && r.ok) { toast("prompt cleared"); closeModal(v); refresh(); }
    };
    const aiBtn = v.querySelector("#pe-ai");
    if (aiBtn) aiBtn.onclick = async function () {
      if (e.source === "hand" || e.source === "chat") {
        const r0 = await post(url, { prompt: "" }, 30000);
        if (!r0 || !r0.ok) return;
      }
      const r = await post(base() + "/prompts/write", { shots: unit, force: true }, 30000);
      if (r && r.ok) {
        toast("the AI is writing " + (unit.length > 1 ? "the take's prompts" : "the prompt") + " (Shots page)");
        closeModal(v);
        Bus.emit("prompts:writing");
      }
    };
    bindTips(v);
  }

  /* ---------------------------------------------- the brief for a chat AI */
  /* A batch at a time: takes of one scene, a few prompts to write (one
     reply's worth, movie.prompt_segments_per_reply; a longer take comes in
     parts). Copy the brief, paste the
     reply, Import & next: the next batch is made after the last one is saved,
     so it carries the prompts written for the shots before it. `shots`: one
     shot's take only (from its prompt editor), its parts one after another. */
  function span(ids) {
    if (!ids.length) return "";
    return ids.length > 2 ? ids[0] + " to " + ids[ids.length - 1] : ids.join(" and ");
  }

  function copyText(text, ta) {
    const done = function () { toast("copied: paste it into the chat AI"); };
    if (ta) { ta.focus(); ta.select(); }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(function () {
        toast("copy it with Ctrl+C (the text is selected)");
      });
    } else {
      try { document.execCommand("copy"); done(); } catch (e) { toast("copy it with Ctrl+C (the text is selected)"); }
    }
  }

  /* a production that stopped for video prompts goes on from here once some
     are in: the top bar's Continue production (more batches can follow while
     it renders; each one saved queues its shots) */
  function offerContinue(box) {
    const p = State.project || {};
    const run = document.getElementById("btn-run");
    if (!run || (State.director && State.director.running)) return;
    if (p.status !== "gate_pending" || p.stage !== "prompts") return;
    const row = document.createElement("div");
    row.className = "ctl-row";
    row.innerHTML = '<span class="muted small">Production waits for video prompts. It renders the shots ' +
      "that have one, and stops again at the next shot without.</span><span class=\"spacer\"></span>" +
      '<button class="btn primary small" type="button">' + Icon.get("play") + "Continue production</button>";
    row.querySelector("button").onclick = function () { row.remove(); run.click(); };
    box.appendChild(row);
  }

  function fixRequest(refused) {
    return "Some of these prompts were refused:\n" +
      Object.keys(refused).map(function (s) { return "- Shot " + s + ": " + refused[s].replace(/^Shot [^:]+: /, ""); }).join("\n") +
      "\nWrite only those shots again with the fixes, in the same format (\"Shot <id>: ...\"), and nothing else.";
  }

  async function brief(shots) {
    if (!State.pid) return;
    let single = Array.isArray(shots) && shots.length ? shots : null;
    const fetchBrief = function () {
      return post(base() + "/prompts/brief", single ? { shots: single } : {}, 60000);
    };
    let r = await fetchBrief();
    if (!r || !r.ok) return;
    if (!r.text) { toast("every shot still to render has a ready prompt"); return; }
    const v = openModal(
      '<div id="pb-head"></div>' +
      '<textarea id="pb-brief" class="mono pb-text" rows="9" readonly spellcheck="false"></textarea>' +
      '<div class="ctl-row"><button class="btn" type="button" id="pb-copy">' + Icon.get("copy") + "Copy brief</button>" +
        '<span class="muted small" id="pb-size"></span></div>' +
      '<p class="muted small" id="pb-step2">2. Paste the chat AI&apos;s whole reply here. Every "Shot &lt;id&gt;:" block is ' +
        "checked like the Video Studio checks a prompt, and against its shot (the cast&apos;s look, the dialogue), then saved.</p>" +
      '<textarea id="pb-reply" class="mono pb-text" rows="7" spellcheck="false" placeholder="Shot s01_001: …"></textarea>' +
      '<div id="pb-result"></div>' +
      '<div class="ctl-row"><span class="spacer"></span>' +
        '<button class="btn" type="button" id="pb-close">Close</button>' +
        '<button class="btn primary" type="button" id="pb-import">' + (single ? "Import reply" : "Import &amp; next") + "</button></div>",
      { title: single ? "A chat AI writes " + span(single) : "Prompts from a chat AI, batch by batch", wide: true });
    if (!v) return;
    const ta = v.querySelector("#pb-brief");
    const show = function (b) {
      r = b;
      const pg = b.progress || {};
      const ids = b.shots || [];
      v.querySelector("#pb-head").innerHTML =
        (single ? "" : '<p class="pb-progress"><b>' + (pg.ready || 0) + " of " + (pg.total || 0) +
          "</b> prompts ready, " + (pg.batches || 0) + (pg.batches === 1 ? " batch" : " batches") + " to go.</p>") +
        '<p class="muted small">1. Copy this brief for ' + esc(span(ids)) + (b.scene ? " (scene " + esc(b.scene) + ")" : "") +
          " into a chat AI (ChatGPT, Claude, Gemini…)" + (single ? "" : " and keep that chat for the next batches") +
          ". It holds the film, the story so far with the prompts already written, these shots with the cast, " +
          "and the rules of the video model.</p>";
      ta.value = b.text;
      v.querySelector("#pb-size").textContent = Math.max(1, Math.round(b.text.length / 1000)) + "k characters";
      v.querySelector("#pb-reply").value = "";
    };
    const finished = function () {
      v.querySelector("#pb-head").innerHTML = '<p class="pb-progress"><b>Every shot still to render has its prompt.</b></p>';
      ["#pb-brief", "#pb-copy", "#pb-size", "#pb-step2", "#pb-reply", "#pb-import"].forEach(function (q) {
        const n = v.querySelector(q);
        if (n) n.classList.add("hidden");
      });
    };
    show(r);
    v.querySelector("#pb-copy").onclick = function () { copyText(r.text, ta); };
    v.querySelector("#pb-close").onclick = function () { closeModal(v); };
    const btn = v.querySelector("#pb-import");
    btn.onclick = async function () {
      const text = v.querySelector("#pb-reply").value;
      if (!text.trim()) { toast("paste the reply first", true); return; }
      btn.disabled = true;
      const res = await API.post(base() + "/prompts/import", { text: text }, 60000);
      btn.disabled = false;
      if (!res) return;
      const refused = res.refused || {};
      const warned = res.warnings || {};
      const saved = res.saved || [];
      const box = v.querySelector("#pb-result");
      box.innerHTML =
        '<ul class="pe-issues">' +
          (saved.length ? '<li class="st-green">saved: ' + esc(span(saved)) + " (" + saved.length + ")</li>" : "") +
          Object.keys(refused).map(function (s) {
            return '<li class="st-red">' + esc(s) + ": " + esc(refused[s].replace(/^Shot [^:]+: /, "")) + "</li>";
          }).join("") +
          Object.keys(warned).map(function (s) {
            return warned[s].map(function (w) { return '<li class="st-amber">' + esc(s) + ": " + esc(w) + "</li>"; }).join("");
          }).join("") +
          ((res.unknown || []).length ? '<li class="st-amber">not shots of this movie: ' +
            esc(res.unknown.join(", ")) + "</li>" : "") +
          (!res.ok && !Object.keys(refused).length ? '<li class="st-red">' + esc(res.error || "nothing saved") + "</li>" : "") +
        "</ul>" +
        (Object.keys(refused).length
          ? '<div class="ctl-row"><button class="btn small" type="button" id="pb-fix">' + Icon.get("copy") +
            "Copy a fix request</button><span class=\"muted small\">Paste it into the same chat, then its reply here.</span></div>"
          : "");
      const fix = box.querySelector("#pb-fix");
      if (fix) fix.onclick = function () { copyText(fixRequest(refused)); };
      if (!saved.length) return;
      toast(saved.length + " prompt(s) saved" + (Object.keys(refused).length ? ", " + Object.keys(refused).length + " refused" : ""));
      offerContinue(box);
      refresh();
      v.querySelector("#pb-reply").value = "";
      if (Object.keys(refused).length) return;            // the refused ones first
      if (single && !(res.waiting || []).length) { finished(); return; }
      if (single) single = res.waiting;                    // the take's next part
      const nb = await fetchBrief();
      if (!nb || !nb.ok) return;
      if (!nb.text) { finished(); return; }
      show(nb);
    };
  }

  Bus.on("prompt:edit", open);
  window.PromptEditor = { open: open, brief: brief, chip: chip };
})();
