/* overview.js — the production board: the project's title card, the one
   thing to do next, the production line (the seven steps with their knots,
   from js/shell.js), the reel (every shot as a frame, by scene, coloured by
   its status), how the director runs (mode, checkpoints, the portrait
   fallback) and recent activity. The transport lives in the topbar. */
(function () {
  "use strict";

  const EVENT_TONE = { rendered: "info", passed: "ok", qa: "accent", error: "bad", failed: "bad", review: "warn" };
  const MODES = [
    { id: "checkpoints", t: "Checkpoints", d: "Runs on its own and stops for your approval at the checkpoints." },
    { id: "autopilot", t: "Autopilot", d: "Runs every step without stopping." },
    { id: "manual", t: "Manual", d: "Each step waits until you start it." },
  ];
  const GATE_WORDS = { story: "the story", keyframes: "the keyframes", review: "the review" };
  const REEL = {   // shot status -> reel class and legend
    passed: "ok", review: "need", failed: "bad", rendering: "live", queued: "queued", post: "live",
    qa: "live", prompt: "need", pending: "", drafting: "live",
  };

  function length(s) {
    s = Number(s || 0);
    if (!s) return "";
    return s >= 60 ? Math.round(s / 60) + " min film" : s + " s film";
  }

  function render(el) {
    const p = State.project;
    if (!p || !State.pid) {
      const html = '<div class="ov-blank">' +
        '<span class="ov-blank-mark">' + Icon.get("reel") + "</span>" +
        "<h1>Make a movie</h1>" +
        "<p>Start with a title and an idea. Kinoloom walks you through the rest, one step at a time: " +
          "the cast, the story, the shots, the render, the cut.</p>" +
        '<button class="btn primary big" type="button" id="ov-new">' + Icon.get("plus") + "Start a new movie</button>" +
        "</div>";
      if (el.__sig !== html) {
        el.__sig = html;
        el.innerHTML = html;
        el.querySelector("#ov-new").onclick = function () { window.dispatchEvent(new CustomEvent("kino:new-project")); };
      }
      return;
    }
    const story = p.story || {};
    const tr = story.treatment || {};
    const html =
      '<section class="ov-top">' +
        '<div class="ov-title">' +
          '<div class="chips">' +
            [p.genre, p.tone, length(p.duration_target)].filter(Boolean).map(function (x) {
              return '<span class="chip">' + esc(x) + "</span>";
            }).join("") +
          "</div>" +
          '<h1 class="title-card">' + esc(p.title) + "</h1>" +
          ((tr.logline || p.idea)
            ? '<p class="ov-logline">' + esc(tr.logline || p.idea) + "</p>"
            : '<p class="ov-logline dim">No idea written yet. Add one on the Story page.</p>') +
        "</div>" +
        nextCard() +
      "</section>" +
      productionLine() +
      reel() +
      '<section class="ov-grid">' + directorCard(p) + activityCard() + "</section>";
    // re-render only on a visible change (the status poll ticks every 5 s and
    // a rebuild would drop the focus of an open control)
    if (el.__sig === html) return;
    el.__sig = html;
    el.innerHTML = html;
    bind(el, p);
  }

  function nextCard() {
    const nx = Shell.next();
    const info = Shell.PAGES[nx.page] || {};
    const step = info.step ? "Up next, step " + info.step : "Up next";
    const open = '<a class="btn' + (nx.action === "open" ? " primary" : "") + '" href="' + Shell.href(nx.page) +
      '" data-page="' + nx.page + '" data-shell="' + info.shell + '">Open ' + esc(info.label || "") + Icon.get("arrow-right") + "</a>";
    const go = nx.action === "start" || nx.action === "approve"
      ? '<button class="btn primary" type="button" data-run>' + Icon.get("play") +
          (nx.action === "approve" ? "Approve &amp; continue" : "Start production") + "</button>"
      : "";
    return '<aside class="ov-next">' +
      '<div class="ov-next-k">' + esc(step) + "</div>" +
      '<div class="ov-next-t">' + esc(nx.title) + "</div>" +
      '<p class="ov-next-d">' + esc(nx.text) + "</p>" +
      '<div class="ov-next-acts">' + go + (nx.page !== "overview" ? open : "") + "</div>" +
    "</aside>";
  }

  function productionLine() {
    return '<section class="line" aria-label="Production steps">' +
      Shell.steps().map(function (s) {
        const info = Shell.PAGES[s.page];
        return '<a class="station ' + s.state + '" href="' + Shell.href(s.page) + '" data-page="' + s.page +
          '" data-shell="' + info.shell + '">' +
          '<span class="knot ' + s.state + '"></span>' +
          '<span class="st-l"><span class="st-n">' + s.n + "</span>" + esc(s.label) + "</span>" +
          '<span class="st-d">' + esc(s.detail) + "</span></a>";
      }).join("") +
    "</section>";
  }

  /* every shot as a frame on a strip, grouped by scene */
  function reel() {
    const shots = (State.shots || []).slice().sort(function (a, b) {
      return (a.scene || "").localeCompare(b.scene || "") || (a.shot_id || "").localeCompare(b.shot_id || "");
    });
    if (!shots.length) {
      return '<section class="reel empty-reel"><div class="reel-head"><h2 class="sec-title">The reel</h2></div>' +
        '<div class="reel-strip blank"><span class="reel-note">Every shot shows up here as a frame once the story ' +
        "has a shot list, and lights up as it renders.</span></div></section>";
    }
    const scenes = [];
    shots.forEach(function (s) {
      const last = scenes[scenes.length - 1];
      if (last && last.id === (s.scene || "")) last.shots.push(s);
      else scenes.push({ id: s.scene || "", shots: [s] });
    });
    const count = function (cls) {
      return shots.filter(function (s) { return (REEL[s.status] || "") === cls; }).length;
    };
    const legend = [["ok", "passed"], ["live", "rendering"], ["queued", "queued"], ["need", "needs you"],
      ["bad", "failed"], ["", "not yet"]].map(function (l) {
      const n = count(l[0]);
      return n ? '<span class="lg"><i class="f ' + l[0] + '"></i>' + l[1] + " " + n + "</span>" : "";
    }).join("");
    return '<section class="reel">' +
      '<div class="reel-head"><h2 class="sec-title">The reel</h2>' +
        '<span class="muted small">' + shots.length + " shots in " + scenes.length + " scenes</span>" +
        '<span class="spacer"></span><div class="reel-legend">' + legend + "</div></div>" +
      '<div class="reel-strip">' + scenes.map(function (sc) {
        return '<div class="reel-scene" data-tip="' + esc((sc.id || "scene") + ": " + sc.shots.length + " shots") + '">' +
          sc.shots.map(function (s) {
            return '<i class="f ' + (REEL[s.status] || "") + '" data-tip="' + esc(s.shot_id + ": " + (s.status || "not rendered")) + '"></i>';
          }).join("") + "</div>";
      }).join("") + "</div></section>";
  }

  function directorCard(p) {
    const mode = p.mode || "checkpoints";
    const gates = p.gates || {};
    const stops = Object.keys(GATE_WORDS).filter(function (g) { return (gates[g] || "manual") !== "auto"; });
    const passes = Object.keys(GATE_WORDS).filter(function (g) { return (gates[g] || "manual") === "auto"; });
    const words = function (gs) { return gs.map(function (g) { return GATE_WORDS[g]; }).join(", "); };
    const gateLine = mode === "autopilot" ? "Nothing stops it: shots that fail their check stay flagged for you."
      : mode === "manual" ? "Every step waits for you."
      : (stops.length ? "Stops for you at " + words(stops) + "." : "") +
        (passes.length ? " Passes " + words(passes) + " on its own." : "");
    const fallback = p.ref_fallback !== 0;
    return '<section class="card ov-director">' +
      '<div class="card-head"><h2>How production runs</h2></div>' +
      '<div class="ov-modes">' + MODES.map(function (m) {
        return '<button class="choice' + (m.id === mode ? " on" : "") + '" type="button" data-mode="' + m.id + '">' +
          '<span class="c-t">' + m.t + '</span><span class="c-d">' + m.d + "</span></button>";
      }).join("") + "</div>" +
      '<p class="ov-gates">' + esc(gateLine.trim()) + "</p>" +
      '<label class="toggle ov-fb" data-tip="On: a shot with no start picture opens on its cast&apos;s portrait. Off: it renders from the prompt alone.">' +
        '<input type="checkbox" id="ov-fallback"' + (fallback ? " checked" : "") + "><span></span>" +
        "<em>Open shots without a start picture on the cast portrait</em></label>" +
    "</section>";
  }

  function activityCard() {
    const events = State.events.slice(-12).reverse();
    return '<section class="card ov-activity">' +
      '<div class="card-head"><h2>Activity</h2></div>' +
      (events.length
        ? '<div class="events">' + events.map(function (e) {
            const tone = EVENT_TONE[e.error ? "error" : e.event] || "dim";
            return '<div class="event"><span class="ev-dot ' + tone + '"></span>' +
              '<span class="ev-body">' + (e.shot_id ? "<b>" + esc(e.shot_id) + "</b> " : "") +
              esc(e.event || "") + (e.error ? ' <span class="bad">' + esc(e.error) + "</span>" : "") + "</span>" +
              '<span class="ev-ts">' + esc(fmt.ago(e.ts)) + "</span></div>";
          }).join("") + "</div>"
        : '<p class="muted small">Nothing yet. What the director and the renderer do shows up here.</p>') +
    "</section>";
  }

  function bind(el, p) {
    el.querySelectorAll(".ov-modes [data-mode]").forEach(function (b) {
      b.onclick = async function () {
        if (b.dataset.mode === (p.mode || "checkpoints")) return;
        const r = await post("/api/projects/" + encodeURIComponent(State.pid), { mode: b.dataset.mode }, 30000);
        if (r) toast(r.ok ? "Production runs on " + b.dataset.mode : "Could not change how production runs", !r.ok);
        State.refreshProject();
      };
    });
    const run = el.querySelector("[data-run]");
    if (run) run.onclick = function () { const b = document.getElementById("btn-run"); if (b) b.click(); };
    const ovFb = el.querySelector("#ov-fallback");
    if (ovFb) ovFb.onchange = async function () {
      const r = await post("/api/projects/" + encodeURIComponent(State.pid) +
        "/refs-fallback", { enabled: ovFb.checked }, 30000);
      if (r && r.ok) {
        toast(ovFb.checked ? "Shots without a start picture open on the cast portrait"
                           : "Shots without a start picture render from the prompt alone");
        State.refreshProject();
      } else {
        ovFb.checked = !ovFb.checked;
        toast((r && r.error) || "Could not save", true);
      }
    };
  }

  Router.register("overview", {
    polls: ["status", "eta", "shots"],
    subs: [],
    mount: function (el) {
      const page = this;
      page.el = el;
      page.render = function () { render(el); };
      page.subs.push(Bus.on("project", page.render));
      page.subs.push(Bus.on("status", page.render));
      page.subs.push(Bus.on("eta", page.render));
      page.subs.push(Bus.on("shots", page.render));
      const onProgress = function () { page.render(); };
      window.addEventListener("shell:progress", onProgress);
      page.subs.push(function () { window.removeEventListener("shell:progress", onProgress); });
      page.render();
    },
    unmount: function () {
      this.subs.forEach(function (off) { off(); });
      this.subs = [];
      if (this.el) { this.el.innerHTML = ""; this.el.__sig = null; }
    },
  });
})();
