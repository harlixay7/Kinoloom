/* batch.js — Batch reference image generation (workspace shell).
   One config (model/LoRAs/steps/CFG/hires) -> generate cinematic start
   images for every selected shot -> auto-assign the sharpest result to
   each clip. Includes the "disable roster portrait fallback" toggle. */
(function () {
  "use strict";

  var cur = null; /* { veil, timer, selected: {} } */
  var selCache = {}; /* scene -> { shot_id: bool } */

  function shotKey(s) { return s.scene + "::" + s.shot_id; }

  function openBatch() {
    if (!State.pid) { toast("no project selected", true); return; }
    if (cur) closeBatch();
    // options + the online fleet (for the "Run on" picker) in one go
    Promise.all([
      get("/api/projects/" + encodeURIComponent(State.pid) + "/refbatch/options",
          20000),
      get("/api/workers", 20000).catch(function () { return null; }),
    ]).then(function (res) {
      var r = res[0], fq = res[1];
      if (!r || !r.shots) {
        toast("batch options unavailable", true);
        return;
      }
      r._workers = ((fq && fq.workers) || []).filter(function (w) {
        var caps = w.capabilities || {};
        var roles = caps.roles || [];
        return w.online && !caps.blocked &&
          (roles.length === 0 || roles.indexOf("image") >= 0);
      });
      renderModal(r);
    });
  }

  function renderModal(r) {
    var veil = openModal("", { title: "Batch reference images", wide: true });
    if (!veil) return;
    cur = { veil: veil, timer: null, running: false, results: null };
    var bodyEl = veil.querySelector(".ws-modal-body");
    bodyEl.innerHTML = "";
    bodyEl.appendChild(buildBody(r, veil));
    bindTips(veil);
  }

  function loraRow(lr) {
    lr = lr || {};
    return '<div class="lr-row">' +
      '<select class="lr-sel">' +
        '<option value="">none</option>' +
        Object.keys(curOpts.loras || {}).map(function (k) {
          var e = curOpts.loras[k] || {};
          var lab = e.label || e.name || k;
          return '<option value="' + esc(k) + '"' +
            (lr.file === k ? " selected" : "") + ">" + esc(lab) + "</option>";
        }).join("") +
      "</select>" +
      '<input type="range" class="lr-str" min="0.05" max="1.5" step="0.05" value="' +
        (lr.strength != null ? Number(lr.strength) : 0.7) + '">' +
      '<output class="lr-out"></output>' +
      '<button class="btn small" type="button" data-act="lr-rm" data-tip="Remove this LoRA">' + Icon.get("x") + "</button>" +
    "</div>";
  }
  var curOpts = {};

  function buildBody(r, veil) {
    curOpts = { loras: r.loras || {}, models: r.models || [] };
    /* group shots by scene, keep selection state */
    var groups = {};
    (r.shots || []).forEach(function (s) {
      (groups[s.scene || "?"] = groups[s.scene || "?"] || []).push(s);
    });
    if (!selCache[r.scene]) selCache[r.scene] = {};
    var body = document.createElement("div");
    body.className = "batch-body";
    body.innerHTML =
      '<div class="batch-head">' +
        '<div class="batch-shotpick">' +
          '<div class="adv-label">1 · Choose shots</div>' +
          '<div class="batch-scenes">' +
            Object.keys(groups).map(function (scene) {
              var rows = groups[scene];
              return '<div class="batch-scene" data-scene="' + esc(scene) + '">' +
                '<label class="toggle batch-toggle" data-tip="Select all shots in this scene">' +
                  '<input type="checkbox" class="bs-all"> <b>' + esc(scene) +
                  "</b> <span class=\"muted small\">(" + rows.length + " shots)</span></label>" +
                '<div class="batch-shots">' +
                  rows.map(function (s) {
                    var k = shotKey(s);
                    return '<label class="bs-shot" data-tip="' +
                      esc((s.action || s.shot_id) || "") + '">' +
                      '<input type="checkbox" class="bs-one" data-k="' + esc(k) + '">' +
                      "<span>" + esc(s.shot_id) + (s.ref_image ? " · ref" : "") +
                      (s.status === "passed" ? " · done" : "") + "</span>" +
                    "</label>";
                  }).join("") +
                "</div></div>";
            }).join("") +
          "</div>" +
        "</div>" +
        '<div class="batch-config">' +
          '<div class="adv-label">2 · Generation settings</div>' +
          '<label class="studio-field">Model' +
            '<select id="bf-model">' +
              (r.models || []).map(function (m) {
                return '<option value="' + esc(m.id || "") + '">' +
                  esc(m.name || m.id || "?") + "</option>";
              }).join("") +
            "</select></label>" +
          '<label class="studio-field">Count per shot' +
            '<select id="bf-count">' +
              [1, 2, 3, 4].map(function (n) {
                return '<option value="' + n + '"' + (n === 1 ? " selected" : "") +
                  ">" + n + "</option>";
              }).join("") +
            "</select></label>" +
          '<label class="studio-field" data-tip="Render this batch on a worker PC (its own ComfyUI) instead of the production farm. Only online, unblocked workers with the image role are listed; the queue keeps them in order.">Run on' +
            '<select id="bf-worker">' +
              '<option value="local">This PC (local)</option>' +
              (r._workers || []).map(function (w) {
                return '<option value="' + esc(w.id) + '">' +
                  esc(w.name || w.id) + "</option>";
              }).join("") +
            "</select></label>" +
          '<div class="studio-section-head">LoRAs <span class="muted small">up to 6</span></div>' +
          '<div id="bf-loras">' + loraRow(null) + "</div>" +
          '<button class="btn small" type="button" id="bf-lora-add" ' +
            'data-tip="Add another LoRA row (max 6)">+ add LoRA</button>' +
          '<div class="studio-section-head">Advanced</div>' +
          '<div class="studio-row">' +
            '<label class="studio-field">Steps<input id="bf-steps" type="number" min="1" max="60" placeholder="auto"></label>' +
            '<label class="studio-field">CFG<input id="bf-cfg" type="number" min="0" max="20" step="0.5" placeholder="auto"></label>' +
            '<label class="studio-field">Seed<input id="bf-seed" type="number" placeholder="random"></label>' +
          "</div>" +
          '<label class="toggle batch-toggle" data-tip="Hires refine (SDXL models only)">' +
            '<input type="checkbox" id="bf-hires"> Hires refine</label>' +
          '<label class="toggle batch-toggle" data-tip="ON: no-reference shots start from the roster portrait · OFF: they render as T2V">' +
            '<input type="checkbox" id="bf-fallback"' +
            (r.ref_fallback !== 0 ? " checked" : "") + "> Portrait fallback</label>" +
          '<div class="batch-actions">' +
            '<button class="btn primary" type="button" id="bf-go">' + Icon.get("wand") + "Generate all</button>" +
            '<button class="btn danger" type="button" id="bf-stop" disabled>' + Icon.get("stop") + "Stop</button>" +
          "</div>" +
          '<div id="bf-progress" class="studio-progress"></div>' +
          '<div id="bf-results" class="studio-results"></div>' +
        "</div>" +
      "</div>";

    /* lora row wiring */
    function wireLr(rowEl) {
      var range = rowEl.querySelector(".lr-str");
      var out = rowEl.querySelector(".lr-out");
      var sync = function () { out.textContent = Number(range.value).toFixed(2); };
      range.oninput = sync;
      sync();
      var rm = rowEl.querySelector('[data-act="lr-rm"]');
      if (rm) rm.onclick = function () {
        if (rowEl.parentNode) rowEl.parentNode.removeChild(rowEl);
      };
    }
    body.querySelectorAll(".lr-row").forEach(wireLr);
    body.querySelector("#bf-lora-add").onclick = function () {
      var box = body.querySelector("#bf-loras");
      if (box.querySelectorAll(".lr-row").length >= 6) {
        toast("max 6 LoRAs", true); return;
      }
      var h = document.createElement("div");
      h.innerHTML = loraRow(null);
      box.appendChild(h.firstElementChild);
      wireLr(box.lastElementChild);
    };

    /* hires availability follows the selected model's capability flags —
       a model that cannot use it never shows it (server refuses otherwise) */
    var modelSel = body.querySelector("#bf-model");
    var hiresChk = body.querySelector("#bf-hires");
    function syncHires() {
      var mi = null;
      (curOpts.models || []).forEach(function (m) {
        if ((m.id || "") === modelSel.value) mi = m;
      });
      var ok = !!(mi && mi.capabilities && mi.capabilities.hires);
      hiresChk.disabled = !ok;
      if (!ok) hiresChk.checked = false;
      var lbl = hiresChk.closest("label");
      if (lbl) lbl.classList.toggle("hidden", !ok);
    }
    modelSel.onchange = syncHires;
    syncHires();

    /* scene select-all + shot selection */
    body.querySelectorAll(".batch-scene").forEach(function (sc) {
      var all = sc.querySelector(".bs-all");
      var ones = sc.querySelectorAll(".bs-one");
      all.onchange = function () {
        ones.forEach(function (o) { o.checked = all.checked; });
      };
      ones.forEach(function (o) {
        o.onchange = function () {
          var allChecked = Array.prototype.every.call(ones, function (x) {
            return x.checked;
          });
          all.checked = allChecked;
        };
        o.checked = true; /* default: everything selected */
      });
    });

    /* fallback toggle persists per project */
    var fb = body.querySelector("#bf-fallback");
    fb.onchange = function () {
      post("/api/projects/" + encodeURIComponent(State.pid) + "/refs-fallback",
        { enabled: fb.checked }, 30000);
    };

    /* generate */
    body.querySelector("#bf-go").onclick = function () {
      var selected = [];
      body.querySelectorAll(".bs-one:checked").forEach(function (o) {
        var parts = o.dataset.k.split("::");
        selected.push(parts[1]);
      });
      if (!selected.length) { toast("select at least one shot", true); return; }
      var config = {
        model: body.querySelector("#bf-model").value,
        count: Number(body.querySelector("#bf-count").value) || 1,
        hires: body.querySelector("#bf-hires").checked,
      };
      var bfw = body.querySelector("#bf-worker");
      if (bfw && bfw.value && bfw.value !== "local") config.worker = bfw.value;
      var loras = [];
      body.querySelectorAll("#bf-loras .lr-row").forEach(function (lr) {
        var sel = lr.querySelector(".lr-sel");
        if (sel && sel.value) {
          loras.push({ file: sel.value,
                       strength: Number(lr.querySelector(".lr-str").value) || 0.7 });
        }
      });
      config.loras = loras;
      var steps = body.querySelector("#bf-steps").value;
      if (steps !== "") config.steps = Number(steps);
      var cfg = body.querySelector("#bf-cfg").value;
      if (cfg !== "") config.cfg = Number(cfg);
      var seed = body.querySelector("#bf-seed").value;
      if (seed !== "") config.seed = Number(seed);
      cur.selected = selected;
      post("/api/projects/" + encodeURIComponent(State.pid) + "/refbatch/start",
        { shots: selected, config: config }, 30000).then(function (r) {
          if (!r) return;
          if (!r.ok) { toast(r.error || "start failed", true); return; }
          toast("batch started — " + selected.length + " shots");
          body.querySelector("#bf-stop").disabled = false;
          body.querySelector("#bf-go").disabled = true;
          startPoll(body);
        });
    };

    body.querySelector("#bf-stop").onclick = function () {
      post("/api/projects/" + encodeURIComponent(State.pid) + "/refbatch/stop",
        {}, 30000).then(function () {
          toast("stopping after the current image…");
        });
    };

    function startPoll(box) {
      if (cur.timer) clearInterval(cur.timer);
      cur.running = true;
      cur.timer = setInterval(function () {
        if (!cur || !cur.veil || !cur.veil.isConnected) {
          clearInterval(cur.timer); cur = null; return;
        }
        get("/api/projects/" + encodeURIComponent(State.pid) + "/refbatch/status",
          15000, true).then(function (st) {
          if (!st || !st.active) return;
          var a = st.active;
          var prog = box.querySelector("#bf-progress");
          if (a.running) {
            prog.textContent = "generating… " + (a.current || "") +
              " (" + (a.done || 0) + "/" + (a.total || 0) + ")";
          } else {
            clearInterval(cur.timer);
            cur.running = false;
            box.querySelector("#bf-stop").disabled = true;
            box.querySelector("#bf-go").disabled = false;
            var res = box.querySelector("#bf-results");
            res.innerHTML = Object.keys(a.results || {}).map(function (sid) {
              var v = a.results[sid];
              if (v.error) {
                return '<div class="gen-thumb"><div class="muted small">' +
                  esc(sid) + ": " + esc(v.error) + "</div></div>";
              }
              var file = v.assigned || (v.files || [])[0] || "";
              var url = "/api/keyframes/" + encodeURIComponent(State.pid) +
                "/" + encodeURIComponent(sid) + "/" + encodeURIComponent(file);
              return '<div class="gen-thumb">' +
                (file ? '<img loading="lazy" src="' + esc(url) +
                        '" alt="' + esc(sid) + '">' : "") +
                '<div class="gen-act"><span class="muted small">' +
                esc(sid) + (v.assigned ? " · assigned" : "") + "</span></div>" +
              "</div>";
            }).join("") || '<span class="muted small">no results</span>';
            prog.textContent = a.message || "done";
            State.refreshShots();
          }
        });
      }, 3000);
    }
    return body;
  }

  function closeBatch() {
    if (cur && cur.timer) clearInterval(cur.timer);
    if (cur && cur.veil) closeModal(cur.veil);
    cur = null;
  }

  function startBatch() {
    if (cur && cur.running) { toast("a batch is running — stop it first", true); return; }
    openBatch();
  }
  /* any [data-batchrefs] button opens the batch (the Shots page header) */
  document.addEventListener("click", function (e) {
    if (e.target.closest && e.target.closest("[data-batchrefs]")) startBatch();
  });

  window.BatchRefs = { open: startBatch, close: closeBatch };
})();
