"""The install scripts run for real, with every heavy step stubbed: INSTALL.bat,
start.bat, install/INSTALL-MAIN.bat, and the checks in install/bootstrap.ps1.

Each test builds a fake Kinoloom folder whose path has spaces, brackets and
letters outside ASCII, copies the real script into it and runs it with cmd.exe.
The steps it calls are stand-ins that print what they got and exit with the
code a test asks for. Nothing is downloaded, and the real launcher never runs."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows scripts")

REPO = Path(__file__).resolve().parents[2]
ODD = "Kino loom (1) \u00e9\u6d4b"         # a space, brackets, a Latin and a CJK letter
SIGNS = "Kino & 100% ^ !'s (2) \u00e9\u6d4b"  # what cmd reads as its own: & % ^ ! and a quote
MARKER = ".kinoloom-incomplete"

LAUNCHER = 'import sys\nprint("LAUNCHER RAN", sys.argv[1:])\n'
STUB_INSTALL = r"""@echo off
echo STUB INSTALL %*
if "%KL_INSTALL_FIXES%"=="1" del "%~dp0tool\.venv\.kinoloom-incomplete" 2>nul
exit /b %KL_INSTALL_RC%
"""
STUB_MAIN = r"""@echo off
echo STUB MAIN %*
if not defined KL_MAIN_REPORT goto :out
if not exist "%~dp0..\logs\install\" mkdir "%~dp0..\logs\install"
> "%~dp0..\logs\install\LAST-FAILURE.txt" echo %KL_MAIN_REPORT%
:out
exit /b %KL_MAIN_RC%
"""
STUB_BOOTSTRAP = r"""
Write-Host "STUB BOOTSTRAP"
if ($env:KL_BOOT_ASK) { Write-Host "BOOT GOT [$(Read-Host 'Install in this OneDrive folder? [y/N]')]" }
if ($env:KL_BOOT_REPORT) {
    $d = Join-Path (Split-Path -Parent $PSScriptRoot) 'logs\install'
    New-Item -ItemType Directory -Force $d | Out-Null
    Set-Content -Path (Join-Path $d 'LAST-FAILURE.txt') -Value $env:KL_BOOT_REPORT
}
exit [int]$env:KL_BOOT_RC
"""
STUB_SUPPORT = r'''import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

if __name__ == "__main__":
    if sys.argv[1] == "finish":
        (ROOT / "FINISHED").write_text("x")
        sys.exit(0)
    if sys.argv[1] == "remove-venv":
        print("STUB REMOVE", sys.argv[2])
        shutil.rmtree(sys.argv[2])
        sys.exit(0)
    print("STUB STEP3", sys.argv[1:], "RERUN=" + os.environ.get("KINOLOOM_RERUN", ""))
    (ROOT / "STEP3_ARGS.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
    mode = os.environ.get("KL_STEP3", "ok")
    if mode == "dead":              # stopped before it could do anything
        sys.exit(9)
    # as the helper does with --venv: unfinished until all went well
    marker = Path(sys.argv[sys.argv.index("--venv") + 1]) / ".kinoloom-incomplete"
    if mode == "ok":
        marker.unlink(missing_ok=True)
        sys.exit(0)
    marker.write_text("x")
    if mode == "fail":
        d = ROOT / "logs" / "install"
        d.mkdir(parents=True, exist_ok=True)
        (d / "LAST-FAILURE.txt").write_text("STUB REPORT: a download failed\n")
        print(" THE INSTALL STOPPED (the helper's own box)")
        sys.exit(1)
    sys.exit(3)
'''
STUB_COMPONENTS = r'''import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
print("STUB PARTS", sys.argv[1:])
if os.environ.get("KL_STEP5") == "fail":
    d = ROOT / "logs" / "install"
    d.mkdir(parents=True, exist_ok=True)
    (d / "LAST-FAILURE.txt").write_text("STUB PARTS REPORT\n")
    sys.exit(1)
if os.environ.get("KL_STEP5") == "crash":
    sys.exit(1)
'''
STUB_VERIFY = r'''import sys

print("STUB VERIFY")
if "--log" in sys.argv:
    with open(sys.argv[sys.argv.index("--log") + 1], "a", encoding="utf-8") as fh:
        fh.write("STUB VERIFY\n")
'''


@pytest.fixture(scope="module")
def template_venv(tmp_path_factory) -> Path:
    """A real venv (without pip) to copy into the fake trees."""
    v = tmp_path_factory.mktemp("venv") / "v"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(v)], check=True,
                   timeout=300)
    return v


def make_tree(tmp_path: Path, template: Path | None = None, *, scripts=("INSTALL.bat", "start.bat"),
              main=True, name: str = ODD) -> Path:
    root = tmp_path / name
    (root / "install").mkdir(parents=True)
    (root / "tool").mkdir()
    for s in scripts:
        shutil.copy2(REPO / s, root / s)
    if main:
        shutil.copy2(REPO / "install" / "INSTALL-MAIN.bat", root / "install" / "INSTALL-MAIN.bat")
    (root / "tool" / "launcher.py").write_text(LAUNCHER, encoding="utf-8")
    (root / "tool" / "requirements.txt").write_text("six==1.17.0\n", encoding="utf-8")
    (root / "tool" / "settings.example.yaml").write_text("ui: {}\n", encoding="utf-8")
    (root / "tool" / "lutgen.py").write_text('print("STUB LUT")\n', encoding="utf-8")
    (root / "install" / "bootstrap.ps1").write_text(STUB_BOOTSTRAP, encoding="ascii")
    (root / "install" / "install_support.py").write_text(STUB_SUPPORT, encoding="utf-8")
    (root / "install" / "install_components.py").write_text(STUB_COMPONENTS, encoding="utf-8")
    (root / "install" / "verify_install.py").write_text(STUB_VERIFY, encoding="utf-8")
    (root / "tools" / "git" / "cmd").mkdir(parents=True)
    (root / "tools" / "git" / "cmd" / "git.exe").write_bytes(b"")      # `where git.exe` finds it
    if template is not None:
        shutil.copytree(template, root / "tool" / ".venv")
    return root


def add_tools_python(root: Path, template: Path) -> None:
    """A relocated venv launcher stands in for tools\\python."""
    (root / "tools" / "python").mkdir(parents=True)
    shutil.copy2(template / "Scripts" / "python.exe", root / "tools" / "python" / "python.exe")
    shutil.copy2(template / "pyvenv.cfg", root / "tools" / "python" / "pyvenv.cfg")


def run_bat(bat: Path, *args: str, stdin: str = "", env: dict | None = None,
            cwd: Path | None = None, lines: bool = False) -> tuple[int, str]:
    """lines=True feeds stdin from a file: from a pipe, the first set /p can
    take the whole input, and the next prompt gets nothing."""
    # cmd /s strips only the outer quotes, so a path with brackets stays whole
    cmd = 'cmd /d /s /c ""{}"{}"'.format(bat, "".join(" " + a for a in args))
    e = {k: v for k, v in os.environ.items()
         if k.upper() not in ("KINOLOOM_RERUN", "PYTHONPATH", "PYTHONHOME") and not k.startswith("KL_")}
    e.update(env or {})
    if lines:
        feed = Path(tempfile.gettempdir()) / f"kl-stdin-{os.getpid()}.txt"
        feed.write_bytes(stdin.encode())
        try:
            with open(feed, "rb") as fh:
                r = subprocess.run(cmd, stdin=fh, capture_output=True, env=e, cwd=cwd or bat.parent,
                                   timeout=600)
        finally:
            feed.unlink(missing_ok=True)
    else:
        r = subprocess.run(cmd, input=stdin.encode(), capture_output=True, env=e, cwd=cwd or bat.parent,
                           timeout=600)
    return r.returncode, (r.stdout + r.stderr).decode("oem", "replace")


NAMES = pytest.mark.parametrize("name", [ODD, SIGNS], ids=["odd", "signs"])


# ---------------------------------------------------------------- the ZIP
@pytest.mark.parametrize("script", ["INSTALL.bat", "start.bat"])
def test_run_from_inside_the_zip_says_to_extract_it(tmp_path, script):
    """Explorer runs a .bat from a ZIP by unpacking only that file."""
    alone = tmp_path / "Temp1_Kinoloom-main.zip" / "Kinoloom-main"
    alone.mkdir(parents=True)
    shutil.copy2(REPO / script, alone / script)
    rc, out = run_bat(alone / script)
    assert rc == 1 and "Extract All" in out and "C:\\Kinoloom" in out, out
    assert "KINOLOOM - INSTALL" not in out and "LAUNCHER" not in out


# ---------------------------------------------------------------- a network folder
def unc(path: Path) -> Path | None:
    """The same folder through the administrative share, when this PC has it."""
    share = Path("\\\\localhost\\{}$".format(path.drive[0])) / str(path)[3:]
    try:
        return share if share.exists() else None
    except OSError:
        return None


@pytest.mark.parametrize("script", ["INSTALL.bat", "start.bat"])
def test_a_network_folder_stops_before_anything_runs(tmp_path, template_venv, script):
    """cmd can't make \\\\server\\share its current folder. Run from another
    Kinoloom folder, the scripts found relative to the current one used to be
    that folder's: its install ran instead."""
    root = make_tree(tmp_path / "share", template_venv)
    other = make_tree(tmp_path / "other", template_venv, main=False)
    (other / "INSTALL.bat").write_text(STUB_INSTALL, encoding="ascii")
    (other / "install" / "INSTALL-MAIN.bat").write_text(STUB_MAIN, encoding="ascii")
    shared = unc(root / script)
    if shared is None:
        pytest.skip("no administrative share on this PC")
    rc, out = run_bat(shared, stdin="\r\n" * 4, cwd=other,
                      env={"KL_MAIN_RC": "0", "KL_INSTALL_RC": "0"})
    assert rc == 1 and "network folder" in out and "C:\\Kinoloom" in out, out
    assert "STUB" not in out and "LAUNCHER" not in out and "KINOLOOM - INSTALL" not in out, out


# ---------------------------------------------------------------- start.bat
@NAMES
def test_start_runs_the_launcher_when_the_install_is_complete(tmp_path, template_venv, name):
    root = make_tree(tmp_path, template_venv, name=name)
    _, out = run_bat(root / "start.bat", "--no-browser")
    assert "LAUNCHER RAN ['--no-browser']" in out, out


def test_start_offers_the_install_when_nothing_is_installed(tmp_path):
    root = make_tree(tmp_path)
    rc, out = run_bat(root / "start.bat", stdin="n\r\n")
    assert rc == 1 and "not installed on this PC yet" in out and "LAUNCHER" not in out, out


def test_start_shows_why_the_last_install_stopped(tmp_path, template_venv):
    root = make_tree(tmp_path, template_venv)
    (root / "tool" / ".venv" / MARKER).write_text("", encoding="utf-8")
    (root / "logs" / "install").mkdir(parents=True)
    (root / "logs" / "install" / "LAST-FAILURE.txt").write_text("STUB REPORT: a download failed\n")
    rc, out = run_bat(root / "start.bat", stdin="n\r\n", lines=True)       # a file, as chcp reads one
    assert rc == 1 and "did not finish" in out and "STUB REPORT: a download failed" in out, out
    assert "LAUNCHER" not in out and "KINOLOOM - INSTALL" not in out, out   # the n was read


def test_bootstraps_questions_get_the_answers_given(tmp_path, template_venv):
    """chcp reads the input it is given: run without an empty one, it took the
    answers meant for bootstrap.ps1's questions."""
    root = make_tree(tmp_path, template_venv)
    rc, out = run_bat(root / "install" / "INSTALL-MAIN.bat", "/auto", stdin="y\r\n",
                      env={"KL_BOOT_ASK": "1"}, lines=True)          # chcp reads a file, not a pipe
    assert rc == 0 and "BOOT GOT [y]" in out, out


def test_start_does_not_launch_an_unfinished_install(tmp_path, template_venv):
    """The case from the first public release: pip stopped in step 3, start.bat
    saw tool\\.venv\\Scripts\\python.exe and started a web app without uvicorn."""
    root = make_tree(tmp_path, template_venv)
    (root / "tool" / ".venv" / MARKER).write_text("", encoding="utf-8")
    rc, out = run_bat(root / "start.bat", stdin="n\r\n")
    assert rc == 1 and "the last run stopped before" in out and "LAUNCHER" not in out, out


@pytest.mark.parametrize("install_rc, fixes, launches", [
    ("0", "1", True),
    ("2", "1", True),           # some render engines failed: the web app still starts
    ("1", "0", False),
    ("0", "0", False),          # said OK but left the marker: trust the files
    ("-1", "1", False),
])
@NAMES
def test_start_runs_install_and_launches_only_a_finished_install(tmp_path, template_venv, name,
                                                                 install_rc, fixes, launches):
    root = make_tree(tmp_path, template_venv, name=name)
    (root / "INSTALL.bat").write_text(STUB_INSTALL, encoding="ascii")
    (root / "tool" / ".venv" / MARKER).write_text("", encoding="utf-8")
    rc, out = run_bat(root / "start.bat", stdin="y\r\n",
                      env={"KL_INSTALL_RC": install_rc, "KL_INSTALL_FIXES": fixes})
    assert "STUB INSTALL /fromstart" in out, out
    assert ("LAUNCHER RAN" in out) == launches, out
    if not launches:
        assert rc == 1


# ---------------------------------------------------------------- INSTALL.bat
@pytest.mark.parametrize("main_rc, rc, shows", [
    ("0", 0, "Kinoloom is installed"),
    ("2", 2, "Some parts did not install"),
    ("1", 1, "The install stopped"),
    ("7", 1, "The install stopped"),
    ("-1", 1, "The install stopped"),
])
@NAMES
def test_install_reports_what_install_main_returned(tmp_path, name, main_rc, rc, shows):
    root = make_tree(tmp_path, main=False, name=name)
    (root / "install" / "INSTALL-MAIN.bat").write_text(STUB_MAIN, encoding="ascii")
    report = "STUB PARTS FAILED" if main_rc == "2" else ""
    got, out = run_bat(root / "INSTALL.bat", "/fromstart",
                       env={"KL_MAIN_RC": main_rc, **({"KL_MAIN_REPORT": report} if report else {})})
    assert got == rc and shows in out and "STUB MAIN /auto" in out, out
    if report:      # the report that scrolled away is shown again
        assert report in out
    if rc == 1:
        assert "logs\\install\\LAST-FAILURE.txt" in out


@pytest.mark.parametrize("answer, launches", [("", True), ("n", False)])
@NAMES
def test_install_starts_kinoloom_in_its_own_window_when_asked(tmp_path, template_venv, name,
                                                              answer, launches):
    """Run on its own (not from start.bat), INSTALL.bat offers to start
    Kinoloom and runs start.bat in the same window."""
    root = make_tree(tmp_path, template_venv, main=False, name=name)
    (root / "install" / "INSTALL-MAIN.bat").write_text(STUB_MAIN, encoding="ascii")
    # from a pipe, pause takes one character: the x
    rc, out = run_bat(root / "INSTALL.bat", stdin=f"x{answer}\r\n\r\n", env={"KL_MAIN_RC": "0"})
    assert rc == 0 and "Kinoloom is installed" in out, out
    assert ("LAUNCHER RAN" in out) == launches, out


@pytest.mark.parametrize("answer", ['"', 'n" & echo BROKE', "é"])
def test_whatever_is_typed_at_a_prompt_does_not_break_the_script(tmp_path, template_venv, answer):
    root = make_tree(tmp_path, template_venv, main=False)
    (root / "install" / "INSTALL-MAIN.bat").write_text(STUB_MAIN, encoding="ascii")
    rc, out = run_bat(root / "INSTALL.bat", stdin=f"x{answer}\r\n\r\n", env={"KL_MAIN_RC": "0"})
    assert rc == 0 and "LAUNCHER RAN" in out and "BROKE" not in out.split("[Y/n]")[-1], out
    (root / "tool" / ".venv" / MARKER).write_text("", encoding="utf-8")
    rc, out = run_bat(root / "start.bat", stdin=f"{answer}\r\n", env={"KL_MAIN_RC": "1"})
    assert rc == 1 and "STUB MAIN /auto" in out and "BROKE" not in out.split("[Y/n]")[-1], out


# ---------------------------------------------------------------- INSTALL-MAIN.bat
def install_main(root: Path, **env: str) -> tuple[int, str]:
    return run_bat(root / "install" / "INSTALL-MAIN.bat", "/auto",
                   env={k: v for k, v in env.items()})


@NAMES
def test_a_whole_run_logs_every_step_and_cleans_up(tmp_path, template_venv, name):
    root = make_tree(tmp_path, template_venv, name=name)
    logs = root / "logs" / "install"
    logs.mkdir(parents=True)
    (logs / "LAST-FAILURE.txt").write_text("from an older run")
    rc, out = install_main(root)
    assert rc == 0, out
    for n in range(1, 6):
        assert f"[{n}/5]" in out
    assert "STUB BOOTSTRAP" in out and "STUB STEP3" in out and "STUB PARTS" in out
    assert "RERUN=run INSTALL.bat again" in out                  # the helpers word it the same
    log = (logs / "install.log").read_text(encoding="utf-8", errors="replace")
    assert "==== Kinoloom install" in log and "mode auto" in log
    assert "[3/5]" in log and "[verify]" in log and ": OK ====" in log
    assert "STUB VERIFY" in log and "tool\\settings.yaml is there" in log  # verify's own lines too
    assert not (logs / "LAST-FAILURE.txt").exists()                # a new run starts without one
    assert not (root / "tool" / ".venv" / MARKER).exists()
    assert (root / "FINISHED").exists()                           # pip's downloads deleted
    assert (root / "tool" / "settings.yaml").is_file()
    got = json.loads((root / "STEP3_ARGS.json").read_text(encoding="utf-8"))
    assert got[got.index("--venv") + 1] == str(root / "tool" / ".venv")   # the path arrives whole
    assert got[got.index("venv") + 2] == str(root / "tool" / "requirements.txt")


def test_a_folder_pasted_in_quotes_is_used(tmp_path, template_venv):
    """Explorer's "Copy as path" puts the path in quotes. Mode [3] on a folder
    with & % ^ and ! in its name (ASCII: a pipe does not carry the console's
    code page)."""
    root = make_tree(tmp_path, template_venv, name="Kino & 100% ^ !'s (2)")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    # run from a copy elsewhere, so only the pasted path can lead to this tree
    shutil.copy2(root / "install" / "INSTALL-MAIN.bat", elsewhere / "INSTALL-MAIN.bat")
    rc, out = run_bat(elsewhere / "INSTALL-MAIN.bat", stdin=f'3\r\n"{root}"\r\n\r\n', lines=True)
    assert rc == 0 and "STUB PARTS" in out and "DONE" in out, out
    assert "STUB VERIFY" in (root / "logs" / "install" / "install.log").read_text(errors="replace")


@pytest.fixture
def console_code_page():
    """The window's code page, put back after the test (chcp sets the one of the
    console this test runs in)."""
    import ctypes
    k = ctypes.windll.kernel32
    out_cp, in_cp = k.GetConsoleOutputCP(), k.GetConsoleCP()
    if not out_cp:
        pytest.skip("no console")
    yield
    k.SetConsoleOutputCP(out_cp)
    k.SetConsoleCP(in_cp)


@pytest.mark.usefixtures("console_code_page")
def test_the_log_is_utf8_in_a_window_with_a_western_code_page(tmp_path, template_venv):
    """cmd writes a file in the window's code page. In a Western one (437) the é
    of the folder's name became a byte no UTF-8 reader takes, and its Chinese
    letter a ?, in the report lines INSTALL-MAIN.bat writes itself."""
    root = make_tree(tmp_path, template_venv)
    bat = root / "install" / "INSTALL-MAIN.bat"
    e = {k: v for k, v in os.environ.items()
         if k.upper() not in ("KINOLOOM_RERUN", "PYTHONPATH", "PYTHONHOME") and not k.startswith("KL_")}
    e["KL_STEP3"] = "crash"
    r = subprocess.run(f'cmd /d /s /c "chcp 437 >nul & "{bat}" /auto & chcp"', capture_output=True,
                       env=e, cwd=root, timeout=600)
    out = r.stdout.decode("oem", "replace")
    assert "THE INSTALL STOPPED" in out and out.rstrip().endswith("437"), out   # code page back
    logs = root / "logs" / "install"
    log = (logs / "install.log").read_bytes().decode("utf-8")                   # strict
    assert "==== FAILED" in log and "exit code 3" in log
    last = (logs / "LAST-FAILURE.txt").read_bytes().decode("utf-8")
    assert f"Full log:  {logs / 'install.log'}" in last, last


@pytest.mark.usefixtures("console_code_page")
def test_start_gives_the_window_its_code_page_back(tmp_path, template_venv):
    root = make_tree(tmp_path, template_venv)
    (root / "tool" / ".venv" / MARKER).write_text("", encoding="utf-8")
    (root / "logs" / "install").mkdir(parents=True)
    (root / "logs" / "install" / "LAST-FAILURE.txt").write_text("STUB REPORT é测\n", encoding="utf-8")
    r = subprocess.run(f'cmd /d /s /c "chcp 437 >nul & "{root / "start.bat"}" & chcp"',
                       input=b"n\r\n", capture_output=True, cwd=root, timeout=600)
    assert "STUB REPORT é测".encode() in r.stdout, r.stdout           # the bytes as written
    assert r.stdout.decode("oem", "replace").rstrip().endswith("437")


def test_a_failure_the_helper_reported_is_not_reported_twice(tmp_path, template_venv):
    root = make_tree(tmp_path, template_venv)
    rc, out = install_main(root, KL_STEP3="fail")
    assert rc == 1, out
    assert out.count("THE INSTALL STOPPED") == 1, out
    last = (root / "logs" / "install" / "LAST-FAILURE.txt").read_text()
    assert last.startswith("STUB REPORT")                         # kept as the helper wrote it
    assert (root / "tool" / ".venv" / MARKER).exists()            # the next run builds it again
    assert not (root / "FINISHED").exists()                       # pip's downloads are kept
    assert ": stopped ====" in (root / "logs" / "install" / "install.log").read_text(errors="replace")


@pytest.mark.parametrize("env, step", [
    ({"KL_STEP3": "crash"}, "step 3 of 5, the web app's packages, tool\\requirements.txt stopped "
                           "with exit code 3"),
    ({"KL_BOOT_RC": "1"}, "install\\bootstrap.ps1 stopped before it could report why (exit code 1)"),
])
@NAMES
def test_a_step_that_stops_without_a_report_gets_one(tmp_path, template_venv, name, env, step):
    root = make_tree(tmp_path, template_venv, name=name)
    rc, out = install_main(root, **env)
    assert rc == 1 and "THE INSTALL STOPPED" in out, out
    assert step in out
    last = (root / "logs" / "install" / "LAST-FAILURE.txt").read_text(errors="replace")
    assert step in last and "run INSTALL.bat again" in last


def test_a_bootstrap_powershell_did_not_finish_logs_powershells_settings(tmp_path, template_venv):
    """A PC's policy can stop PowerShell scripts before bootstrap.ps1 runs a
    line; install.log then holds the policy and the language mode."""
    root = make_tree(tmp_path, template_venv)
    rc, out = install_main(root, KL_BOOT_RC="1")
    assert rc == 1 and "policy of this PC blocks PowerShell scripts" in out, out
    log = (root / "logs" / "install" / "install.log").read_text(errors="replace")
    assert "policy MachinePolicy: " in log and "language mode: " in log, log
    assert "STUB STEP3" not in out


def test_bootstraps_own_report_is_kept(tmp_path, template_venv):
    root = make_tree(tmp_path, template_venv)
    rc, out = install_main(root, KL_BOOT_RC="1", KL_BOOT_REPORT="BOOTSTRAP REPORT")
    assert rc == 1 and "THE INSTALL STOPPED" not in out, out
    assert (root / "logs" / "install" / "LAST-FAILURE.txt").read_text().startswith("BOOTSTRAP REPORT")
    assert "STUB STEP3" not in out


def test_parts_that_failed_end_in_exit_code_2_and_keep_the_downloads(tmp_path, template_venv):
    root = make_tree(tmp_path, template_venv)
    rc, out = install_main(root, KL_STEP5="fail")
    assert rc == 2, out
    assert (root / "logs" / "install" / "LAST-FAILURE.txt").read_text().startswith("STUB PARTS REPORT")
    assert not (root / "FINISHED").exists()
    assert "some parts did not install" in (root / "logs" / "install" / "install.log").read_text(
        errors="replace")


@NAMES
def test_a_venv_pip_did_not_finish_is_removed_and_made_again(tmp_path, template_venv, name):
    """Step 2 removes tool\\.venv with install_support.py remove-venv (run by
    tools\\python) and creates it again before step 3."""
    root = make_tree(tmp_path, template_venv, name=name)
    (root / "tool" / ".venv" / MARKER).write_text("", encoding="utf-8")
    (root / "tool" / ".venv" / "left-over.txt").write_text("x")
    add_tools_python(root, template_venv)
    rc, out = install_main(root)
    assert rc == 0, out
    assert "was not finished last time; building it again" in out and "STUB REMOVE" in out
    assert not (root / "tool" / ".venv" / "left-over.txt").exists()
    assert (root / "tool" / ".venv" / "Scripts" / "python.exe").is_file()
    assert not (root / "tool" / ".venv" / MARKER).exists()
    assert "--venv" in out                                          # the helper keeps the marker


def test_a_venv_folder_left_without_its_python_is_made_again(tmp_path, template_venv):
    """A removal that stopped half-way took Scripts\\python.exe but not the rest."""
    root = make_tree(tmp_path, template_venv)
    (root / "tool" / ".venv" / "Scripts" / "python.exe").unlink()
    add_tools_python(root, template_venv)
    rc, out = install_main(root)
    assert rc == 0 and "building it again" in out, out
    assert (root / "tool" / ".venv" / "Scripts" / "python.exe").is_file()


def move_venv_base(venv: Path) -> None:
    """What moving the Kinoloom folder does to a venv: its pyvenv.cfg names a
    python that is no longer there, and its python.exe stops at once."""
    cfg = venv / "pyvenv.cfg"
    lines = cfg.read_text(encoding="utf-8").splitlines()
    cfg.write_text("\n".join("home = C:\\moved-away\\tools\\python" if x.startswith("home") else x
                             for x in lines) + "\n", encoding="utf-8")


def test_a_venv_that_no_longer_starts_is_made_again(tmp_path, template_venv):
    root = make_tree(tmp_path, template_venv)
    move_venv_base(root / "tool" / ".venv")
    add_tools_python(root, template_venv)
    rc, out = run_bat(root / "start.bat", stdin="n\r\n")
    assert rc == 1 and "no longer starts" in out and "LAUNCHER" not in out, out
    rc, out = install_main(root)
    assert rc == 0 and "no longer starts" in out and "STUB REMOVE" in out, out
    assert "no longer starts" in (root / "logs" / "install" / "install.log").read_text(errors="replace")
    _, out = run_bat(root / "start.bat", "--no-browser")
    assert "LAUNCHER RAN" in out, out


def test_a_new_venv_is_marked_unfinished_before_it_is_made(tmp_path, template_venv):
    """Stopped before step 3 could do anything, the new venv still counts as
    unfinished, so start.bat does not start it."""
    root = make_tree(tmp_path)
    add_tools_python(root, template_venv)
    rc, out = install_main(root, KL_STEP3="dead")
    assert rc == 1, out
    assert (root / "tool" / ".venv" / "Scripts" / "python.exe").is_file()
    assert (root / "tool" / ".venv" / MARKER).exists()
    rc, out = run_bat(root / "start.bat", stdin="n\r\n")
    assert rc == 1 and "LAUNCHER" not in out, out


@pytest.mark.parametrize("step5", ["fail", "crash"])
def test_mode_3_with_a_part_that_did_not_install_does_not_say_done(tmp_path, template_venv, step5):
    """The weights are added with mode [3]: a download that failed there was
    followed by "DONE. Start the studio" and exit code 0."""
    root = make_tree(tmp_path, template_venv)
    rc, out = run_bat(root / "install" / "INSTALL-MAIN.bat", stdin=f"3\r\n{root}\r\n\r\n",
                      env={"KL_STEP5": step5}, lines=True)
    assert rc == 2, out
    assert "NOT FINISHED: some parts did not install" in out and "DONE" not in out, out
    assert "run this installer again with mode [3]" in out
    log = (root / "logs" / "install" / "install.log").read_text(encoding="utf-8", errors="replace")
    assert "some parts did not install ====" in log and ": OK ====" not in log
    assert (root / "logs" / "install" / "LAST-FAILURE.txt").exists()


def test_parts_that_failed_without_a_report_get_one(tmp_path, template_venv):
    root = make_tree(tmp_path, template_venv)
    rc, out = install_main(root, KL_STEP5="crash")
    assert rc == 2, out
    last = (root / "logs" / "install" / "LAST-FAILURE.txt").read_text(errors="replace")
    assert "SOME PARTS DID NOT INSTALL" in last and "step 5 of 5" in last and "exit code 1" in last


# ---------------------------------------------------------------- bootstrap.ps1
PS_HARNESS = r"""
param([string]$Script, [string]$Root, [string]$Answer, [string]$Scenario, [string]$OneDrive,
      [string]$Need)
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Script, [ref]$null, [ref]$null)
foreach ($name in 'Write-Log', 'Format-Wrapped', 'Stop-Bootstrap', 'Get-OneDriveRoot',
                  'Assert-NotInOneDrive', 'Assert-Room', 'Assert-PlainPath', 'Invoke-Native',
                  'Expand-Pinned', 'Move-Retry') {
    $fn = $ast.FindAll({ param($a) $a -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                         $a.Name -eq $name }, $true)[0].Extent.Text
    . ([scriptblock]::Create($fn))
}
$LogDir = Join-Path $Root 'logs\install'
$InstallLog = Join-Path $LogDir 'install.log'
$LastFailure = Join-Path $LogDir 'LAST-FAILURE.txt'
$StepName = 'step 1 of 5, Python 3.12, Git and the Visual C++ runtime'
$Rerun = 'run INSTALL.bat again'
$RerunCap = 'Run INSTALL.bat again'
$Work = Join-Path $Root 'temp\bootstrap'
$NeedFreshGB = [double]$Need
$CacheGB = 6
$KeepGB = 18
$Utf8 = New-Object System.Text.UTF8Encoding $false
$env:OneDrive = $OneDrive
$env:OneDriveConsumer = $null
$env:OneDriveCommercial = $null
$env:PIP_CACHE_DIR = $null
function Say([string]$t) { Write-Host $t }
function Confirm-Step([string]$q, [bool]$d) { Write-Host "ASKED $q $d"; return $Answer -eq 'y' }
switch ($Scenario) {
    'onedrive' { Assert-NotInOneDrive }
    'plain' { Assert-PlainPath }
    'expand' { Expand-Pinned ([pscustomobject]@{ subdir = 'python' }) (Join-Path $Work 'python.tar.gz') (Join-Path $Root 'tools\python') }
    default { Assert-Room }
}
Write-Host 'CONTINUES'
"""


def run_ps(tmp_path: Path, root: Path, scenario: str, answer="n", onedrive="", need="0"):
    harness = tmp_path / "harness.ps1"
    harness.write_text(PS_HARNESS, encoding="ascii")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-File", str(harness), "-Script", str(REPO / "install" / "bootstrap.ps1"),
                        "-Root", str(root), "-Answer", answer, "-Scenario", scenario,
                        "-OneDrive", onedrive, "-Need", need],
                       capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout + r.stderr


@pytest.mark.parametrize("answer, code", [("n", 1), ("y", 0)])
def test_a_folder_in_onedrive_asks_first_and_stops_calmly(tmp_path, answer, code):
    od = tmp_path / "OneDrive"
    root = od / "Desktop" / "Kinoloom-main"
    root.mkdir(parents=True)
    rc, out = run_ps(tmp_path, root, "onedrive", answer, onedrive=str(od))
    assert rc == code, out
    assert "ASKED Install in this OneDrive folder? False" in out          # the safe default: no
    assert ("CONTINUES" in out) == (code == 0)
    if code:
        last = " ".join((root / "logs" / "install" / "LAST-FAILURE.txt").read_text(encoding="utf-8").split())
        assert "out of OneDrive" in last and "C:\\Kinoloom" in last


def test_a_folder_beside_onedrive_is_not_in_it(tmp_path):
    od = tmp_path / "OneDrive"
    root = tmp_path / "OneDrive-backup" / "Kinoloom"
    root.mkdir(parents=True)
    rc, out = run_ps(tmp_path, root, "onedrive", onedrive=str(od))
    assert rc == 0 and "ASKED" not in out and "CONTINUES" in out, out


def test_a_first_install_without_the_room_it_needs_stops_before_any_download(tmp_path):
    root = tmp_path / "K"
    root.mkdir()
    rc, out = run_ps(tmp_path, root, "room", need="1000000")
    assert rc == 1 and "not enough free disk space" in " ".join(out.split()), out
    logged = (root / "logs" / "install" / "install.log").read_text(encoding="utf-8")
    assert "==== FAILED" in logged and "not enough free disk space" in " ".join(logged.split())
    assert (root / "logs" / "install" / "LAST-FAILURE.txt").exists()
    rc, out = run_ps(tmp_path, root, "room", need="0")
    assert rc == 0 and "CONTINUES" in out, out


def test_python_unpacks_into_a_folder_whose_name_is_outside_the_code_page(tmp_path):
    """tar.exe reads its arguments in the ANSI code page: a CJK letter in the
    folder's path did not open on a Western PC."""
    import tarfile
    root = tmp_path / ODD
    work = root / "temp" / "bootstrap"
    work.mkdir(parents=True)
    exe = tmp_path / "python.exe"
    exe.write_bytes(b"MZ stand-in")
    with tarfile.open(work / "python.tar.gz", "w:gz") as t:
        t.add(exe, arcname="python/python.exe")
    rc, out = run_ps(tmp_path, root, "expand")
    assert rc == 0 and "CONTINUES" in out, out
    assert (root / "tools" / "python" / "python.exe").read_bytes() == b"MZ stand-in"
    assert not (work / "python.tar.gz").exists() and not (work / "x-python").exists()


@pytest.mark.parametrize("name, stops", [("Kino [1]", True), ("Kino (1)", False), (SIGNS, False)])
def test_a_folder_with_square_brackets_stops_before_anything_is_installed(tmp_path, name, stops):
    root = tmp_path / name
    root.mkdir()
    rc, out = run_ps(tmp_path, root, "plain")
    assert (rc == 1) == stops and ("CONTINUES" in out) != stops, out
    if stops:
        last = " ".join((root / "logs" / "install" / "LAST-FAILURE.txt").read_text(encoding="utf-8").split())
        assert "[ or ]" in last and "C:\\Kinoloom" in last


def test_bootstrap_itself_stops_calmly_in_a_folder_with_square_brackets(tmp_path):
    """The whole script, not just the check: everything before it must cope
    with the brackets too."""
    root = tmp_path / "Kino [1]"
    (root / "install").mkdir(parents=True)
    for f in ("bootstrap.ps1", "components.json"):
        shutil.copy2(REPO / "install" / f, root / "install" / f)
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-File", str(root / "install" / "bootstrap.ps1")],
                       capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    out = r.stdout + r.stderr
    assert r.returncode == 1 and "unexpected" not in out, out
    last = " ".join((root / "logs" / "install" / "LAST-FAILURE.txt").read_text(encoding="utf-8").split())
    assert "[ or ]" in last and "Nothing was installed" in last
    assert not (root / "tools").exists() and not (root / "temp").exists()


def test_a_damaged_components_json_ends_in_the_report(tmp_path):
    """Read before the functions existed, a broken list ended in a bare
    PowerShell error with nothing saved."""
    root = tmp_path / ODD
    (root / "install").mkdir(parents=True)
    shutil.copy2(REPO / "install" / "bootstrap.ps1", root / "install" / "bootstrap.ps1")
    (root / "install" / "components.json").write_text('{"tools": {', encoding="utf-8")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-File", str(root / "install" / "bootstrap.ps1")],
                       capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    out = r.stdout + r.stderr
    assert r.returncode == 1 and "THE INSTALL STOPPED" in out, out
    last = " ".join((root / "logs" / "install" / "LAST-FAILURE.txt").read_text(encoding="utf-8").split())
    assert "Something unexpected went wrong" in last and "install.log" in last
    assert not (root / "tools").exists() and not (root / "temp").exists()


def test_a_later_run_is_not_held_to_the_first_installs_space(tmp_path):
    """pip's cache and the venvs of an earlier run already use part of it."""
    root = tmp_path / "K"
    (root / "tool" / ".venv").mkdir(parents=True)
    rc, out = run_ps(tmp_path, root, "room", need="1000000")
    assert rc == 0 and "CONTINUES" in out, out
