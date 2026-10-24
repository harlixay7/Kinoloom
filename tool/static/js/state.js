/* state.js — single source of truth for the workspace: current project,
   cache, event bus, hash router, and pollers (stopped when not needed). */
(function () {
  "use strict";

  /* ------------------------------------------------------------ event bus */
  const Bus = {
    map: {},
    on: function (evt, fn) {
      (Bus.map[evt] = Bus.map[evt] || []).push(fn);
      return function () { Bus.off(evt, fn); };
    },
    off: function (evt, fn) {
      const a = Bus.map[evt] || [];
      const i = a.indexOf(fn);
      if (i >= 0) a.splice(i, 1);
    },
    emit: function (evt, data) {
      (Bus.map[evt] || []).slice().forEach(function (fn) {
        try { fn(data); } catch (e) { console.error("bus handler failed", e); }
      });
    },
  };

  /* ----------------------------------------------------------------- state */
  const _last = {};
  function changed(key, data) {
    /* only emit events when the data ACTUALLY changed — the 3-5s pollers
       were re-rendering every page on every tick (constant flicker) */
    const sig = JSON.stringify(data);
    if (_last[key] === sig) return false;
    _last[key] = sig;
    return true;
  }

  const State = {
    pid: null,
    projects: [],
    project: null,
    bible: null,
    stats: {},
    eta: null,
    gpu: [],
    events: [],
    orch: null,
    director: null,
    shots: [],
    loras: null,

    async loadProjects() {
      const r = await API.get("/api/projects", 30000);
      if (!r) return false;
      State.projects = Array.isArray(r.projects) ? r.projects : [];
      return true;
    },

    async setProject(pid) {
      State.pid = pid;
      try { localStorage.setItem("ws.lastProject", pid); } catch (e) { /* private mode */ }
      await State.refreshProject();
      await State.refreshShots();
      await State.refreshEta();
    },

    async refreshProject() {
      if (!State.pid) return null;
      const r = await API.get("/api/projects/" + encodeURIComponent(State.pid), 30000, true);
      if (!r) return null;
      State.project = r.project || null;
      State.bible = r.bible || null;
      State.stats = r.stats || {};
      State.director = r.director || {};
      // The cast/world pages render from State.bible, so a BIBLE change must
      // re-render too — deleting a character changed the bible while the
      // project record stayed identical, so the old `changed("project")`
      // check never fired and the deleted card stayed on screen.
      const bibleSig = Object.keys((State.bible && State.bible.characters) || {}).sort().join(",")
        + "|" + Object.keys((State.bible && State.bible.locations) || {}).sort().join(",");
      const bibleChanged = bibleSig !== (State._bibleSig || "");
      State._bibleSig = bibleSig;
      if (changed("project", State.project) || bibleChanged) {
        Bus.emit("project", State.project);
      }
      if (changed("director", State.director)) Bus.emit("director", State.director);
      return r;
    },

    async refreshStatus() {
      const r = await API.get("/api/status", 15000, true);
      if (!r) return false;
      State.gpu = Array.isArray(r.gpu) ? r.gpu : [];
      State.events = Array.isArray(r.events) ? r.events : [];
      State.orch = r.orchestrator || null;
      if (changed("status", r)) Bus.emit("status", r);
      if (State.pid) State.refreshProject();
      return true;
    },

    async refreshShots() {
      if (!State.pid) return;
      const r = await API.get("/api/projects/" + encodeURIComponent(State.pid) + "/shots", 20000, true);
      if (!r) return;
      State.shots = Array.isArray(r.shots) ? r.shots : [];
      if (r.stats) State.stats = r.stats;
      if (changed("shots", State.shots)) Bus.emit("shots", State.shots);
    },

    async refreshEta() {
      if (!State.pid) return;
      const r = await API.get("/api/projects/" + encodeURIComponent(State.pid) + "/eta", 15000, true);
      if (!r) return;
      State.eta = r;
      if (changed("eta", r)) Bus.emit("eta", r);
    },

    async refreshLoras(force) {
      if (force) State.loras = null;
      if (State.loras) return State.loras;
      const r = await API.get("/api/lora/video", 20000);
      State.loras = (r && Array.isArray(r.entries)) ? r.entries : [];
      Bus.emit("loras", State.loras);
      return State.loras;
    },
  };

  /* ---------------------------------------------------------------- router */
  const Router = {
    pages: {},
    current: null,
    register: function (name, def) { Router.pages[name] = def; },
    go: function (name) {
      const def = Router.pages[name] || Router.pages.overview;
      if (Router.current && Router.current.name === name) return;
      if (Router.current) {
        if (Router.current.def && Router.current.def.unmount) {
          try { Router.current.def.unmount(); } catch (e) { console.error(e); }
        }
      }
      const el = document.getElementById("page-" + name) || document.getElementById("page-overview");
      document.querySelectorAll("#page-root > .page").forEach(function (p) {
        p.classList.toggle("active", p === el);
      });
      Shell.setActive(Router.pages[name] ? name : "overview");
      Router.current = { name: name, def: def };
      if (def && def.mount) {
        try { def.mount(el); } catch (e) { console.error("mount failed", name, e); }
      }
      Poll.recalc();
    },
  };

  /* -------------------------------------------------------------- pollers */
  const Poll = {
    timers: { status: null, shots: null, eta: null, project: null },
    recalc: function () {
      const def = Router.current && Router.current.def;
      const polls = (def && def.polls) || [];
      const need = { status: false, shots: false, eta: true };
      polls.forEach(function (p) { need[p] = true; });
      // while the director runs, keep the transport current on every page
      // (the status poll already refreshes the project where it runs)
      const running = !!(State.director && State.director.running);
      Poll.sync("status", need.status, 5000, function () { State.refreshStatus(); });
      Poll.sync("shots", need.shots, 5000, function () { State.refreshShots(); });
      Poll.sync("eta", need.eta && !!State.pid, 30000, function () { State.refreshEta(); });
      Poll.sync("project", running && !need.status && !!State.pid, 10000, function () { State.refreshProject(); });
    },
    sync: function (key, on, ms, fn) {
      if (on && !Poll.timers[key]) {
        Poll.timers[key] = setInterval(fn, ms);
        fn();
      } else if (!on && Poll.timers[key]) {
        clearInterval(Poll.timers[key]);
        Poll.timers[key] = null;
      }
    },
    stop: function () {
      Object.keys(Poll.timers).forEach(function (k) {
        if (Poll.timers[k]) clearInterval(Poll.timers[k]);
        Poll.timers[k] = null;
      });
    },
  };

  /* ------------------------------------------------------------- top shell */
  function modeLabel(mode) {
    return { autopilot: "AUTOPILOT", checkpoints: "CHECKPOINTS", manual: "MANUAL" }[mode] || String(mode || "—").toUpperCase();
  }

  /* the director transport: one primary button whose verb follows the
     project (run / resume past a gate), and Stop only while it runs */
  function transport(p) {
    const running = !!(State.director && State.director.running);
    const gate = !!p && p.status === "gate_pending";
    const stopped = !!p && (p.status === "stopped" || p.stage === "paused");
    return {
      running: running,
      verb: gate ? "resume" : "run",
      label: gate ? "Approve & continue" : stopped ? "Resume" : "Run",
      tip: gate ? "Approve the " + (p.stage || "pending") + " gate and continue"
        : stopped ? "Continue the director from where it stopped"
        : "Run the director through the project's stages",
    };
  }

  function renderTop() {
    const p = State.project;
    const titleEl = document.getElementById("tb-title");
    const modeEl = document.getElementById("tb-mode");
    const statusEl = document.getElementById("tb-status");
    const etaEl = document.getElementById("tb-eta");
    if (!titleEl) return;
    titleEl.textContent = p ? p.title : "No project";
    if (modeEl) {
      modeEl.textContent = p ? modeLabel(p.mode) : "—";
      modeEl.className = "badge mode-" + (p ? String(p.mode) : "none");
      modeEl.classList.toggle("hidden", !p);
    }
    const t = transport(p);
    if (statusEl) {
      const st = p ? (p.status || "—") : "none";
      const stage = p && p.stage ? p.stage : "";
      statusEl.textContent = st.replace(/_/g, " ") + (stage && stage !== st ? " · " + stage : "");
      statusEl.className = "pill " + (t.running ? "run" : window.statusClass(st));
      statusEl.classList.toggle("hidden", !p);
    }
    if (etaEl) {
      const e = State.eta;
      etaEl.textContent = e && e.remaining ? fmt.min(e.eta_min) + " left · " + e.remaining + " shots" : "";
    }
    const run = document.getElementById("btn-run");
    if (run) {
      run.dataset.verb = t.verb;
      run.dataset.tip = t.tip;
      run.querySelector(".lbl").textContent = t.label;
      run.classList.toggle("hidden", t.running);
      run.disabled = !p;
    }
    const stop = document.getElementById("btn-stop");
    if (stop) stop.classList.toggle("hidden", !t.running);
  }

  function renderSwitcher() {
    const sel = document.getElementById("proj-select");
    if (!sel) return;
    sel.innerHTML = State.projects.length
      ? State.projects.map(function (p) {
          return '<option value="' + esc(p.id) + '"' + (p.id === State.pid ? " selected" : "") +
            ">" + esc(p.title) + "</option>";
        }).join("")
      : '<option value="">no projects</option>';
  }

  function bindShell() {
    /* hash routing: the shared sidebar (js/shell.js) links to #page */
    Shell.mount("project");
    window.addEventListener("hashchange", function () {
      const page = location.hash.replace(/^#/, "") || "overview";
      if (Shell.shellOf(page) === "tools") { location.href = Shell.HOME.tools + "#" + page; return; }
      Router.go(page);
    });

    /* project switcher + new project modal */
    const sel = document.getElementById("proj-select");
    if (sel) sel.onchange = function () {
      if (sel.value) State.setProject(sel.value);
    };
    const btnNew = document.getElementById("btn-new-project");
    if (btnNew) btnNew.onclick = function () {
      const m = document.getElementById("np-modal");
      if (m) { m.classList.remove("hidden"); document.getElementById("np-title").focus(); }
    };
    const btnDel = document.getElementById("btn-del-project");
    if (btnDel) btnDel.onclick = function () {
      if (!State.pid) { toast("no project selected", true); return; }
      const v = openModal(
        '<p>Delete project <b>' + esc((State.project && State.project.title) || State.pid) +
        "</b>? Its shots are removed from the queue; rendered clips and files stay on disk.</p>" +
        '<div class="ctl-row"><button class="btn" type="button" id="npd-cancel">Cancel</button>' +
        '<button class="btn warn" type="button" id="npd-go">Delete project</button></div>',
        { title: "Delete project" });
      if (!v) return;
      v.querySelector("#npd-cancel").onclick = function () { closeModal(v); };
      v.querySelector("#npd-go").onclick = async function () {
        closeModal(v);
        const r = await API.post("/api/projects/" +
          encodeURIComponent(State.pid) + "/delete", {}, 60000);
        if (r && r.ok) {
          toast("project deleted");
          State.pid = null;
          State.project = null;
          try { localStorage.removeItem("ws.lastProject"); } catch (e) { /* ignore */ }
          await State.loadProjects();
          Bus.emit("project", null);
          renderSwitcher();
          Router.go("overview");
        } else {
          toast("delete failed: " + ((r && r.error) || "?"), true);
        }
      };
    };
    ["np-close", "np-cancel"].forEach(function (id) {
      const b = document.getElementById(id);
      if (b) b.onclick = function () { document.getElementById("np-modal").classList.add("hidden"); };
    });
    const npModal = document.getElementById("np-modal");
    if (npModal) npModal.addEventListener("click", function (e) {
      if (e.target === npModal) npModal.classList.add("hidden");
    });
    const npCreate = document.getElementById("np-create");
    if (npCreate) npCreate.onclick = async function () {
      const title = document.getElementById("np-title").value.trim();
      if (!title) { toast("title required", true); return; }
      const r = await post("/api/projects", {
        title: title,
        idea: document.getElementById("np-idea").value,
        genre: document.getElementById("np-genre").value,
        tone: document.getElementById("np-tone").value,
        duration_target: Number(document.getElementById("np-duration").value) || 60,
        mode: document.getElementById("np-mode").value,
      }, 30000);
      if (!r || !r.ok) { if (r) toast(r.error || "create failed", true); return; }
      npModal.classList.add("hidden");
      State.projects.unshift(r.project);
      renderSwitcher();
      await State.setProject(r.project.id);
      toast("project created — " + r.project.title);
    };

    /* director transport (renderTop sets the run button's verb) */
    const DONE = { run: "director running", resume: "continuing", stop: "director stopped" };
    ["btn-run", "btn-stop"].forEach(function (id) {
      const b = document.getElementById(id);
      if (!b) return;
      b.onclick = async function () {
        if (!State.pid) { toast("no project selected", true); return; }
        const verb = id === "btn-stop" ? "stop" : (b.dataset.verb || "run");
        b.disabled = true;
        const r = await post("/api/projects/" + encodeURIComponent(State.pid) + "/" + verb, {}, 30000);
        b.disabled = false;
        if (r) toast(r.ok ? DONE[verb] : (r.error || "failed"), !r.ok);
        State.refreshProject();
      };
    });
  }

  /* ------------------------------------------------------------------ boot */
  function boot() {
    /* materialize static [data-icon] placeholders (nav, topbar, brand) */
    document.querySelectorAll("[data-icon]").forEach(function (el) {
      el.innerHTML = window.Icon ? Icon.get(el.dataset.icon) : el.innerHTML;
    });
    bindShell();
    Bus.on("project", function () { renderTop(); renderSwitcher(); });
    Bus.on("director", function () { renderTop(); Poll.recalc(); });
    Bus.on("status", renderTop);
    Bus.on("eta", renderTop);
    Bus.on("shots:changed", function () { State.refreshShots(); });
    renderTop();
    const first = Shell.initialPage();
    if (!first) return;                       // a Studio Tools page: handed over
    State.loadProjects().then(async function (ok) {
      if (!ok) toast("backend unreachable — is the server running?", true);
      renderSwitcher();
      let pid = null;
      try { pid = localStorage.getItem("ws.lastProject"); } catch (e) { /* ignore */ }
      const exists = State.projects.some(function (p) { return p.id === pid; });
      if (!exists) pid = State.projects.length ? State.projects[0].id : null;
      if (pid) await State.setProject(pid);
      Router.go(first);
      Poll.recalc();
    });
  }

  window.Bus = Bus;
  window.State = State;
  window.Router = Router;
  window.Poll = Poll;
  window.Boot = boot;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
