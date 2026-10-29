/* shell.js — the one sidebar both app shells share, and the movie's progress.

   Two studios, one switch at the top of the sidebar:
   - Movie: one project at a time, as a thread of seven steps (Casting, Cast
     & World, Story, Shots, Render, Review, Film) under its Overview. Each
     step has a knot that shows how far it is: todo, part, done, need (it
     waits for you) or live (working now). The Overview shows the same steps
     and the one thing to do next (Shell.steps / Shell.next).
   - Studio: single clips and stills (Video Studio, Image Studio, Library).
   The system pages (Render queue, Worker PCs, Settings, Logs) and the AI
   writer sit in the dock at the bottom, behind the GPU meter.

   Two HTML shells serve the pages: the Movie pages (/project) and the Studio
   and system pages plus Casting (/studio-tools). Every entry is a plain link
   (/project#story, /studio-tools#casting): a page of this shell changes the
   hash and the shell's router takes it from there, a page of the other shell
   loads that shell with a fade. Old routes (#generate, #training) still work.

   Progress: the Movie shell feeds what it already loads (Shell.feed from
   state.js); the Studio shell loads the current project itself. Also owns
   the GPU meter (GET /api/gpu/health, announced as a "shell:gpu" window
   event), the step footer (#step-next) and the [data-tip] tooltips.

   Usage: Shell.mount("project" | "tools") once at boot, then
   Shell.setActive(page) from the shell's router. */
(function () {
  "use strict";

  const PAGES = {
    overview: { studio: "movie", shell: "project", label: "Overview", icon: "board" },
    casting: { studio: "movie", shell: "tools", label: "Casting", step: 1,
      next: "Generate looks for your characters, places and powers, and keep the best." },
    cast: { studio: "movie", shell: "project", label: "Cast & World", step: 2,
      next: "Give every character and place the look you picked." },
    story: { studio: "movie", shell: "project", label: "Story", step: 3,
      next: "Turn your idea and your cast into a script and a shot list." },
    shots: { studio: "movie", shell: "project", label: "Shots", step: 4,
      next: "Check the shot list and the video prompt of each shot." },
    render: { studio: "movie", shell: "project", label: "Render", step: 5,
      next: "Pick the video model and start production." },
    review: { studio: "movie", shell: "project", label: "Review", step: 6,
      next: "Approve the flagged shots or send them back." },
    film: { studio: "movie", shell: "project", label: "Film", step: 7,
      next: "Watch the cut and download it." },
    videostudio: { studio: "studio", shell: "tools", label: "Video Studio", icon: "clapper" },
    images: { studio: "studio", shell: "tools", label: "Image Studio", icon: "image" },
    videos: { studio: "studio", shell: "tools", label: "Library", icon: "grid" },
    queue: { studio: null, shell: "tools", label: "Render queue", icon: "layers" },
    workers: { studio: null, shell: "tools", label: "Worker PCs", icon: "server" },
    settings: { studio: null, shell: "tools", label: "Settings", icon: "sliders" },
    logs: { studio: null, shell: "tools", label: "Logs", icon: "list" },
  };
  const MOVIE = ["casting", "cast", "story", "shots", "render", "review", "film"];
  const STUDIO = ["videostudio", "images", "videos"];
  const ALIAS = { generate: "render", training: "casting" };
  const HOME = { project: "/project", tools: "/studio-tools" };
  const DEFAULT = { project: "overview", tools: "videostudio" };
  const STUDIO_HOME = { movie: "overview", studio: "videostudio" };
  const STUDIO_TIP = {
    movie: "Movie: one film, step by step, from the cast to the cut",
    studio: "Studio: single videos and images, no project needed",
  };
  const GPU_POLL_MS = 7000;
  const PROGRESS_POLL_MS = 30000;

  let here = null;          // this shell: "project" | "tools"
  let current = null;       // the page on screen
  let studio = "movie";     // the studio the sidebar shows
  const snap = { pid: null, project: null, bible: null, stats: null, director: null, datasets: null };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function icon(name) { return window.Icon ? window.Icon.get(name) : ""; }
  function store(k, v) { try { if (v == null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch (e) { /* private mode */ } }
  function load(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function list(x) {
    if (Array.isArray(x)) return x;
    if (x && typeof x === "object") return Object.keys(x).map(function (k) { return x[k]; });
    return [];
  }

  function canon(page) { page = String(page || ""); return ALIAS[page] || page; }
  function shellOf(page) { const p = PAGES[canon(page)]; return p ? p.shell : null; }
  function href(page) { page = canon(page); return HOME[PAGES[page].shell] + "#" + page; }
  function studioOf(page) { const p = PAGES[canon(page)]; return p ? p.studio : null; }

  /* ------------------------------------------------------------- progress */
  /* The movie's steps from what the pages already load: the project (story,
     status), its bible (cast), the shot stats and the Casting batches. */
  function steps() {
    const p = snap.project;
    const story = (p && p.story) || {};
    const st = snap.stats || {};
    const by = st.by_status || {};
    const total = st.total || 0;
    const running = !!(snap.director && snap.director.running);
    const gate = p && p.status === "gate_pending" ? (p.stage || "") : "";
    // the keyframes gate of LTX-2.5 / H3: opening frames to look at on the Shots page
    const frames = gate === "keyframes" && !!p.error;
    const chars = list((snap.bible || {}).characters).filter(function (c) { return c && c.enabled !== false; });
    const faced = chars.filter(function (c) { return (c.reference_images || []).length > 0; }).length;
    const batches = snap.datasets == null ? null : snap.datasets;
    const parts = ["treatment", "bible", "beats", "scenes", "shots"].filter(function (s) {
      return story[s] && Object.keys(story[s] || {}).length;
    }).length;
    const finished = (by.passed || 0) + (by.review || 0) + (by.failed || 0);
    const inflight = (by.rendering || 0) + (by.queued || 0) + (by.post || 0) + (by.qa || 0);
    const waiting = by.prompt || 0;
    const review = by.review || 0;
    const done = !!p && (p.status === "done" || p.stage === "film");
    const out = [];
    const add = function (page, state, detail, badge) {
      out.push({ page: page, n: PAGES[page].step, label: PAGES[page].label, state: state,
        detail: detail, badge: badge || "" });
    };
    add("casting", (batches || faced) ? "done" : "todo",
      batches ? batches + (batches === 1 ? " batch of looks" : " batches of looks") : "No looks generated yet");
    add("cast", !chars.length ? "todo" : (faced >= chars.length ? "done" : "part"),
      !chars.length ? "No characters yet"
        : chars.length + (chars.length === 1 ? " character" : " characters") +
          (faced < chars.length ? ", " + faced + " with a picked look" : ""));
    add("story", gate === "story" ? "need" : story.treatment ? (parts >= 4 ? "done" : "part") :
      (running && p && p.stage === "story" ? "live" : "todo"),
      gate === "story" ? (p.error ? "Waiting to be written" : "Waiting for your approval")
        : story.treatment ? (parts >= 4 ? "Written" : parts + " of 5 parts written") : "Not written yet");
    add("shots", (gate === "prompts" || gate === "shots" || frames || waiting) ? "need" : total ? "done" : "todo",
      waiting ? waiting + " waiting for a video prompt" : gate === "prompts" ? "Video prompts needed"
        : frames ? "Opening frames wait for you"
        : gate === "shots" ? (p.error ? "Scenes need their shots" : "Waiting for your approval")
        : total ? total + " shots" : "No shot list yet",
      waiting ? String(waiting) : "");
    const renderState = !total ? "todo" : (finished >= total ? "done"
      : (running && inflight) ? "live" : (finished || inflight) ? "part" : "todo");
    add("render", gate === "keyframes" && !frames ? "need" : renderState,
      gate === "keyframes" && !frames ? "Keyframes wait for your approval"
        : !total ? "Nothing to render yet" : finished + " of " + total + " rendered",
      renderState === "live" ? finished + "/" + total : "");
    add("review", review ? "need" : (total && finished >= total ? "done" : "todo"),
      review ? review + (review === 1 ? " shot waits" : " shots wait") + " for you"
        : (total && finished >= total ? "Nothing waiting" : "Shots appear here after they render"),
      review ? String(review) : "");
    add("film", done ? "done" : (running && p && p.stage === "assemble" ? "live" : "todo"),
      done ? "The cut is ready" : "Not cut yet");
    return out;
  }

  /* The one thing to do next, for the Overview. */
  function next() {
    const s = steps();
    const p = snap.project;
    const by = (snap.stats || {}).by_status || {};
    const running = !!(snap.director && snap.director.running);
    const find = function (page) { return s.filter(function (x) { return x.page === page; })[0]; };
    if (!p) return { page: "overview", title: "Start a project", text: "Give it a title and a one-paragraph idea.", action: "new" };
    if (p.status === "gate_pending") {
      const s = stopInfo(p, false);
      return { page: s.page, title: s.title, text: s.text, action: s.waits ? "open" : "approve" };
    }
    if (running) {
      return { page: "render", title: "Production is running",
        text: "The director works through the steps on its own. You can keep editing while it runs.", action: "watch" };
    }
    if (find("review").state === "need") {
      return { page: "review", title: (by.review || 0) + " shots wait for your review",
        text: "Approve them, or send them back to render again.", action: "open" };
    }
    if (find("casting").state === "todo") {
      return { page: "casting", title: "Create your cast's looks",
        text: "Generate photos of each character, place and power, then keep the best ones.", action: "open" };
    }
    if (find("cast").state === "todo") {
      return { page: "cast", title: "Add your cast",
        text: "Send a batch from Casting, or add a character by hand.", action: "open" };
    }
    if (find("cast").state === "part") {
      return { page: "cast", title: "Give every character a look",
        text: "Pick a look from a batch for each character, so every shot shows the same face.", action: "open" };
    }
    if (find("story").state !== "done") {
      return { page: "story", title: "Write the story",
        text: "The AI writer turns your idea and cast into a script and a shot list. Or press Start production and the director writes it.",
        action: "open" };
    }
    if (find("shots").state === "need") {
      return { page: "shots", title: "Shots need their video prompts",
        text: "Let the AI write them, or write them yourself.", action: "open" };
    }
    if (find("render").state !== "done") {
      return { page: "render", title: "Pick the video model and start",
        text: "Production renders every shot, checks it and cuts the film.", action: "start" };
    }
    if (find("film").state !== "done") {
      return { page: "film", title: "Cut the film", text: "Every shot is rendered: press Start production to cut them together.", action: "start" };
    }
    return { page: "film", title: "Your film is ready", text: "Watch it, download it, or retake any shot.", action: "open" };
  }

  /* What production that stopped waits for: the page to open, a title and
     the words (the director's reason when it gave one), or null while it
     runs or when nothing waits. `waits`: something to write, not to approve. */
  function stopInfo(p, running) {
    if (!p || running) return null;
    if (p.status === "error") {
      return { tone: "bad", page: "overview", title: "Production stopped with an error",
        text: p.error || "See the activity on the Overview." };
    }
    if (p.status !== "gate_pending") return null;
    const stage = p.stage || "";
    // a wait with a reason at the story, the shots or the prompts: something to write
    const waits = !!p.error && (stage === "story" || stage === "shots" || stage === "prompts");
    const frames = stage === "keyframes" && !!p.error;
    const where = stage === "prompts" || frames ? "shots" : stage === "shots" ? (waits ? "story" : "shots")
      : stage === "review" ? "review" : stage === "keyframes" ? "render" : stage === "cast" ? "cast" : "story";
    return { tone: "need", page: where, waits: waits,
      title: stage === "prompts" ? "Shots need their video prompts"
        : waits && stage === "story" ? "Write the story" : waits ? "Scenes need their shots"
        : frames ? "Opening frames to look at" : "Production waits for you",
      text: p.error || ("Check the " + (stage || "work") + ", then press Approve & continue.") };
  }

  function feed(data) {
    if (!data) return;
    Object.keys(data).forEach(function (k) { if (k in snap) snap[k] = data[k]; });
    paintThread();
    window.dispatchEvent(new CustomEvent("shell:progress"));
    watchStops();
  }

  /* -------------------------------------------------- production stops */
  /* Production that stops for you (a checkpoint, or an error) says so where
     it is seen: a dialog on the page, the tab's title, and a desktop
     notification while the window is in the background (once the browser
     allows them: askNotify, on Start production). Each stop is announced
     once; closing its dialog in one tab settles it for every tab. */
  let baseTitle = "";
  let stop = null;            // { key, info } of the stop on screen
  let stopOpen = false;

  function watchStops() {
    const p = snap.project;
    const s = snap.pid ? stopInfo(p, !!(snap.director && snap.director.running)) : null;
    if (!baseTitle) baseTitle = document.title;
    document.title = s ? (s.tone === "bad" ? "Stopped · " : "Waiting for you · ") + baseTitle : baseTitle;
    const key = s ? [snap.pid, p.status, p.stage || "", p.updated || ""].join("|") : "";
    if (!s) { stop = null; return; }
    if (stop && stop.key === key) return;
    stop = { key: key, info: s };
    if (load("kino.stop.seen") === key) return;
    tellDesktop(s, key);
    showStop();
  }

  function showStop() {
    if (!stop || stopOpen || !window.Dlg || document.visibilityState !== "visible") return;
    const cur = stop;
    if (load("kino.stop.seen") === cur.key) return;
    stopOpen = true;
    const page = PAGES[cur.info.page] || PAGES.overview;
    window.Dlg.confirm({ tone: cur.info.tone, title: cur.info.title, message: esc(cur.info.text),
      okLabel: "Open the " + page.label + " page", cancelLabel: "Later" }).then(function (go) {
      stopOpen = false;
      store("kino.stop.seen", cur.key);
      if (go) goTo(cur.info.page);
    });
  }

  function tellDesktop(s, key) {
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    if (document.visibilityState === "visible" && document.hasFocus()) return;   // the dialog shows it
    if (load("kino.stop.told") === key) return;                                 // another tab told already
    store("kino.stop.told", key);
    try {
      const n = new Notification("Kinoloom: " + s.title, { body: s.text, tag: "kinoloom-stop" });
      n.onclick = function () { window.focus(); n.close(); };
    } catch (e) { /* a browser without page notifications */ }
  }

  /* the browser's own question, once: asked from a click (Start production) */
  function askNotify() {
    try {
      if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
    } catch (e) { /* not supported */ }
  }

  function goTo(page) {
    page = canon(page);
    if (!PAGES[page]) return;
    if (PAGES[page].shell === here) location.hash = "#" + page;
    else leave(href(page));
  }

  async function loadDatasets() {
    if (!window.API) return;
    const r = await window.API.get("/api/training/datasets", 20000, true);
    if (r && Array.isArray(r.datasets)) feed({ datasets: r.datasets.length });
  }

  /* The Studio shell loads the current project itself (the Movie shell
     feeds from state.js). */
  async function loadProject() {
    if (here !== "tools" || !window.API) return;
    const pid = load("ws.lastProject");
    if (!pid) { feed({ pid: null, project: null }); return; }
    const r = await window.API.get("/api/projects/" + encodeURIComponent(pid), 20000, true);
    if (r && r.project) feed({ pid: pid, project: r.project, bible: r.bible, stats: r.stats, director: r.director });
  }

  /* ---------------------------------------------------------------- render */
  function renderSwitch() {
    const box = document.getElementById("studio-switch");
    if (!box) return;
    box.innerHTML =
      '<div class="studio-switch" role="tablist" aria-label="Studio">' +
        ["movie", "studio"].map(function (s) {
          const last = canon(load("kino.last." + s));
          const to = PAGES[last] && PAGES[last].studio === s ? last : STUDIO_HOME[s];
          return '<a class="ss-tab" role="tab" data-studio="' + s + '" data-page="' + to + '" data-shell="' +
            PAGES[to].shell + '" href="' + href(to) + '" data-tip="' + esc(STUDIO_TIP[s]) + '">' +
            icon(s === "movie" ? "film" : "clapper") + "<span>" + (s === "movie" ? "Movie" : "Studio") + "</span></a>";
        }).join("") +
        '<span class="ss-glider" aria-hidden="true"></span>' +
      "</div>";
  }

  function renderProject() {
    const slot = document.getElementById("proj-slot");
    if (!slot || slot.dataset.ready) return;
    slot.dataset.ready = "1";
    slot.innerHTML =
      '<div class="proj-switch">' +
        '<select id="proj-select" title="Current project" aria-label="Current project"></select>' +
        '<button class="btn small icon ghost" id="btn-new-project" type="button" data-tip="New project">' + icon("plus") + "</button>" +
        (here === "project"
          ? '<button class="btn small icon ghost" id="btn-del-project" type="button" data-tip="Delete this project (rendered files stay on disk)">' + icon("trash") + "</button>"
          : "") +
      "</div>";
    if (here !== "tools") return;
    // the Studio shell: pick the project the Movie pages open on
    const sel = document.getElementById("proj-select");
    sel.onchange = function () {
      store("ws.lastProject", sel.value || null);
      snap.project = null;
      loadProject();
      window.dispatchEvent(new CustomEvent("shell:project", { detail: sel.value }));
    };
    document.getElementById("btn-new-project").onclick = function () {
      store("kino.newProject", "1");
      leave("/project#overview");
    };
    if (window.API) window.API.get("/api/projects", 20000, true).then(function (r) {
      const ps = (r && r.projects) || [];
      let pid = load("ws.lastProject");
      if (!ps.some(function (p) { return p.id === pid; })) pid = ps.length ? ps[0].id : "";
      if (pid) store("ws.lastProject", pid);
      sel.innerHTML = ps.length ? ps.map(function (p) {
        return '<option value="' + esc(p.id) + '"' + (p.id === pid ? " selected" : "") + ">" + esc(p.title) + "</option>";
      }).join("") : '<option value="">No projects yet</option>';
    });
  }

  function navHtml() {
    if (studio === "studio") {
      return '<div class="nav-list">' + STUDIO.map(function (page) {
        const p = PAGES[page];
        return '<a class="nav-item' + (page === current ? " active" : "") + '" href="' + href(page) +
          '" data-page="' + page + '" data-shell="' + p.shell + '"><span class="nav-ico">' + icon(p.icon) +
          "</span><span>" + esc(p.label) + "</span></a>";
      }).join("") + "</div>";
    }
    const ov = PAGES.overview;
    return '<div class="thread">' +
      '<a class="th-item board' + (current === "overview" ? " active" : "") + '" href="' + href("overview") +
        '" data-page="overview" data-shell="' + ov.shell + '"><span class="nav-ico">' + icon(ov.icon) +
        '</span><span class="th-label">Overview</span><span></span></a>' +
      MOVIE.map(function (page) {
        const p = PAGES[page];
        return '<a class="th-item step' + (page === current ? " active" : "") + '" href="' + href(page) +
          '" data-page="' + page + '" data-shell="' + p.shell + '">' +
          '<span class="knot todo"></span>' +
          '<span class="th-label"><span class="th-n">' + p.step + "</span>" + esc(p.label) + "</span>" +
          '<span class="th-end"></span></a>';
      }).join("") +
    "</div>";
  }

  function renderNav() {
    const nav = document.getElementById("nav");
    if (!nav) return;
    nav.innerHTML = navHtml();
    paintThread();
    const sw = document.querySelector(".studio-switch");
    if (sw) {
      sw.dataset.on = studio;
      sw.querySelectorAll(".ss-tab").forEach(function (t) {
        const on = t.dataset.studio === studio;
        t.classList.toggle("on", on);
        t.setAttribute("aria-selected", on ? "true" : "false");
      });
    }
    const slot = document.getElementById("proj-slot");
    if (slot) slot.classList.toggle("hidden", studio !== "movie");
  }

  /* knots and badges only: the thread itself stays, so its lights animate */
  function paintThread() {
    const items = document.querySelectorAll("#nav .th-item.step");
    if (!items.length) return;
    const byPage = {};
    steps().forEach(function (s) { byPage[s.page] = s; });
    items.forEach(function (a) {
      const s = byPage[a.dataset.page];
      if (!s) return;
      const k = a.querySelector(".knot");
      if (k) k.className = "knot " + s.state;
      a.classList.toggle("lit", s.state === "done");
      a.dataset.tip = s.detail;
      const end = a.querySelector(".th-end");
      const html = s.badge ? '<span class="th-badge' + (s.state === "live" ? " live" : "") + '">' + esc(s.badge) + "</span>" : "";
      if (end && end.__html !== html) { end.innerHTML = html; end.__html = html; }
    });
  }

  /* ------------------------------------------------------------- the dock */
  function renderDock() {
    const dock = document.getElementById("dock");
    if (!dock) return;
    dock.innerHTML =
      '<button class="dock-btn" type="button" aria-haspopup="menu" aria-expanded="false" data-tip="Render queue, worker PCs, the AI writer, settings and logs">' +
        '<div id="side-gpu"><div class="gm-sub">GPU</div></div>' +
        '<div class="dock-row"><span class="dock-sys">' + icon("sliders") + "System</span>" +
          '<span class="chev">' + icon("chevron-up") + "</span></div>" +
      "</button>";
    const btn = dock.querySelector(".dock-btn");
    btn.onclick = function (e) {
      e.stopPropagation();
      if (dock.classList.contains("open")) closeDock(); else openDock();
    };
    document.addEventListener("click", function (e) { if (!dock.contains(e.target)) closeDock(); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeDock(); });
  }

  function closeDock() {
    const dock = document.getElementById("dock");
    if (!dock || !dock.classList.contains("open")) return;
    dock.classList.remove("open");
    const pop = dock.querySelector(".dock-pop");
    if (pop) pop.remove();
    dock.querySelector(".dock-btn").setAttribute("aria-expanded", "false");
  }

  function openDock() {
    const dock = document.getElementById("dock");
    if (!dock) return;
    dock.classList.add("open");
    dock.querySelector(".dock-btn").setAttribute("aria-expanded", "true");
    const item = function (page, hintId) {
      const p = PAGES[page];
      return '<a class="dock-item' + (page === current ? " active" : "") + '" role="menuitem" href="' + href(page) +
        '" data-page="' + page + '" data-shell="' + p.shell + '">' + icon(p.icon) + "<span>" + esc(p.label) +
        "</span>" + (hintId ? '<span class="hint" id="' + hintId + '"></span>' : "") + "</a>";
    };
    const pop = document.createElement("div");
    pop.className = "dock-pop";
    pop.setAttribute("role", "menu");
    pop.innerHTML =
      item("queue", "dock-h-queue") + item("workers", "dock-h-workers") +
      '<button class="dock-item" type="button" role="menuitem" id="dock-ai">' + icon("spark") +
        '<span>AI writer</span><span class="hint" id="dock-h-ai"></span></button>' +
      item("settings") + item("logs");
    dock.insertBefore(pop, dock.firstChild);
    pop.querySelector("#dock-ai").onclick = function () {
      closeDock();
      if (window.AiSettings) window.AiSettings.open();
    };
    if (!window.API) return;
    const hint = function (id, text, cls) {
      const el = document.getElementById(id);
      if (el) { el.textContent = text; el.className = "hint" + (cls ? " " + cls : ""); }
    };
    window.API.get("/api/queue", 15000, true).then(function (q) {
      const act = ((q && q.items) || []).filter(function (r) {
        return ["draft", "held", "queued", "running"].indexOf(r.status) >= 0;
      });
      const run = act.filter(function (r) { return r.status === "running"; }).length;
      hint("dock-h-queue", act.length ? act.length + " active" : "empty", run ? "on" : "");
    });
    window.API.get("/api/workers", 15000, true).then(function (w) {
      const ws = (w && w.workers) || [];
      const on = ws.filter(function (x) { return x.online; }).length;
      hint("dock-h-workers", ws.length ? on + " of " + ws.length + " online" : "none", on ? "on" : "");
    });
    window.API.get("/api/ai/config", 15000, true).then(function (c) {
      if (!c) return;
      hint("dock-h-ai", c.ready ? (c.model || "connected") : "not set up", c.ready ? "on" : "warn");
    });
  }

  /* ------------------------------------------------------------ navigation */
  function leave(url) {
    document.body.classList.add("shell-leaving");
    setTimeout(function () { location.href = url; }, 140);
  }

  function bindLinks() {
    // a link to the other shell fades the content out instead of cutting
    document.addEventListener("click", function (e) {
      const a = e.target.closest && e.target.closest("a[data-shell]");
      if (!a || e.defaultPrevented || e.ctrlKey || e.metaKey || e.shiftKey || e.button) return;
      if (a.dataset.studio) {
        store("kino.studio", a.dataset.studio);
        if (a.dataset.shell === here) {
          studio = a.dataset.studio;
          renderNav();
        }
      }
      if (a.dataset.shell === here) { closeDock(); return; }
      e.preventDefault();
      leave(a.getAttribute("href"));
    });
  }

  /* The page this shell should show for the current hash; a page of the
     other shell is handed over to it (an old bookmark, a typed URL). */
  function initialPage() {
    const raw = location.hash.replace(/^#/, "");
    const page = canon(raw);
    const owner = shellOf(page);
    if (owner && owner !== here) {
      location.replace(HOME[owner] + "#" + page);
      return null;
    }
    if (owner && raw !== page) history.replaceState(null, "", "#" + page);
    return owner ? page : DEFAULT[here];
  }

  function setActive(page) {
    page = canon(page);
    current = page;
    const s = studioOf(page);
    if (s) {
      studio = s;
      store("kino.studio", s);
      store("kino.last." + s, page);
    }
    renderSwitch();
    renderNav();
    const dock = document.getElementById("dock");
    if (dock) dock.classList.toggle("here", !s && !!PAGES[page]);
    const f = document.getElementById("step-next");
    if (f) {
      const html = footer(page);
      f.innerHTML = html;
      f.classList.toggle("hidden", !html);
    }
    if (s === "movie" && here === "tools" && !snap.project) loadProject();
  }

  /* the footer under a movie step: the step that comes after it */
  function footer(page) {
    const p = PAGES[canon(page)];
    if (!p || !p.step || p.step >= MOVIE.length) return "";
    const nxt = MOVIE[p.step];
    const n = PAGES[nxt];
    return '<a class="step-next" href="' + href(nxt) + '" data-page="' + nxt + '" data-shell="' + n.shell + '">' +
      "<div><div class=\"sn-k\">Next, step " + n.step + "</div>" +
      '<div class="sn-t">' + esc(n.label) + "</div>" +
      '<div class="sn-d">' + esc(n.next) + "</div></div>" +
      '<span class="sn-go">' + icon("arrow-right") + "</span></a>";
  }

  /* ------------------------------------------------------------- GPU meter */
  function shortName(n) {
    return String(n || "GPU").replace(/^NVIDIA\s+/i, "").replace(/^GeForce\s+/i, "");
  }
  function mib(v) { return parseFloat(String(v || "").replace(/[^\d.]/g, "")) || 0; }

  function paintGpu(gpus) {
    const box = document.getElementById("side-gpu");
    if (!box) return;
    const html = !gpus || !gpus.length ? '<div class="gm-sub">GPU not found</div>' : gpus.map(function (g) {
      const used = mib(g.mem_used), total = mib(g.mem_total) || 1;
      const t = Number(g.temp) || 0;
      const pct = Math.min(100, Math.round(used / total * 100));
      return '<div class="gpu-meter">' +
        '<div class="gm-top"><span class="gm-name">' + esc(shortName(g.name)) + "</span>" +
        '<span class="gm-temp' + (t >= 80 ? " hot" : t >= 68 ? " warm" : "") + '">' + t + "°C</span></div>" +
        '<div class="gm-bar"><div class="gm-fill" style="width:' + pct + '%"></div></div>' +
        '<div class="gm-sub">' + (used / 1024).toFixed(1) + " of " + (total / 1024).toFixed(1) +
        " GB in use</div></div>";
    }).join("");
    if (box.__html !== html) { box.innerHTML = html; box.__html = html; }   // unchanged: leave the DOM alone
  }

  async function pollGpu() {
    if (document.visibilityState === "visible" && window.API) {
      const r = await window.API.get("/api/gpu/health", 8000, true);
      if (r && Array.isArray(r.gpu)) {
        Shell.gpu = r.gpu;
        paintGpu(r.gpu);
        window.dispatchEvent(new CustomEvent("shell:gpu", { detail: r.gpu }));
      }
    }
    setTimeout(pollGpu, GPU_POLL_MS);
  }

  /* the Studio shell: the Movie pages' progress while one is on screen, and
     the current project on any page while production runs (to see it stop) */
  function pollProgress() {
    const running = !!(snap.director && snap.director.running);
    if (here === "tools" && (running || (document.visibilityState === "visible" && studio === "movie"))) loadProject();
    setTimeout(pollProgress, PROGRESS_POLL_MS);
  }

  /* -------------------------------------------------------------- tooltips */
  /* [data-tip] on any element: a styled tooltip after a short hover. */
  const tipRoots = new WeakSet();
  let tipEl = null;
  let tipTimer = null;
  function hideTip() {
    clearTimeout(tipTimer);
    if (tipEl) tipEl.className = "tip-box";
  }
  function bindTips(root) {
    root = root || document;
    if (tipRoots.has(root)) return;
    tipRoots.add(root);
    root.addEventListener("mouseover", function (e) {
      const t = e.target && e.target.closest ? e.target.closest("[data-tip]") : null;
      if (!t || !t.dataset.tip) { hideTip(); return; }
      clearTimeout(tipTimer);
      tipTimer = setTimeout(function () {
        if (!tipEl) {
          tipEl = document.createElement("div");
          tipEl.className = "tip-box";
          document.body.appendChild(tipEl);
        }
        tipEl.textContent = t.dataset.tip;
        tipEl.className = "tip-box show";
        const r = t.getBoundingClientRect();
        const tw = tipEl.offsetWidth;
        const th = tipEl.offsetHeight;
        let left = Math.round(r.left + r.width / 2 - tw / 2);
        left = Math.max(8, Math.min(left, window.innerWidth - tw - 8));
        let top = Math.round(r.top - th - 8);
        if (top < 8) top = Math.round(r.bottom + 8);
        tipEl.style.left = left + "px";
        tipEl.style.top = top + "px";
      }, 380);
    });
    root.addEventListener("mouseout", function (e) {
      const t = e.target && e.target.closest ? e.target.closest("[data-tip]") : null;
      const rel = e.relatedTarget;
      if (t && rel && rel.closest && t.contains(rel)) return;
      hideTip();
    });
    root.addEventListener("mousedown", hideTip);
    root.addEventListener("scroll", hideTip, true);
  }

  function mount(shell) {
    here = shell;
    const s = load("kino.studio");
    if (s === "movie" || s === "studio") studio = s;
    bindTips(document);
    bindLinks();
    renderSwitch();
    renderProject();
    renderNav();
    renderDock();
    // the incoming shell fades in (the leaving one faded out)
    document.body.classList.add("shell-entering");
    requestAnimationFrame(function () {
      requestAnimationFrame(function () { document.body.classList.remove("shell-entering"); });
    });
    pollGpu();
    loadDatasets();
    if (here === "tools") {
      loadProject();                          // whether production runs, on any page
      setTimeout(pollProgress, PROGRESS_POLL_MS);
    }
    // a stop seen while the tab was in the background shows when it comes back
    document.addEventListener("visibilitychange", showStop);
  }

  const Shell = {
    PAGES: PAGES, MOVIE: MOVIE, HOME: HOME, DEFAULT: DEFAULT, gpu: null,
    mount: mount, setActive: setActive, initialPage: initialPage, shellOf: shellOf, canon: canon,
    href: href, bindTips: bindTips, feed: feed, steps: steps, next: next, footer: footer,
    stopInfo: stopInfo, askNotify: askNotify, reloadDatasets: loadDatasets,
  };
  window.Shell = Shell;
})();
