/* icons.js — the Gold Slate inline icon set.
   Hand-drawn on a 24x24 grid, 1.6 stroke, square terminals to match the
   2px structural radius of the UI. No external icon font or CDN.
   Usage: Icon.get("play")  /  Icon.get("play", "extra-class")
   Replaces the old Unicode/emoji glyphs everywhere in both shells. */
(function () {
  "use strict";

  var P = {
    "gauge": '<path d="M4.5 15.5a7.5 7.5 0 0 1 15 0"/><path d="M12 15.5l3.6-4.8"/><circle cx="12" cy="15.5" r="1" fill="currentColor" stroke="none"/>',
    "pen": '<path d="M13.8 6.2l4 4"/><path d="M4 20l1.2-4.2L16.4 4.6a2.05 2.05 0 0 1 2.9 2.9L8.2 18.7 4 20z"/>',
    "users": '<circle cx="9" cy="8.2" r="3.2"/><path d="M3.5 20c.3-3.2 2.6-5 5.5-5s5.2 1.8 5.5 5"/><path d="M15.5 5.4a3.2 3.2 0 0 1 0 5.6"/><path d="M17.3 15.3c1.9.8 3 2.4 3.2 4.7"/>',
    "film": '<rect x="3.5" y="4.5" width="17" height="15"/><path d="M8.2 4.5v15M15.8 4.5v15"/><path d="M3.5 9h4.7M3.5 15h4.7M15.8 9h4.7M15.8 15h4.7"/>',
    "clapper": '<path d="M4 9.5L5.5 5h15L19 9.5"/><path d="M4 9.5h16V20H4z"/><path d="M9.6 5L8.1 9.5M14.8 5l-1.5 4.5"/>',
    "clapper-play": '<path d="M4 9.5L5.5 5h15L19 9.5"/><path d="M4 9.5h16V20H4z"/><path d="M10.3 12.2l4.6 2.55-4.6 2.55v-5.1z"/>',
    "shield-check": '<path d="M12 3l7 2.7v5.5c0 4.6-2.9 7.7-7 9.3-4.1-1.6-7-4.7-7-9.3V5.7L12 3z"/><path d="M8.8 11.6l2.3 2.3 4.2-4.6"/>',
    "play": '<path d="M7.5 4.8l11.5 7.2-11.5 7.2V4.8z"/>',
    "stop": '<rect x="6.5" y="6.5" width="11" height="11"/>',
    "pause": '<path d="M8.7 5.5v13M15.3 5.5v13"/>',
    "resume": '<path d="M20 12a8 8 0 1 1-2.34-5.66"/><path d="M20.5 2.5V7H16"/>',
    "refresh": '<path d="M19.5 12a7.5 7.5 0 0 0-13.3-4.8"/><path d="M6 2.8v4.5h4.5"/><path d="M4.5 12a7.5 7.5 0 0 0 13.3 4.8"/><path d="M18 21.2v-4.5h-4.5"/>',
    "upload": '<path d="M12 15.5V4.5"/><path d="M7.5 9L12 4.5 16.5 9"/><path d="M4.5 15v4.5h15V15"/>',
    "download": '<path d="M12 4.5v11"/><path d="M7.5 11L12 15.5 16.5 11"/><path d="M4.5 19.5h15"/>',
    "copy": '<rect x="9.5" y="9.5" width="10" height="10"/><path d="M6.5 14.5h-2v-10h10v2"/>',
    "import": '<path d="M12 3.5v9"/><path d="M8.5 9L12 12.5 15.5 9"/><path d="M4.5 13.5V20h15v-6.5"/>',
    "sliders": '<path d="M4 7h3.2M11 7h9M4 12h9M16.8 12H20M4 17h2.2M10 17h10"/><circle cx="9.1" cy="7" r="1.9"/><circle cx="14.9" cy="12" r="1.9"/><circle cx="7.9" cy="17" r="1.9"/>',
    "trash": '<path d="M4.5 6.5h15"/><path d="M9.2 6.5V4.5h5.6v2"/><path d="M6.4 6.5l.9 13.5h9.4l.9-13.5"/><path d="M10 10.2v6M14 10.2v6"/>',
    "plus": '<path d="M12 5.5v13M5.5 12h13"/>',
    "x": '<path d="M6 6l12 12M18 6L6 18"/>',
    "chevron-down": '<path d="M6.5 9.5l5.5 5.5 5.5-5.5"/>',
    "chevron-right": '<path d="M9.5 6.5l5.5 5.5-5.5 5.5"/>',
    "external": '<path d="M13.5 4H20v6.5"/><path d="M20 4l-8.5 8.5"/><path d="M19 13.5V20H4V5h6.5"/>',
    "warning": '<path d="M12 3.5L22 20.5H2L12 3.5z"/><path d="M12 9.5v4.5"/><circle cx="12" cy="16.8" r="1" fill="currentColor" stroke="none"/>',
    "image": '<rect x="3.5" y="4.5" width="17" height="15"/><circle cx="8.8" cy="9.8" r="1.6"/><path d="M20.5 15l-4.7-4.7L7.5 19.5"/>',
    "pin": '<path d="M12 21.5S5 14.9 5 10a7 7 0 0 1 14 0c0 4.9-7 11.5-7 11.5z"/><circle cx="12" cy="10" r="2.2"/>',
    "folder": '<path d="M3.5 6A1.5 1.5 0 0 1 5 4.5h4.6l2.3 2.3H19a1.5 1.5 0 0 1 1.5 1.5V18a1.5 1.5 0 0 1-1.5 1.5H5A1.5 1.5 0 0 1 3.5 18V6z"/>',
    "eye": '<path d="M2.5 12S6 5.8 12 5.8 21.5 12 21.5 12 18 18.2 12 18.2 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="2.7"/>',
    "help": '<circle cx="12" cy="12" r="8.5"/><path d="M9.4 9.2a2.7 2.7 0 0 1 5.2.9c0 1.8-2.6 2.1-2.6 3.6"/><circle cx="12" cy="16.6" r="1" fill="currentColor" stroke="none"/>',
    "check": '<path d="M4.5 12.5l4.8 4.8L19.5 7"/>',
    "dots": '<circle cx="5.5" cy="12" r="1.3" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.3" fill="currentColor" stroke="none"/><circle cx="18.5" cy="12" r="1.3" fill="currentColor" stroke="none"/>',
    "arrow-left": '<path d="M20 12H4.5"/><path d="M10.5 5.5L4 12l6.5 6.5"/>',
    "arrow-right": '<path d="M4 12h15.5"/><path d="M13.5 5.5L20 12l-6.5 6.5"/>',
    "cpu": '<rect x="6" y="6" width="12" height="12"/><rect x="10" y="10" width="4" height="4"/><path d="M9 3.5V6M15 3.5V6M9 18v2.5M15 18v2.5M3.5 9H6M3.5 15H6M18 9h2.5M18 15h2.5"/>',
    "clock": '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.2V12l3.4 2"/>',
    "book": '<path d="M4.5 5.5A2.5 2.5 0 0 1 7 3h12.5v14.5H7A2.5 2.5 0 0 0 4.5 20V5.5z"/><path d="M4.5 20A2.5 2.5 0 0 1 7 17.5h12.5"/>',
    "layers": '<path d="M12 3.5l9 4.5-9 4.5L3 8l9-4.5z"/><path d="M3 12.5l9 4.5 9-4.5"/><path d="M3 16.5L12 21l9-4.5"/>',
    "box": '<path d="M12 3l8.5 4.5v9L12 21l-8.5-4.5v-9L12 3z"/><path d="M3.5 7.5L12 12l8.5-4.5"/><path d="M12 12v9"/>',
    "video": '<rect x="3" y="6.5" width="13" height="11"/><path d="M16 10.5l5-3v9l-5-3"/>',
    "list": '<path d="M8.5 6.5H20M8.5 12H20M8.5 17.5H20"/><circle cx="4.6" cy="6.5" r="1" fill="currentColor" stroke="none"/><circle cx="4.6" cy="12" r="1" fill="currentColor" stroke="none"/><circle cx="4.6" cy="17.5" r="1" fill="currentColor" stroke="none"/>',
    "wand": '<path d="M4 20l9.5-9.5"/><path d="M15.5 3.5v5M13 6h5"/><path d="M19 12.5v4M17 14.5h4"/>',
    "grid": '<rect x="4" y="4" width="7" height="7"/><rect x="13" y="4" width="7" height="7"/><rect x="4" y="13" width="7" height="7"/><rect x="13" y="13" width="7" height="7"/>',
    "search": '<circle cx="10.8" cy="10.8" r="6.3"/><path d="M15.5 15.5L21 21"/>',
    "server": '<rect x="4" y="4.5" width="16" height="6"/><rect x="4" y="13.5" width="16" height="6"/><circle cx="7.6" cy="7.5" r="1" fill="currentColor" stroke="none"/><circle cx="7.6" cy="16.5" r="1" fill="currentColor" stroke="none"/><path d="M11.5 7.5h5M11.5 16.5h5"/>',
    "link": '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  };

  var ALIAS = {
    "edit": "pen", "settings": "sliders", "close": "x", "generate": "wand",
    "ai": "wand", "training": "cpu", "gpu": "cpu", "roster": "box",
    "assembly": "layers", "bible": "book", "logs": "list", "images": "image",
    "videos": "video", "story": "pen", "overview": "gauge", "review": "shield-check",
    "film": "clapper-play", "shots": "film", "cast": "users",
  };

  window.Icon = {
    has: function (name) { return !!P[name] || !!ALIAS[name]; },
    get: function (name, cls) {
      var key = P[name] ? name : (ALIAS[name] || "clapper");
      return '<svg class="ic' + (cls ? " " + cls : "") + '" viewBox="0 0 24 24" aria-hidden="true">' + P[key] + "</svg>";
    },
  };
})();
