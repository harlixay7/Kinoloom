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
    el.innerHTML =
      '<section class="card">' +
        '<div class="card-head"><h2>Assemble</h2></div>' +
        '<div class="ctl-row">' +
          '<input id="film-title" placeholder="movie title" value="' + esc(defaultTitle) + '">' +
          '<button class="btn primary" type="button" id="film-assemble">' + Icon.get("layers") + "Assemble draft + master</button>" +
        "</div>" +
        '<div id="film-result" class="muted small" style="margin-top:8px"></div>' +
        '<div class="table-wrap" style="margin-top:12px"><table class="table"><thead><tr>' +
          "<th>include</th><th>scene</th><th>passed</th><th>total</th><th>in review</th></tr></thead><tbody>" +
          (ids.length ? ids.map(function (s) {
            const sc = scenes[s];
            return "<tr><td><input type=\"checkbox\" class=\"film-scene\" value=\"" + esc(s) + '"' +
              (sc.passed ? " checked" : "") + "></td>" +
              "<td>" + esc(s) + "</td>" +
              '<td class="st-green">' + sc.passed + "</td>" +
              "<td>" + sc.total + "</td>" +
              '<td class="' + (sc.review ? "st-red" : "") + '">' + sc.review + "</td></tr>";
          }).join("")
            : '<tr><td colspan="5" class="muted small">no shots — assemble after renders pass</td></tr>') +
        "</tbody></table></div>" +
      "</section>" +
      '<section class="card">' +
        '<div class="card-head"><h2>Outputs</h2>' +
          '<span class="spacer"></span>' +
          '<button class="btn small" type="button" id="film-refresh">' + Icon.get("refresh") + "Refresh</button></div>" +
        '<div id="film-outputs" class="muted small">loading…</div>' +
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
      if (!scenes.length) { toast("pick at least one scene", true); return; }
      const title = (el.querySelector("#film-title").value || "movie").trim();
      btn.disabled = true;
      toast("assembling " + scenes.length + " scene(s) — this can take a while…");
      // this project's passed shots in story order (seamless takes rejoin
      // frame for frame), never another project's drafts
      const r = await post("/api/assembly/run",
        { scenes: scenes, title: title, project: State.pid }, 1800000);
      btn.disabled = false;
      const out = el.querySelector("#film-result");
      if (r) {
        if (r.ok) {
          out.textContent = "done — master: " + (r.master || "");
          toast("assembly complete");
        } else {
          out.textContent = "failed: " + (r.error || "?");
          toast("assembly failed: " + (r.error || "?"), true);
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
    if (!r) { box.textContent = "outputs unavailable"; return; }
    const cats = [
      ["films", "Films"], ["drafts", "Drafts"], ["scenes", "Scenes"], ["raw", "Raw renders"],
    ];
    box.innerHTML = cats.map(function (c) {
      const items = r[c[0]] || [];
      return '<div class="out-cat">' +
        '<div class="out-cat-head">' + esc(c[1]) + " · " + items.length + "</div>" +
        (items.length
          ? '<div class="out-grid">' + items.map(function (f) {
              const url = URLS.video(c[0], f.name);
              return '<div class="out-card">' +
                '<video src="' + esc(url) + '" preload="metadata" muted playsinline title="click to play"></video>' +
                '<div class="out-meta"><div class="out-name" title="' + esc(f.name) + '">' +
                  esc(f.name) + "</div>" +
                  '<div class="muted small">' + esc(f.size_mb != null ? f.size_mb + " MB" : "") +
                    (f.duration ? " · " + esc(f.duration) : "") +
                    (f.date ? " · " + esc(f.date) : "") + "</div></div>" +
                '<div class="out-acts">' +
                  '<button class="btn small" type="button" data-play="' + esc(url) + '" ' +
                    'data-title="' + esc(f.name) + '">' + Icon.get("play") + "Play</button>" +
                  '<a class="btn small" href="' + esc(url) + '" download>' + Icon.get("download") + "Download</a>" +
                "</div></div>";
            }).join("") + "</div>"
          : '<div class="muted small" style="margin:4px 0 14px">none</div>') +
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
