/* cast.js — Cast & World: the control room for the project bible — editable
   characters (body/outfit/emotions/powers/LoRA), locations, the movie seed
   reference image, and cast-driven story regeneration. */
(function () {
  "use strict";

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
    el.innerHTML =
      '<div class="page-slate">' +
        "<h1>Cast &amp; World</h1>" +
        '<span class="slate-meta">' + chars.length + " characters · " + locs.length + " locations</span>" +
        '<span class="spacer"></span>' +
        '<div class="slate-acts">' +
          '<button class="btn primary" type="button" id="cast-regen" ' +
            'data-tip="Rebuilds beats/scenes/shots from the cast below — versioned, rollback in Story">' +
            Icon.get("refresh") + "Regenerate story from cast</button>" +
        "</div>" +
      "</div>" +
      '<section class="card">' +
        '<div class="card-head"><h2>Characters in this movie</h2>' +
          '<span class="muted small">' + chars.length + " characters</span>" +
          '<span class="spacer"></span>' +
          '<button class="btn small" type="button" id="cast-new-char" ' +
            'data-tip="Add a new character to the bible">' + Icon.get("plus") + "New character</button>" +
        "</div>" +
        (chars.length
          ? '<div class="cast-grid">' + chars.map(function (c) { return charCard(c); }).join("") + "</div>"
          : empty("No characters in the project bible yet",
              "Generate the story first, or add a character here.")) +
      "</section>" +
      '<section class="card">' +
        '<div class="card-head"><h2>Locations / environments</h2>' +
          '<span class="spacer"></span>' +
          '<button class="btn small" type="button" id="cast-new-loc" ' +
            'data-tip="Add a new location to the bible">' + Icon.get("plus") + "New location</button>" +
        "</div>" +
        (locs.length
          ? '<div class="cast-grid">' + locs.map(function (l) { return locCard(l); }).join("") + "</div>"
          : '<div class="muted small">No locations yet.</div>') +
      "</section>" +
      '<section class="card">' +
        '<div class="card-head"><h2 data-tip="The active reference seeds every keyframe — clips generate from it (I2V)">Reference image (movie seed)</h2>' +
          '<span class="spacer"></span>' +
          '<input type="file" id="cast-ref-file" accept="image/png,image/jpeg,image/webp" multiple class="hidden">' +
          '<button class="btn small" type="button" id="cast-ref-upload" ' +
            'data-tip="Upload one or more seed images">' + Icon.get("upload") + "Upload</button>" +
        "</div>" +
        '<div class="ref-strip" id="cast-ref-strip">' +
          '<span class="muted small">loading…</span>' +
        "</div>" +
      "</section>";
    bind(el);
    loadRefs(el);
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

  function charCard(ch) {
    const cid = ch.id || "";
    const emo = (ch.emotion_palette || []).join(", ");
    const enabled = ch.enabled !== false;
    const curLora = ch.lora && ch.lora.file ? ch.lora.file : "";
    const str = Number(ch.lora && ch.lora.strength != null ? ch.lora.strength : 0.8) || 0.8;
    const powers = ch.powers || {};
    return '<div class="cast-card" data-cid="' + esc(cid) + '">' +
      '<div class="cast-head">' +
        '<input type="text" class="cast-name" data-f="name" value="' + esc(ch.name || "") +
          '" placeholder="character name" spellcheck="false">' +
        '<select class="cast-tier" data-f="tier" data-tip="Role weight in the movie">' +
          TIERS.map(function (t) {
            return '<option value="' + t + '"' + ((ch.tier || "supporting") === t ? " selected" : "") +
              ">" + t + "</option>";
          }).join("") +
        "</select>" +
        '<span class="spacer"></span>' +
        '<label class="toggle cast-toggle" data-tip="Whether this character appears in the movie">In movie' +
          '<input type="checkbox" data-f="enabled"' + (enabled ? " checked" : "") + ">" +
          "<span></span></label>" +
      "</div>" +
      '<div class="cast-field"><label data-tip="Stable identity traits (build, hair, marks) as TEXT. The reference images are never converted to text: this is what the story LLM reads and what every video prompt states verbatim. Refs reinforce it — fill it in briefly and concretely.">Body</label>' +
        '<textarea data-f="body" rows="2" spellcheck="false">' + esc(ch.body || "") + "</textarea>" +
        '<div class="locref-ctl"><button class="btn small" type="button" data-act="autofill" ' +
          'data-tip="Fill Body + Outfit from a batch still&apos;s recorded provenance (identity axes + the outfit/weapon that still was generated with). Review and Save after.">Auto-fill from batch still…</button></div></div>' +
      '<div class="cast-field"><label data-tip="The character&apos;s DEFAULT look, as text. Used whenever no loadout is active for a shot. Loadouts override it per shot (and carry their own composite reference image). The story LLM reads this too.">Outfit</label>' +
        '<textarea data-f="outfit" rows="2" spellcheck="false">' + esc(ch.outfit || "") + "</textarea></div>" +
      '<div class="cast-field"><label>Emotion palette (comma separated)</label>' +
        '<input type="text" data-f="emotion" value="' + esc(emo) +
          '" placeholder="calm, focused, tense" spellcheck="false"></div>' +
      '<div class="cast-field"><label>Powers</label>' +
        '<div class="pw-list">' +
          (Object.keys(powers).length
            ? Object.keys(powers).map(function (pn) { return powerRow(pn); }).join("")
            : '<div class="muted small">no powers yet</div>') +
        "</div>" +
        '<div class="pw-add">' +
          '<select class="pw-type-sel">' +
            POWER_TYPES.map(function (t) {
              return '<option value="' + t + '">' + t + "</option>";
            }).join("") +
          "</select>" +
          '<button class="btn small" type="button" data-act="add-power" ' +
            'data-tip="Add a power of the selected type">+ add power</button>' +
        "</div>" +
      "</div>" +
      '<div class="cast-field"><label data-tip="Weapon CONFIG (design text + handling + sfx). This is what the story LLM uses to write scenes, and what the prompt states. A weapon only APPEARS in renders through a loadout that carries it (with its composite reference image) — configure it here, then bind it in a loadout.">Weapons</label>' +
        '<div class="wp-list">' +
          (Object.keys(ch.weapons || {}).length
            ? Object.keys(ch.weapons || {}).map(function (wn) { return weaponRow(wn); }).join("")
            : '<div class="muted small">no weapons yet</div>') +
        "</div>" +
        '<div class="pw-add">' +
          '<select class="wp-type-sel">' +
            WEAPON_TYPES.map(function (t) {
              return '<option value="' + esc(t) + '">' + esc(t) + "</option>";
            }).join("") +
          "</select>" +
          '<button class="btn small" type="button" data-act="add-weapon" ' +
            'data-tip="Add a weapon of the selected type — configure it, then pick design stills from a weapon batch">+ add weapon</button>' +
        "</div>" +
      "</div>" +
      '<div class="cast-field"><label data-tip="Named variants: an outfit text + a weapon + ONE composite reference image (character wearing the outfit, holding the weapon). The story and the scene map pick a loadout per shot; the resolver passes exactly one picture per character.">Loadouts' +
        '<span class="muted small" style="margin-left:6px">outfit + weapon + composite ref</span></label>' +
        '<div class="ld-list">' +
          (Object.keys(ch.loadouts || {}).length
            ? Object.keys(ch.loadouts || {}).map(function (ln) {
                return loadoutRow(ln, (ch.loadouts || {})[ln]);
              }).join("")
            : '<div class="muted small">no loadouts yet — the base outfit is used</div>') +
        "</div>" +
        '<div class="pw-add">' +
          '<button class="btn small" type="button" data-act="add-loadout" ' +
            'data-tip="Add a named loadout (e.g. JulietWithWeapon)">+ add loadout</button>' +
        "</div>" +
        '<div class="ld-scene-map-wrap">' +
          '<label data-tip="Which loadout this character wears per scene — the shot override beats this map, the beat text beats the default.">Scene &rarr; loadout</label>' +
          '<div class="ld-scene-map"><span class="muted small">save the character first</span></div>' +
        "</div>" +
      "</div>" +
      '<div class="cast-field lora-row"><label>LoRA</label>' +
        '<div class="lora-ctl">' +
          '<select class="cast-lora" data-tip="Character LoRA, applied only when the shot renders on the legacy Wan 2.2 model (LTX-2.5 and H3 use the reference images)">' +
            '<option value="">none</option>' +
            loraOptions(characterLoras(), curLora, curLora) +
          "</select>" +
          '<input type="number" class="cast-lora-str" min="0.1" max="1.5" step="0.05" value="' +
            str.toFixed(2) + '"' + (curLora ? "" : " disabled") + ' data-tip="LoRA strength 0.1–1.5">' +
        "</div>" +
        (curLora
          ? '<div class="muted small">trained LoRA: ' + esc(curLora) + "</div>"
          : '<div class="muted small">no LoRA attached</div>') +
      "</div>" +
      '<div class="cast-field heroref-field"><label data-tip="Identity lock for generated reference images (IP-Adapter)">Hero reference (identity)</label>' +
        '<div class="heroref-strip">' +
          '<span class="muted small">loading…</span>' +
        "</div>" +
        '<div class="heroref-ctl">' +
          '<input type="file" class="heroref-file" accept="image/png,image/jpeg,image/webp" multiple hidden>' +
          '<button class="btn small" type="button" data-act="heroref-upload" ' +
            'data-tip="Upload identity reference images for this character">' + Icon.get("upload") + "Upload</button>" +
          '<button class="btn small" type="button" data-act="heroref-pick" ' +
            'data-tip="Choose any photo from a training batch as this character&apos;s identity reference">Pick from batch</button>' +
        "</div>" +
      "</div>" +
      '<div class="cast-actions">' +
        '<button class="btn small primary" type="button" data-act="save" ' +
          'data-tip="Save all fields for this character">Save character</button>' +
        '<button class="btn small danger" type="button" data-act="delete" ' +
          'data-tip="Remove this character from the bible">Delete</button>' +
      "</div>" +
    "</div>";
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
      '<textarea class="wp-desc" rows="1" placeholder="descriptor (materials, color, glow)" spellcheck="false"></textarea>' +
      '<input type="text" class="wp-handling" placeholder="handling (raised overhead, planted…)" spellcheck="false">' +
      '<input type="text" class="wp-sfx" placeholder="sfx (steel ring, energy hum)" spellcheck="false">' +
      '<button class="btn small" type="button" data-act="rm-weapon" ' +
        'data-tip="Remove this weapon">' + Icon.get("x") + "</button>" +
      '<div class="cast-field wp-ref-field">' +
        '<label data-tip="Weapon design stills (weapon-only or in-hand). They ride along as EXTRA pictures on models with spare reference capacity (H3); the loadout composite already carries the weapon in one image.">Weapon refs</label>' +
        '<div class="wp-ref-strip"><span class="muted small">loading…</span></div>' +
        '<div class="locref-ctl">' +
          '<button class="btn small" type="button" data-act="weaponref-pick" ' +
            'data-tip="Pick an image from a WEAPON training batch">Pick from weapon batch</button>' +
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
        '<option value="">no weapon</option>' +
      "</select>" +
      '<button class="btn small" type="button" data-act="loadoutref-pick" ' +
        'data-tip="Pick this loadout&apos;s composite reference (character in this outfit, holding this weapon) from the character batch — ONE image carries identity + outfit + weapon">Pick composite ref</button>' +
      '<button class="btn small" type="button" data-act="rm-loadout" ' +
        'data-tip="Remove this loadout">' + Icon.get("x") + "</button>" +
      '<div class="ld-ref-strip"><span class="muted small">no composite ref</span></div>' +
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
        '<label data-tip="Power VFX references, one per intensity level. The scene&apos;s power level picks the image whose level is the highest at or below it (floor rule).">Power refs' +
          '<span class="muted small" style="margin-left:6px">level &rarr; image</span></label>' +
        '<div class="powref-strip"><span class="muted small">loading…</span></div>' +
        '<div class="locref-ctl">' +
          '<button class="btn small" type="button" data-act="powref-pick" ' +
            'data-tip="Pick an image from a POWER training batch; it is added at the next intensity level (edit the number on a thumbnail to change it)">Pick from power batch</button>' +
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
        ws.innerHTML = '<option value="">no weapon</option>' +
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
    const enabled = l.enabled !== false;
    const curLora = l.lora && l.lora.file ? l.lora.file : "";
    const str = Number(l.lora && l.lora.strength != null ? l.lora.strength : 0.6) || 0.6;
    return '<div class="cast-card" data-lid="' + esc(l.id || "") + '">' +
      '<div class="cast-head">' +
        '<input type="text" class="cast-name mono" data-f="lid" value="' + esc(l.id || "") +
          '" placeholder="location id (arena, palace)" spellcheck="false" ' +
          'data-tip="The location ID is the vocabulary the story LLM and the prompts use. Rename it to a real name (e.g. arena, palace) — references and shots follow.">' +
        '<button class="btn small" type="button" data-act="loc-rename" ' +
          'data-tip="Rename this location id (moves its references and updates shots)">Rename id</button>' +
        '<span class="spacer"></span>' +
        '<label class="toggle cast-toggle" data-tip="Whether this location is used in the movie">In movie' +
          '<input type="checkbox" data-f="enabled"' + (enabled ? " checked" : "") + ">" +
          "<span></span></label>" +
      "</div>" +
      '<div class="cast-field"><label>Descriptor</label>' +
        '<textarea data-f="descriptor" rows="3" spellcheck="false">' + esc(l.descriptor || "") + "</textarea></div>" +
      '<div class="cast-field"><label>Lighting</label>' +
        '<input type="text" data-f="lighting" value="' + esc(l.lighting || "") +
          '" placeholder="e.g. cold moonlight, harsh fluorescents" spellcheck="false"></div>' +
      '<div class="cast-field lora-row"><label>Environment LoRA</label>' +
        '<div class="lora-ctl">' +
          '<select class="loc-lora" data-tip="Environment LoRA, applied only when a shot here renders on the legacy Wan 2.2 model (LTX-2.5 and H3 use the reference images)">' +
            '<option value="">none</option>' +
            loraOptions(envLoras(), curLora, curLora) +
          "</select>" +
          '<input type="number" class="loc-lora-str" min="0.1" max="1.5" step="0.05" value="' +
            str.toFixed(2) + '"' + (curLora ? "" : " disabled") + ' data-tip="LoRA strength 0.1–1.5">' +
        "</div>" +
        (curLora
          ? '<div class="muted small">environment LoRA: ' + esc(curLora) + "</div>"
          : '<div class="muted small">no environment LoRA attached</div>') +
      "</div>" +
      '<div class="cast-field locref-field"><label data-tip="Guides generated images for this location (IP-Adapter style guidance)">Reference image</label>' +
        '<div class="locref-strip">' +
          '<span class="muted small">loading…</span>' +
        "</div>" +
        '<div class="locref-ctl">' +
          '<button class="btn small" type="button" data-act="locref-pick" ' +
            'data-tip="Choose an environment photo from a training batch as this location&apos;s reference">Pick from environment batch</button>' +
          '<input type="file" class="locref-file" accept="image/png,image/jpeg,image/webp" multiple hidden>' +
          '<button class="btn small" type="button" data-act="locref-upload" ' +
            'data-tip="Upload reference images for this location">' + Icon.get("upload") + "Upload</button>" +
        "</div>" +
      "</div>" +
      '<div class="cast-field locscene-field"><label data-tip="Bind each scene to one of this location&apos;s named variants. Auto = the primary image (or the first tagged one). Shots can also override this individually.">Scene → variant</label>' +
        '<div class="locscene-map"><span class="muted small">loading…</span></div>' +
      "</div>" +
      '<div class="cast-actions">' +
        '<button class="btn small primary" type="button" data-act="save">Save</button>' +
        '<button class="btn small danger" type="button" data-act="delete">Delete</button>' +
      "</div>" +
    "</div>";
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
    if (!name) { toast("character needs a name", true); return; }
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
      toast("saved " + (r.character && r.character.name ? r.character.name : name));
      await State.refreshProject();
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
        if (r.ok) await State.refreshProject();
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
    if (r.ok) { toast("character added"); await State.refreshProject(); }
    else toast(r.error || "failed", true);
  }

  async function saveLocation(card, l) {
    const lid = l.id || "";
    if (!lid) { toast("location id missing", true); return; }
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
    toast(r.ok ? "location saved" : (r.error || "save failed"), !r.ok);
    if (r.ok) await State.refreshProject();
  }

  async function renameLocation(card, l) {
    const inp = card.querySelector('[data-f="lid"]');
    const want = ((inp && inp.value) || "").trim();
    if (!want || want === (l.id || "")) { toast("type the new id first", true); return; }
    const r = await post("/api/projects/" + encodeURIComponent(pid()) +
      "/cast/locations/" + encodeURIComponent(l.id || "") + "/rename",
      { new_id: want }, 60000);
    if (r && r.ok) {
      toast("location renamed → " + r.id
        + (r.shots_updated ? " (" + r.shots_updated + " shots updated)" : ""));
      await State.refreshProject();
      await State.refreshShots();
    } else {
      toast((r && r.error) || "rename failed", true);
    }
  }

  function deleteLocation(card, l) {
    confirmBox('<p>Delete location <b>' + esc(l.id || "?") + '</b> from the bible?</p>',
      async function () {
        const r = await post("/api/projects/" + encodeURIComponent(pid()) +
          "/cast/locations/" + encodeURIComponent(l.id || "") + "/delete", {}, 30000);
        if (!r) return;
        toast(r.ok ? "location deleted" : (r.error || "delete failed"), !r.ok);
        if (r.ok) await State.refreshProject();
      });
  }

  async function newLocation() {
    const r = await post("/api/projects/" + encodeURIComponent(pid()) + "/cast/locations", {
      id: "new_location",
      descriptor: "",
      lighting: "",
      enabled: true,
    }, 30000);
    if (!r) return;
    if (r.ok) { toast("location added"); await State.refreshProject(); }
    else toast(r.error || "failed", true);
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
      toast("reference uploaded" + (r.refs && r.refs.length ? " — " + r.refs.length + " image(s)" : ""));
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
      strip.innerHTML = '<span class="muted small">unavailable</span>';
      return;
    }
    const refs = Array.isArray(r.refs) ? r.refs : [];
    const auto = Array.isArray(r.auto) ? r.auto : [];
    const manifest = Array.isArray(r.references) ? r.references : [];
    if (!refs.length && !auto.length && !manifest.length) {
      strip.innerHTML = '<span class="muted small">no identity references yet</span>';
      return;
    }
    strip.innerHTML =
      (manifest.length
        ? '<div class="muted small" style="flex-basis:100%">picked — used first</div>'
        : "") +
      manifest.map(function (m) {
        const role = m.role || "primary";
        const tip = "LTX reference — " + (m.type || "face") + "/" + (m.name || "") +
          " · " + role + (m.tag ? " · loadout: " + m.tag : "") +
          " (click × to remove)";
        return '<div class="heroref-thumb' + (role === "secondary" ? " auto" : "") + '" ' +
          'data-tip="' + esc(tip) + '">' +
          '<img loading="lazy" src="' + esc(heroRefUrl(cid, m.copied || m.file)) + '" alt="batch reference">' +
          '<span class="heroref-tag">' + esc(m.tag ? "loadout" : role) + "</span>" +
          '<button class="ref-x" type="button" data-ref-rm="' + esc(m.file) + '" ' +
            'data-tip="remove this reference">×</button>' +
        "</div>";
      }).join("") +
      (refs.filter(function (f) {
        return !manifest.some(function (m) { return (m.copied || m.file) === f; });
      }).length
        ? '<div class="muted small" style="flex-basis:100%">uploaded — used if nothing is picked</div>'
        : "") +
      refs.filter(function (f) {
        return !manifest.some(function (m) { return (m.copied || m.file) === f; });
      }).map(function (f) {
        return '<div class="heroref-thumb" ' +
          'data-tip="Uploaded identity reference">' +
          '<img loading="lazy" src="' + esc(heroRefUrl(cid, f)) + '" alt="hero reference">' +
          '<span class="heroref-tag">REF</span>' +
        "</div>";
      }).join("") +
      (auto.length
        ? '<div class="muted small" style="flex-basis:100%">auto fallback — used ONLY when no reference is picked</div>'
        : "") +
      auto.map(function (a) {
        return '<div class="heroref-thumb auto" ' +
          'data-tip="Auto-detected batch master (fallback when no pick is made)">' +
          '<img loading="lazy" src="' + esc(a.url || "") + '" alt="batch master">' +
          '<span class="heroref-tag">fallback</span>' +
        "</div>";
      }).join("");
    strip.querySelectorAll("[data-ref-rm]").forEach(function (b) {
      b.onclick = async function (e) {
        e.stopPropagation();
        const r2 = await post("/api/projects/" + encodeURIComponent(pid()) +
          "/cast/characters/" + encodeURIComponent(cid) + "/herorefs/remove",
          { file: b.dataset.refRm }, 30000);
        if (r2 && r2.ok) { toast("reference removed"); loadHeroRefs(card, cid); }
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
      toast("hero reference uploaded");
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
        if (r2 && r2.ok) { toast("power reference removed"); loadPowerRefs(card, cid, pw); }
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
        if (r2 && r2.ok) toast("power level updated");
        else toast((r2 && r2.error) || "level update failed", true);
        loadPowerRefs(card, cid, pw);
      };
    });
  }

  function openPowerPick(card, cid, pw) {
    openBatchPicker({
      type: "power",
      title: "Pick power reference from batch — " + pw,
      onSelect: async function (name, file) {
        const r = await post("/api/projects/" + encodeURIComponent(pid()) +
          "/cast/characters/" + encodeURIComponent(cid) + "/powers/" +
          encodeURIComponent(pw) + "/refs/select",
          { type: "power", name: name, file: file, append: true }, 30000);
        if (!r) return false;
        if (r.ok) { toast("power reference added"); return true; }
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

  function openBatchPicker(opts) {
    const st = { closed: false, datasets: [] };
    const multi = Array.isArray(opts.roles) && opts.roles.length > 0;
    const veil = openModal(
      '<div class="batch-pick-head">' +
        '<label class="batch-label">Batch' +
          '<select id="pick-batch"><option value="">loading…</option></select>' +
        "</label>" +
        (multi
          ? '<label class="batch-label">role' +
              '<select id="pick-role">' +
                opts.roles.map(function (r) {
                  return '<option value="' + esc(r.value) + '">' + esc(r.label) + "</option>";
                }).join("") +
              "</select></label>" +
            '<label class="batch-label" data-tip="add this as another reference instead of replacing the one with the same role">' +
              '<input type="checkbox" id="pick-append"> add without replacing</label>'
          : "") +
        (opts.extraInput
          ? '<label class="batch-label" data-tip="' + esc(opts.extraInput.tip || "") + '">' +
              esc(opts.extraInput.label || "") +
              '<input type="text" id="pick-extra" placeholder="' +
                esc(opts.extraInput.placeholder || "") + '" spellcheck="false">' +
            "</label>"
          : "") +
        '<label class="batch-label" data-tip="Composites record which weapon/outfit they were generated with — filter armed vs plain instead of guessing from the thumbnail">filter' +
          '<select id="pick-filter">' +
            '<option value="all">all stills</option>' +
            '<option value="armed">with weapon</option>' +
            '<option value="plain">without weapon</option>' +
          "</select></label>" +
        '<span class="pick-count muted small"></span>' +
      "</div>" +
      '<div class="pick-grid" id="pick-grid">' +
        '<span class="muted small">loading batches…</span>' +
      "</div>" +
      '<div class="muted small" id="pick-note"></div>',
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
      const rej = d.rejected != null ? d.rejected : 0;
      if (count) count.textContent = kept + " kept / " + rej + " rejected";
      const shown = cands.slice(0, 60);
      grid.innerHTML = shown.map(function (c) {
        const m = metaOf(c) || {};
        const chips = [
          m.weapon ? "weapon: " + m.weapon : "no weapon",
          m.outfit ? "outfit: " + m.outfit : "",
          m.handling ? m.handling : "",
          m.level ? "level " + m.level : "",
        ].filter(Boolean).join(" · ");
        const tip = "Use this photo as " +
          (opts.type === "face" ? "identity / composite reference" :
           opts.type === "power" ? "power reference" :
           opts.type === "weapon" ? "weapon reference" :
           "environment reference") + (chips ? " — " + chips : "");
        return '<div class="pick-thumb" data-file="' + esc(c) +
          '" data-tip="' + esc(tip) + '">' +
          '<img loading="lazy" src="' + esc(datasetImgUrl(opts.type, d.name, c)) +
          '" alt="batch photo">' +
          (m.weapon
            ? '<span class="pick-chip armed">ARMED</span>'
            : (m.outfit || m.level
              ? '<span class="pick-chip">' + esc(m.level ? "L" + m.level : "PLAIN") + "</span>"
              : "")) +
        "</div>";
      }).join("");
      if (note) note.textContent = cands.length > 60
        ? "showing first 60 of " + cands.length : "";
      grid.querySelectorAll(".pick-thumb").forEach(function (th) {
        th.onclick = async function () {
          if (st.closed) return;
          th.classList.add("picked");
          const roleSel = veil.querySelector("#pick-role");
          const appendBox = veil.querySelector("#pick-append");
          const extraEl = veil.querySelector("#pick-extra");
          const ok = await opts.onSelect(
            d.name, th.dataset.file,
            roleSel ? roleSel.value : "primary",
            appendBox ? !!appendBox.checked : false,
            extraEl ? extraEl.value : "");
          if (st.closed) return;
          th.classList.remove("picked");
          if (!ok) return;
          if (appendBox && appendBox.checked) {
            // multi-add: keep the picker open so a second image can follow
            toast("reference added");
            return;
          }
          close();
          opts.onDone();
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
          '<span class="muted small">no training batches found</span>';
        return;
      }
      st.datasets = r.datasets;
      sel.innerHTML = r.datasets.map(function (d, i) {
        const kept = d.kept != null ? d.kept : 0;
        const rej = d.rejected != null ? d.rejected : 0;
        return '<option value="' + i + '">' + esc(d.name || "batch") +
          " (" + kept + " kept / " + rej + " rejected)</option>";
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
      title: "Auto-fill Body + Outfit from a batch still",
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
        toast("filled from " + file + (r.weapon ? " (weapon: " + r.weapon + ")" : "")
          + " — review and Save");
        return true;
      },
      onDone: function () { /* values are in the fields; Save persists them */ },
    });
  }

  function openHeroPick(card, cid) {
    openBatchPicker({
      type: "face",
      title: "Pick identity reference from batch",
      roles: [{ value: "primary", label: "primary" },
              { value: "secondary", label: "secondary" }],
      onSelect: async function (name, file, role, append) {
        const r = await post("/api/projects/" + encodeURIComponent(pid()) +
          "/cast/characters/" + encodeURIComponent(cid) + "/herorefs/select",
          { type: "face", name: name, file: file, role: role, append: append }, 30000);
        if (!r) return false;
        if (r.ok) { toast(role === "secondary" ? "secondary ref added" : "identity ref set"); return true; }
        toast(r.error || "select failed", true);
        return false;
      },
      onDone: function () { loadHeroRefs(card, cid); },
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
      strip.innerHTML = '<span class="muted small">unavailable</span>';
      return;
    }
    const manifest = Array.isArray(r.references) ? r.references : [];
    if (!r.refs.length && !manifest.length) {
      strip.innerHTML = '<span class="muted small">no reference images yet</span>';
      return;
    }
    strip.innerHTML = manifest.map(function (m) {
      const tag = (m.tag || "").trim();
      const tip = "Environment reference — " + (m.type || "environment") + "/" +
        (m.name || "") + (tag ? " · variant: " + tag : "") + " (click × to remove)";
      return '<div class="locref-thumb" data-tip="' + esc(tip) + '">' +
        '<img loading="lazy" src="' + esc(locRefUrl(lid, m.copied || m.file)) + '" alt="location reference">' +
        '<span class="locref-tag">' + esc(tag || "ENV") + "</span>" +
        '<button class="ref-x" type="button" data-ref-rm="' + esc(m.file) + '" ' +
          'data-tip="remove this reference">×</button>' +
      "</div>";
    }).join("") +
    r.refs.filter(function (f) {
      return !manifest.some(function (m) { return (m.copied || m.file) === f; });
    }).map(function (f) {
      return '<div class="locref-thumb" data-tip="Environment reference">' +
        '<img loading="lazy" src="' + esc(locRefUrl(lid, f)) + '" alt="location reference">' +
        '<span class="locref-tag">ENV</span>' +
      "</div>";
    }).join("");
    strip.querySelectorAll("[data-ref-rm]").forEach(function (b) {
      b.onclick = async function (e) {
        e.stopPropagation();
        const r2 = await post("/api/projects/" + encodeURIComponent(pid()) +
          "/cast/locations/" + encodeURIComponent(lid) + "/refs/remove",
          { file: b.dataset.refRm }, 30000);
        if (r2 && r2.ok) { toast("reference removed"); loadLocRefs(card, lid); }
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
      toast("location reference uploaded");
      loadLocRefs(card, lid);
    } else {
      toast(r.error || "upload failed", true);
    }
  }

  function openLocPick(card, lid) {
    openBatchPicker({
      type: "environment",
      title: "Pick environment reference from batch",
      extraInput: {
        label: "variant name",
        placeholder: "arena / palace lobby …",
        tip: "Name this environment variant (e.g. 'arena'). Scenes bound to the same name — via the Scene → variant map below or a shot's location tag — render this image. Re-picking the same name replaces it; a new name adds another variant.",
      },
      onSelect: async function (name, file, role, append, extra) {
        const r = await post("/api/projects/" + encodeURIComponent(pid()) +
          "/cast/locations/" + encodeURIComponent(lid) + "/refs/select",
          { type: "environment", name: name, file: file, append: append,
            tag: String(extra || "").trim() }, 30000);
        if (!r) return false;
        if (r.ok) {
          const tag = String(extra || "").trim();
          toast(tag ? "variant '" + tag + "' set" : "environment ref set");
          return true;
        }
        toast(r.error || "select failed", true);
        return false;
      },
      onDone: function () { loadLocRefs(card, lid); loadLocSceneMap(card, lid); },
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
      box.innerHTML = '<span class="muted small">no scenes yet — generate the shot plan first</span>';
      return;
    }
    if (!tags.length) {
      box.innerHTML = '<span class="muted small">add at least one named variant above ' +
        "(the variant name field in the picker) to map scenes</span>";
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
        if (r2 && r2.ok) { toast("weapon reference removed"); loadWeaponRefs(card, cid, wid); }
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
      strip.innerHTML = '<span class="muted small">no composite ref — identity still will be used</span>';
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
        if (r2 && r2.ok) { toast("composite reference removed"); loadLoadoutRefs(card, cid, name); }
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
      box.innerHTML = '<span class="muted small">no scenes yet — generate the shot plan first</span>';
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
      title: "Pick weapon still — " + wid,
      onSelect: async function (name, file) {
        const r = await post("/api/projects/" + encodeURIComponent(pid()) +
          "/cast/characters/" + encodeURIComponent(cid) + "/slotrefs/weapon/" +
          encodeURIComponent(wid) + "/select",
          { type: "weapon", name: name, file: file, append: true }, 30000);
        if (!r) return false;
        if (r.ok) { toast("weapon reference added"); return true; }
        toast(r.error || "select failed", true);
        return false;
      },
      onDone: function () { loadWeaponRefs(card, cid, wid); },
    });
  }

  function openLoadoutPick(card, cid, name) {
    openBatchPicker({
      type: "face",
      title: "Pick composite reference — " + name,
      onSelect: async function (bname, file) {
        const r = await post("/api/projects/" + encodeURIComponent(pid()) +
          "/cast/characters/" + encodeURIComponent(cid) + "/herorefs/select",
          { type: "face", name: bname, file: file, tag: name, append: true }, 30000);
        if (!r) return false;
        if (r.ok) { toast("composite reference added"); return true; }
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
        if (act === "save") saveCharacter(card, ch);
        else if (act === "delete") deleteCharacter(card, ch);
        else if (act === "add-power") addPowerRow(card, ch);
        else if (act === "heroref-upload") {
          const fileIn = card.querySelector(".heroref-file");
          if (fileIn) fileIn.click();
        }
        else if (act === "heroref-pick") openHeroPick(card, ch.id || "");
        else if (act === "autofill") openAutofillPick(card, ch);
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
          if (!wid) { toast("give the weapon an id first", true); return; }
          refPickGuard(card, ch,
            function (saved) { return !!(saved.weapons || {})[wid]; },
            function (card2, cid2) { openWeaponPick(card2, cid2, wid); });
        }
        else if (act === "rm-weapon") {
          const row = b.closest(".wp-row");
          if (row && row.parentNode) row.parentNode.removeChild(row);
          const list = card.querySelector(".wp-list");
          if (list && !list.querySelector(".wp-row")) {
            list.innerHTML = '<div class="muted small">no weapons yet</div>';
          }
        }
        else if (act === "add-loadout") addLoadoutRow(card, ch);
        else if (act === "loadoutref-pick") {
          const row = b.closest(".ld-row");
          const name = row
            ? ((row.querySelector(".ld-name") || {}).value || "").trim() : "";
          if (!name) {
            toast("name the loadout first (e.g. AnnaWithBlade)", true);
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
            list.innerHTML = '<div class="muted small">no loadouts yet — the base outfit is used</div>';
          }
        }
        else if (act === "rm-power") {
          const row = b.closest(".pw-row");
          if (row && row.parentNode) row.parentNode.removeChild(row);
          const list = card.querySelector(".pw-list");
          if (list && !list.querySelector(".pw-row")) {
            list.innerHTML = '<div class="muted small">no powers yet</div>';
          }
        }
      };
    });
  }

  /* References attach to the SAVED bible entry (the endpoint looks the power /
     weapon / loadout up in the YAML), so an unsaved row must be persisted
     first. Saves, then opens the picker on the re-rendered card. */
  async function refPickGuard(card, ch, exists, run) {
    const saved = ((State.bible || {}).characters || {})[ch.id || ""];
    if (saved && exists(saved)) { run(card, ch.id || ""); return; }
    toast("saving the character first — references attach to the saved entry");
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
        ws.innerHTML = '<option value="">no weapon</option>' + weaponOptions(card, cur);
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
    if (ws) ws.innerHTML = '<option value="">no weapon</option>' + weaponOptions(card, "");
    list.appendChild(row);
    bindTips(row);
    bindCardActions(card, ch);   // the new row's buttons need handlers
  }

  /* -------------------------------------------------------------- regenerate */
  async function regenerate() {
    const bible = State.bible || {};
    const chars = asList(bible.characters).filter(function (c) { return c.enabled !== false; });
    const locs = asList(bible.locations).filter(function (l) { return l.enabled !== false; });
    if (!chars.length && !locs.length) {
      toast("the cast is empty — enable at least one character or location first", true);
      return;
    }
    toast("regenerating story from this cast — this can take minutes…");
    const r = await post("/api/projects/" + encodeURIComponent(pid()) + "/story/generate", {
      stages: ["beats", "scenes", "shots"],
      cast_config: {
        characters: chars.map(function (c) {
          return {
            id: c.id || "",
            name: c.name || "",
            tier: c.tier || "supporting",
            body: c.body || "",
            outfit: c.outfit || "",
            emotion_palette: c.emotion_palette || [],
            powers: c.powers || {},
            weapons: c.weapons || {},
            outfits: c.outfits || {},
            loadouts: c.loadouts || {},
            scene_tags: c.scene_tags || {},
            lora: c.lora || null,
            enabled: true,
          };
        }),
        locations: locs.map(function (l) {
          return {
            id: l.id || "",
            descriptor: l.descriptor || "",
            lighting: l.lighting || "",
            lora: l.lora || null,
            enabled: true,
          };
        }),
      },
    }, 900000);
    if (!r) return;
    if (r.ok) {
      toast(r.shots_ingested != null ? r.shots_ingested + " shots regenerated" : "story regenerated");
      await State.refreshProject();
      await State.refreshShots();
    } else {
      toast("generation failed: " + (r.error || "?"), true);
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
          if (b.dataset.act === "save") saveLocation(card, l);
          else if (b.dataset.act === "delete") deleteLocation(card, l);
          else if (b.dataset.act === "loc-rename") renameLocation(card, l);
          else if (b.dataset.act === "locref-pick") openLocPick(card, l.id || "");
          else if (b.dataset.act === "locref-upload") {
            const fileIn = card.querySelector(".locref-file");
            if (fileIn) fileIn.click();
          }
        };
      });
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
    mount: function (el) {
      const page = this;
      page.el = el;
      page.render = function () { render(el); };
      page.subs.push(Bus.on("project", page.render));
      page.subs.push(Bus.on("loras", page.render));
      page.render();
      State.refreshLoras(false);
    },
    unmount: function () {
      this.subs.forEach(function (off) { off(); });
      this.subs = [];
      if (this.el) this.el.innerHTML = "";
    },
  });
})();
