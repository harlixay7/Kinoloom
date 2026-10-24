import socket
from unittest.mock import patch
import pytest
from dataset import validate_safe_url


def test_validate_safe_url_disallowed_schemes():
    """file://, http://, and ftp:// schemes are rejected."""
    bad_urls = [
        "file:///etc/passwd",
        "file:///C:/Windows/win.ini",
        "http://127.0.0.1:8080/image.jpg",
        "http://example.com/asset.png",
        "ftp://example.com/files/1.jpg",
        "gopher://127.0.0.1/",
    ]
    for url in bad_urls:
        with pytest.raises(ValueError, match="Disallowed scheme"):
            validate_safe_url(url)


def test_validate_safe_url_leading_dash():
    """URLs starting with a dash (CLI flag injection vectors) are rejected."""
    dash_urls = [
        "-https://example.com/test.jpg",
        " --output=/tmp/pwned https://example.com/test.jpg",
        "-v",
    ]
    for url in dash_urls:
        with pytest.raises(ValueError, match="URL cannot start with a dash"):
            validate_safe_url(url)


def test_validate_safe_url_credentials_rejected():
    """URLs containing embedded credentials are rejected."""
    with pytest.raises(ValueError, match="URL must not contain credentials"):
        validate_safe_url("https://admin:secret123@example.com/image.png")


def test_validate_safe_url_private_and_cloud_metadata_ips():
    """Private, loopback, link-local, and cloud metadata (169.254.169.254) are rejected."""
    prohibited_ips = [
        "127.0.0.1",          # Loopback
        "10.0.0.1",           # RFC 1918 Private
        "172.16.0.1",         # RFC 1918 Private
        "192.168.1.1",        # RFC 1918 Private
        "169.254.169.254",    # Link-local / Cloud metadata service
        "::1",                # IPv6 Loopback
        "fe80::1",            # IPv6 Link-local
    ]

    for ip in prohibited_ips:
        fake_addr_info = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]
        with patch("socket.getaddrinfo", return_value=fake_addr_info):
            with pytest.raises(ValueError, match="Resolved to non-public IP address"):
                validate_safe_url("https://internal-target.com/image.png")


def test_validate_safe_url_ipv6_mapped_ipv4():
    """IPv6-mapped IPv4 addresses pointing to private ranges are rejected."""
    mapped_ips = [
        "::ffff:127.0.0.1",
        "::ffff:192.168.1.1",
        "::ffff:169.254.169.254",
    ]
    for ip in mapped_ips:
        fake_addr_info = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, 443, 0, 0))]
        with patch("socket.getaddrinfo", return_value=fake_addr_info):
            with pytest.raises(ValueError, match="(Resolved to non-public|non-public IPv4-mapped address)"):
                validate_safe_url("https://mapped-test.com/photo.jpg")


@pytest.mark.parametrize("ip", ["100.64.0.1", "100.127.255.254",  # CGNAT
                                "0.0.0.0", "224.0.0.1", "ff02::1", "fc00::1"])
def test_validate_safe_url_cgnat_multicast_ula(ip):
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
    with patch("socket.getaddrinfo",
               return_value=[(fam, socket.SOCK_STREAM, 6, "", (ip, 443))]):
        with pytest.raises(ValueError, match="non-public"):
            validate_safe_url("https://cgnat.example/x.png")


def test_validate_safe_url_any_private_answer_rejects():
    """A mixed DNS answer (one public, one private) is rejected outright."""
    info = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))]
    with patch("socket.getaddrinfo", return_value=info):
        with pytest.raises(ValueError, match="non-public"):
            validate_safe_url("https://mixed.example/x.png")


def test_import_urls_pins_validated_address(monkeypatch):
    """curl is pinned (--resolve) to the address that was validated, so a
    second DNS lookup cannot rebind the download to a private host."""
    import dataset
    calls = []

    class _R:
        returncode = 22
        stderr = b"404"

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    info = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    monkeypatch.setattr("dataset.subprocess.run", fake_run)
    monkeypatch.setattr("dataset.shutil.which", lambda _n: "curl")
    with patch("socket.getaddrinfo", return_value=info):
        out = dataset.import_urls(["https://img.example/a.png"], "face",
                                  "pin_test", "")
    assert out["ok"] is False and out["added"] == 0
    cmd = calls[0]
    assert cmd[cmd.index("--resolve") + 1] == "img.example:443:93.184.216.34"
    assert cmd[cmd.index("--max-redirs") + 1] == "0"
    assert "--max-filesize" in cmd and cmd[-2] == "--"


def test_import_urls_caps_url_count():
    import dataset
    out = dataset.import_urls(["https://x.example/a.png"] * (dataset.MAX_IMPORT_URLS + 1),
                              "face", "cap_test", "")
    assert out["ok"] is False and "too many" in out["error"]


def test_validate_safe_url_valid_public_https():
    """Valid public HTTPS URLs with public IP resolution succeed."""
    # 93.184.216.34 (example.com public IP)
    public_ip = "93.184.216.34"
    fake_addr_info = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (public_ip, 443))]
    with patch("socket.getaddrinfo", return_value=fake_addr_info):
        url = "https://images.example.com/assets/hero_reference.png"
        validated = validate_safe_url(url)
        assert validated == url
