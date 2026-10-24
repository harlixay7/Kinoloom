"""End-to-end checks of the API security gate (tool/api.py + tool/security.py).

Peers and Host headers are simulated with TestClient(base_url=..., client=...):
the local operator is a loopback peer on a loopback Host; a LAN browser is a
192.168.x peer addressing the studio by its LAN IP.
"""
import pytest

from routers import common

TOKEN = "production_secret_token"
LAN = dict(base_url="http://192.168.1.10:8080", peer="192.168.1.50")


@pytest.fixture
def security_cfg(monkeypatch):
    """Patch the live security section for one test (restored afterwards)."""
    def _set(**values):
        sec = dict(common.cfg.get("security") or {})
        sec.update(values)
        monkeypatch.setitem(common.cfg, "security", sec)
    monkeypatch.delenv("KINOLOOM_API_TOKEN", raising=False)
    return _set


# ------------------------------------------------------------ authentication
def test_local_operator_needs_no_token(test_client, security_cfg):
    security_cfg(api_token=TOKEN, local_bypass=True)
    assert test_client.get("/api/projects").status_code == 200


def test_local_bypass_off_requires_token_even_locally(test_client, security_cfg):
    security_cfg(api_token=TOKEN, local_bypass=False)
    assert test_client.get("/api/projects").status_code == 401
    ok = test_client.get("/api/projects", headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200


def test_remote_refused_when_no_token_configured(make_client, security_cfg):
    security_cfg(api_token="")
    res = make_client(**LAN).get("/api/projects")
    assert res.status_code == 401
    assert "requires an API token" in res.json()["error"]


@pytest.mark.parametrize("headers,cookies", [
    ({"Authorization": f"Bearer {TOKEN}"}, {}),
    ({"X-API-Token": TOKEN}, {}),
    ({}, {"kinoloom_token": TOKEN}),
])
def test_remote_with_valid_token(make_client, security_cfg, headers, cookies):
    security_cfg(api_token=TOKEN)
    client = make_client(**LAN)
    client.cookies.update(cookies)
    assert client.get("/api/projects", headers=headers).status_code == 200


def test_remote_with_bad_or_missing_token(make_client, security_cfg):
    security_cfg(api_token=TOKEN)
    client = make_client(**LAN)
    res = client.get("/api/projects")
    assert res.status_code == 401
    assert res.json() == {"ok": False, "error": "Unauthorized"}
    bad = client.get("/api/projects", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401


def test_env_token_overrides_settings(make_client, security_cfg, monkeypatch):
    security_cfg(api_token="settings-token")
    monkeypatch.setenv("KINOLOOM_API_TOKEN", TOKEN)
    client = make_client(**LAN)
    assert client.get("/api/projects", headers={"X-API-Token": TOKEN}).status_code == 200
    assert client.get("/api/projects",
                      headers={"X-API-Token": "settings-token"}).status_code == 401


def test_status_is_not_public(make_client, security_cfg):
    security_cfg(api_token=TOKEN)
    assert make_client(**LAN).get("/api/status").status_code == 401


def test_public_paths_reachable_without_token(make_client, security_cfg):
    security_cfg(api_token=TOKEN)
    client = make_client(**LAN)
    assert client.get("/api/health").status_code == 200
    assert client.get("/").status_code == 200          # UI shell (token prompt)


def test_worker_token_is_not_an_api_token(make_client, security_cfg):
    """The old gate let X-Worker-Token through for every route."""
    security_cfg(api_token=TOKEN)
    wtok = common.cfg["workers"]["shared_token"]
    res = make_client(**LAN).get("/api/settings", headers={"X-Worker-Token": wtok})
    assert res.status_code == 401


def test_worker_callbacks_bypass_api_token_but_check_worker_token(make_client, security_cfg):
    security_cfg(api_token=TOKEN)
    res = make_client(**LAN).post("/api/workers/register",
                                  json={"name": "w1", "host": "192.168.1.50",
                                        "token": "wrong"})
    # reached the handler (not the gate) and was refused there
    assert res.status_code == 401
    assert "worker token" in res.json()["detail"]


# ---------------------------------------------------------- host / origin
def test_dns_rebinding_host_rejected(make_client):
    # loopback peer, but the browser addressed a hostname that resolved to us
    res = make_client(base_url="http://attacker.example:8080").get("/api/projects")
    assert res.status_code == 400


def test_allowed_hosts_admits_named_host(make_client, security_cfg):
    client = make_client(base_url="http://studio.lan:8080")
    assert client.get("/api/projects").status_code == 400
    security_cfg(allowed_hosts=["studio.lan"], api_token=TOKEN)
    # past the Host gate; a named Host is not loopback, so the token applies
    assert client.get("/api/projects").status_code == 401
    ok = client.get("/api/projects", headers={"X-API-Token": TOKEN})
    assert ok.status_code == 200


def test_cross_origin_post_blocked(test_client):
    res = test_client.post("/api/health", headers={"Origin": "http://evil.example"})
    assert res.status_code == 403


@pytest.mark.parametrize("headers", [
    {"Origin": "null"},
    {"Sec-Fetch-Site": "cross-site"},
])
def test_opaque_or_cross_site_post_blocked(test_client, headers):
    assert test_client.post("/api/health", headers=headers).status_code == 403


def test_same_origin_post_passes_gate(test_client):
    # passes the gate; /api/health has no POST handler -> 405, not 403
    res = test_client.post("/api/health", headers={"Origin": "http://127.0.0.1:8080"})
    assert res.status_code == 405


def test_cli_post_without_origin_passes_gate(test_client):
    assert test_client.post("/api/health").status_code == 405


# ------------------------------------------------------------- CORS + headers
def test_cors_preflight_for_configured_origin(test_client):
    # settings.example.yaml security.cors_origins (read once at import)
    res = test_client.options("/api/projects", headers={
        "Origin": "http://localhost:8080",
        "Access-Control-Request-Method": "GET",
    })
    assert res.status_code == 200
    assert res.headers.get("access-control-allow-origin") == "http://localhost:8080"
    assert res.headers.get("access-control-allow-credentials") == "true"


def test_cors_preflight_unlisted_origin_gets_no_grant(test_client):
    res = test_client.options("/api/projects", headers={
        "Origin": "http://localhost:3000",
        "Access-Control-Request-Method": "GET",
    })
    assert res.headers.get("access-control-allow-origin") is None


def test_baseline_security_headers(test_client):
    res = test_client.get("/api/health")
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["x-frame-options"] == "SAMEORIGIN"
    assert res.headers["referrer-policy"] == "same-origin"


# ------------------------------------------------------------------ secrets
def test_settings_get_redacts_secrets(test_client):
    data = test_client.get("/api/settings").json()
    assert data["workers"]["shared_token"] == "********"
    assert common.cfg["workers"]["shared_token"] != "********"


def test_settings_roundtrip_keeps_secrets(test_client):
    real = common.cfg["workers"]["shared_token"]
    data = test_client.get("/api/settings").json()
    res = test_client.post("/api/settings", json={"settings": data})
    assert res.status_code == 200 and res.json()["ok"] is True
    assert common.cfg["workers"]["shared_token"] == real


def test_settings_post_rejects_fragment(test_client):
    res = test_client.post("/api/settings", json={"settings": {"ui": {"port": 1}}})
    assert res.status_code == 422
    assert "paths" in common.cfg            # live settings untouched
