/* film.js — assemble the film from passed scenes + browse films/drafts/
   scenes/raw outputs with player modal and download links. */
(function () {
  "use strict";

  function render(el) {
    if (!State.pid) { el.innerHTML = noProject(); return; }
    const shots = State.shots || [];
    const scenes = {};
    shots.forEach(function (r) {
      const s = (scenes[r.scene || "?"] = scenes[r.scene || "?"] ||
        { passed: 0, total: 0, review: 0 });
      s.total += 1;
      if (r.status === "passed") s.passed += 1;
      if (r.status === "review") s.review += 1;
    });
    const ids = Object.keys(scenes).sort();
    const defaultTitle = ((State.project && State.project.title) || "movie")
      .replace(/[^\w\-]+/g, "_") || "movie";
    const ready = ids.filter(function (s) { return scenes[s].passed; }).length;
    el.innerHTML =
      '<header class="page-intro">' +
        '<div class="pi-text"><h1>Film</h1>' +
          "<p>Cut the passed shots together, in story order. Shots of one seamless take join frame for frame. " +
          "You get a quick draft and the full master.</p></div>" +
      "</header>" +
      '<section class="card cut-card">' +
        '<div class="card-head"><h2>The cut</h2>' +
          '<span class="muted small">' + (ids.length ? ready + " of " + ids.length + " scenes have passed shots" : "") + "</span></div>" +
        (ids.length
          ? '<div class="cut-list">' + ids.map(function (s) {
              const sc = scenes[s];
              const pct = sc.total ? Math.round((sc.passed / sc.total) * 100) : 0;
              return '<label class="cut-row' + (sc.passed ? "" : " dim") + '">' +
                '<input type="checkbox" class="film-scene" value="' + esc(s) + '"' + (sc.passed ? " checked" : "") + ">" +
                '<span class="cut-id">' + esc(s) + "</span>" +
                '<span class="cut-bar"><i style="width:' + pct + '%"></i></span>' +
                '<span class="cut-n">' + sc.passed + " of " + sc.total + " passed" +
                  (sc.review ? ', <span class="st-amber">' + sc.review + " to review</span>" : "") + "</span>" +
              "</label>";
            }).join("") + "</div>"
          : '<p class="muted">No shots yet. Scenes show up here once their shots render and pass.</p>') +
        '<div class="cut-go">' +
          '<label class="gen-field"><span>File name</span>' +
            '<input id="film-title" placeholder="movie" value="' + esc(defaultTitle) + '"></label>' +
          '<button class="btn primary" type="button" id="film-assemble"' + (ready ? "" : " disabled") + ">" +
            Icon.get("layers") + "Assemble the film</button>" +
          '<div id="film-result" class="muted small"></div>' +
        "</div>" +
      "</section>" +
      '<section class="card">' +
        '<div class="card-head"><h2>Films and renders</h2>' +
          '<span class="spacer"></span>' +
          '<button class="btn small ghost icon" type="button" id="film-refresh" data-tip="Refresh">' + Icon.get("refresh") + "</button></div>" +
        '<div id="film-outputs" class="muted small">Loading…</div>' +
      "</section>";
    bind(el);
    loadOutputs(el);
  }

  function bind(el) {
    const btn = el.querySelector("#film-assemble");
    if (btn) btn.onclick = async function () {
      const scenes = [];
      el.querySelectorAll(".film-scene").forEach(function (c) {
        if (c.checked) scenes.push(c.value);
      });
      if (!scenes.length) { toast("Tick at least one scene", true); return; }
      const title = (el.querySelector("#film-title").value || "movie").trim();
      btn.disabled = true;
      toast("Assembling " + scenes.length + (scenes.length === 1 ? " scene" : " scenes") + ". This can take a while…");
      // this project's passed shots in story order (seamless takes rejoin
      // frame for frame), never another project's drafts
      const r = await post("/api/assembly/run",
        { scenes: scenes, title: title, project: State.pid }, 1800000);
      btn.disabled = false;
      const out = el.querySelector("#film-result");
      if (r) {
        if (r.ok) {
          out.textContent = "Assembled. The master is " + (r.master || "below") + ".";
          toast("Film assembled");
        } else {
          out.textContent = "Could not assemble it: " + (r.error || "unknown error");
          toast("Could not assemble the film: " + (r.error || "unknown error"), true);
        }
      }
      loadOutputs(el);
    };
    const rf = el.querySelector("#film-refresh");
    if (rf) rf.onclick = function () { loadOutputs(el); };
  }

  async function loadOutputs(el) {
    const box = el.querySelector("#film-outputs");
    if (!box) return;
    const r = await get("/api/outputs", 30000);
    if (!r) { box.textContent = "Could not list the files."; return; }
    const cats = [
      ["films", "Films"], ["drafts", "Drafts"], ["scenes", "Scenes"], ["raw", "Raw renders"],
    ];
    const shown = cats.filter(function (c) { return (r[c[0]] || []).length; });
    if (!shown.length) {
      box.innerHTML = '<p class="muted">Nothing yet. Assembled films and the renders behind them show up here.</p>';
      return;
    }
    box.innerHTML = shown.map(function (c) {
      const items = r[c[0]] || [];
      return '<div class="out-cat">' +
        '<div class="out-cat-head">' + esc(c[1]) + '<span class="sec-count">' + items.length + "</span></div>" +
        '<div class="out-grid">' + items.map(function (f) {
          const url = URLS.video(c[0], f.name);
          return '<div class="out-card">' +
            '<button class="out-frame" type="button" data-play="' + esc(url) + '" data-title="' + esc(f.name) + '" aria-label="Play ' + esc(f.name) + '">' +
              '<video src="' + esc(url) + '" preload="metadata" muted playsinline></video>' +
              '<span class="out-play">' + Icon.get("play") + "</span></button>" +
            '<div class="out-meta"><div class="out-name" title="' + esc(f.name) + '">' +
              esc(f.name) + "</div>" +
              '<div class="muted small">' + esc([f.size_mb != null ? f.size_mb + " MB" : "", f.duration || "", f.date || ""]
                .filter(Boolean).join(", ")) + "</div></div>" +
            '<a class="btn small ghost icon" href="' + esc(url) + '" download data-tip="Download">' + Icon.get("download") + "</a>" +
          "</div>";
        }).join("") + "</div>" +
      "</div>";
    }).join("");
    box.querySelectorAll("[data-play]").forEach(function (b) {
      b.onclick = function () { playVideo(b.dataset.play, b.dataset.title); };
    });
  }

  Router.register("film", {
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
      this.subs.forEach(function (off) { off(); });
      this.subs = [];
      if (this.el) this.el.innerHTML = "";
    },
  });
})();
