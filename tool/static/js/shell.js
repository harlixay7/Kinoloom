/* shell.js — the one sidebar both app shells share.

   The Workspace (/project) holds the movie, per project; Studio Tools
   (/studio-tools) holds the studios and the system pages. Every nav entry is
   a plain link (/project#story, /studio-tools#videostudio): a page of this
   shell changes the hash and the shell's own router takes it from there, a
   page of the other shell loads that shell. The sidebar is identical in
   both, so moving between them reads as one app. Also owns the sidebar GPU
   meter (GET /api/gpu/health) and announces each reading as a "shell:gpu"
   window event for pages that show it too.

   Usage: Shell.mount("project" | "tools") once at boot, then
   Shell.setActive(page) from the shell's router. */
(function () {
  "use strict";

  const NAV = [
    { label: "Project", shell: "project", items: [
      { page: "overview", label: "Overview", icon: "gauge" },
      { page: "story", label: "Story", icon: "pen" },
      { page: "cast", label: "Cast & World", icon: "users" },
      { page: "shots", label: "Shots", icon: "film" },
      { page: "generate", label: "Generate", icon: "play" },
      { page: "review", label: "Review", icon: "shield-check" },
      { page: "film", label: "Film", icon: "clapper-play" },
    ] },
    { label: "Studio", shell: "tools", items: [
      { page: "videostudio", label: "Video Studio", icon: "clapper" },
      { page: "images", label: "Images", icon: "image" },
      { page: "training", label: "Training", icon: "cpu" },
      { page: "videos", label: "Library", icon: "grid" },
    ] },
    { label: "System", shell: "tools", items: [
      { page: "queue", label: "Queue", icon: "layers" },
      { page: "workers", label: "Workers", icon: "server" },
      { page: "settings", label: "Settings", icon: "sliders" },
      { page: "logs", label: "Logs", icon: "list" },
    ] },
  ];
  const HOME = { project: "/project", tools: "/studio-tools" };
  const DEFAULT = { project: "overview", tools: "videostudio" };
  const GPU_POLL_MS = 7000;

  let here = null;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function icon(name) { return window.Icon ? window.Icon.get(name) : ""; }

  function shellOf(page) {
    for (const g of NAV) {
      if (g.items.some(function (it) { return it.page === page; })) return g.shell;
    }
    return null;
  }

  function render(nav) {
    nav.innerHTML = NAV.map(function (g) {
      return '<div class="nav-group"><div class="nav-label">' + esc(g.label) + "</div>" +
        g.items.map(function (it) {
          return '<a class="nav-item" href="' + HOME[g.shell] + "#" + it.page + '" data-page="' +
            it.page + '" data-shell="' + g.shell + '"><span class="nav-ico">' + icon(it.icon) +
            "</span><span>" + esc(it.label) + "</span></a>";
        }).join("") + "</div>";
    }).join("");
    // leaving for the other shell: fade the content out instead of cutting
    nav.addEventListener("click", function (e) {
      const a = e.target.closest("a.nav-item");
      if (!a || a.dataset.shell === here || e.ctrlKey || e.metaKey || e.shiftKey || e.button) return;
      e.preventDefault();
      document.body.classList.add("shell-leaving");
      setTimeout(function () { location.href = a.getAttribute("href"); }, 120);
    });
  }

  /* The page this shell should show for the current hash; a page of the
     other shell is handed over to it (an old bookmark, a typed URL). */
  function initialPage() {
    const page = location.hash.replace(/^#/, "");
    const owner = shellOf(page);
    if (owner && owner !== here) {
      location.replace(HOME[owner] + "#" + page);
      return null;
    }
    return owner ? page : DEFAULT[here];
  }

  function setActive(page) {
    document.querySelectorAll("#nav .nav-item").forEach(function (a) {
      a.classList.toggle("active", a.dataset.page === page && a.dataset.shell === here);
    });
  }

  /* ------------------------------------------------------------- GPU meter */
  function shortName(n) {
    return String(n || "GPU").replace(/^NVIDIA\s+/i, "").replace(/^GeForce\s+/i, "");
  }
  function mib(v) { return parseFloat(String(v || "").replace(/[^\d.]/g, "")) || 0; }

  function paintGpu(gpus) {
    const box = document.getElementById("side-gpu");
    if (!box) return;
    const html = !gpus || !gpus.length ? '<div class="gm-sub">GPU —</div>' : gpus.map(function (g) {
      const used = mib(g.mem_used), total = mib(g.mem_total) || 1;
      const t = Number(g.temp) || 0;
      const pct = Math.min(100, Math.round(used / total * 100));
      return '<div class="gpu-meter" title="' + esc(g.name) + " · " + esc(g.mem_used) + " / " +
        esc(g.mem_total) + '">' +
        '<div class="gm-top"><span class="gm-name">' + esc(shortName(g.name)) + "</span>" +
        '<span class="gm-temp' + (t >= 80 ? " hot" : t >= 68 ? " warm" : "") + '">' + t + "°</span></div>" +
        '<div class="gm-bar"><div class="gm-fill" style="width:' + pct + '%"></div></div>' +
        '<div class="gm-sub">' + (used / 1024).toFixed(1) + " / " + (total / 1024).toFixed(1) +
        " GB · " + esc(String(g.util || "").replace(/\s+/g, "")) + "</div></div>";
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
      }, 350);
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
    bindTips(document);
    const nav = document.getElementById("nav");
    if (nav) render(nav);
    // the incoming shell fades in (the leaving one faded out)
    document.body.classList.add("shell-entering");
    requestAnimationFrame(function () {
      requestAnimationFrame(function () { document.body.classList.remove("shell-entering"); });
    });
    pollGpu();
  }

  const Shell = {
    NAV: NAV, HOME: HOME, DEFAULT: DEFAULT, gpu: null,
    mount: mount, setActive: setActive, initialPage: initialPage, shellOf: shellOf,
    bindTips: bindTips,
  };
  window.Shell = Shell;
})();
