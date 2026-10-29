/* cast.js — Cast & World: the project bible's characters and places. Each is
   a card with its portrait (the first picked look) that opens into the
   editor: looks, body, outfit, emotions, and folds for powers, weapons,
   outfits per scene and the legacy Wan 2.2 LoRA. Also the keyframe seed
   picture and cast-driven story regeneration. */
(function () {
  "use strict";
  const openCards = {};   // which cards are open, kept across re-renders
  const openFolds = {};   // and which folds inside them
  // cards with edits not saved yet ("c:<id>", "l:<id>"): the page does not
  // re-render over them (the status poll re-renders it whenever the project
  // changes), and the browser asks before they are lost
  const unsaved = new Set();
  let renderedPid = null;
  let staleRender = false;
  let rerender = null;
  window.addEventListener("beforeunload", function (e) {
    if (unsaved.size) { e.preventDefault(); e.returnValue = ""; }
  });

  const TIERS = ["main", "supporting", "recurring", "crowd"];
  const POWER_TYPES = ["lightning", "fire", "ice", "water", "wind", "earth",
    "force", "arcane", "divine", "shadow", "void", "plasma", "nature", "custom"];
  // Mirrors promptspec.WEAPON_TYPES — the batch wizard owns the full config
  // (material/style/size/glow); the Cast tab picks the family + text.
  const WEAPON_TYPES = ["longsword", "greatsword", "katana", "rapier",
    "scimitar", "dagger", "twin daggers", "spear", "glaive", "halberd",
    "battle axe", "greataxe", "war hammer", "mace", "bow", "crossbow",
    "staff", "scepter", "orb focus", "whip", "chain blade", "gauntlets",
    "scythe", "trident", "custom"];
  const FALLBACK_COLOR = "#e8b04b";

  function pid() { return window.State.pid; }
  function safeColor(c) {
    return /^#[0-9a-fA-F]{3,8}$/.test(String(c || "")) ? String(c) : FALLBACK_COLOR;
  }
  function normPath(p) { return String(p || "").replace(/\\/g, "/"); }
  function filePart(p) {
    p = normPath(p);
    const i = p.lastIndexOf("/");
    return i >= 0 ? p.slice(i + 1) : p;
  }
  function refUrl(file) {
    const segs = normPath(file).split("/").map(function (s) { return encodeURIComponent(s); });
    return "/api/refs/" + encodeURIComponent(pid()) + "/" + segs.join("/");
  }

  /* ------------------------------------------------------------------ main */
  function render(el) {
    const p = State.project;
    if (!p || !State.pid) { el.innerHTML = noProject(); return; }
    const bible = State.bible || {};
    const chars = asList(bible.characters);
    const locs = asList(bible.locations);
    const story = p.story || {};
    const hasStory = !!(story.beats && Object.keys(story.beats).length);
    const inMovie = chars.filter(function (c) { return c.enabled !== false; });
    const withLook = inMovie.filter(function (c) { return (c.reference_images || []).length; }).length;
    const mismatched = inMovie.filter(function (c) { return (State.castChecks || {})[c.id]; });
    const castingLink = '<a class="btn small" href="' + Shell.href("casting") + '" data-page="casting" data-shell="tools">' +
      Icon.get("aperture") + "Open Casting</a>";
    const status = !chars.length
      ? '<div class="cast-status need"><span class="cs-mark">' + Icon.get("users") + "</span>" +
          "<div><b>No cast yet.</b> Add characters here, or write the story first: it invents a cast you can then edit.</div></div>"
      : mismatched.length
        ? '<div class="cast-status need"><span class="cs-mark">' + Icon.get("warning") + "</span>" +
            "<div><b>The look text of " + esc(mismatched.map(function (c) { return c.name || c.id; }).join(", ")) +
            " does not match the main picture.</b> Every prompt describes them with it, so the film waits until " +
            "it is put right on their card.</div></div>"
      : withLook < inMovie.length
        ? '<div class="cast-status need"><span class="cs-mark">' + Icon.get("aperture") + "</span>" +
            "<div><b>" + withLook + " of " + inMovie.length + " characters have a look.</b> " +
            "Generate looks on Casting and pick one per character. Shots of a character without one render from the words alone.</div>" +
            castingLink + "</div>"
        : '<div class="cast-status ok"><span class="cs-mark">' + Icon.get("check") + "</span>" +
            "<div><b>Every character has a look.</b> Their shots open on it.</div></div>";
    el.innerHTML =
      '<header class="page-intro">' +
        '<div class="pi-text"><h1>Cast &amp; World</h1>' +
          "<p>Who is in the movie and where it happens. The story writer uses this cast, and the video models open each " +
          "character&apos;s shots on their picked look.</p></div>" +
        '<div class="pi-acts">' +
          (hasStory ? '<button class="btn ghost" type="button" id="cast-regen" ' +
            'data-tip="Write the story again around this cast: with the AI writer, or with a chat AI on the Story page. The old versions stay in the Story history.">' +
            Icon.get("refresh") + "Rewrite the story around this cast</button>" : "") +
        "</div>" +
      "</header>" +
      status +
      '<section class="cast-sec">' +
        '<div class="sec-head"><h2 class="sec-title">Characters</h2>' +
          '<span class="sec-count">' + chars.length + "</span>" +
          '<span class="spacer"></span>' +
          '<button class="btn small" type="button" id="cast-new-char">' + Icon.get("plus") + "New character</button>" +
        "</div>" +
        (chars.length
          ? '<div class="cast-grid">' + chars.map(function (c) { return charCard(c); }).join("") + "</div>"
          : empty("No characters yet", "Add one, or write the story and it brings its own.")) +
      "</section>" +
      '<section class="cast-sec">' +
        '<div class="sec-head"><h2 class="sec-title">Places</h2>' +
          '<span class="sec-count">' + locs.length + "</span>" +
          '<span class="spacer"></span>' +
          '<button class="btn small" type="button" id="cast-new-loc">' + Icon.get("plus") + "New place</button>" +
        "</div>" +
        (locs.length
          ? '<div class="cast-grid">' + locs.map(function (l) { return locCard(l); }).join("") + "</div>"
          : empty("No places yet", "Add one, or let the story bring them.")) +
      "</section>" +
      '<details class="fold legacy" data-fold="seed"' + (openFolds.seed ? " open" : "") + ">" +
        '<summary><span class="fold-t">Keyframe seed picture</span>' +
          '<span class="fold-d">Only the keyframe step uses it. LTX-2.5 and MiniMax H3 shots use the looks above.</span></summary>' +
        '<div class="fold-body">' +
          '<div class="ctl-row">' +
            '<span class="muted small">The active picture seeds the keyframe pictures.</span><span class="spacer"></span>' +
            '<input type="file" id="cast-ref-file" accept="image/png,image/jpeg,image/webp" multiple class="hidden">' +
            '<button class="btn small" type="button" id="cast-ref-upload">' + Icon.get("upload") + "Upload</button>" +
          "</div>" +
          '<div class="ref-strip" id="cast-ref-strip"><span class="muted small">Loading…</span></div>' +
        "</div>" +
      "</details>";
    bind(el);
    loadRefs(el);
  }

  /* open or close a card in place (no re-render: unsaved edits stay) */
  function toggleCard(card, key) {
    const open = !card.classList.contains("open");
    openCards[key] = open;
    card.classList.toggle("open", open);
    card.querySelectorAll(".cast-edit-btn").forEach(function (b) {
      b.classList.toggle("ghost", !open);
      b.innerHTML = open ? Icon.get("chevron-up") + "Close" : Icon.get("pen") + "Edit";
    });
    if (open) {
      const first = card.querySelector(".cast-edit textarea, .cast-edit input");
      card.scrollIntoView({ block: "nearest", behavior: "smooth" });
      if (first && !first.value) first.focus({ preventScroll: true });
    }
  }

  /* the look text against what the main picture was made with (its Casting
     record): a text that says another gender, age or outfit pulls every
     shot away from the picture */
  function lookCheck(cid) {
    const c = (State.castChecks || {})[cid];
    if (!c || c.ok) return "";
    const pic = [c.body, c.outfit ? "wearing " + c.outfit : ""].filter(Boolean).join(", ");
    return '<div class="look-check">' +
      "<p><b>The Body and Outfit text does not match the main look:</b> " + esc((c.problems || []).join("; ")) + ".</p>" +
      (pic ? '<p class="lc-pic">The picture was made as: ' + esc(pic) + "</p>" : "") +
      '<div class="lc-acts">' +
        '<button class="btn small" type="button" data-act="look-apply" ' +
          'data-tip="Write Body and Outfit from what the main look was made with">Use the picture&apos;s look</button>' +
        '<button class="btn small ghost" type="button" data-act="look-keep" ' +
          'data-tip="The text stays as written and is not checked again">Keep mine</button>' +
      "</div></div>";
  }

  async function lookAnswer(card, ch, apply) {
    // "Keep mine" keeps what is in the fields, so typed edits are saved first
    if (!apply && unsaved.has(cardKey(card)) && !(await saveCharacter(card, ch))) return;
    const r = await post("/api/projects/" + encodeURIComponent(pid()) + "/cast/characters/" +
      encodeURIComponent(ch.id || "") + "/look/" + (apply ? "apply" : "keep"), {}, 30000);
    if (!r) return;
    if (!r.ok) { toast(r.error || "That did not work", true); return; }
    if (apply && r.character) {
      ["body", "outfit"].forEach(function (k) {
        const f = card.querySelector('[data-f="' + k + '"]');
        if (f) f.value = r.character[k] || "";
      });
      const sum = card.querySelector(".cast-sum");
      if (sum && r.character.body) { sum.className = "cast-sum"; sum.textContent = r.character.body; }
    }
    const box = card.querySelector(".look-check");
    if (box) box.remove();
    toast(apply ? "Body and Outfit now describe the main look" : "Your text stays as written");
    await State.refreshProject();
  }

  /* the card's count of picked pictures, kept current after a pick or a removal */
  function setLookState(card, n, one, many, none) {
    const s = card.querySelector(".look-state");
    if (!s) return;
    s.className = "look-state " + (n ? "ok" : "need");
    s.textContent = n ? n + " " + (n === 1 ? one : many) : none;
  }

  /* the card's portrait: its first picture */
  function setPortrait(card, url) {
    const pr = card.querySelector(".cast-portrait");
    if (!pr) return;
    const img = pr.querySelector("img");
    if (!url) { if (img) img.remove(); pr.classList.add("none"); return; }
    pr.classList.remove("none");
    if (img) { img.src = url; return; }
    pr.insertAdjacentHTML("afterbegin", '<img alt="" loading="lazy" src="' + esc(url) + '">');
  }

  /* ---------------------------------------------------------- character UI */
  function characterLoras() {
    const all = State.loras || [];
    return all.filter(function (e) {
      return String(e.category || "").toLowerCase() === "character";
    });
  }

  function powerLoras() {
    const all = State.loras || [];
    return all.filter(function (e) {
      return String(e.category || "").toLowerCase() === "power";
    });
  }

  function envLoras() {
    const all = State.loras || [];
    return all.filter(function (e) {
      return String(e.category || "").toLowerCase() === "environment";
    });
  }

  function loraOptions(list, cur, sel) {
    /* registry entries + the currently saved file (so a saved LoRA is
       always visible even when the registry hasn't scanned it) */
    const files = list.map(function (e) { return e.file || ""; });
    if (cur && files.indexOf(cur) < 0) {
      files.unshift(cur);
    }
    return files.map(function (f) {
      const e = list.filter(function (x) { return (x.file || "") === f; })[0];
      return '<option value="' + esc(f) + '"' +
        ((sel || "") === f ? " selected" : "") + ">" +
        esc((e && (e.display || e.file)) || f) + "</option>";
    }).join("");
  }

  /* a fold inside a card; its open state survives the page re-renders */
  function cardFold(key, title, meta, body, cls) {
    return '<details class="cf' + (cls ? " " + cls : "") + '" data-fold="' + esc(key) + '"' +
      (openFolds[key] ? " open" : "") + ">" +
      '<summary><span class="cf-t">' + title + '</span><span class="cf-m">' + meta + "</span></summary>" +
      '<div class="cf-body">' + body + "</div></details>";
  }

  function count(n, one, many) { return n ? n + " " + (n === 1 ? one : many) : "none yet"; }

  function editButton(open) {
    return '<button class="btn small cast-edit-btn ' + (open ? "" : "ghost") + '" type="button" data-act="toggle">' +
      (open ? Icon.get("chevron-up") + "Close" : Icon.get("pen") + "Edit") + "</button>";
  }

  /* ------------------------------------------------------------ unsaved edits */
  function cardKey(card) {
    return card.dataset.cid != null ? "c:" + card.dataset.cid : "l:" + (card.dataset.lid || "");
  }

  function setUnsaved(card, on) {
    if (on) unsaved.add(cardKey(card)); else unsaved.delete(cardKey(card));
    card.classList.toggle("unsaved", on);
    const btn = card.querySelector('.cast-actions [data-act="save"]');
    if (btn) btn.textContent = on ? "Save changes" : (card.dataset.cid != null ? "Save character" : "Save place");
    const chip = card.querySelector(".unsaved-chip");
    const meta = card.querySelector(".cast-meta");
    if (on && !chip && meta) {
      meta.insertAdjacentHTML("beforeend", '<span class="unsaved-chip" data-tip="Open the card and press Save changes">' +
        "Unsaved changes</span>");
    } else if (!on && chip) {
      chip.remove();
    }
  }

  /* after a save: what the page held back while edits were open shows now */
  function afterSave() {
    if (!unsaved.size && staleRender && rerender) rerender();
  }

  // controls that save on their own: the card's head, the pictures' own
  // settings and the file pickers
  const SELF_SAVED = '[type="file"], .powref-lvl, .locscene-sel, .ldscene-sel, .cast-head [data-f]';
  const ROW_EDITS = ["add-power", "rm-power", "add-weapon", "rm-weapon", "add-loadout", "rm-loadout"];

  function trackEdits(card) {
    const mark = function (e) {
      const t = e.target;
      if (!t || !t.matches || !t.matches("input, textarea, select") || t.matches(SELF_SAVED)) return;
      setUnsaved(card, true);
    };
    card.addEventListener("input", mark);
    card.addEventListener("change", mark);
  }

  /* The head of a card saves itself as it changes: a character's name and
     part, and whether a character or place is in the movie. */
  async function saveHead(path, body, done) {
    const r = await post("/api/projects/" + encodeURIComponent(pid()) + path, body, 30000);
    if (!r || !r.ok) return false;          // post() already said why
    toast(done);
    await State.refreshProject();
    return true;
  }

  function bindHead(card, item, kind) {
    const path = () => (kind === "c" ? "/cast/characters/" : "/cast/locations/") + encodeURIComponent(item.id || "");
    const en = card.querySelector('.cast-head [data-f="enabled"]');
    if (en) en.onchange = async function () {
      const ok = await saveHead(path(), { enabled: en.checked }, en.checked ? "In the movie" : "Left out of the movie");
      if (ok) { item.enabled = en.checked; card.classList.toggle("benched", !en.checked); }
      else en.checked = !en.checked;
    };
    if (kind !== "c") return;                 // a place's name renames it (renameLocation)
    const name = card.querySelector('.cast-head [data-f="name"]');
    if (name) {
      name.onkeydown = function (e) {
        if (e.key === "Enter") { e.preventDefault(); name.blur(); }
        else if (e.key === "Escape") { name.value = item.name || ""; name.blur(); }
      };
      name.onchange = async function () {
        const v = name.value.trim();
        if (!v) { toast("Give the character a name first", true); name.value = item.name || ""; return; }
        if (v === (item.name || "")) return;
        if (await saveHead(path(), { name: v }, "Name saved")) item.name = v;
        else name.value = item.name || "";
      };
    }
    const tier = card.querySelector('.cast-head [data-f="tier"]');
    if (tier) tier.onchange = async function () {
      if (await saveHead(path(), { tier: tier.value }, "Part saved")) item.tier = tier.value;
      else tier.value = item.tier || "supporting";
    };
  }

  function inMovie(enabled, what) {
    return '<label class="toggle cast-toggle" data-tip="Whether this ' + what + ' is in the movie">' +
      '<input type="checkbox" data-f="enabled"' + (enabled ? " checked" : "") + "><span></span><em>In the movie</em></label>";
  }

  /* The card: a portrait (the first picked look) and who it is, closed; the
     whole editor when open. Every field stays in the DOM either way, so the
     save and collect code reads the same elements. */
  function charCard(ch) {
    const cid = ch.id || "";
    const emo = (ch.emotion_palette || []).join(", ");
    const enabled = ch.enabled !== false;
    const curLora = ch.lora && ch.lora.file ? ch.lora.file : "";
    const str = Number(ch.lora && ch.lora.strength != null ? ch.lora.strength : 0.8) || 0.8;
    const powers = ch.powers || {};
    const weapons = ch.weapons || {};
    const loadouts = ch.loadouts || {};
    const looks = (ch.reference_images || []).length;
    const open = !!openCards["c:" + cid];
    return '<article class="cast-card char' + (open ? " open" : "") + (enabled ? "" : " benched") + '" data-cid="' + esc(cid) + '">' +
      '<div class="cast-head">' +
        '<button class="cast-portrait' + (looks ? "" : " none") + '" type="button" data-act="toggle" ' +
          'data-tip="' + (looks ? "Open the card" : "No look picked yet: generate one on Casting, or upload a photo") + '">' +
          '<span class="cp-ph">' + Icon.get("aperture") + "</span></button>" +
        '<div class="cast-id">' +
          '<div class="cast-top">' +
            '<input type="text" class="cast-name" data-f="name" value="' + esc(ch.name || "") +
              '" placeholder="Name" spellcheck="false" aria-label="Name">' +
            editButton(open) +
          "</div>" +
          '<div class="cast-meta">' +
            '<select class="cast-tier" data-f="tier" data-tip="How big the part is: main characters get the most close-ups">' +
              TIERS.map(function (t) {
                return '<option value="' + t + '"' + ((ch.tier || "supporting") === t ? " selected" : "") +
                  ">" + t.charAt(0).toUpperCase() + t.slice(1) + "</option>";
              }).join("") +
            "</select>" +
            '<span class="look-state ' + (looks ? "ok" : "need") + '">' + (looks ? count(looks, "look", "looks") : "No look yet") + "</span>" +
            inMovie(enabled, "character") +
          "</div>" +
          (ch.body ? '<p class="cast-sum">' + esc(ch.body) + "</p>"
            : looks ? '<p class="cast-sum need">No look written yet. Every video prompt describes the character ' +
                "with it: open the card and fill it in from a Casting picture.</p>"
            : '<p class="cast-sum dim">No description yet.</p>') +
          lookCheck(cid) +
        "</div>" +
      "</div>" +
      '<div class="cast-edit">' +
        '<div class="cast-field heroref-field"><label data-tip="The pictures the video models open this character&apos;s shots on. Picked looks come first.">Looks</label>' +
          '<div class="heroref-strip"><span class="muted small">Loading…</span></div>' +
          '<div class="heroref-ctl">' +
            '<input type="file" class="heroref-file" accept="image/png,image/jpeg,image/webp" multiple hidden>' +
            '<button class="btn small" type="button" data-act="heroref-pick" ' +
              'data-tip="Pick a picture from a Casting batch as this character&apos;s look">' + Icon.get("aperture") + "Pick from Casting</button>" +
            '<button class="btn small ghost" type="button" data-act="heroref-upload" ' +
              'data-tip="Upload your own photos of this character">' + Icon.get("upload") + "Upload</button>" +
          "</div>" +
        "</div>" +
        '<div class="cast-cols">' +
          '<div class="cast-field"><label data-tip="What never changes: build, face, hair, marks, in words. The story writer reads it, and every video prompt states it. Keep it short and concrete.">Body</label>' +
            '<textarea data-f="body" rows="3" spellcheck="false" placeholder="Tall, lean, close-cropped grey hair, a scar through the left eyebrow">' + esc(ch.body || "") + "</textarea>" +
            '<div class="locref-ctl"><button class="linkish" type="button" data-act="autofill" ' +
              'data-tip="Fill Body and Outfit from what a Casting picture was made with. Read them, then Save.">Fill in from a Casting picture</button></div></div>' +
          '<div class="cast-field"><label data-tip="What they wear unless an outfit below says otherwise. The story writer reads it too.">Outfit</label>' +
            '<textarea data-f="outfit" rows="3" spellcheck="false" placeholder="Worn leather coat over a dark wool sweater">' + esc(ch.outfit || "") + "</textarea></div>" +
        "</div>" +
        '<div class="cast-field"><label>Emotions they show, separated by commas</label>' +
          '<input type="text" data-f="emotion" value="' + esc(emo) +
            '" placeholder="calm, focused, tense" spellcheck="false"></div>' +
        cardFold("c:" + cid + ":powers", "Powers", count(Object.keys(powers).length, "power", "powers"),
          '<div class="pw-list">' +
            (Object.keys(powers).length
              ? Object.keys(powers).map(function (pn) { return powerRow(pn); }).join("")
              : '<div class="muted small">No powers. Most characters need none.</div>') +
          "</div>" +
          '<div class="pw-add">' +
            '<select class="pw-type-sel">' +
              POWER_TYPES.map(function (t) {
                return '<option value="' + t + '">' + t + "</option>";
              }).join("") +
            "</select>" +
            '<button class="btn small" type="button" data-act="add-power" ' +
              'data-tip="Add a power of the selected type">' + Icon.get("plus") + "Add power</button>" +
          "</div>") +
        cardFold("c:" + cid + ":weapons", "Weapons", count(Object.keys(weapons).length, "weapon", "weapons"),
          '<p class="cf-note">What the story writer and the prompts know about the weapon. It shows up in a render through an outfit below that carries it.</p>' +
          '<div class="wp-list">' +
            (Object.keys(weapons).length
              ? Object.keys(weapons).map(function (wn) { return weaponRow(wn); }).join("")
              : '<div class="muted small">No weapons.</div>') +
          "</div>" +
          '<div class="pw-add">' +
            '<select class="wp-type-sel">' +
              WEAPON_TYPES.map(function (t) {
                return '<option value="' + esc(t) + '">' + esc(t) + "</option>";
              }).join("") +
            "</select>" +
            '<button class="btn small" type="button" data-act="add-weapon" ' +
              'data-tip="Add a weapon of the selected type, then pick its design pictures from a weapon batch">' + Icon.get("plus") + "Add weapon</button>" +
          "</div>") +
        cardFold("c:" + cid + ":loadouts", "Outfits for scenes", count(Object.keys(loadouts).length, "outfit", "outfits"),
          '<p class="cf-note">A named outfit, maybe with a weapon, and one picture of the character wearing it. ' +
            "Each scene can use a different one; the base outfit is used otherwise.</p>" +
          '<div class="ld-list">' +
            (Object.keys(loadouts).length
              ? Object.keys(loadouts).map(function (ln) {
                  return loadoutRow(ln, loadouts[ln]);
                }).join("")
              : '<div class="muted small">No extra outfits: the base outfit is used.</div>') +
          "</div>" +
          '<div class="pw-add">' +
            '<button class="btn small" type="button" data-act="add-loadout" ' +
              'data-tip="Add a named outfit (for example JulietArmored)">' + Icon.get("plus") + "Add outfit</button>" +
          "</div>" +
          '<div class="ld-scene-map-wrap">' +
            '<label data-tip="Which outfit this character wears in each scene. A shot can still override it.">Outfit per scene</label>' +
            '<div class="ld-scene-map"><span class="muted small">Save the character first.</span></div>' +
          "</div>") +
        cardFold("c:" + cid + ":lora", "Wan 2.2 LoRA", "Legacy: only the old Wan 2.2 model uses it",
          '<div class="cast-field lora-row">' +
            '<div class="lora-ctl">' +
              '<select class="cast-lora" data-tip="A character LoRA. Only shots rendered on the legacy Wan 2.2 model use it; LTX-2.5 and MiniMax H3 use the looks above.">' +
                '<option value="">None</option>' +
                loraOptions(characterLoras(), curLora, curLora) +
              "</select>" +
              '<input type="number" class="cast-lora-str" min="0.1" max="1.5" step="0.05" value="' +
                str.toFixed(2) + '"' + (curLora ? "" : " disabled") + ' data-tip="LoRA strength, 0.1 to 1.5">' +
            "</div>" +
          "</div>", "legacy") +
        '<div class="cast-actions">' +
          '<button class="btn small danger quiet" type="button" data-act="delete" ' +
            'data-tip="Remove this character from the movie&apos;s cast">' + Icon.get("trash") + "Remove</button>" +
          '<span class="spacer"></span>' +
          '<button class="btn small primary" type="button" data-act="save">Save character</button>' +
        "</div>" +
      "</div>" +
    "</article>";
  }

  function weaponRow(wid) {
    const types = WEAPON_TYPES.indexOf(wid) >= 0
      ? WEAPON_TYPES
      : WEAPON_TYPES.concat([wid]);
    return '<div class="wp-row" data-wid="' + esc(wid) + '">' +
      '<input type="text" class="wp-name" value="' + esc(wid) +
        '" placeholder="weapon id" spellcheck="false" data-tip="Weapon id — loadouts and the story reference it by this name">' +
      '<select class="wp-type-input" data-tip="Weapon family — shapes the prompt text">' +
        types.map(function (t) {
          return '<option value="' + esc(t) + '"' + (t === wid ? " selected" : "") +
            ">" + esc(t) + "</option>";
        }).join("") +
      "</select>" +
      '<textarea class="wp-desc" rows="1" placeholder="What it looks like: material, colour, glow" spellcheck="false"></textarea>' +
      '<input type="text" class="wp-handling" placeholder="handling (raised overhead, planted…)" spellcheck="false">' +
      '<input type="text" class="wp-sfx" placeholder="sfx (steel ring, energy hum)" spellcheck="false">' +
      '<button class="btn small" type="button" data-act="rm-weapon" ' +
        'data-tip="Remove this weapon">' + Icon.get("x") + "</button>" +
      '<div class="cast-field wp-ref-field">' +
        '<label data-tip="Design pictures of the weapon, alone or in hand. MiniMax H3 can take them as extra pictures; an outfit picture already shows the weapon.">Weapon pictures</label>' +
        '<div class="wp-ref-strip"><span class="muted small">loading…</span></div>' +
        '<div class="locref-ctl">' +
          '<button class="btn small" type="button" data-act="weaponref-pick" ' +
            'data-tip="Pick a picture from a weapon batch on Casting">Pick from Casting</button>' +
        "</div>" +
      "</div>" +
    "</div>";
  }

  function loadoutRow(name, ld) {
    ld = ld || {};
    return '<div class="ld-row" data-ld="' + esc(name) + '">' +
      '<input type="text" class="ld-name" value="' + esc(name) +
        '" placeholder="loadout name (JulietWithWeapon)" spellcheck="false">' +
      '<input type="text" class="ld-outfit" value="' + esc(ld.outfit || "") +
        '" placeholder="outfit text (crimson battle dress, leather + chainmail)" spellcheck="false">' +
      '<select class="ld-weapon" data-tip="Weapon this loadout carries">' +
        '<option value="">No weapon</option>' +
      "</select>" +
      '<button class="btn small" type="button" data-act="loadoutref-pick" ' +
        'data-tip="Pick one picture of the character in this outfit, holding this weapon, from a Casting batch">Pick its picture</button>' +
      '<button class="btn small" type="button" data-act="rm-loadout" ' +
        'data-tip="Remove this loadout">' + Icon.get("x") + "</button>" +
      '<div class="ld-ref-strip"><span class="muted small">No picture yet</span></div>' +
    "</div>";
  }

  function powerRow(pn) {
    const types = POWER_TYPES.indexOf(pn) >= 0
      ? POWER_TYPES
      : POWER_TYPES.concat([pn]);
    return '<div class="pw-row" data-pw="' + esc(pn) + '">' +
      '<span class="pw-swatch"></span>' +
      '<select class="pw-type-input" data-tip="Power type — change it and Save to re-type this power">' +
        types.map(function (t) {
          return '<option value="' + esc(t) + '"' + (t === pn ? " selected" : "") +
            ">" + esc(t) + "</option>";
        }).join("") +
      "</select>" +
      '<input type="color" class="pw-color" data-tip="power color">' +
      '<input type="text" class="pw-hex mono" maxlength="9" spellcheck="false" title="hex color">' +
      '<textarea class="pw-desc" rows="1" placeholder="descriptor" spellcheck="false"></textarea>' +
      '<input type="text" class="pw-motion" placeholder="motion" spellcheck="false">' +
      '<button class="btn small" type="button" data-act="rm-power" ' +
        'data-tip="Remove this power">' + Icon.get("x") + "</button>" +
      '<div class="pw-lora">' +
        '<span class="pw-lora-label">LoRA</span>' +
        '<select class="pw-lora-sel" data-tip="Power LoRA, applied only when the shot renders on the legacy Wan 2.2 model (LTX-2.5 and H3 use the reference images)">' +
          '<option value="">none</option>' +
          powerLoras().map(function (e) {
            const f = e.file || "";
            return '<option value="' + esc(f) + '">' +
              esc(e.display || e.file || e.id || "?") + "</option>";
          }).join("") +
        "</select>" +
        '<input type="number" class="pw-lora-str" min="0.1" max="1.5" step="0.05" value="0.70" disabled ' +
          'data-tip="Power LoRA strength 0.1–1.5">' +
      "</div>" +
      '<div class="cast-field powref-field">' +
        '<label data-tip="Effect pictures, one per strength level. A scene uses the picture of the highest level at or below its own.">Power pictures' +
          '<span class="muted small" style="margin-left:6px">one per level</span></label>' +
        '<div class="powref-strip"><span class="muted small">loading…</span></div>' +
        '<div class="locref-ctl">' +
          '<button class="btn small" type="button" data-act="powref-pick" ' +
            'data-tip="Pick a picture from a power batch on Casting. It goes in at the next level; change the number on it to move it.">Pick from Casting</button>' +
        "</div>" +
      "</div>" +
    "</div>";
  }

  function hydrateCard(card, ch) {
    const powers = ch.powers || {};
    card.querySelectorAll(".pw-row").forEach(function (rw) {
      const pd = powers[rw.dataset.pw] || {};
      setRowValues(rw, pd);
      loadPowerRefs(card, card.dataset.cid || "", rw.dataset.pw);
    });
    const cid = card.dataset.cid || "";
    const weapons = ch.weapons || {};
    card.querySelectorAll(".wp-row").forEach(function (rw) {
      const wd = weapons[rw.dataset.wid] || {};
      const de = rw.querySelector(".wp-desc");
      const ha = rw.querySelector(".wp-handling");
      const sf = rw.querySelector(".wp-sfx");
      if (de) de.value = wd.descriptor || "";
      if (ha) ha.value = Array.isArray(wd.handling)
        ? wd.handling.join(", ") : (wd.handling || "");
      if (sf) sf.value = wd.sfx || "";
      const ts = rw.querySelector(".wp-type-input");
      if (ts && wd.type && !Array.prototype.some.call(ts.options,
          function (o) { return o.value === wd.type; })) {
        const opt = document.createElement("option");
        opt.value = wd.type;
        opt.textContent = wd.type;
        ts.appendChild(opt);
      }
      if (ts && wd.type) ts.value = wd.type;
      loadWeaponRefs(card, cid, rw.dataset.wid);
    });
    const loadouts = ch.loadouts || {};
    card.querySelectorAll(".ld-row").forEach(function (rw) {
      const ld = loadouts[rw.dataset.ld] || {};
      const ws = rw.querySelector(".ld-weapon");
      if (ws) {
        ws.innerHTML = '<option value="">No weapon</option>' +
          weaponOptions(card, ld.weapon || "");
      }
      loadLoadoutRefs(card, cid, rw.dataset.ld);
    });
    if (Object.keys(loadouts).length) loadCharSceneMap(card, cid);
  }

  function setRowValues(rw, pd) {
    const col = safeColor(pd.color);
    const sw = rw.querySelector(".pw-swatch");
    const ci = rw.querySelector(".pw-color");
    const hi = rw.querySelector(".pw-hex");
    const de = rw.querySelector(".pw-desc");
    const mo = rw.querySelector(".pw-motion");
    if (sw) sw.style.backgroundColor = col;
    if (ci) ci.value = col;
    if (hi) hi.value = col;
    if (de) de.value = pd.descriptor || "";
    if (mo) mo.value = pd.motion || "";
    const ls = rw.querySelector(".pw-lora-sel");
    const li = rw.querySelector(".pw-lora-str");
    const lf = pd.lora && pd.lora.file ? pd.lora.file : "";
    if (ls) {
      if (lf && !Array.prototype.some.call(ls.options, function (o) { return o.value === lf; })) {
        const opt = document.createElement("option");
        opt.value = lf;
        opt.textContent = filePart(lf);
        ls.appendChild(opt);
      }
      ls.value = lf;
    }
    if (li) {
      li.value = Number(pd.lora && pd.lora.strength != null ? pd.lora.strength : 0.7).toFixed(2);
      li.disabled = !lf;
    }
  }

  function wirePowerRows(card) {
    card.querySelectorAll(".pw-row").forEach(function (rw) {
      const ci = rw.querySelector(".pw-color");
      const hi = rw.querySelector(".pw-hex");
      const sw = rw.querySelector(".pw-swatch");
      const ls = rw.querySelector(".pw-lora-sel");
      const li = rw.querySelector(".pw-lora-str");
      const pb = rw.querySelector('[data-act="powref-pick"]');
      if (pb) pb.onclick = function () {
        openPowerPick(card, card.dataset.cid || "", rw.dataset.pw || "");
      };
      if (ls && li) ls.onchange = function () {
        li.disabled = !ls.value;
      };
      if (!ci || !hi || !sw) return;
      ci.oninput = function () {
        hi.value = ci.value;
        sw.style.backgroundColor = safeColor(ci.value);
      };
      hi.onchange = function () {
        if (/^#[0-9a-fA-F]{3,8}$/.test(hi.value.trim())) {
          ci.value = hi.value.trim();
          sw.style.backgroundColor = ci.value;
        } else {
          hi.value = ci.value;
        }
      };
    });
  }

  /* ----------------------------------------------------------- location UI */
  function locCard(l) {
    const lid = l.id || "";
    const enabled = l.enabled !== false;
    const curLora = l.lora && l.lora.file ? l.lora.file : "";
    const str = Number(l.lora && l.lora.strength != null ? l.lora.strength : 0.6) || 0.6;
    const pics = (l.reference_images || []).length;
    const open = !!openCards["l:" + lid];
    return '<article class="cast-card place' + (open ? " open" : "") + (enabled ? "" : " benched") + '" data-lid="' + esc(lid) + '">' +
      '<div class="cast-head">' +
        '<button class="cast-portrait wide' + (pics ? "" : " none") + '" type="button" data-act="toggle" ' +
          'data-tip="' + (pics ? "Open the card" : "No picture yet: pick one from a Casting batch, or upload one") + '">' +
          '<span class="cp-ph">' + Icon.get("image") + "</span></button>" +
        '<div class="cast-id">' +
          '<div class="cast-top loc-id-row">' +
            '<input type="text" class="cast-name" data-f="lid" value="' + esc(lid) +
              '" placeholder="Short name (arena, palace)" spellcheck="false" aria-label="Place name" ' +
              'data-tip="The name the story writer and the prompts use for this place. Type a new one and press Enter: its pictures and shots follow.">' +
            editButton(open) +
          "</div>" +
          '<div class="cast-meta"><span class="look-state ' + (pics ? "ok" : "need") + '">' +
            (pics ? pics + (pics === 1 ? " picture" : " pictures") : "No picture yet") + "</span>" +
            inMovie(enabled, "place") + "</div>" +
          (l.descriptor ? '<p class="cast-sum">' + esc(l.descriptor) + "</p>" : '<p class="cast-sum dim">No description yet.</p>') +
        "</div>" +
      "</div>" +
      '<div class="cast-edit">' +
        '<div class="cast-field locref-field"><label data-tip="Pictures of this place. Shots here use them to keep the place the same.">Pictures</label>' +
          '<div class="locref-strip"><span class="muted small">Loading…</span></div>' +
          '<div class="locref-ctl">' +
            '<button class="btn small" type="button" data-act="locref-pick" ' +
              'data-tip="Pick a picture of this place from a Casting batch">' + Icon.get("aperture") + "Pick from Casting</button>" +
            '<input type="file" class="locref-file" accept="image/png,image/jpeg,image/webp" multiple hidden>' +
            '<button class="btn small ghost" type="button" data-act="locref-upload" ' +
              'data-tip="Upload your own pictures of this place">' + Icon.get("upload") + "Upload</button>" +
          "</div>" +
        "</div>" +
        '<div class="cast-cols">' +
          '<div class="cast-field"><label>What it looks like</label>' +
            '<textarea data-f="descriptor" rows="3" spellcheck="false" placeholder="A flooded subway platform, tiles cracked, water up to the ankles">' + esc(l.descriptor || "") + "</textarea></div>" +
          '<div class="cast-field"><label>Light</label>' +
            '<input type="text" data-f="lighting" value="' + esc(l.lighting || "") +
              '" placeholder="cold moonlight, harsh fluorescents" spellcheck="false"></div>' +
        "</div>" +
        cardFold("l:" + lid + ":scenes", "Picture per scene", "Which of its pictures each scene uses",
          '<div class="cast-field locscene-field">' +
            '<div class="locscene-map"><span class="muted small">Loading…</span></div>' +
          "</div>") +
        cardFold("l:" + lid + ":lora", "Wan 2.2 LoRA", "Legacy: only the old Wan 2.2 model uses it",
          '<div class="cast-field lora-row">' +
            '<div class="lora-ctl">' +
              '<select class="loc-lora" data-tip="An environment LoRA. Only shots rendered on the legacy Wan 2.2 model use it; LTX-2.5 and MiniMax H3 use the pictures above.">' +
                '<option value="">None</option>' +
                loraOptions(envLoras(), curLora, curLora) +
              "</select>" +
              '<input type="number" class="loc-lora-str" min="0.1" max="1.5" step="0.05" value="' +
                str.toFixed(2) + '"' + (curLora ? "" : " disabled") + ' data-tip="LoRA strength, 0.1 to 1.5">' +
            "</div>" +
          "</div>", "legacy") +
        '<div class="cast-actions">' +
          '<button class="btn small danger quiet" type="button" data-act="delete">' + Icon.get("trash") + "Remove</button>" +
          '<span class="spacer"></span>' +
          '<button class="btn small primary" type="button" data-act="save">Save place</button>' +
        "</div>" +
      "</div>" +
    "</article>";
  }

  /* ------------------------------------------------------------- data utils */
  function splitCsv(s) {
    return String(s || "").split(",").map(function (x) { return x.trim(); }).filter(Boolean);
  }

  function collectPowers(card) {
    /* Power rows own descriptor/motion/color/lora — but `reference_images`
       (the level images) live in the saved YAML and are edited through their
       own endpoints, so they MUST be carried over here or every character
       Save would silently wipe the power's levels. */
    const ch = ((State.bible || {}).characters || {})[card.dataset.cid] || {};
    const prev = ch.powers || {};
    const out = {};
    card.querySelectorAll(".pw-row").forEach(function (rw) {
      const t = (rw.querySelector(".pw-type-input").value || "").trim();
      if (!t) return;
      out[t] = {
        descriptor: rw.querySelector(".pw-desc").value || "",
        motion: rw.querySelector(".pw-motion").value || "",
        color: safeColor(rw.querySelector(".pw-color").value),
      };
      if (prev[t] && Array.isArray(prev[t].reference_images)) {
        out[t].reference_images = prev[t].reference_images;
      }
      const ls = rw.querySelector(".pw-lora-sel");
      const li = rw.querySelector(".pw-lora-str");
      if (ls && ls.value) {
        out[t].lora = {
          file: ls.value,
          strength: li ? Number(li.value) || 0.7 : 0.7,
        };
      }
    });
    return out;
  }

  function collectWeapons(card) {
    /* Weapon rows own type/descriptor/handling/sfx — but `reference_images`
       (the design stills) live in the saved YAML and are edited through
       their own endpoints, so they MUST be carried over or every character
       Save would silently wipe them. */
    const ch = ((State.bible || {}).characters || {})[card.dataset.cid] || {};
    const prev = ch.weapons || {};
    const out = {};
    card.querySelectorAll(".wp-row").forEach(function (rw) {
      const wid = (rw.querySelector(".wp-name").value || "").trim();
      if (!wid) return;
      out[wid] = {
        type: (rw.querySelector(".wp-type-input") || {}).value || "custom",
        descriptor: (rw.querySelector(".wp-desc") || {}).value || "",
        handling: splitCsv((rw.querySelector(".wp-handling") || {}).value || ""),
        sfx: (rw.querySelector(".wp-sfx") || {}).value || "",
      };
      if (prev[wid] && Array.isArray(prev[wid].reference_images)) {
        out[wid].reference_images = prev[wid].reference_images;
      }
      if (prev[wid] && prev[wid].lora) out[wid].lora = prev[wid].lora;
    });
    return out;
  }

  function collectLoadouts(card) {
    const out = {};
    card.querySelectorAll(".ld-row").forEach(function (rw) {
      const name = (rw.querySelector(".ld-name").value || "").trim();
      if (!name) return;
      out[name] = {
        outfit: (rw.querySelector(".ld-outfit") || {}).value || "",
        weapon: (rw.querySelector(".ld-weapon") || {}).value || "",
      };
    });
    return out;
  }

  function confirmBox(html, onYes) {    const veil = openModal('<div>' + html +
      '<div class="ctl-row" style="justify-content:flex-end;margin-top:12px">' +
        '<button class="btn" type="button" data-cm="no">Cancel</button>' +
        '<button class="btn danger" type="button" data-cm="yes">Confirm</button>' +
      "</div></div>", { title: "Confirm" });
    if (!veil) return;
    veil.querySelector('[data-cm="yes"]').onclick = function () { closeModal(veil); onYes(); };
    veil.querySelector('[data-cm="no"]').onclick = function () { closeModal(veil); };
  }

  /* ---------------------------------------------------------------- actions */
  async function saveCharacter(card, ch) {
    const name = (card.querySelector('[data-f="name"]').value || "").trim();
    if (!name) { toast("Give the character a name first", true); return; }
    const body = {
      name: name,
      tier: card.querySelector('[data-f="tier"]').value,
      body: card.querySelector('[data-f="body"]').value,
      outfit: card.querySelector('[data-f="outfit"]').value,
      emotion_palette: splitCsv(card.querySelector('[data-f="emotion"]').value),
      powers: collectPowers(card),
      weapons: collectWeapons(card),
      loadouts: collectLoadouts(card),
      enabled: card.querySelector('[data-f="enabled"]').checked,
    };
    const lf = card.__lookFrom;
    if (lf && lf.from && lf.body === body.body && lf.outfit === body.outfit) body.look_source = "casting:" + lf.from;
    const sel = card.querySelector(".cast-lora");
    const f = sel ? sel.value : "";
    body.lora = f
      ? { file: f, strength: Number(card.querySelector(".cast-lora-str").value) || 0.8 }
      : null;
    const cid = ch.id || "";
    const url = "/api/projects/" + encodeURIComponent(pid()) + "/cast/characters" +
      (cid ? "/" + encodeURIComponent(cid) : "");
    const r = await post(url, body, 60000);
    if (!r) return null;
    if (r.ok) {
      toast("Saved " + (r.character && r.character.name ? r.character.name : name));
      setUnsaved(card, false);
      await State.refreshProject();
      afterSave();
      return r.character || null;    // callers need the id for new characters
    } else {
      toast(r.error || "save failed", true);
    }
  }

  function deleteCharacter(card, ch) {
    const name = ch.name || ch.id || "this character";
    confirmBox('<p>Delete <b>' + esc(name) + '</b> from the project bible?</p>' +
      '<p class="muted small">Regenerate the story afterwards if you want it reflected there.</p>',
      async function () {
        const r = await post("/api/projects/" + encodeURIComponent(pid()) +
          "/cast/characters/" + encodeURIComponent(ch.id || "") + "/delete", {}, 30000);
        if (!r) return;
        toast(r.ok ? "character deleted" : (r.error || "delete failed"), !r.ok);
        if (r.ok) { unsaved.delete("c:" + (ch.id || "")); await State.refreshProject(); afterSave(); }
      });
  }

  async function newCharacter() {
    const r = await post("/api/projects/" + encodeURIComponent(pid()) + "/cast/characters", {
      name: "New Character",
      tier: "supporting",
      body: "",
      outfit: "",
      emotion_palette: ["calm", "focused", "tense"],
      powers: {},
      enabled: true,
    }, 30000);
    if (!r) return;
    if (r.ok) { toast("Character added"); await State.refreshProject(); }
    else toast(r.error || "failed", true);
  }

  async function saveLocation(card, l) {
    // a new name typed in the card is applied first (leaving the field
    // already started it), so the fields are saved under that name
    let lid = card.__renaming ? await card.__renaming : await renameLocation(card, l);
    if (!lid) { toast("Give the place a name first", true); return; }
    const body = {
      id: lid,
      descriptor: card.querySelector('[data-f="descriptor"]').value,
      lighting: card.querySelector('[data-f="lighting"]').value,
      enabled: card.querySelector('[data-f="enabled"]').checked,
    };
    const ls = card.querySelector(".loc-lora");
    const li = card.querySelector(".loc-lora-str");
    if (ls && ls.value) {
      body.lora = {
        file: ls.value,
        strength: li ? Number(li.value) || 0.6 : 0.6,
      };
    } else {
      body.lora = null;   // explicitly cleared
    }
    const r = await post("/api/projects/" + encodeURIComponent(pid()) +
      "/cast/locations/" + encodeURIComponent(lid), body, 30000);
    if (!r) return;
    toast(r.ok ? "Place saved" : (r.error || "The place was not saved"), !r.ok);
    if (r.ok) {
      setUnsaved(card, false);
      await State.refreshProject();
      afterSave();
    }
  }

  /* Apply the name typed in the card (on Enter, on leaving the field, and
     before a save). Resolves to the place's name afterwards: the new one,
     or the old one when it was not changed or could not be. */
  async function renameLocation(card, l) {
    const old = l.id || "";
    const inp = card.querySelector('[data-f="lid"]');
    const want = ((inp && inp.value) || "").trim();
    if (!want || want === old) { if (inp) inp.value = old; return old; }
    const r = await post("/api/projects/" + encodeURIComponent(pid()) +
      "/cast/locations/" + encodeURIComponent(old) + "/rename",
      { new_id: want }, 60000);
    if (r && r.ok) {
      toast("Renamed to " + r.id
        + (r.shots_updated ? " (" + r.shots_updated + " shots updated)" : ""));
      if (openCards["l:" + old]) { openCards["l:" + r.id] = true; delete openCards["l:" + old]; }
      l.id = r.id;
      await State.refreshProject();
      await State.refreshShots();
      return r.id;
    }
    toast((r && r.error) || "The place was not renamed", true);
    if (inp) inp.value = old;
    return old;
  }

  function deleteLocation(card, l) {
    confirmBox('<p>Delete location <b>' + esc(l.id || "?") + '</b> from the bible?</p>',
      async function () {
        const r = await post("/api/projects/" + encodeURIComponent(pid()) +
          "/cast/locations/" + encodeURIComponent(l.id || "") + "/delete", {}, 30000);
        if (!r) return;
        toast(r.ok ? "location deleted" : (r.error || "delete failed"), !r.ok);
        if (r.ok) { unsaved.delete("l:" + (l.id || "")); await State.refreshProject(); afterSave(); }
      });
  }

  async function newLocation() {
    const name = await Dlg.prompt({
      title: "New place",
      label: "Its short name, the way the story calls it. The story writer and the prompts use this name.",
      placeholder: "arena, palace lobby", okLabel: "Add place" });
    if (name == null) return;
    if (!name.trim()) { toast("Give the place a name", true); return; }
    const r = await post("/api/projects/" + encodeURIComponent(pid()) + "/cast/locations", {
      name: name.trim(),
      descriptor: "",
      lighting: "",
      enabled: true,
    }, 30000);
    if (!r) return;
    if (r.ok) {
      const lid = (r.location && r.location.id) || "";
      openCards["l:" + lid] = true;     // open: its pictures come next
      toast("Added " + lid);
      await State.refreshProject();
    } else toast(r.error || "The place was not added", true);
  }

  /* -------------------------------------------------------------- ref image */
  async function loadRefs(el) {
    const strip = el.querySelector("#cast-ref-strip");
    if (!strip) return;
    const r = await window.API.get("/api/projects/" + encodeURIComponent(pid()) + "/refs", 20000, true);
    if (!r || !Array.isArray(r.refs)) {
      strip.innerHTML = '<span class="muted small">unavailable</span>';
      return;
    }
    if (!r.refs.length) {
      strip.innerHTML = '<span class="muted small">no reference images yet</span>';
      return;
    }
    strip.innerHTML = r.refs.map(function (ref) {
      const file = ref.file || "";
      return '<div class="ref-thumb' + (ref.active ? " active" : "") + '" data-file="' + esc(file) +
        '" data-tip="' + (ref.active ? "Active reference — seeds every keyframe" : "Click to make this the active reference") + '">' +
        '<img loading="lazy" src="' + esc(refUrl(file)) + '" alt="reference">' +
        (ref.active ? '<span class="ref-tag">ACTIVE</span>' : "") +
        '<button class="ref-x" type="button" title="delete" data-tip="Delete this reference">' + Icon.get("x") + "</button>" +
      "</div>";
    }).join("");
    strip.querySelectorAll(".ref-thumb").forEach(function (th) {
      th.onclick = function () { activateRef(el, th.dataset.file); };
      const x = th.querySelector(".ref-x");
      if (x) x.onclick = function (e) {
        e.stopPropagation();
        deleteRef(el, th.dataset.file);
      };
    });
  }

  async function activateRef(el, file) {
    const r = await post("/api/projects/" + encodeURIComponent(pid()) + "/refs/" +
      encodeURIComponent(filePart(file)) + "/activate", {}, 30000);
    if (!r) return;
    toast(r.ok ? "reference activated" : (r.error || "failed"), !r.ok);
    if (r.ok) loadRefs(el);
  }

  async function deleteRef(el, file) {
    const r = await post("/api/projects/" + encodeURIComponent(pid()) + "/refs/" +
      encodeURIComponent(filePart(file)) + "/delete", {}, 30000);
    if (!r) return;
    toast(r.ok ? "reference deleted" : (r.error || "failed"), !r.ok);
    if (r.ok) loadRefs(el);
  }

  async function uploadRefs(el) {
    const inp = el.querySelector("#cast-ref-file");
    if (!inp) return;
    const files = Array.prototype.slice.call(inp.files || []);
    if (!files.length) return;
    const r = await uploadImage("/api/projects/" + encodeURIComponent(pid()) + "/refs/upload", files);
    inp.value = "";
    if (!r) return;
    if (r.ok) {
      toast("Uploaded" + (r.refs && r.refs.length ? " " + r.refs.length + (r.refs.length === 1 ? " picture" : " pictures") : ""));
      loadRefs(el);
    } else {
      toast(r.error || "upload failed", true);
    }
  }

  /* ----------------------------------------------------- character hero refs */
  function heroRefUrl(cid, file) {
    return "/api/herorefs/" + encodeURIComponent(pid()) + "/" +
      encodeURIComponent(cid) + "/" + encodeURIComponent(file);
  }

  async function loadHeroRefs(card, cid) {
    const strip = card.querySelector(".heroref-strip");
    if (!strip) return;
    const r = await window.API.get("/api/projects/" + encodeURIComponent(pid()) +
      "/cast/characters/" + encodeURIComponent(cid) + "/herorefs", 20000, true);
    if (!r) {
      strip.innerHTML = '<span class="muted small">Could not load the looks.</span>';
      return;
    }
    const refs = Array.isArray(r.refs) ? r.refs : [];
    const auto = Array.isArray(r.auto) ? r.auto : [];
    const manifest = Array.isArray(r.references) ? r.references : [];
    const uploaded = refs.filter(function (f) {
      return !manifest.some(function (m) { return (m.copied || m.file) === f; });
    });
    // the portrait: the first picture the models would use
    setPortrait(card, manifest.length ? heroRefUrl(cid, manifest[0].copied || manifest[0].file)
      : uploaded.length ? heroRefUrl(cid, uploaded[0]) : auto.length ? (auto[0].url || "") : "");
    setLookState(card, manifest.length, "look", "looks", "No look yet");
    if (!refs.length && !auto.length && !manifest.length) {
      strip.innerHTML = '<span class="muted small">No looks yet. Pick one from a Casting batch, or upload a photo.</span>';
      return;
    }
    const group = function (label, html) {
      return html ? '<div class="look-group"><span class="look-h">' + label + '</span><div class="look-row">' + html + "</div></div>" : "";
    };
    strip.innerHTML =
      group("Picked, used first", manifest.map(function (m) {
        const role = m.role || "primary";
        const tip = (m.type || "face") + ": " + (m.name || "") + ", " + role +
          (m.tag ? ", for the outfit " + m.tag : "") + ". × removes it.";
        return '<div class="heroref-thumb' + (role === "secondary" ? " auto" : "") + '" ' +
          'data-tip="' + esc(tip) + '">' +
          '<img loading="lazy" src="' + esc(heroRefUrl(cid, m.copied || m.file)) + '" alt="picked look">' +
          '<span class="heroref-tag">' + esc(m.tag ? "outfit" : role === "primary" ? "main" : "extra") + "</span>" +
          '<button class="ref-x" type="button" data-ref-rm="' + esc(m.file) + '" ' +
            'data-tip="Remove this look">×</button>' +
        "</div>";
      }).join("")) +
      group("Uploaded, used when nothing is picked", uploaded.map(function (f) {
        return '<div class="heroref-thumb" data-tip="An uploaded photo">' +
          '<img loading="lazy" src="' + esc(heroRefUrl(cid, f)) + '" alt="uploaded look">' +
        "</div>";
      }).join("")) +
      group("Found in a Casting batch, used only when nothing else is there", auto.map(function (a) {
        return '<div class="heroref-thumb auto" data-tip="The batch&apos;s main picture, found by name">' +
          '<img loading="lazy" src="' + esc(a.url || "") + '" alt="batch picture">' +
        "</div>";
      }).join(""));
    strip.querySelectorAll("[data-ref-rm]").forEach(function (b) {
      b.onclick = async function (e) {
        e.stopPropagation();
        const r2 = await post("/api/projects/" + encodeURIComponent(pid()) +
          "/cast/characters/" + encodeURIComponent(cid) + "/herorefs/remove",
          { file: b.dataset.refRm }, 30000);
        if (r2 && r2.ok) { toast("Look removed"); loadHeroRefs(card, cid); }
        else toast((r2 && r2.error) || "remove failed", true);
      };
    });
  }

  async function uploadHeroRefs(card, cid) {
    const inp = card.querySelector(".heroref-file");
    if (!inp) return;
    const files = Array.prototype.slice.call(inp.files || []);
    if (!files.length) return;
    const r = await uploadImage("/api/projects/" + encodeURIComponent(pid()) +
      "/cast/characters/" + encodeURIComponent(cid) + "/herorefs/upload", files);
    inp.value = "";
    if (!r) return;
    if (r.ok) {
      toast("Photo uploaded");
      loadHeroRefs(card, cid);
    } else {
      toast(r.error || "upload failed", true);
    }
  }

  /* -------------------------------------------------------- power refs */
  function powerRefUrl(cid, pw, file) {
    return "/api/powerrefs/" + encodeURIComponent(pid()) + "/" +
      encodeURIComponent(cid) + "/" + encodeURIComponent(pw) + "/" +
      encodeURIComponent(file);
  }

  function powerRowEl(card, pw) {
    let hit = null;
    card.querySelectorAll(".pw-row").forEach(function (rw) {
      if ((rw.dataset.pw || "") === pw) hit = rw;
    });
    return hit;
  }

  async function loadPowerRefs(card, cid, pw) {
    const rw = powerRowEl(card, pw);
    if (!rw) return;
    const strip = rw.querySelector(".powref-strip");
    if (!strip) return;
    const r = await window.API.get("/api/projects/" + encodeURIComponent(pid()) +
      "/cast/characters/" + encodeURIComponent(cid) + "/powers/" +
      encodeURIComponent(pw) + "/refs", 20000, true);
    if (!r) { strip.innerHTML = '<span class="muted small">unavailable</span>'; return; }
    const manifest = Array.isArray(r.references) ? r.references : [];
    const files = Array.isArray(r.refs) ? r.refs : [];
    if (!manifest.length && !files.length) {
      strip.innerHTML = '<span class="muted small">no power references yet — add one image per intensity level</span>';
      return;
    }
    strip.innerHTML = manifest.map(function (m) {
      const lvl = m.level != null ? m.level : "";
      return '<div class="heroref-thumb powref-thumb" ' +
          'data-tip="Power reference · ' + esc((m.name || "") + "/" + (m.file || "")) +
          ' · level ' + esc(lvl) + ' (edit the number, × removes)">' +
        '<img loading="lazy" src="' + esc(powerRefUrl(cid, pw, m.copied || m.file)) + '" alt="power reference">' +
        '<input type="number" class="powref-lvl" min="1" max="99" value="' + esc(lvl) + '" ' +
          'data-ref="' + esc(m.file) + '" data-tip="Intensity level (1..N)">' +
        '<button class="ref-x" type="button" data-ref-rm="' + esc(m.file) + '" ' +
          'data-tip="remove this power reference">×</button>' +
      "</div>";
    }).join("") + files.filter(function (f) {
      return !manifest.some(function (m) { return (m.copied || m.file) === f; });
    }).map(function (f) {
      return '<div class="heroref-thumb powref-thumb" data-tip="Uploaded power reference">' +
        '<img loading="lazy" src="' + esc(powerRefUrl(cid, pw, f)) + '" alt="power reference">' +
      "</div>";
    }).join("");
    strip.querySelectorAll("[data-ref-rm]").forEach(function (b) {
      b.onclick = async function (e) {
        e.stopPropagation();
        const r2 = await post("/api/projects/" + encodeURIComponent(pid()) +
          "/cast/characters/" + encodeURIComponent(cid) + "/powers/" +
          encodeURIComponent(pw) + "/refs/remove", { file: b.dataset.refRm }, 30000);
        if (r2 && r2.ok) { toast("Power picture removed"); loadPowerRefs(card, cid, pw); }
        else toast((r2 && r2.error) || "remove failed", true);
      };
    });
    strip.querySelectorAll(".powref-lvl").forEach(function (inp) {
      inp.onchange = async function () {
        const v = parseInt(inp.value, 10);
        const r2 = await post("/api/projects/" + encodeURIComponent(pid()) +
          "/cast/characters/" + encodeURIComponent(cid) + "/powers/" +
          encodeURIComponent(pw) + "/refs/update",
          { file: inp.dataset.ref, level: isNaN(v) ? 1 : v }, 30000);
        if (r2 && r2.ok) toast("Power level changed");
        else toast((r2 && r2.error) || "level update failed", true);
        loadPowerRefs(card, cid, pw);
      };
    });
  }

  function openPowerPick(card, cid, pw) {
    openBatchPicker({
      type: "power",
      title: "Pick a picture of the power " + pw,
      onSelect: async function (name, file) {
        const r = await post("/api/projects/" + encodeURIComponent(pid()) +
          "/cast/characters/" + encodeURIComponent(cid) + "/powers/" +
          encodeURIComponent(pw) + "/refs/select",
          { type: "power", name: name, file: file, append: true }, 30000);
        if (!r) return false;
        if (r.ok) { toast("Power picture added"); return true; }
        toast(r.error || "select failed", true);
        return false;
      },
      onDone: function () { loadPowerRefs(card, cid, pw); },
    });
  }

  /* ----------------------------------------------------------- batch picker */
  function datasetImgUrl(type, name, file) {
    return "/api/dataset_img?type=" + encodeURIComponent(type) +
      "&name=" + encodeURIComponent(String(name || "")) +
      "&file=" + encodeURIComponent(normPath(file));
  }

  /* A picture from a Casting batch: a click selects it, "Use this picture"
     (or a double-click, or Enter) hands it to opts.onSelect. While one is
     being added, further clicks wait: a double-click adds it once. */
  function openBatchPicker(opts) {
    const st = { closed: false, datasets: [], busy: false, file: "" };
    const multi = Array.isArray(opts.roles) && opts.roles.length > 0;
    const veil = openModal(
      '<div class="batch-pick-head">' +
        '<label class="batch-label">Batch' +
          '<select id="pick-batch"><option value="">Loading…</option></select>' +
        "</label>" +
        (multi
          ? '<label class="batch-label">As' +
              '<select id="pick-role">' +
                opts.roles.map(function (r) {
                  return '<option value="' + esc(r.value) + '">' + esc(r.label) + "</option>";
                }).join("") +
              "</select></label>" +
            '<label class="toggle batch-label" data-tip="Add it as another picture instead of replacing the one it would replace">' +
              '<input type="checkbox" id="pick-append"><span></span><em>Keep the others</em></label>'
          : "") +
        (opts.extraInput
          ? '<label class="batch-label" data-tip="' + esc(opts.extraInput.tip || "") + '">' +
              esc(opts.extraInput.label || "") +
              '<input type="text" id="pick-extra" placeholder="' +
                esc(opts.extraInput.placeholder || "") + '" spellcheck="false">' +
            "</label>"
          : "") +
        (opts.type === "face"
          ? '<label class="batch-label" data-tip="Each picture records the weapon it was made with">Show' +
              '<select id="pick-filter">' +
                '<option value="all">All pictures</option>' +
                '<option value="armed">With a weapon</option>' +
                '<option value="plain">Without a weapon</option>' +
              "</select></label>"
          : "") +
        '<span class="pick-count muted small"></span>' +
      "</div>" +
      '<div class="pick-grid" id="pick-grid" role="listbox" aria-label="Pictures">' +
        '<span class="muted small">Loading the batches…</span>' +
      "</div>" +
      '<div class="pick-foot">' +
        '<span class="muted small" id="pick-note">Click a picture to select it.</span>' +
        '<button class="btn" type="button" id="pick-cancel">Cancel</button>' +
        '<button class="btn primary" type="button" id="pick-use" disabled>Use this picture</button>' +
      "</div>",
      { title: opts.title, wide: true });
    if (!veil) return;
    bindTips(veil);
    const close = function () {
      if (st.closed) return;
      st.closed = true;
      closeModal(veil);
    };
    const x = veil.querySelector(".ws-modal-x");
    if (x) x.onclick = close;
    veil.addEventListener("click", function (e) {
      if (e.target === veil) close();
    });
    veil.querySelector("#pick-cancel").onclick = close;
    const useBtn = veil.querySelector("#pick-use");

    function select(th) {
      if (st.busy) return;
      veil.querySelectorAll(".pick-thumb.sel").forEach(function (o) {
        o.classList.remove("sel");
        o.setAttribute("aria-selected", "false");
      });
      th.classList.add("sel");
      th.setAttribute("aria-selected", "true");
      st.file = th.dataset.file;
      useBtn.disabled = false;
    }

    async function use() {
      const d = st.datasets[Number(veil.querySelector("#pick-batch").value) || 0];
      if (st.closed || st.busy || !d || !st.file) return;
      st.busy = true;
      useBtn.disabled = true;
      useBtn.textContent = "Adding…";
      const th = veil.querySelector(".pick-thumb.sel");
      if (th) th.classList.add("picked");
      const roleSel = veil.querySelector("#pick-role");
      const appendBox = veil.querySelector("#pick-append");
      const extraEl = veil.querySelector("#pick-extra");
      let ok = false;
      try {
        ok = await opts.onSelect(
          d.name, st.file,
          roleSel ? roleSel.value : "primary",
          appendBox ? !!appendBox.checked : false,
          extraEl ? extraEl.value : "");
      } finally {
        st.busy = false;
        useBtn.textContent = "Use this picture";
        useBtn.disabled = !st.file;
        if (th) th.classList.remove("picked");
      }
      if (st.closed || !ok) return;
      if (appendBox && appendBox.checked) {
        // keep the picker open so another picture can follow
        toast("Picture added");
        return;
      }
      close();
      opts.onDone();
    }
    useBtn.onclick = use;

    function renderGrid() {
      if (st.closed) return;
      const sel = veil.querySelector("#pick-batch");
      const grid = veil.querySelector("#pick-grid");
      const note = veil.querySelector("#pick-note");
      const count = veil.querySelector(".pick-count");
      const d = st.datasets[Number(sel.value) || 0];
      if (!d) { grid.innerHTML = ""; return; }
      const meta = d.meta || {};
      const filt = (veil.querySelector("#pick-filter") || {}).value || "all";
      const metaOf = function (c) {
        return meta[filePart(normPath(c))] || meta[normPath(c)] || null;
      };
      let cands = Array.isArray(d.candidates) ? d.candidates : [];
      if (filt === "armed") cands = cands.filter(function (c) {
        return metaOf(c) && metaOf(c).weapon;
      });
      if (filt === "plain") cands = cands.filter(function (c) {
        return !metaOf(c) || !metaOf(c).weapon;
      });
      const kept = d.kept != null ? d.kept : 0;
      if (count) count.textContent = kept + " kept";
      const shown = cands.slice(0, 60);
      if (shown.indexOf(st.file) < 0) st.file = "";
      useBtn.disabled = st.busy || !st.file;
      grid.innerHTML = shown.length ? shown.map(function (c) {
        const m = metaOf(c) || {};
        const tip = [
          m.weapon ? "with " + m.weapon : opts.type === "face" ? "no weapon" : "",
          m.outfit ? "outfit: " + m.outfit : "",
          m.handling ? m.handling : "",
          m.level ? "level " + m.level : "",
        ].filter(Boolean).join(", ");
        const on = c === st.file;
        return '<div class="pick-thumb' + (on ? " sel" : "") + '" role="option" tabindex="0" aria-selected="' +
          on + '" data-file="' + esc(c) + '"' + (tip ? ' data-tip="' + esc(tip) + '"' : "") + ">" +
          '<img loading="lazy" src="' + esc(datasetImgUrl(opts.type, d.name, c)) +
          '" alt="">' +
          (m.weapon
            ? '<span class="pick-chip armed">Armed</span>'
            : (m.outfit || m.level
              ? '<span class="pick-chip">' + esc(m.level ? "Level " + m.level : "Plain") + "</span>"
              : "")) +
          '<span class="pick-mark" aria-hidden="true">' + Icon.get("check") + "</span>" +
        "</div>";
      }).join("") : '<span class="muted small">No pictures in this batch to show.</span>';
      if (note) note.textContent = cands.length > 60
        ? "Showing the first 60 of " + cands.length + "."
        : "Click a picture to select it, then Use this picture. A double-click adds it at once.";
      grid.querySelectorAll(".pick-thumb").forEach(function (th) {
        th.onclick = function () { if (!st.closed) select(th); };
        th.ondblclick = function () { if (!st.closed) { select(th); use(); } };
        th.onkeydown = function (e) {
          if (e.key === " " || e.key === "Enter") {
            e.preventDefault();
            select(th);
            if (e.key === "Enter") use();
          }
        };
      });
    }

    (async function () {
      const r = await window.API.get("/api/pick/images?type=" +
        encodeURIComponent(opts.type), 20000);
      if (st.closed) return;
      const sel = veil.querySelector("#pick-batch");
      const grid = veil.querySelector("#pick-grid");
      if (!r || !Array.isArray(r.datasets) || !r.datasets.length) {
        if (grid) grid.innerHTML =
          '<span class="muted small">No batch of this kind on Casting yet. Make one there, keep the best pictures, then pick one here.</span>';
        sel.innerHTML = '<option value="">None yet</option>';
        return;
      }
      st.datasets = r.datasets;
      sel.innerHTML = r.datasets.map(function (d, i) {
        const kept = d.kept != null ? d.kept : 0;
        return '<option value="' + i + '">' + esc(d.name || "Batch") + " (" + kept + " kept)</option>";
      }).join("");
      sel.onchange = renderGrid;
      const filt = veil.querySelector("#pick-filter");
      if (filt) filt.onchange = renderGrid;
      renderGrid();
    })();
  }

  /* Auto-fill Body/Outfit from a batch still's provenance: the batch already
     knows the identity axes and the outfit/weapon each still was made with,
     so the operator never retypes what the metadata records. */
  function openAutofillPick(card, ch) {
    openBatchPicker({
      type: "face",
      title: "Fill in Body and Outfit from a Casting picture",
      onSelect: async function (name, file) {
        const r = await post("/api/projects/" + encodeURIComponent(pid()) +
          "/cast/characters/" + encodeURIComponent(ch.id || "") + "/autofill",
          { type: "face", name: name, file: file }, 30000);
        if (!r) return false;
        if (!r.ok) { toast(r.error || "autofill failed", true); return false; }
        const b = card.querySelector('[data-f="body"]');
        const o = card.querySelector('[data-f="outfit"]');
        if (b && r.body) b.value = r.body;
        if (o && r.outfit) o.value = r.outfit;
        // Save tells the server this text is the picture's while it still is
        card.__lookFrom = { from: r.from || "", body: b ? b.value : "", outfit: o ? o.value : "" };
        setUnsaved(card, true);
        toast("Filled in from " + file + (r.weapon ? " (weapon: " + r.weapon + ")" : "")
          + ". Read it, then Save.");
        return true;
      },
      onDone: function () { /* values are in the fields; Save persists them */ },
    });
  }

  function openHeroPick(card, cid) {
    openBatchPicker({
      type: "face",
      title: "Pick a look from a Casting batch",
      roles: [{ value: "primary", label: "Main look" },
              { value: "secondary", label: "Extra look" }],
      onSelect: async function (name, file, role, append) {
        const r = await post("/api/projects/" + encodeURIComponent(pid()) +
          "/cast/characters/" + encodeURIComponent(cid) + "/herorefs/select",
          { type: "face", name: name, file: file, role: role, append: append }, 30000);
        if (!r) return false;
        if (!r.ok) { toast(r.error || "select failed", true); return false; }
        // Body / Outfit were written from what the picture was made with;
        // text being typed on the card stays in its field
        const filled = r.look_filled || {};
        const typing = unsaved.has(cardKey(card));
        ["body", "outfit"].forEach(function (k) {
          const f = card.querySelector('[data-f="' + k + '"]');
          if (f && filled[k] && !typing) f.value = filled[k];
        });
        toast((role === "secondary" ? "Extra look added" : "Look picked") +
          (!Object.keys(filled).length ? ""
            : typing ? ". Body and Outfit were written from its Casting prompt; your unsaved text is still in the fields, and Save keeps it."
            : ". Body and Outfit were written from its Casting prompt: read them over."));
        return true;
      },
      // the main look decides whether the look text matches it
      onDone: async function () { loadHeroRefs(card, cid); await State.refreshProject(); },
    });
  }

  /* ------------------------------------------------------- location refs */
  function locRefUrl(lid, file) {
    return "/api/locrefs/" + encodeURIComponent(pid()) + "/" +
      encodeURIComponent(lid) + "/" + encodeURIComponent(normPath(file));
  }

  async function loadLocRefs(card, lid) {
    const strip = card.querySelector(".locref-strip");
    if (!strip) return;
    const r = await window.API.get("/api/projects/" + encodeURIComponent(pid()) +
      "/cast/locations/" + encodeURIComponent(lid) + "/refs", 20000, true);
    if (!r || !Array.isArray(r.refs)) {
      strip.innerHTML = '<span class="muted small">Could not load the pictures.</span>';
      return;
    }
    const manifest = Array.isArray(r.references) ? r.references : [];
    const uploaded = r.refs.filter(function (f) {
      return !manifest.some(function (m) { return (m.copied || m.file) === f; });
    });
    setPortrait(card, manifest.length ? locRefUrl(lid, manifest[0].copied || manifest[0].file)
      : uploaded.length ? locRefUrl(lid, uploaded[0]) : "");
    setLookState(card, manifest.length, "picture", "pictures", "No picture yet");
    if (!r.refs.length && !manifest.length) {
      strip.innerHTML = '<span class="muted small">No pictures yet. Pick one from a Casting batch, or upload one.</span>';
      return;
    }
    strip.innerHTML = manifest.map(function (m) {
      const tag = (m.tag || "").trim();
      const tip = (m.type || "environment") + ": " + (m.name || "") +
        (tag ? ", the variant " + tag : "") + ". × removes it.";
      return '<div class="locref-thumb" data-tip="' + esc(tip) + '">' +
        '<img loading="lazy" src="' + esc(locRefUrl(lid, m.copied || m.file)) + '" alt="picture of the place">' +
        (tag ? '<span class="locref-tag">' + esc(tag) + "</span>" : "") +
        '<button class="ref-x" type="button" data-ref-rm="' + esc(m.file) + '" ' +
          'data-tip="Remove this picture">×</button>' +
      "</div>";
    }).join("") +
    uploaded.map(function (f) {
      return '<div class="locref-thumb" data-tip="An uploaded picture">' +
        '<img loading="lazy" src="' + esc(locRefUrl(lid, f)) + '" alt="picture of the place">' +
      "</div>";
    }).join("");
    strip.querySelectorAll("[data-ref-rm]").forEach(function (b) {
      b.onclick = async function (e) {
        e.stopPropagation();
        const r2 = await post("/api/projects/" + encodeURIComponent(pid()) +
          "/cast/locations/" + encodeURIComponent(lid) + "/refs/remove",
          { file: b.dataset.refRm }, 30000);
        if (r2 && r2.ok) { toast("Picture removed"); loadLocRefs(card, lid); }
        else toast((r2 && r2.error) || "remove failed", true);
      };
    });
  }

  async function uploadLocRefs(card, lid) {
    const inp = card.querySelector(".locref-file");
    if (!inp) return;
    const files = Array.prototype.slice.call(inp.files || []);
    if (!files.length) return;
    const r = await uploadImage("/api/projects/" + encodeURIComponent(pid()) +
      "/cast/locations/" + encodeURIComponent(lid) + "/refs/upload", files);
    inp.value = "";
    if (!r) return;
    if (r.ok) {
      toast("Picture uploaded");
      loadLocRefs(card, lid);
    } else {
      toast(r.error || "upload failed", true);
    }
  }

  function openLocPick(card, lid) {
    openBatchPicker({
      type: "environment",
      title: "Pick a picture of " + lid,
      extraInput: {
        label: "View name (optional)",
        placeholder: "arena, lobby",
        tip: "Only for a place with more than one view. Name this view, and pick which scene uses it under Picture per scene. Leave it empty for the main picture: a scene without a view uses every picture of the place.",
      },
      onSelect: async function (name, file, role, append, extra) {
        const r = await post("/api/projects/" + encodeURIComponent(pid()) +
          "/cast/locations/" + encodeURIComponent(lid) + "/refs/select",
          { type: "environment", name: name, file: file, append: append,
            tag: String(extra || "").trim() }, 30000);
        if (!r) return false;
        if (r.ok) {
          const tag = String(extra || "").trim();
          toast(tag ? "Picture added as the view " + tag : "Picture added");
          if (r.warning) toast(r.warning, true);
          return true;
        }
        toast(r.error || "The picture was not added", true);
        return false;
      },
      onDone: async function () {
        await State.refreshProject();       // the scene map reads the views from the bible
        loadLocRefs(card, lid); loadLocSceneMap(card, lid);
      },
    });
  }

  /* ------------------------------------------------- location scene map */
  async function loadLocSceneMap(card, lid) {
    const box = card.querySelector(".locscene-map");
    if (!box) return;
    const loc = ((State.bible || {}).locations || {})[lid] || {};
    const tags = (loc.reference_images || []).map(function (m) {
      return String(m.tag || "").trim();
    }).filter(function (t, i, a) { return t && a.indexOf(t) === i; });
    const r = await window.API.get("/api/projects/" + encodeURIComponent(pid()) +
      "/shots", 30000, true);
    const shots = (r && Array.isArray(r.shots)) ? r.shots : [];
    const scenes = [];
    shots.forEach(function (s) {
      const sc = s.scene || (s.json && s.json.scene) || "";
      if (sc && scenes.indexOf(sc) < 0) scenes.push(sc);
    });
    scenes.sort();
    if (!scenes.length) {
      box.innerHTML = '<span class="muted small">No scenes yet: write the shot list on the Story page first.</span>';
      return;
    }
    if (!tags.length) {
      box.innerHTML = '<span class="muted small">Every scene here uses all of its pictures. For a place ' +
        "with more than one view, give a picture a view name when you pick it, then choose its scenes here.</span>";
      return;
    }
    const smap = loc.scene_tags || {};
    box.innerHTML = scenes.map(function (sc) {
      const cur = String(smap[sc] || "");
      return '<div class="locscene-row">' +
        '<span class="mono">' + esc(sc) + "</span>" +
        '<select class="locscene-sel" data-scene="' + esc(sc) + '">' +
          '<option value=""' + (cur ? "" : " selected") + ">auto (primary)</option>" +
          tags.map(function (t) {
            return '<option value="' + esc(t) + '"' + (cur === t ? " selected" : "") +
              ">" + esc(t) + "</option>";
          }).join("") +
        "</select></div>";
    }).join("");
    box.querySelectorAll(".locscene-sel").forEach(function (sel) {
      sel.onchange = async function () {
        const r2 = await post("/api/projects/" + encodeURIComponent(pid()) +
          "/cast/locations/" + encodeURIComponent(lid) + "/scene-map",
          { scene: sel.dataset.scene, tag: sel.value }, 30000);
        if (r2 && r2.ok) toast(sel.dataset.scene + " → " + (sel.value || "auto"));
        else toast((r2 && r2.error) || "scene map failed", true);
      };
    });
  }

  /* ------------------------------------------------- weapons + loadouts */
  async function loadWeaponRefs(card, cid, wid) {
    const row = card.querySelector('.wp-row[data-wid="' + CSS.escape(wid) + '"]');
    if (!row) return;
    const strip = row.querySelector(".wp-ref-strip");
    if (!strip) return;
    const r = await window.API.get("/api/projects/" + encodeURIComponent(pid()) +
      "/cast/characters/" + encodeURIComponent(cid) + "/slotrefs/weapon/" +
      encodeURIComponent(wid), 30000, true);
    const refs = (r && r.references) || [];
    if (!refs.length) {
      strip.innerHTML = '<span class="muted small">no weapon stills yet</span>';
      return;
    }
    strip.innerHTML = refs.map(function (m) {
      const url = "/api/slotrefs/" + encodeURIComponent(pid()) + "/" +
        encodeURIComponent(cid) + "/weapon/" + encodeURIComponent(wid) + "/" +
        encodeURIComponent(m.copied || m.file);
      return '<div class="heroref-thumb" data-tip="' + esc((m.name || "") + "/" + (m.file || "")) + '">' +
        '<img loading="lazy" src="' + esc(url) + '" alt="weapon reference">' +
        '<button class="ref-x" type="button" data-wref-rm="' + esc(m.file) + '" data-tip="remove">×</button>' +
      "</div>";
    }).join("");
    strip.querySelectorAll("[data-wref-rm]").forEach(function (b) {
      b.onclick = async function (e) {
        e.stopPropagation();
        const r2 = await post("/api/projects/" + encodeURIComponent(pid()) +
          "/cast/characters/" + encodeURIComponent(cid) + "/slotrefs/weapon/" +
          encodeURIComponent(wid) + "/remove", { file: b.dataset.wrefRm }, 30000);
        if (r2 && r2.ok) { toast("Weapon picture removed"); loadWeaponRefs(card, cid, wid); }
        else toast((r2 && r2.error) || "remove failed", true);
      };
    });
  }

  async function loadLoadoutRefs(card, cid, name) {
    const row = card.querySelector('.ld-row[data-ld="' + CSS.escape(name) + '"]');
    if (!row) return;
    const strip = row.querySelector(".ld-ref-strip");
    if (!strip) return;
    const r = await window.API.get("/api/projects/" + encodeURIComponent(pid()) +
      "/cast/characters/" + encodeURIComponent(cid) + "/herorefs", 30000, true);
    const refs = ((r && r.references) || []).filter(function (m) {
      return String(m.tag || "") === name;
    });
    if (!refs.length) {
      strip.innerHTML = '<span class="muted small">No picture yet: the main look is used</span>';
      return;
    }
    strip.innerHTML = refs.map(function (m) {
      const url = "/api/herorefs/" + encodeURIComponent(pid()) + "/" +
        encodeURIComponent(cid) + "/" + encodeURIComponent(m.copied || m.file);
      return '<div class="heroref-thumb" data-tip="' + esc((m.name || "") + "/" + (m.file || "")) + '">' +
        '<img loading="lazy" src="' + esc(url) + '" alt="loadout reference">' +
        '<button class="ref-x" type="button" data-ldref-rm="' + esc(m.file) + '" data-tip="remove">×</button>' +
      "</div>";
    }).join("");
    strip.querySelectorAll("[data-ldref-rm]").forEach(function (b) {
      b.onclick = async function (e) {
        e.stopPropagation();
        const r2 = await post("/api/projects/" + encodeURIComponent(pid()) +
          "/cast/characters/" + encodeURIComponent(cid) + "/herorefs/remove",
          { file: b.dataset.ldrefRm }, 30000);
        if (r2 && r2.ok) { toast("Outfit picture removed"); loadLoadoutRefs(card, cid, name); }
        else toast((r2 && r2.error) || "remove failed", true);
      };
    });
  }

  async function loadCharSceneMap(card, cid) {
    const box = card.querySelector(".ld-scene-map");
    if (!box) return;
    const ch = ((State.bible || {}).characters || {})[cid] || {};
    const loadouts = Object.keys(ch.loadouts || {});
    if (!loadouts.length) {
      box.innerHTML = '<span class="muted small">add a loadout first</span>';
      return;
    }
    const r = await window.API.get("/api/projects/" + encodeURIComponent(pid()) +
      "/shots", 30000, true);
    const shots = (r && Array.isArray(r.shots)) ? r.shots : [];
    const scenes = [];
    shots.forEach(function (s) {
      const sc = s.scene || (s.json && s.json.scene) || "";
      if (sc && scenes.indexOf(sc) < 0) scenes.push(sc);
    });
    scenes.sort();
    if (!scenes.length) {
      box.innerHTML = '<span class="muted small">No scenes yet: write the shot list on the Story page first.</span>';
      return;
    }
    const smap = ch.scene_tags || {};
    box.innerHTML = scenes.map(function (sc) {
      const cur = String(smap[sc] || "");
      return '<div class="locscene-row">' +
        '<span class="mono">' + esc(sc) + "</span>" +
        '<select class="ldscene-sel" data-scene="' + esc(sc) + '">' +
          '<option value=""' + (cur ? "" : " selected") + ">auto (default)</option>" +
          loadouts.map(function (n) {
            return '<option value="' + esc(n) + '"' + (cur === n ? " selected" : "") +
              ">" + esc(n) + "</option>";
          }).join("") +
        "</select></div>";
    }).join("");
    box.querySelectorAll(".ldscene-sel").forEach(function (sel) {
      sel.onchange = async function () {
        const r2 = await post("/api/projects/" + encodeURIComponent(pid()) +
          "/cast/characters/" + encodeURIComponent(cid) + "/scene-map",
          { scene: sel.dataset.scene, tag: sel.value }, 30000);
        if (r2 && r2.ok) toast(sel.dataset.scene + " → " + (sel.value || "auto"));
        else toast((r2 && r2.error) || "scene map failed", true);
      };
    });
  }

  function openWeaponPick(card, cid, wid) {
    openBatchPicker({
      type: "weapon",
      title: "Pick a picture of the weapon " + wid,
      onSelect: async function (name, file) {
        const r = await post("/api/projects/" + encodeURIComponent(pid()) +
          "/cast/characters/" + encodeURIComponent(cid) + "/slotrefs/weapon/" +
          encodeURIComponent(wid) + "/select",
          { type: "weapon", name: name, file: file, append: true }, 30000);
        if (!r) return false;
        if (r.ok) { toast("Weapon picture added"); return true; }
        toast(r.error || "select failed", true);
        return false;
      },
      onDone: function () { loadWeaponRefs(card, cid, wid); },
    });
  }

  function openLoadoutPick(card, cid, name) {
    openBatchPicker({
      type: "face",
      title: "Pick the picture for the outfit " + name,
      onSelect: async function (bname, file) {
        const r = await post("/api/projects/" + encodeURIComponent(pid()) +
          "/cast/characters/" + encodeURIComponent(cid) + "/herorefs/select",
          { type: "face", name: bname, file: file, tag: name, append: true }, 30000);
        if (!r) return false;
        if (r.ok) { toast("Outfit picture added"); return true; }
        toast(r.error || "select failed", true);
        return false;
      },
      onDone: function () { loadLoadoutRefs(card, cid, name); },
    });
  }

  function weaponOptions(card, cur) {
    const ids = Object.keys(collectWeapons(card));
    if (!ids.length) {
      return '<option value="">no weapon — add one in Weapons above</option>';
    }
    return ids.map(function (wid) {
      return '<option value="' + esc(wid) + '"' + ((cur || "") === wid ? " selected" : "") +
        ">" + esc(wid) + "</option>";
    }).join("");
  }

  function freshCard(cid) {
    if (!cid) return null;
    return document.querySelector('.cast-card[data-cid="' + CSS.escape(cid) + '"]');
  }

  /* Bind every [data-act] button in a card. MUST be re-run after any row is
     added dynamically — otherwise the new row's buttons (remove, pick refs)
     have no handlers and appear "broken" (nothing happens on click). */
  function bindCardActions(card, ch) {
    card.querySelectorAll("[data-act]").forEach(function (b) {
      b.onclick = function () {
        const act = b.dataset.act;
        if (act === "toggle") toggleCard(card, "c:" + (ch.id || ""));
        else if (act === "save") saveCharacter(card, ch);
        else if (act === "delete") deleteCharacter(card, ch);
        else if (act === "add-power") addPowerRow(card, ch);
        else if (act === "heroref-upload") {
          const fileIn = card.querySelector(".heroref-file");
          if (fileIn) fileIn.click();
        }
        else if (act === "heroref-pick") openHeroPick(card, ch.id || "");
        else if (act === "autofill") openAutofillPick(card, ch);
        else if (act === "look-apply" || act === "look-keep") lookAnswer(card, ch, act === "look-apply");
        else if (act === "powref-pick") {
          const row = b.closest(".pw-row");
          const pw = row ? row.dataset.pw || "" : "";
          refPickGuard(card, ch,
            function (saved) { return !!(saved.powers || {})[pw]; },
            function (card2, cid2) { openPowerPick(card2, cid2, pw); });
        }
        else if (act === "add-weapon") addWeaponRow(card, ch);
        else if (act === "weaponref-pick") {
          const row = b.closest(".wp-row");
          const wid = row
            ? (((row.querySelector(".wp-name") || {}).value || "")
               || row.dataset.wid || "").trim() : "";
          if (!wid) { toast("Give the weapon a name first", true); return; }
          refPickGuard(card, ch,
            function (saved) { return !!(saved.weapons || {})[wid]; },
            function (card2, cid2) { openWeaponPick(card2, cid2, wid); });
        }
        else if (act === "rm-weapon") {
          const row = b.closest(".wp-row");
          if (row && row.parentNode) row.parentNode.removeChild(row);
          const list = card.querySelector(".wp-list");
          if (list && !list.querySelector(".wp-row")) {
            list.innerHTML = '<div class="muted small">No weapons.</div>';
          }
        }
        else if (act === "add-loadout") addLoadoutRow(card, ch);
        else if (act === "loadoutref-pick") {
          const row = b.closest(".ld-row");
          const name = row
            ? ((row.querySelector(".ld-name") || {}).value || "").trim() : "";
          if (!name) {
            toast("Name the outfit first (for example AnnaWithBlade)", true);
            return;
          }
          refPickGuard(card, ch,
            function (saved) { return !!(saved.loadouts || {})[name]; },
            function (card2, cid2) { openLoadoutPick(card2, cid2, name); });
        }
        else if (act === "rm-loadout") {
          const row = b.closest(".ld-row");
          if (row && row.parentNode) row.parentNode.removeChild(row);
          const list = card.querySelector(".ld-list");
          if (list && !list.querySelector(".ld-row")) {
            list.innerHTML = '<div class="muted small">No extra outfits — the base outfit is used</div>';
          }
        }
        else if (act === "rm-power") {
          const row = b.closest(".pw-row");
          if (row && row.parentNode) row.parentNode.removeChild(row);
          const list = card.querySelector(".pw-list");
          if (list && !list.querySelector(".pw-row")) {
            list.innerHTML = '<div class="muted small">No powers.</div>';
          }
        }
        if (ROW_EDITS.indexOf(act) >= 0) setUnsaved(card, true);
      };
    });
  }

  /* References attach to the SAVED bible entry (the endpoint looks the power /
     weapon / loadout up in the YAML), so an unsaved row must be persisted
     first. Saves, then opens the picker on the re-rendered card. */
  async function refPickGuard(card, ch, exists, run) {
    const saved = ((State.bible || {}).characters || {})[ch.id || ""];
    if (saved && exists(saved)) { run(card, ch.id || ""); return; }
    toast("Saving the character first, so the picture has somewhere to go");
    const created = await saveCharacter(card, ch);
    const cid2 = (created && created.id) || ch.id || "";
    run(freshCard(cid2) || card, cid2);
  }

  function addWeaponRow(card, ch) {
    const sel = card.querySelector(".wp-type-sel");
    const t = sel ? sel.value : "custom";
    const list = card.querySelector(".wp-list");
    if (!list) return;
    const note = list.querySelector(".muted");
    if (note) list.innerHTML = "";
    const holder = document.createElement("div");
    holder.innerHTML = weaponRow(t);
    const row = holder.firstElementChild;
    if (!row) return;
    list.appendChild(row);
    row.querySelector(".wp-ref-strip").innerHTML =
      '<span class="muted small">save the character, then pick weapon stills</span>';
    // keep every loadout's weapon dropdown in sync with the new weapon list
    card.querySelectorAll(".ld-row").forEach(function (lr) {
      const ws = lr.querySelector(".ld-weapon");
      if (ws) {
        const cur = ws.value;
        ws.innerHTML = '<option value="">No weapon</option>' + weaponOptions(card, cur);
      }
    });
    bindTips(row);
    bindCardActions(card, ch);   // the new row's buttons need handlers
  }

  function addLoadoutRow(card, ch) {
    const list = card.querySelector(".ld-list");
    if (!list) return;
    const note = list.querySelector(".muted");
    if (note) list.innerHTML = "";
    const holder = document.createElement("div");
    holder.innerHTML = loadoutRow("", {});
    const row = holder.firstElementChild;
    if (!row) return;
    row.dataset.ld = "";
    const ws = row.querySelector(".ld-weapon");
    if (ws) ws.innerHTML = '<option value="">No weapon</option>' + weaponOptions(card, "");
    list.appendChild(row);
    bindTips(row);
    bindCardActions(card, ch);   // the new row's buttons need handlers
  }

  /* -------------------------------------------------------------- regenerate */
  /* The story around the cast as it is now: the AI writer writes the outline
     and every scene again (its prompts carry the cast, its weapons and
     loadouts); without one, the Story page's chat prompt carries it too. */
  async function regenerate() {
    const bible = State.bible || {};
    const chars = asList(bible.characters).filter(function (c) { return c.enabled !== false; });
    const locs = asList(bible.locations).filter(function (l) { return l.enabled !== false; });
    if (!chars.length && !locs.length) {
      toast("The cast is empty: put at least one character or place in the movie first", true);
      return;
    }
    const c = (await window.API.get("/api/ai/config", 15000, true)) || {};
    if (!c.ready) {
      toast("Copy the story prompt on the Story page into any chat AI: it carries this cast. Or connect an AI writer.");
      Router.go("story");
      return;
    }
    const okGo = await Dlg.confirm({
      title: "Rewrite the story around this cast?",
      message: "The AI writer writes the outline and every scene's shots again, one scene at a time, " +
        "with this cast. The current versions stay in the Story page's History.",
      okLabel: "Rewrite",
    });
    if (!okGo) return;
    const r = await post("/api/projects/" + encodeURIComponent(pid()) + "/story/generate-ai", {}, 60000);
    if (!r) return;
    if (r.ok) {
      toast("The AI writer is rewriting the story: follow it on the Story page");
      Router.go("story");
    } else {
      toast("Could not start the AI writer: " + (r.error || "unknown error"), true);
    }
  }

  /* ------------------------------------------------------------------ bind */
  function bind(el) {
    const chars = asList((State.bible || {}).characters);
    const locs = asList((State.bible || {}).locations);
    const byCid = {};
    chars.forEach(function (c) { byCid[c.id || ""] = c; });
    const byLid = {};
    locs.forEach(function (l) { byLid[l.id || ""] = l; });

    const regen = el.querySelector("#cast-regen");
    if (regen) regen.onclick = regenerate;
    const nc = el.querySelector("#cast-new-char");
    if (nc) nc.onclick = newCharacter;
    const nl = el.querySelector("#cast-new-loc");
    if (nl) nl.onclick = newLocation;
    const rf = el.querySelector("#cast-ref-file");
    const ru = el.querySelector("#cast-ref-upload");
    if (rf && ru) ru.onclick = function () { rf.click(); };
    if (rf) rf.onchange = function () { uploadRefs(el); };

    el.querySelectorAll(".cast-card[data-cid]").forEach(function (card) {
      const ch = byCid[card.dataset.cid];
      if (!ch) return;
      hydrateCard(card, ch);
      wirePowerRows(card);
      bindCardActions(card, ch);
      trackEdits(card);
      bindHead(card, ch, "c");
      const loraSel = card.querySelector(".cast-lora");
      const strIn = card.querySelector(".cast-lora-str");
      if (loraSel && strIn) loraSel.onchange = function () {
        strIn.disabled = !loraSel.value;
      };
      const hrFile = card.querySelector(".heroref-file");
      if (hrFile) hrFile.onchange = function () { uploadHeroRefs(card, ch.id || ""); };
      loadHeroRefs(card, ch.id || "");
    });

    el.querySelectorAll(".cast-card[data-lid]").forEach(function (card) {
      const l = byLid[card.dataset.lid];
      if (!l) return;
      card.querySelectorAll("[data-act]").forEach(function (b) {
        b.onclick = function () {
          if (b.dataset.act === "toggle") toggleCard(card, "l:" + (l.id || ""));
          else if (b.dataset.act === "save") saveLocation(card, l);
          else if (b.dataset.act === "delete") deleteLocation(card, l);
          else if (b.dataset.act === "locref-pick") openLocPick(card, l.id || "");
          else if (b.dataset.act === "locref-upload") {
            const fileIn = card.querySelector(".locref-file");
            if (fileIn) fileIn.click();
          }
        };
      });
      trackEdits(card);
      bindHead(card, l, "l");
      const nameIn = card.querySelector('[data-f="lid"]');
      if (nameIn) {
        nameIn.onkeydown = function (e) {
          if (e.key === "Enter") { e.preventDefault(); nameIn.blur(); }
          else if (e.key === "Escape") { nameIn.value = l.id || ""; nameIn.blur(); }
        };
        nameIn.onchange = function () { card.__renaming = renameLocation(card, l); };
      }
      const lrFile = card.querySelector(".locref-file");
      if (lrFile) lrFile.onchange = function () { uploadLocRefs(card, l.id || ""); };
      const llSel = card.querySelector(".loc-lora");
      const llStr = card.querySelector(".loc-lora-str");
      if (llSel && llStr) llSel.onchange = function () {
        llStr.disabled = !llSel.value;
      };
      loadLocRefs(card, l.id || "");
      loadLocSceneMap(card, l.id || "");
    });

    el.querySelectorAll("details[data-fold]").forEach(function (d) {
      d.addEventListener("toggle", function () { openFolds[d.dataset.fold] = d.open; });
    });
    bindTips(el);
  }

  function addPowerRow(card, ch) {
    const sel = card.querySelector(".pw-type-sel");
    const t = sel ? sel.value : "custom";
    const list = card.querySelector(".pw-list");
    if (!list) return;
    const note = list.querySelector(".muted");
    if (note) list.innerHTML = "";
    const holder = document.createElement("div");
    holder.innerHTML = powerRow(t);
    const row = holder.firstElementChild;
    if (!row) return;
    const def = powerDefaults(t);
    setRowValues(row, { descriptor: def.descriptor, motion: "", color: def.color });
    list.appendChild(row);
    wirePowerRows(card);
    bindCardActions(card, ch);   // the new row's buttons need handlers
  }

  /* ------------------------------------------------------------ registration */
  Router.register("cast", {
    polls: ["status"],
    subs: [],
    unsaved: function () { return unsaved.size > 0; },
    mount: function (el) {
      const page = this;
      page.el = el;
      page.render = function () {
        // unsaved edits stay on screen; another project renders at once
        if (unsaved.size && renderedPid === State.pid) { staleRender = true; return; }
        unsaved.clear();
        staleRender = false;
        renderedPid = State.pid;
        render(el);
      };
      rerender = page.render;
      page.subs.push(Bus.on("project", page.render));
      page.subs.push(Bus.on("loras", page.render));
      page.render();
      State.refreshLoras(false);
    },
    unmount: function () {
      this.subs.forEach(function (off) { off(); });
      this.subs = [];
      unsaved.clear();
      staleRender = false;
      renderedPid = null;
      rerender = null;
      if (this.el) this.el.innerHTML = "";
    },
  });
})();
