"""install/install_support.py: the install log, pip runs, the failure report.

No network and no real pip: subprocess.run is faked, and the log texts are in
the format pip 26 writes with --log."""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
STAMP = "2026-10-26T00:12:25,312 "


@pytest.fixture()
def sup(sandbox_root, tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "install_support_under_test", sandbox_root / "install" / "install_support.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod                        # dataclasses look the module up
    spec.loader.exec_module(mod)
    logs = tmp_path / "logs" / "install"
    for name, value in {"ROOT": tmp_path, "LOG_DIR": logs, "INSTALL_LOG": logs / "install.log",
                        "LAST_FAILURE": logs / "LAST-FAILURE.txt",
                        "PIP_CACHE": tmp_path / "temp" / "pip-cache",
                        "PIP_TMP": tmp_path / "temp" / "pip-tmp"}.items():
        monkeypatch.setattr(mod, name, value)
    monkeypatch.setattr(mod, "keep_awake", lambda: None)
    monkeypatch.setattr(mod, "no_quick_edit", lambda: None)
    monkeypatch.setattr(mod, "RETRY_WAIT_S", 0)
    monkeypatch.setattr(mod, "windows_proxies", lambda: {})      # no proxy unless a test sets one
    monkeypatch.delenv("KINOLOOM_RERUN", raising=False)
    yield mod
    sys.modules.pop(spec.name, None)


def piplog(*lines: str) -> str:
    return "".join(STAMP + line + "\n" for line in lines)


# ---------------------------------------------------------------- classify
UNREACHABLE_INDEX = piplog(
    "WARNING: Retrying (Retry(total=9, connect=None, read=None, redirect=None, status=None)) "
    "after connection broken by 'NewConnectionError(\"HTTPConnection(host='127.0.0.1', port=9): "
    "Failed to establish a new connection: [WinError 10061] No connection could be made because "
    "the target machine actively refused it\")': /simple/six/",
    "Could not fetch URL http://127.0.0.1:9/simple/six/: connection error: [WinError 10061] - skipping",
    "ERROR: Could not find a version that satisfies the requirement six==1.17.0 (from versions: none)",
    "ERROR: No matching distribution found for six==1.17.0",
    "Exception information:",
    "Traceback (most recent call last):",
    '  File "C:\\K\\pip\\_internal\\cli\\base_command.py", line 106, in _run_wrapper',
    "pip._internal.exceptions.DistributionNotFound: No matching distribution found for six==1.17.0")

# an error pip did not expect: the traceback's last line is the cause
DOWNLOAD_BROKE = piplog(
    "ERROR: Exception:",
    "Traceback (most recent call last):",
    '  File "C:\\K\\pip\\_vendor\\urllib3\\response.py", line 438, in _error_catcher',
    "    yield",
    "pip._vendor.urllib3.exceptions.ReadTimeoutError: HTTPSConnectionPool(host='download-r2.pytorch.org', "
    "port=443): Read timed out.")

GIT_UNREACHABLE = piplog(
    "  Running command git clone --filter=blob:none --quiet https://github.com/x/y 'C:\\T\\y'",
    "  fatal: unable to access 'https://github.com/x/y/': Failed to connect to github.com port 443 "
    "after 21045 ms: Could not connect to server",
    "  ERROR: git clone --filter=blob:none --quiet https://github.com/x/y 'C:\\T\\y' exited with 128",
    "ERROR: Failed to build 'y' when git clone --filter=blob:none --quiet https://github.com/x/y")


@pytest.mark.parametrize("text, cause", [
    (UNREACHABLE_INDEX, "network"),
    (GIT_UNREACHABLE, "network"),
    (DOWNLOAD_BROKE, "network"),
    (piplog("ERROR: Exception:", "Traceback (most recent call last):", '  File "x.py", line 1, in f',
            "ConnectionResetError: [WinError 10054] An existing connection was forcibly closed by the "
            "remote host"), "network"),
    (piplog("WARNING: Retrying (Retry(total=9)) after connection broken by 'NameResolutionError"
            "(\"Failed to resolve 'pypi.org' ([Errno 11001] getaddrinfo failed)\")': /simple/torch/",
            "ERROR: No matching distribution found for torch==2.13.0+cu126"), "dns"),
    (piplog("fatal: unable to access 'https://github.com/x/y/': Could not resolve host: github.com",
            "ERROR: Failed to build 'y' when git clone"), "dns"),
    (piplog("ERROR: Could not install packages due to an OSError: HTTPSConnectionPool("
            "host='download-r2.pytorch.org', port=443): Read timed out."), "network"),
    (piplog("ERROR: Could not install packages due to an OSError: HTTPSConnectionPool(host='pypi.org', "
            "port=443): Max retries exceeded with url: /simple/x/ (Caused by SSLError(SSLCertVerificationError"
            "(1, '[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer "
            "certificate (_ssl.c:1010)')))"), "ssl"),
    (piplog("WARNING: Retrying (Retry(total=9)) after connection broken by 'ProxyError('Unable to "
            "connect to proxy', ...)': /simple/x/", "ERROR: No matching distribution found for x==1"),
     "proxy"),
    (piplog("ERROR: Could not install packages due to an OSError: [Errno 28] No space left on device"),
     "disk"),
    (piplog("ERROR: Could not install packages due to an OSError: [WinError 5] Access is denied: "
            "'C:\\K\\tool\\.venv\\Lib\\site-packages\\torch\\lib\\c10.dll'", "Check the permissions."),
     "locked"),
    (piplog("ERROR: Could not install packages due to an OSError: [WinError 225] Operation did not "
            "complete successfully because the file contains a virus or potentially unwanted software"),
     "virus"),
    (piplog("ERROR: Could not install packages due to an OSError: [Errno 2] No such file or directory: "
            "'C:\\K\\x'", "HINT: This error might have occurred since this system does not have "
            "Windows Long Path support enabled. You can find information on how to enable this at "
            "https://pip.pypa.io/warnings/enable-long-paths"), "longpath"),
    (piplog("ERROR: THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE."), "hash"),
    (piplog("  error: subprocess-exited-with-error", "  \u2570\u2500> [3 lines of output]",
            "      Traceback (most recent call last):", "      RuntimeError: boom",
            "      [end of output]", "ERROR: Failed to build 'x' when getting requirements to build wheel"),
     "build"),
    (piplog("ERROR: Could not find a version that satisfies the requirement nope==9 (from versions: 1.0)",
            "ERROR: No matching distribution found for nope==9"), "notfound"),
    (piplog("ERROR: something nobody has seen before"), "unknown"),
    ("download failed: https://codeload.github.com/o/r/zip/abc (URLError: <urlopen error "
     "[Errno 11001] getaddrinfo failed>)", "dns"),
    ("download failed: https://github.com/x.zip (IncompleteRead: IncompleteRead(100 bytes read, "
     "900 more expected))", "network"),
    ("BadZipFile: File is not a zip file", "damaged"),           # a sign-in page instead
    ("BadZipFile: Bad CRC-32 for file 'ComfyUI-master/nodes.py'", "damaged"),
    ("ReadError: not a gzip file", "damaged"),
])
def test_a_failure_is_classified_by_what_caused_it(sup, text, cause):
    got, lines = sup.classify(text)
    assert got == cause, lines
    assert lines and all(not line.startswith("2026-") for line in lines)     # no time stamps


def test_the_evidence_is_the_lines_that_show_it(sup):
    lines = sup.evidence(UNREACHABLE_INDEX + piplog(
        "WARNING: Retrying (Retry(total=8, connect=None, read=None, redirect=None, status=None)) "
        "after connection broken by 'NewConnectionError(\"HTTPConnection(host='127.0.0.1', port=9): "
        "Failed to establish a new connection: [WinError 10061] No connection could be made because "
        "the target machine actively refused it\")': /simple/six/"))
    assert sum("Retrying" in x for x in lines) == 1                 # the same retry, counted once
    assert "ERROR: No matching distribution found for six==1.17.0" in lines
    assert not any(x.startswith("pip.") for x in lines)             # pip's own traceback is left out
    build = sup.evidence(piplog("  \u2570\u2500> [2 lines of output]", "      a line of the build",
                                "      ValueError: the reason", "      [end of output]"))
    assert build == ["a line of the build", "ValueError: the reason"]


PIP_TRACE = piplog(        # what pip 26 writes after its ERROR lines (an E2E run, shortened)
    "Exception information:",
    "Traceback (most recent call last):",
    '  File "C:\\K\\pip\\_vendor\\resolvelib\\resolvers.py", line 397, in resolve',
    "    self._add_to_criteria(self.state.criteria, r, parent=None)",
    "pip._vendor.resolvelib.resolvers.RequirementsConflicted: Requirements conflict: "
    "SpecifierRequirement('pip==26.2.2')",
    "",
    "During handling of the above exception, another exception occurred:",
    "",
    "Traceback (most recent call last):",
    '  File "C:\\K\\urllib3\\connection.py", line 1, in connect',
    "urllib3.exceptions.NewConnectionError: <pip._vendor.urllib3.connection.HTTPConnection "
    "object at 0x0000023447398740>: Failed to establish a new connection",
    "Removed build tracker: 'C:\\K\\temp\\pip-tmp\\pip-build-tracker-x'")


def test_pips_own_traceback_is_not_evidence(sup):
    lines = sup.evidence(UNREACHABLE_INDEX + PIP_TRACE)
    assert lines[-1] == "ERROR: No matching distribution found for six==1.17.0", lines
    assert not any("Exception information" in x or "Traceback" in x or "urllib3.exceptions" in x
                   for x in lines)
    assert not any(" object at 0x" in x for x in lines)
    assert sup.classify(UNREACHABLE_INDEX + PIP_TRACE)[0] == "network"


# ---------------------------------------------------------------- the report
def test_a_failure_is_printed_logged_and_saved(sup, capsys, monkeypatch):
    monkeypatch.setenv("KINOLOOM_RERUN", "run this installer again with mode [3]")
    pip_log = sup.LOG_DIR / "pip-tool-venv.log"
    sup.report([sup.Failure("step 3 of 5, the web app's packages", "network",
                            ["ERROR: No matching distribution found for six==1.17.0"],
                            [sup.INSTALL_LOG, pip_log])])
    out = capsys.readouterr().out
    assert out.count("=" * 74) == 2 and "THE INSTALL STOPPED" in out
    assert "Step: step 3 of 5, the web app's packages" in out
    words = " ".join(out.split())                                    # the box wraps at 74
    assert "What happened: A download failed" in words
    assert "run this installer again with mode [3]" in words
    assert "ERROR: No matching distribution found for six==1.17.0" in out
    assert str(sup.LAST_FAILURE) in out and str(pip_log) in out
    saved = sup.LAST_FAILURE.read_text(encoding="utf-8")
    assert saved.startswith("Kinoloom install, ") and "What to do:" in saved
    logged = sup.INSTALL_LOG.read_text(encoding="utf-8")
    assert "==== FAILED " in logged and "No matching distribution" in logged


def test_the_advice_starts_a_sentence_where_it_is_one(sup):
    text = "\n".join(sup.failure_text([sup.Failure("s", "hash", ["x"])], "T"))
    assert "What to do: Run INSTALL.bat again. If it happens again" in text


# ---------------------------------------------------------------- pip
class FakePip:
    """subprocess.run for the venv flow: pip runs append to their --log file."""

    def __init__(self, results=None, imports=0, import_err=""):
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []
        self.results = list(results or [])
        self.imports, self.import_err = imports, import_err

    def __call__(self, cmd, **kw):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        self.envs.append(kw.get("env") or {})
        if cmd[1] == "-c":
            return types.SimpleNamespace(returncode=self.imports, stdout="", stderr=self.import_err)
        rc, text = self.results.pop(0) if self.results else (0, "")
        log = Path(cmd[cmd.index("--log") + 1])
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(text)
        return types.SimpleNamespace(returncode=rc)


def requirements(tmp_path: Path) -> Path:
    req = tmp_path / "requirements.txt"
    req.write_text("--extra-index-url https://download.pytorch.org/whl/cu126\n"
                   "-e git+https://github.com/o/lib@abc#egg=library\n"
                   "SAM-2 @ git+https://github.com/o/sam2@def  # [second-pass]: built against torch\n"
                   "torch==2.13.0+cu126\n", encoding="utf-8")
    return req


def test_a_venv_is_filled_pip_first_then_the_list_then_the_late_builds(sup, tmp_path, monkeypatch):
    fake = FakePip()
    monkeypatch.setattr(sup, "subprocess", types.SimpleNamespace(run=fake, TimeoutExpired=TimeoutError))
    monkeypatch.setenv("PYTHONPATH", "C:\\elsewhere")
    monkeypatch.setenv("PIP_USER", "1")
    monkeypatch.setenv("PIP_TIMEOUT", "120")
    for var in ("PIP_CACHE_DIR", "PIP_NO_CACHE_DIR", "PIP_RETRIES"):
        monkeypatch.delenv(var, raising=False)
    py = tmp_path / "venv" / "Scripts" / "python.exe"
    assert sup.install_venv_packages(py, requirements(tmp_path), "tool-venv",
                                     imports=["uvicorn", "torch"]) is None
    pip = [c[3:c.index("--log")] for c in fake.calls if c[1:3] == ["-m", "pip"]]
    assert pip[0] == ["install", "--upgrade", sup.PIP]
    assert pip[1][:2] == ["install", "-r"]
    first = Path(pip[1][2]).read_text(encoding="utf-8")
    assert "SAM-2" not in first and "--extra-index-url" in first and "-e git+" in first
    assert pip[2] == ["install", "--no-build-isolation", "--no-deps",
                      "SAM-2 @ git+https://github.com/o/sam2@def"]
    assert fake.calls[-1][1] == "-c" and fake.calls[-1][-2:] == ["uvicorn", "torch"]
    env = fake.envs[0]
    assert "PYTHONPATH" not in env and "PIP_USER" not in env
    assert env["PIP_TIMEOUT"] == "120" and env["PIP_RETRIES"] == "10"          # a PC's own value wins
    assert env["PIP_NO_INPUT"] == "1" and env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["PIP_CACHE_DIR"] == str(sup.PIP_CACHE) and env["TEMP"] == str(sup.PIP_TMP)
    log = sup.INSTALL_LOG.read_text(encoding="utf-8")
    assert "pip logs to" in log and "second pass, built in the venv: SAM-2" in log
    assert "imports OK: uvicorn, torch" in log


def test_a_download_that_broke_off_is_tried_once_more(sup, tmp_path, monkeypatch):
    broke = piplog("ERROR: Could not install packages due to an OSError: HTTPSConnectionPool("
                   "host='files.pythonhosted.org', port=443): Read timed out.")
    fake = FakePip(results=[(0, ""), (1, broke), (0, ""), (0, "")])
    monkeypatch.setattr(sup, "subprocess", types.SimpleNamespace(run=fake, TimeoutExpired=TimeoutError))
    req = tmp_path / "r.txt"
    req.write_text("six==1.17.0\n", encoding="utf-8")
    assert sup.install_venv_packages(tmp_path / "python.exe", req, "t") is None
    assert sum("-r" in c for c in fake.calls) == 2
    log = sup.INSTALL_LOG.read_text(encoding="utf-8")
    assert "Trying once more" in log and "A download failed (ERROR: " in log and "Read timed out" in log


def test_the_retry_names_the_connection_that_failed(sup, tmp_path, monkeypatch):
    fake = FakePip(results=[(1, UNREACHABLE_INDEX + PIP_TRACE), (0, "")])
    monkeypatch.setattr(sup, "subprocess", types.SimpleNamespace(run=fake, TimeoutExpired=TimeoutError))
    assert sup.pip_install(tmp_path / "python.exe", ["six==1.17.0"], sup.LOG_DIR / "pip-t.log", "s") is None
    log = sup.INSTALL_LOG.read_text(encoding="utf-8")
    assert "A download failed (Could not fetch URL http://127.0.0.1:9/simple/six/" in log, log


def test_pips_log_keeps_what_happened_but_not_every_file_it_saw(sup, tmp_path, monkeypatch):
    seen = piplog("Fetched page https://pypi.org/simple/six/ as application/vnd.pypi.simple.v1+json",
                  "  Found link https://files.pythonhosted.org/six-1.17.0.tar.gz (from https://pypi.org/"
                  "simple/six/) (requires-python:>=2.7), version: 1.17.0",
                  "  Skipping link: none of the wheel's tags (cp27-none-any) are compatible "
                  "(run pip debug --verbose to show compatible tags): https://x/six-1.0-cp27-none-any.whl",
                  "Collecting six==1.17.0")
    fake = FakePip(results=[(0, seen), (1, seen + piplog("ERROR: No matching distribution found for x==1"))])
    monkeypatch.setattr(sup, "subprocess", types.SimpleNamespace(run=fake, TimeoutExpired=TimeoutError))
    req = tmp_path / "r.txt"
    req.write_text("six==1.17.0\nx==1\n", encoding="utf-8")
    sup.LOG_DIR.mkdir(parents=True)
    (sup.LOG_DIR / "pip-t.log").write_text(seen, encoding="utf-8")      # a run that was stopped
    fail = sup.install_venv_packages(tmp_path / "python.exe", req, "t")
    assert fail is not None and fail.cause == "notfound"
    log = (sup.LOG_DIR / "pip-t.log").read_text(encoding="utf-8")
    assert log.count("Fetched page") == 2 and log.count("Collecting six") == 2
    assert "Found link" not in log and "Skipping link" not in log
    assert "No matching distribution found for x==1" in log
    prev = (sup.LOG_DIR / "pip-t.prev.log").read_text(encoding="utf-8")
    assert "Collecting six" in prev and "Found link" not in prev
    assert not list(sup.LOG_DIR.glob("*.tmp"))


@pytest.mark.parametrize("log_text, cause, tries", [
    (piplog("ERROR: Could not install packages due to an OSError: Read timed out."), "network", 2),
    (piplog("fatal: unable to access 'https://github.com/x/': Could not resolve host: github.com",
            "ERROR: Failed to build 'x'"), "dns", 1),
    (piplog("ERROR: Could not install packages due to an OSError: [Errno 28] No space left on device"),
     "disk", 1),
])
def test_a_failure_that_stays_is_returned_with_its_cause(sup, tmp_path, monkeypatch, log_text, cause,
                                                         tries):
    fake = FakePip(results=[(0, "")] + [(1, log_text)] * 2)
    monkeypatch.setattr(sup, "subprocess", types.SimpleNamespace(run=fake, TimeoutExpired=TimeoutError))
    req = tmp_path / "r.txt"
    req.write_text("six==1.17.0\n", encoding="utf-8")
    fail = sup.install_venv_packages(tmp_path / "python.exe", req, "t", step="the step")
    assert fail is not None and fail.cause == cause and fail.step == "the step"
    assert sum("-r" in c for c in fake.calls) == tries
    assert fail.logs == [sup.INSTALL_LOG, sup.LOG_DIR / "pip-t.log"]
    assert not any(c[1] == "-c" for c in fake.calls)               # no import check after a failure


@pytest.mark.parametrize("stderr, cause", [
    ("Traceback (most recent call last):\nImportError: DLL load failed while importing cv2: "
     "The specified module could not be found.", "media"),
    ("Traceback (most recent call last):\nOSError: [WinError 126] The specified module could not be "
     "found. Error loading \"C:\\K\\torch\\lib\\c10.dll\" or one of its dependencies.", "dll"),
    ("Traceback (most recent call last):\nModuleNotFoundError: No module named 'uvicorn'", "incomplete"),
])
def test_a_venv_whose_packages_do_not_import_fails(sup, tmp_path, monkeypatch, stderr, cause):
    fake = FakePip(imports=1, import_err=stderr)
    monkeypatch.setattr(sup, "subprocess", types.SimpleNamespace(run=fake, TimeoutExpired=TimeoutError))
    req = tmp_path / "r.txt"
    req.write_text("six==1.17.0\n", encoding="utf-8")
    fail = sup.install_venv_packages(tmp_path / "python.exe", req, "t", imports=["cv2"])
    assert fail is not None and fail.cause == cause
    assert stderr.splitlines()[-1].strip() in fail.lines


def test_the_command_line_reports_a_failure_and_exits_1(sup, tmp_path, monkeypatch, capsys):
    fake = FakePip(results=[(0, ""), (1, piplog("ERROR: No matching distribution found for x==1"))])
    monkeypatch.setattr(sup, "subprocess", types.SimpleNamespace(run=fake, TimeoutExpired=TimeoutError))
    req = tmp_path / "r.txt"
    req.write_text("x==1\n", encoding="utf-8")
    assert sup.main(["venv", "tool-venv", str(req), "--step", "step 3 of 5"]) == 1
    assert "Step: step 3 of 5" in capsys.readouterr().out
    assert "notfound" not in sup.LAST_FAILURE.read_text(encoding="utf-8")
    assert "could not find a package version" in sup.LAST_FAILURE.read_text(encoding="utf-8")


def test_a_bug_in_the_helper_is_reported_too(sup, tmp_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise ValueError("unexpected")

    monkeypatch.setattr(sup, "install_venv_packages", boom)
    assert sup.main(["venv", "tool-venv", str(tmp_path / "r.txt")]) == 1
    assert "The installer's own helper failed" in capsys.readouterr().out
    assert "ValueError: unexpected" in sup.LAST_FAILURE.read_text(encoding="utf-8")


def test_a_venv_is_marked_unfinished_until_it_is_done_and_then_only_checked(sup, tmp_path,
                                                                           monkeypatch):
    """A second run of the installer must not need the network for a venv it
    finished before, nor leave that venv marked unfinished when it fails."""
    fake = FakePip()
    monkeypatch.setattr(sup, "subprocess", types.SimpleNamespace(run=fake, TimeoutExpired=TimeoutError))
    venv = tmp_path / "venv"
    venv.mkdir()
    req = requirements(tmp_path)
    py = venv / "Scripts" / "python.exe"
    assert sup.build_venv(py, req, "t", venv, imports=["torch"]) is None
    assert not (venv / sup.MARKER).exists()
    assert (venv / sup.DONE).read_text(encoding="utf-8").strip() == sup.list_stamp(req)
    assert sum(c[1:3] == ["-m", "pip"] for c in fake.calls) == 3

    fake.calls.clear()
    assert sup.build_venv(py, req, "t", venv, imports=["torch"]) is None
    assert [c[1] for c in fake.calls] == ["-c"]                     # imports only, no pip
    assert "is up to date" in sup.INSTALL_LOG.read_text(encoding="utf-8")

    req.write_text(req.read_text(encoding="utf-8") + "six==1.17.0\n", encoding="utf-8")
    fake.calls.clear()
    fake.results = [(0, ""), (1, piplog("ERROR: Could not install packages due to an OSError: "
                                        "[Errno 28] No space left on device"))]
    fail = sup.build_venv(py, req, "t", venv, imports=["torch"])
    assert fail is not None and fail.cause == "disk"
    assert (venv / sup.MARKER).exists() and not (venv / sup.DONE).exists()


def test_a_finished_venv_that_no_longer_imports_is_built_again_next_time(sup, tmp_path, monkeypatch):
    monkeypatch.setattr(sup, "subprocess", types.SimpleNamespace(
        run=FakePip(imports=1, import_err="ModuleNotFoundError: No module named 'torch'"),
        TimeoutExpired=TimeoutError))
    venv = tmp_path / "venv"
    venv.mkdir()
    req = requirements(tmp_path)
    (venv / sup.DONE).write_text(sup.list_stamp(req) + "\n", encoding="utf-8")
    fail = sup.build_venv(venv / "Scripts" / "python.exe", req, "t", venv, imports=["torch"])
    assert fail is not None and fail.cause == "incomplete"
    assert (venv / sup.MARKER).exists()


def test_the_command_line_marks_the_venv_it_is_given(sup, tmp_path, monkeypatch):
    fake = FakePip(results=[(0, ""), (1, piplog("ERROR: No matching distribution found for x==1"))])
    monkeypatch.setattr(sup, "subprocess", types.SimpleNamespace(run=fake, TimeoutExpired=TimeoutError))
    venv = tmp_path / "venv"
    venv.mkdir()
    req = tmp_path / "r.txt"
    req.write_text("x==1\n", encoding="utf-8")
    assert sup.main(["venv", "tool-venv", str(req), "--venv", str(venv)]) == 1
    assert (venv / sup.MARKER).exists()


@pytest.mark.skipif(sys.platform != "win32", reason="a file held open cannot be deleted on Windows")
def test_a_venv_removal_that_stops_half_way_leaves_it_marked(sup, tmp_path):
    venv = tmp_path / "venv"
    for f in ("Scripts/python.exe", "Lib/site-packages/torch/lib/c10.dll", "pyvenv.cfg"):
        (venv / f).parent.mkdir(parents=True, exist_ok=True)
        (venv / f).write_bytes(b"x")
    held = open(venv / "Lib" / "site-packages" / "torch" / "lib" / "c10.dll", "rb")
    try:
        with pytest.raises(OSError):
            sup.remove_venv(venv)
        assert (venv / sup.MARKER).exists()                 # whatever is left is not started
    finally:
        held.close()
    assert sup.main(["remove-venv", str(venv)]) == 0
    assert not venv.exists()
    assert sup.main(["remove-venv", str(venv)]) == 0          # nothing there: nothing to do


def test_a_mapped_network_drive_gets_the_long_unc_form(sup):
    assert sup.long_path("C:\\K\\tool\\.venv") == "\\\\?\\C:\\K\\tool\\.venv"
    assert sup.long_path("\\\\nas\\share\\K") == "\\\\?\\UNC\\nas\\share\\K"
    assert sup.long_path("\\\\?\\C:\\K") == "\\\\?\\C:\\K"


@pytest.mark.parametrize("own_env, git_has, passed", [
    ({}, "", True),
    ({}, "http://gits-own:3128", False),            # git's own setting stays
    ({"HTTPS_PROXY": "http://own:3128"}, "", False),  # so does the PC's variable
])
def test_git_gets_the_windows_proxy_that_pip_reads_itself(sup, monkeypatch, own_env, git_has, passed):
    """pip reads the Windows proxy with its bypass list itself; exporting it
    as HTTPS_PROXY would drop that list, so only git gets it."""
    monkeypatch.setattr(sup, "windows_proxies",
                        lambda: {"http": "http://me:secret@proxy:8080", "https": "http://me:secret@proxy:8080"})
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "GIT_CONFIG_COUNT"):
        monkeypatch.delenv(var, raising=False)
    for k, v in own_env.items():
        monkeypatch.setenv(k, v)
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0 if git_has else 1, stdout=git_has, stderr="")

    monkeypatch.setattr(sup, "subprocess", types.SimpleNamespace(run=fake_run, TimeoutExpired=TimeoutError))
    env = sup.install_env()
    assert ("GIT_CONFIG_VALUE_0" in env) == passed
    assert env.get("HTTPS_PROXY") == own_env.get("HTTPS_PROXY") and "HTTP_PROXY" not in env
    if passed:
        assert env["GIT_CONFIG_COUNT"] == "1" and env["GIT_CONFIG_KEY_0"] == "http.proxy"
        assert env["GIT_CONFIG_VALUE_0"] == "http://me:secret@proxy:8080"
        logged = sup.INSTALL_LOG.read_text(encoding="utf-8")
        assert "proxy:8080" in logged and "secret" not in logged      # no password in the log
    sup.install_env()
    assert len(calls) == (0 if own_env else 1)                          # git is asked once a run


def test_a_rerun_that_cannot_reach_a_server_leaves_a_working_venv_as_it_was(sup, tmp_path, monkeypatch):
    """A venv from 0.1.0 has no stamp, and an updated list has another one;
    pip downloads everything before it changes anything."""
    fake = FakePip(results=[(1, UNREACHABLE_INDEX)] * 2)
    monkeypatch.setattr(sup, "subprocess", types.SimpleNamespace(run=fake, TimeoutExpired=TimeoutError))
    venv = tmp_path / "venv"
    venv.mkdir()
    py = venv / "Scripts" / "python.exe"
    fail = sup.build_venv(py, requirements(tmp_path), "t", venv)
    assert fail is not None and fail.cause == "network"
    assert not (venv / sup.MARKER).exists() and not (venv / sup.DONE).exists()
    assert "works as before" in sup.INSTALL_LOG.read_text(encoding="utf-8")
    (venv / sup.MARKER).write_text("x", encoding="utf-8")              # one a run left unfinished
    fake.results = [(1, UNREACHABLE_INDEX)] * 2
    assert sup.build_venv(py, requirements(tmp_path), "t", venv).cause == "network"
    assert (venv / sup.MARKER).exists()


def test_finish_deletes_pips_downloads(sup):
    for d in (sup.PIP_CACHE / "http-v2" / "a", sup.PIP_TMP / "x"):
        d.mkdir(parents=True)
        (d / "f.bin").write_bytes(b"x")
    assert sup.main(["finish"]) == 0
    assert not sup.PIP_CACHE.exists() and not sup.PIP_TMP.exists()


# ---------------------------------------------------------------- the lists
def test_sam2_is_built_after_torch_and_nothing_else_is():
    lines = (REPO / "tool" / "requirements.txt").read_text(encoding="utf-8").splitlines()
    late = [x for x in lines if "[second-pass]" in x]
    assert len(late) == 1 and late[0].startswith("SAM-2 @ git+https://github.com/facebookresearch/sam2@")
    # what it needs to build, and its own dependencies, are pinned in the first pass
    for pin in ("torch==", "setuptools==", "numpy==", "tqdm==", "hydra-core==", "iopath==", "pillow=="):
        assert any(x.startswith(pin) for x in lines), pin


def test_the_pip_pin_is_the_one_of_the_frozen_base(sup):
    base = (REPO / "install" / "requirements-frozen" / "base-python.txt").read_text(encoding="utf-8")
    assert sup.PIP in base.splitlines()
