/* ai-settings.js — the AI writer: the AI that writes the story (the Story
   Architect) and the movie's video prompts. Three steps: where it runs (the
   provider presets of GET /api/ai/providers), its key, its model (the
   preset's suggested models, or "List models" for what the endpoint offers).

   Works in both shells (it needs only js/api.js and js/icons.js):
     AiSettings.open(onSaved)         a dialog
     AiSettings.mount(el, onSaved)    inline (Settings → AI writer)
   The saved key is never shown (only its last four characters); "List
   models" and "Test" save first and then use the saved settings. */
(function () {
  "use strict";

  const CLOUD = ["openai", "gemini", "openrouter", "deepseek", "groq", "mistral"];
  const LOCAL = ["ollama", "lmstudio"];
  let providers = null;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function icon(n) { return window.Icon ? window.Icon.get(n) : ""; }
  function say(msg, err) { if (window.toast) window.toast(msg, err); }
  function trim(u) { return String(u || "").replace(/\/+$/, ""); }

  async function loadProviders() {
    if (providers) return providers;
    const r = await API.get("/api/ai/providers", 20000, true);
    providers = (r && Array.isArray(r.providers)) ? r.providers : [];
    return providers;
  }
  function preset(id) {
    return (providers || []).filter(function (p) { return p.id === id; })[0] || null;
  }

  function provBtn(id, cur) {
    const p = preset(id);
    if (!p) return "";
    return '<button class="ai-prov' + (id === cur ? " on" : "") + '" type="button" data-prov="' + esc(id) + '">' +
      "<b>" + esc(p.label) + "</b><span>" + esc(p.blurb || "") + "</span></button>";
  }

  function formHtml(cur) {
    const prov = cur.provider || "";
    return '<div class="ai-status" id="ais-status"></div>' +
      '<p class="ai-note" style="margin-top:6px">It writes the story and the video prompt of every shot. ' +
        "You can also skip it and paste replies from any chat AI on the Story and Shots pages.</p>" +
      '<div class="ai-step" id="ais-s1"><span class="ai-n">1</span><h4>Where it runs</h4>' +
        '<div class="ai-body">' +
          '<div class="ai-group">In the cloud, with a key</div>' +
          '<div class="ai-provs">' + CLOUD.map(function (id) { return provBtn(id, prov); }).join("") + "</div>" +
          '<div class="ai-group">On this PC, free</div>' +
          '<div class="ai-provs">' + LOCAL.map(function (id) { return provBtn(id, prov); }).join("") +
            provBtn("custom", prov) + "</div>" +
        "</div></div>" +
      '<div class="ai-step" id="ais-s2"><span class="ai-n">2</span><h4 id="ais-s2-h">Key</h4>' +
        '<div class="ai-body">' +
          '<div class="field" id="ais-url-row"><label for="ais-url">Endpoint</label>' +
            '<input id="ais-url" autocomplete="off" spellcheck="false" placeholder="https://…/v1"></div>' +
          '<div class="field" id="ais-key-row"><label for="ais-key">API key</label>' +
            '<div class="ai-row"><input id="ais-key" type="password" autocomplete="new-password" spellcheck="false">' +
            '<a class="btn small" id="ais-key-link" target="_blank" rel="noopener noreferrer">' + icon("external") + "Get a key</a></div></div>" +
          '<div class="ai-note" id="ais-local"></div>' +
        "</div></div>" +
      '<div class="ai-step" id="ais-s3"><span class="ai-n">3</span><h4>Model</h4>' +
        '<div class="ai-body">' +
          '<div class="ai-models" id="ais-sugg"></div>' +
          '<div class="ai-row"><input id="ais-model" list="ais-models" autocomplete="off" spellcheck="false" placeholder="model name" value="' +
            esc(cur.model || "") + '"><datalist id="ais-models"></datalist>' +
            '<button class="btn small" type="button" id="ais-load" data-tip="Save, then list the models this endpoint offers your key">' +
              icon("refresh") + "List models</button></div>" +
          '<label class="toggle"><input type="checkbox" id="ais-tools"' + (cur.tools === "off" ? "" : " checked") +
            "><span></span><em>Let it look up shots, scenes and characters, and check its prompts, while it writes</em></label>" +
        "</div></div>" +
      '<div class="dlg-actions" style="padding:18px 0 0">' +
        '<button class="btn" type="button" id="ais-test">' + icon("spark") + "Test</button>" +
        '<span class="ai-note" id="ais-msg" style="margin-right:auto"></span>' +
        '<button class="btn" type="button" id="ais-cancel">Close</button>' +
        '<button class="btn primary" type="button" id="ais-save">Save</button>' +
      "</div>";
  }

  /* wire a form inside `root`; close() when it lives in a dialog */
  function wire(root, cur, onSaved, close) {
    const q = function (s) { return root.querySelector(s); };
    let prov = cur.provider || "";
    let listed = null;          // the models "List models" returned
    const url = q("#ais-url"), key = q("#ais-key"), model = q("#ais-model");

    function msg(text, cls) { const m = q("#ais-msg"); m.textContent = text || ""; m.className = "ai-note" + (cls ? " " + cls : ""); }

    function paintStatus() {
      const s = q("#ais-status");
      const p = preset(cur.provider) || {};
      s.innerHTML = '<span class="dock-dot' + (cur.ready ? " on" : "") + '"></span>' +
        (cur.ready ? "Connected: " + esc(p.label || cur.base_url) + ", " + esc(cur.model)
          : "Not set up yet");
    }

    function paintModels() {
      const p = preset(prov) || {};
      const sugg = (p.models || []);
      const box = q("#ais-sugg");
      box.innerHTML = sugg.length
        ? sugg.map(function (m) {
            const gone = listed && listed.indexOf(m) < 0;
            return '<button class="ai-model' + (model.value.trim() === m ? " on" : "") + (gone ? " gone" : "") +
              '" type="button" data-m="' + esc(m) + '"' + (gone ? ' data-tip="This endpoint does not list it for your key"' : "") +
              ">" + esc(m) + "</button>";
          }).join("") + '<span class="ai-note" style="align-self:center">suggested</span>'
        : '<span class="ai-note">' + (prov === "lmstudio" ? "Load a model in LM Studio, then press List models."
          : "Press List models to see what this endpoint offers.") + "</span>";
      box.querySelectorAll("[data-m]").forEach(function (b) {
        b.onclick = function () { model.value = b.dataset.m; paintModels(); };
      });
    }

    function sync() {
      const p = preset(prov) || {};
      root.querySelectorAll(".ai-prov").forEach(function (b) { b.classList.toggle("on", b.dataset.prov === prov); });
      q("#ais-s1").classList.toggle("done", !!prov);
      const custom = prov === "custom";
      if (p.base_url) url.value = p.base_url;
      q("#ais-url-row").classList.toggle("hidden", !custom);
      q("#ais-key-row").classList.toggle("hidden", !p.needs_key && !custom);
      q("#ais-s2-h").textContent = p.local ? "Before you start" : custom ? "Endpoint and key" : "Key";
      const sameHost = cur.base_url && trim(url.value) === trim(cur.base_url);
      key.placeholder = cur.has_key && sameHost ? "saved: " + cur.api_key + " (leave empty to keep it)"
        : (p.needs_key ? "paste your key" : "only if the endpoint needs one");
      const link = q("#ais-key-link");
      link.classList.toggle("hidden", !p.key_url);
      if (p.key_url) link.href = p.key_url;
      q("#ais-local").innerHTML = prov === "ollama"
        ? "Ollama must be running, with a model pulled, for example <code>ollama pull qwen3:8b</code>. " +
          "It shares the GPU with the renders: it writes while no render runs, and the model is unloaded after each take it writes."
        : prov === "lmstudio"
          ? "LM Studio's local server must be running with a model loaded. It shares the GPU with the renders: it writes while no render runs."
          : !prov ? "Pick where it runs first." : "";
      q("#ais-s2").classList.toggle("done", !!prov && (!p.needs_key || !!key.value.trim() || (cur.has_key && sameHost)));
      q("#ais-s3").classList.toggle("done", !!model.value.trim());
      paintModels();
    }

    url.value = cur.base_url || "";
    paintStatus();
    sync();
    root.querySelectorAll(".ai-prov").forEach(function (b) {
      b.onclick = function () {
        if (b.dataset.prov === prov) return;
        prov = b.dataset.prov;
        listed = null;
        if (!(preset(prov) || {}).base_url) url.value = "";
        const sugg = (preset(prov) || {}).models || [];
        model.value = sugg[0] || "";
        msg("");
        sync();
      };
    });
    model.oninput = function () { sync(); };
    key.oninput = function () { sync(); };

    async function save(quiet) {
      if (!prov) { msg("Pick where it runs first.", "bad"); return null; }
      const r = await API.post("/api/ai/config", {
        provider: prov, base_url: url.value.trim(), model: model.value.trim(),
        api_key: key.value.trim(), tools: q("#ais-tools").checked ? "auto" : "off",
      }, 30000);
      if (!r) return null;
      if (!r.ok) { msg(r.error || "Could not save", "bad"); return null; }
      key.value = "";
      Object.assign(cur, r);
      url.value = r.base_url || url.value;
      paintStatus();
      sync();
      if (r.api_key_cleared) msg("The saved key was for another endpoint and was removed.", "bad");
      else if (!quiet) say("AI writer saved");
      return r;
    }
    q("#ais-load").onclick = async function () {
      if (!(await save(true))) return;
      msg("Asking the endpoint for its models…");
      const r = await API.get("/api/ai/models", 30000, true);
      if (!r || !r.ok) { msg((r && r.error) || "The endpoint did not answer", "bad"); return; }
      listed = r.models || [];
      q("#ais-models").innerHTML = listed.map(function (m) { return '<option value="' + esc(m) + '"></option>'; }).join("");
      msg(listed.length + " models: type or pick one in the field", "live");
      paintModels();
      model.focus();
    };
    q("#ais-test").onclick = async function () {
      if (!model.value.trim()) { msg("Pick a model first.", "bad"); return; }
      if (!(await save(true))) return;
      msg("Asking the AI to say hello…");
      const r = await API.post("/api/ai/test", {}, 90000, true);
      if (r && r.ok) msg("It answered: " + (r.reply || "OK"), "live");
      else msg((r && r.error) || "No answer", "bad");
    };
    q("#ais-save").onclick = async function () {
      if (!model.value.trim()) { msg("Pick a model (List models shows them).", "bad"); return; }
      const r = await save(false);
      if (!r) return;
      if (close) close();
      if (onSaved) onSaved(r);
    };
    q("#ais-cancel").onclick = function () { if (close) close(); };
    if (!close) q("#ais-cancel").classList.add("hidden");
    if (window.Shell) window.Shell.bindTips(root);
  }

  async function load() {
    await loadProviders();
    return (await API.get("/api/ai/config", 20000, true)) || {};
  }

  /* the dialog */
  async function open(onSaved) {
    const cur = await load();
    const veil = document.createElement("div");
    veil.className = "dlg-veil";
    veil.innerHTML = '<div class="dlg-card ai-card" role="dialog" aria-modal="true" aria-labelledby="ais-h">' +
      '<div class="dlg-head"><h3 id="ais-h">AI writer</h3><span class="spacer"></span>' +
        '<button class="btn ghost icon small" type="button" data-x title="Close">' + icon("x") + "</button></div>" +
      '<div class="dlg-body">' + formHtml(cur) + "</div></div>";
    document.body.appendChild(veil);
    const close = function () {
      document.removeEventListener("keydown", onKey, true);
      veil.remove();
    };
    const onKey = function (e) { if (e.key === "Escape") { e.stopPropagation(); close(); } };
    document.addEventListener("keydown", onKey, true);
    veil.addEventListener("mousedown", function (e) { if (e.target === veil) close(); });
    veil.querySelector("[data-x]").onclick = close;
    wire(veil, cur, onSaved, close);
  }

  /* inline, in a page */
  async function mount(el, onSaved) {
    if (!el) return;
    const cur = await load();
    el.innerHTML = formHtml(cur);
    wire(el, cur, onSaved, null);
  }

  window.AiSettings = { open: open, mount: mount };
})();
