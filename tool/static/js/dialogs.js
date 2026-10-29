/* dialogs.js — styled confirm/prompt dialogs replacing native
   window.confirm() / window.prompt(). Promise-based:
     Dlg.confirm({title, message, okLabel, danger, tone}) -> Promise<boolean>
       (tone "need" | "bad": a mark before the title, in the warn/danger colour)
     Dlg.prompt({title, label, value, placeholder, okLabel}) -> Promise<string|null>
   Cancel / Esc / backdrop click resolve exactly like the native dialogs
   did: confirm -> false, prompt -> null. */
(function () {
  "use strict";

  function escAttr(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function escHtml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function build(title, bodyHtml, okLabel, okClass, cancelLabel, tone) {
    var veil = document.createElement("div");
    veil.className = "dlg-veil";
    var mark = tone && window.Icon
      ? '<span class="dlg-mark ' + escAttr(tone) + '">' + window.Icon.get("warning") + "</span>" : "";
    veil.innerHTML =
      '<div class="dlg-card" role="dialog" aria-modal="true">' +
        '<div class="dlg-head">' + mark + "<h3>" + escHtml(title || "Confirm") + "</h3></div>" +
        '<div class="dlg-body">' + bodyHtml + "</div>" +
        '<div class="dlg-actions">' +
          '<button type="button" class="btn" data-dlg-cancel>' + escHtml(cancelLabel || "Cancel") + "</button>" +
          '<button type="button" class="btn ' + (okClass || "primary") + '" data-dlg-ok>' + escHtml(okLabel || "OK") + "</button>" +
        "</div>" +
      "</div>";
    document.body.appendChild(veil);
    return veil;
  }

  function finish(veil, value) {
    document.removeEventListener("keydown", veil._onKey, true);
    veil.remove();
    var r = veil._resolve;
    veil._resolve = null;
    if (r) r(value);
  }

  function wire(veil) {
    veil._onKey = function (e) {
      if (e.key === "Escape") { e.stopPropagation(); finish(veil, null); }
    };
    document.addEventListener("keydown", veil._onKey, true);
    veil.querySelector("[data-dlg-cancel]").onclick = function () { finish(veil, null); };
    veil.addEventListener("mousedown", function (e) {
      if (e.target === veil) finish(veil, null);
    });
    var okBtn = veil.querySelector("[data-dlg-ok]");
    if (okBtn && !veil.querySelector("input")) okBtn.focus();
  }

  window.Dlg = {
    confirm: function (opts) {
      opts = opts || {};
      return new Promise(function (resolve) {
        var veil = build(
          opts.title,
          "<p>" + (opts.message || "Are you sure?") + "</p>",
          opts.okLabel || "Confirm", opts.danger ? "danger" : "primary",
          opts.cancelLabel, opts.tone);
        veil._resolve = resolve;
        wire(veil);
        veil.querySelector("[data-dlg-ok]").onclick = function () { finish(veil, true); };
      });
    },
    prompt: function (opts) {
      opts = opts || {};
      return new Promise(function (resolve) {
        var val = opts.value == null ? "" : String(opts.value);
        var veil = build(
          opts.title,
          (opts.label ? "<p>" + opts.label + "</p>" : "") +
            '<input class="dlg-input" type="text" value="' + escAttr(val) + '"' +
            (opts.placeholder ? ' placeholder="' + escAttr(opts.placeholder) + '"' : "") + ">",
          opts.okLabel || "OK", "primary", opts.cancelLabel);
        veil._resolve = resolve;
        wire(veil);
        var input = veil.querySelector("input");
        input.focus();
        input.select();
        input.addEventListener("keydown", function (e) {
          if (e.key === "Enter") { e.preventDefault(); finish(veil, input.value); }
        });
        veil.querySelector("[data-dlg-ok]").onclick = function () { finish(veil, input.value); };
      });
    },
    /* choice dialog: Dlg.choose({title, message, choices:[{label,value,danger}]})
       resolves the chosen value, or null on cancel/Esc. */
    choose: function (opts) {
      opts = opts || {};
      return new Promise(function (resolve) {
        var veil = build(opts.title, "<p>" + (opts.message || "") + "</p>", "", "", opts.cancelLabel);
        var actions = veil.querySelector(".dlg-actions");
        actions.innerHTML = "";
        (opts.choices || []).forEach(function (c) {
          var btn = document.createElement("button");
          btn.type = "button";
          btn.className = "btn" + (c.danger ? " danger" : "");
          btn.textContent = c.label;
          btn.onclick = function () { finish(veil, c.value); };
          actions.appendChild(btn);
        });
        var cancelBtn = document.createElement("button");
        cancelBtn.type = "button";
        cancelBtn.className = "btn";
        cancelBtn.setAttribute("data-dlg-cancel", "");
        cancelBtn.textContent = opts.cancelLabel || "Cancel";
        cancelBtn.onclick = function () { finish(veil, null); };
        actions.appendChild(cancelBtn);
        veil._resolve = resolve;
        wire(veil);
      });
    },
  };
})();
