/* model-catalog.js — the video model pickers (Video Studio and the movie's
   Generate page) list the registry the same way: one optgroup per catalog
   group in display order, recommended configs first (★), short labels, and
   ⛓ on the models whose chains / CONTINUE runs render as one seamless take.
   The full name, timing, note and availability go in the option tooltip.
   Data: GET /api/videostudio/models -> {groups: [{id, label}], models: [...]}
   (already sorted by video_models.catalog). */
(function () {
  "use strict";

  const SEAMLESS_NOTE = "⛓ = chains and CONTINUE shots render as one seamless take";

  function escHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function label(m) {
    const c = m.catalog || {};
    return (c.recommended ? "★ " : "") + (c.label || m.display_name || m.id) +
      (m.seamless ? "  ⛓" : "") +
      (m.available ? "" : "  (unavailable)");
  }

  function tooltip(m) {
    const c = m.catalog || {};
    return [
      m.display_name || m.id,
      c.detail || "",
      m.seamless ? "Seamless takes: one continuous render, " +
                     (m.seamless === "h3" ? "exact" : "soft-pinned") + " latent carry between segments, seam QA"
                 : "Chains continue from the previous segment's tail frame",
      m.note || "",
      m.vram_note || "",
      m.experimental && m.declared_status !== "production" ? "Experimental (not promoted to production)" : "",
      m.available ? "" : "Unavailable — missing: " + (m.missing || []).join(", "),
      "id: " + m.id,
    ].filter(Boolean).join("\n");
  }

  /* <optgroup>/<option> markup. opts.empty: label of a leading "" option. */
  function optionsHtml(payload, selected, opts) {
    const models = (payload && payload.models) || [];
    const groups = (payload && payload.groups) || [];
    const o = opts || {};
    const option = function (m) {
      return '<option value="' + escHtml(m.id) + '"' +
        (m.id === selected ? " selected" : "") + (m.available ? "" : " disabled") +
        ' title="' + escHtml(tooltip(m)) + '">' + escHtml(label(m)) + "</option>";
    };
    let html = o.empty != null
      ? '<option value=""' + (!selected ? " selected" : "") + ">" + escHtml(o.empty) + "</option>"
      : "";
    const placed = {};
    groups.forEach(function (g) {
      const members = models.filter(function (m) { return (m.catalog || {}).group === g.id; });
      if (!members.length) return;
      members.forEach(function (m) { placed[m.id] = true; });
      html += '<optgroup label="' + escHtml(g.label) + '">' + members.map(option).join("") + "</optgroup>";
    });
    const rest = models.filter(function (m) { return !placed[m.id]; });
    if (rest.length) html += '<optgroup label="Other">' + rest.map(option).join("") + "</optgroup>";
    // a stored id the registry no longer has stays visible instead of
    // silently showing another model
    if (selected && !models.some(function (m) { return m.id === selected; })) {
      html += '<option value="' + escHtml(selected) + '" selected disabled>' +
        escHtml(selected) + " (not in the registry)</option>";
    }
    return html;
  }

  function find(payload, id) {
    return ((payload && payload.models) || []).find(function (m) { return m.id === id; }) || null;
  }

  window.ModelCatalog = {
    optionsHtml: optionsHtml, label: label, tooltip: tooltip, find: find,
    SEAMLESS_NOTE: SEAMLESS_NOTE,
  };
})();
