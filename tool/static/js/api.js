/* api.js — thin fetch wrappers. No framework. Tolerates a backend still
   running old code: 404s toast "backend restart required", network errors
   toast "unreachable", and every wrapper returns null so pages stay alive. */
(function () {
  "use strict";

  /* ------------------------------------------------------------------ auth
     The ONE place the browser's API token is read and written: localStorage
     "kinoloom_token" plus the kinoloom_token cookie (so <img>/<video> URLs
     authenticate too). app.js, ui.js and workers-page.js all go through
     window.Auth; both index.html and project.html load this file first.
     Rules: prompt only on HTTP 401 (403 = cross-origin / permission, a token
     can't fix it), one prompt in flight shared by every caller, and after a
     cancel stay quiet until the page reloads or a token is saved. Never
     store the "********" redaction placeholder GET /api/settings returns. */
  const TOKEN_KEY = "kinoloom_token";

  function cleanToken(t) {
    const v = String(t == null ? "" : t).trim();
    return v && !/^\*+$/.test(v) ? v : "";
  }
  function getToken() {
    try { return cleanToken(localStorage.getItem(TOKEN_KEY)); } catch (_) { return ""; }
  }
  function writeCookie(v) {
    try {
      document.cookie = "kinoloom_token=" + (v ? encodeURIComponent(v) : "") +
        "; path=/; SameSite=Lax" + (v ? "" : "; max-age=0") +
        (location.protocol === "https:" ? "; Secure" : "");
    } catch (_) {}
  }
  // One-time move of a token saved before the project was renamed to
  // Kinoloom (localStorage "ai_studio_token", cookie "aistudio_token").
  (function migrateLegacyToken() {
    try {
      const old = cleanToken(localStorage.getItem("ai_studio_token"));
      if (old && !getToken()) { localStorage.setItem(TOKEN_KEY, old); writeCookie(old); }
      localStorage.removeItem("ai_studio_token");
      document.cookie = "aistudio_token=; path=/; SameSite=Lax; max-age=0";
    } catch (_) {}
  })();
  function syncSettingsInput(v) {
    const inp = document.getElementById("settings-token-input");
    if (inp) inp.value = v;
  }

  let _declined = false;      // user cancelled the prompt
  let _prompting = null;      // the single in-flight prompt

  function setToken(t) {
    const v = cleanToken(t);
    if (!v) return false;
    try { localStorage.setItem(TOKEN_KEY, v); } catch (_) {}
    writeCookie(v);
    _declined = false;
    syncSettingsInput(v);
    return true;
  }
  function clearToken() {
    try { localStorage.removeItem(TOKEN_KEY); } catch (_) {}
    writeCookie("");
    _declined = false;
    syncSettingsInput("");
  }

  function promptForToken(status) {
    if (_prompting) return _prompting;
    if (_declined) return Promise.resolve(null);
    _prompting = (async function () {
      try {
        let entered = null;
        const dlg = window.Dlg;
        const msg = "API access token required" + (status ? " (HTTP " + status + ")" : "") + ". Enter your token:";
        if (dlg && typeof dlg.prompt === "function") {
          entered = await dlg.prompt({
            title: "API Authentication",
            label: msg,
            placeholder: "Token...",
            okLabel: "Authenticate",
          });
        } else if (typeof window.prompt === "function") {
          entered = window.prompt(msg);
        }
        if (!setToken(entered)) {
          _declined = true;
          _warn("no API token entered — reload the page (or save one in Settings) to retry");
          return null;
        }
        return getToken();
      } finally {
        _prompting = null;
      }
    })();
    return _prompting;
  }

  function withAuth(headers, token) {
    const h = Object.assign({}, headers || {});
    if (token) {
      delete h.authorization;
      h.Authorization = "Bearer " + token;
    }
    return h;
  }

  /* 401 because the server has no API token configured at all: nothing the
     user could type would help, so don't prompt. */
  async function serverHasNoToken(res) {
    try {
      const j = await res.clone().json();
      return /requires an API token/i.test(String((j && (j.error || j.detail)) || ""));
    } catch (_) { return false; }
  }

  /* fetch() + the stored token + at most one 401 prompt/retry. Returns the
     Response like fetch does; the caller handles !res.ok. */
  async function authFetch(url, init) {
    init = init || {};
    const sent = getToken();
    const res = await fetch(url, Object.assign({}, init, { headers: withAuth(init.headers, sent) }));
    if (res.status !== 401 || await serverHasNoToken(res)) return res;
    // Another request may already have obtained a newer token while this one
    // was in flight: reuse it instead of prompting again.
    let tok = getToken();
    if (!tok || tok === sent) tok = await promptForToken(401);
    if (!tok || tok === sent) return res;
    return fetch(url, Object.assign({}, init, { headers: withAuth(init.headers, tok) }));
  }

  // ?token=… capture on boot; strip it from the address bar afterwards so it
  // doesn't linger in history, bookmarks or screenshots. Otherwise re-issue
  // the (session) cookie from localStorage so media URLs work after a
  // browser restart.
  try {
    const params = new URLSearchParams(window.location.search);
    if (params.has("token")) {
      setToken(params.get("token"));
      params.delete("token");
      const qs = params.toString();
      window.history.replaceState(window.history.state, document.title,
        window.location.pathname + (qs ? "?" + qs : "") + window.location.hash);
    } else if (getToken()) {
      writeCookie(getToken());
    } else if (localStorage.getItem(TOKEN_KEY)) {
      clearToken();   // a stored redaction placeholder from an older build
    }
  } catch (_) {}

  window.Auth = {
    getToken: getToken,
    setToken: setToken,
    clearToken: clearToken,
    promptForToken: promptForToken,
    fetch: authFetch,
  };

  function ApiError(status, message) {
    this.status = status || 0;
    this.message = message || "request failed";
  }
  ApiError.prototype = Object.create(Error.prototype);

  function _warn(msg) {
    try { window.toast && window.toast(msg, true); } catch (e) { /* boot */ }
  }

  async function raw(url, opts, timeoutMs) {
    const ttl = timeoutMs || 60000;
    const ctrl = new AbortController();
    const timer = setTimeout(function () { ctrl.abort(); }, ttl);
    try {
      const headers = Object.assign({
        "Content-Type": "application/json",
      }, (opts && opts.headers) || {});
      const res = await authFetch(url, Object.assign({}, opts || {}, {
        headers: headers,
        signal: ctrl.signal,
      }));
      const ct = res.headers.get("content-type") || "";
      let body = null;
      try {
        body = ct.indexOf("application/json") >= 0
          ? await res.json()
          : await res.text();
      } catch (e) { body = null; }
      if (!res.ok) {
        const detail = body && (body.detail || body.error) || res.statusText;
        throw new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail));
      }
      return body;
    } finally {
      clearTimeout(timer);
    }
  }

  function _fail(e, url, quiet) {
    if (!quiet) {
      if (e instanceof ApiError && e.status === 404) {
        _warn("endpoint missing — backend restart required");
      } else if (e.name === "AbortError") {
        _warn("request timed out: " + url);
      } else {
        _warn("request failed: " + (e.message || e));
      }
    }
    return null;
  }

  async function get(url, timeoutMs, quiet) {
    try {
      return await raw(url, { method: "GET" }, timeoutMs);
    } catch (e) {
      return _fail(e, url, quiet);
    }
  }

  async function post(url, body, timeoutMs, quiet) {
    try {
      return await raw(url, {
        method: "POST",
        body: JSON.stringify(body == null ? {} : body),
      }, timeoutMs);
    } catch (e) {
      return _fail(e, url, quiet);
    }
  }

  /* any other verb (PUT / PATCH / DELETE); same null-on-failure contract */
  async function send(method, url, body, timeoutMs, quiet) {
    try {
      return await raw(url, {
        method: String(method || "GET").toUpperCase(),
        body: body == null ? undefined : JSON.stringify(body),
      }, timeoutMs);
    } catch (e) {
      return _fail(e, url, quiet);
    }
  }

  window.API = { get: get, post: post, send: send, raw: raw, ApiError: ApiError };
})();
