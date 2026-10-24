/* overview.js — project dashboard: hero title card, progress, production
   strip, director mode + gates, story summary, recent events.
   The transport lives in the topbar, the GPU meter in the sidebar. */
(function () {
  "use strict";

  const EVENT_TONE = { rendered: "info", passed: "ok", qa: "accent", error: "bad", failed: "bad", review: "warn" };

  function render(el) {
    const p = State.project;
    if (!p || !State.pid) { el.innerHTML = noProject(); el.__sig = null; return; }
    const story = p.story || {};
    const tr = story.treatment || {};
    const st = State.stats || {};
    const by = st.by_status || {};
    const total = st.total || State.shots.length || 0;
    const passed = by.passed || 0;
    const review = by.review || 0;
    const failed = by.failed || 0;
    const rendered = (by.rendering || 0) + (by.queued || 0) + (by.post || 0) + (by.qa || 0);
    const pct = total ? Math.round((passed / total) * 100) : 0;
    const events = State.events.slice(-12).reverse();
    const stages = ["treatment", "bible", "beats", "scenes", "shots"];
    const doneStages = stages.filter(function (s) {
      return story[s] && Object.keys(story[s] || {}).length;
    });
    const mode = p.mode || "checkpoints";
    const gates = p.gates || {};
    const fallback = p.ref_fallback !== 0;

    const html =
      '<section class="ov-hero">' +
        '<div class="ov-hero-left">' +
          '<div class="ov-kicker">' + esc(p.genre || "untitled genre") + " · " +
            esc(p.tone || "—") + " · " + fmt.min(p.duration_target) + ' target · mode ' +
            '<span class="badge mode-' + esc(mode) + '">' + esc(mode) + "</span></div>" +
          "<h1>" + esc(p.title) + "</h1>" +
          '<p class="ov-logline">' + esc(tr.logline || p.idea || "No story yet — run the director or generate in the Story page.") + "</p>" +
        "</div>" +
        '<div class="ov-hero-right">' +
          '<div class="ov-big-pct">' + pct + '<span>%</span></div>' +
          '<div class="ov-big-sub">' + passed + " / " + total + " passed</div>" +
        "</div>" +
      "</section>" +
      '<div class="ov-progress"><div class="ov-progress-fill" style="width:' + pct + '%"></div></div>' +
      '<section class="stat-strip">' +
        statCell("Shots", String(total), "") +
        statCell("Passed", String(passed), "", passed ? "ok" : "") +
        statCell("In review", String(review), review ? "needs a decision" : "", review ? "warn" : "") +
        statCell("Failed", String(failed), failed ? "auto-retries" : "", failed ? "bad" : "") +
        statCell("In flight", String(rendered), "render · queue · post") +
        statCell("Render time", fmt.s(st.render_time_s || 0), "") +
        statCell("ETA", State.eta ? fmt.min(State.eta.eta_min) : "—",
          State.eta ? (State.eta.remaining + " left · avg " + State.eta.avg_render_min + " min") : "") +
      "</section>" +
      '<section class="ov-grid">' +
        '<div class="card ov-director">' +
          '<div class="card-head"><h2>Director</h2></div>' +
          '<div class="ov-row"><span class="ov-k">Mode</span>' +
            '<select id="ov-mode" data-tip="autopilot: no stops · checkpoints: pauses at the manual gates · manual: every step waits for you">' +
              ["autopilot", "checkpoints", "manual"].map(function (m) {
                return "<option" + (mode === m ? " selected" : "") + ">" + m + "</option>";
              }).join("") +
            "</select></div>" +
          '<div class="ov-row"><span class="ov-k">Gates</span><span class="ov-gates">' +
            ["story", "keyframes", "review"].map(function (g) {
              const v = (gates[g] || "manual");
              return '<span class="gate-chip ' + esc(v) + '" data-tip="' + esc(g) + " gate: " +
                (v === "auto" ? "passes on its own" : "waits for your approval") + '">' + g + "</span>";
            }).join("") + "</span></div>" +
          '<div class="ov-row"><span class="ov-k">Portrait fallback</span>' +
            '<label class="toggle" data-tip="On: a shot without a reference image starts from the roster portrait. Off: it renders from the prompt and LoRAs alone.">' +
              '<input type="checkbox" id="ov-fallback"' + (fallback ? " checked" : "") + "><span></span>" +
              '<em class="ov-v">' + (fallback ? "On" : "Off") + "</em></label></div>" +
        "</div>" +
        '<div class="card ov-story">' +
          '<div class="card-head"><h2>Story</h2>' +
            '<span class="muted small">' + doneStages.length + " of " + stages.length + " stages</span>" +
            '<span class="spacer"></span><a href="#story" class="link small">Open</a></div>' +
          (tr.logline
            ? '<p class="ov-synopsis clamp">' + esc(tr.synopsis || tr.logline) + "</p>"
            : '<div class="muted">No story yet. <a href="#story" class="link">Open Story</a> to write the treatment.</div>') +
        "</div>" +
      "</section>" +
      '<div class="card">' +
        '<div class="card-head"><h2>Recent events</h2></div>' +
        (events.length
          ? '<div class="events">' + events.map(function (e) {
              const tone = EVENT_TONE[e.error ? "error" : e.event] || "dim";
              return '<div class="event"><span class="ev-dot ' + tone + '"></span>' +
                '<span class="ev-body">' + (e.shot_id ? '<b class="mono">' + esc(e.shot_id) + "</b> " : "") +
                esc(e.event || "") + (e.error ? ' <span class="bad">— ' + esc(e.error) + "</span>" : "") + "</span>" +
                '<span class="ev-ts">' + esc(fmt.ago(e.ts)) + "</span></div>";
            }).join("") + "</div>"
          : '<div class="muted small">Nothing yet. Events appear here as shots render.</div>') +
      "</div>";

    // re-render only on a visible change (the status poll ticks every 5 s and
    // a rebuild would drop the focus of an open select)
    if (el.__sig === html) return;
    el.__sig = html;
    el.innerHTML = html;
    bind(el, p);
  }

  function statCell(label, value, sub, tone) {
    return '<div class="stat-cell' + (tone ? " " + tone : "") + '">' +
      '<div class="stat-label">' + esc(label) + "</div>" +
      '<div class="stat-value">' + esc(value) + "</div>" +
      (sub ? '<div class="stat-sub">' + esc(sub) + "</div>" : "") +
      "</div>";
  }

  function bind(el, p) {
    const modeSel = el.querySelector("#ov-mode");
    if (modeSel) modeSel.onchange = async function () {
      const r = await post("/api/projects/" + encodeURIComponent(State.pid),
        { mode: modeSel.value }, 30000);
      if (r) toast(r.ok ? "mode: " + modeSel.value : "mode change failed", !r.ok);
      State.refreshProject();
    };
    const ovFb = el.querySelector("#ov-fallback");
    if (ovFb) ovFb.onchange = async function () {
      const r = await post("/api/projects/" + encodeURIComponent(State.pid) +
        "/refs-fallback", { enabled: ovFb.checked }, 30000);
      if (r && r.ok) {
        toast(ovFb.checked ? "portrait fallback on" : "portrait fallback off");
        State.refreshProject();
      } else {
        ovFb.checked = !ovFb.checked;
        toast((r && r.error) || "failed to save", true);
      }
    };
  }

  Router.register("overview", {
    polls: ["status", "eta"],
    subs: [],
    mount: function (el) {
      const page = this;
      page.el = el;
      page.render = function () { render(el); };
      page.subs.push(Bus.on("project", page.render));
      page.subs.push(Bus.on("status", page.render));
      page.subs.push(Bus.on("eta", page.render));
      page.render();
    },
    unmount: function () {
      this.subs.forEach(function (off) { off(); });
      this.subs = [];
      if (this.el) { this.el.innerHTML = ""; this.el.__sig = null; }
    },
  });
})();
