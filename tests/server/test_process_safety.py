"""Executable pinning, minimal environment, output caps and orphan cleanup."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path, PureWindowsPath

import pytest

from pc_diag_mcp.collectors import CollectorError, CollectorUnavailable
from pc_diag_mcp.collectors import powershell as ps
from pc_diag_mcp.collectors.powershell import (
    NVIDIA_SMI_MISSING,
    PS_DIR,
    find_windows_root,
    run_process,
)

from .conftest import POWERSHELL, WIN_ROOT, FakeRunner, live

# The native branch follows Windows path rules on every OS.
NVSMI = Path(
    str(
        PureWindowsPath(WIN_ROOT).parent.joinpath(
            "Program Files", "NVIDIA Corporation", "NVSMI", "nvidia-smi.exe"
        )
    )
)


def only(*allowed: str | Path) -> ps.Exists:
    names = {str(a) for a in allowed}
    return lambda path: str(path) in names


def test_powershell_never_comes_from_path(
    runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/evil/bin")
    live(runner, exists=only(POWERSHELL)).system_info()
    assert runner.calls[-1].argv[0] == POWERSHELL
    with pytest.raises(CollectorUnavailable, match="WindowsPowerShell"):
        live(runner, exists=only("/evil/bin/powershell.exe")).system_info()


@pytest.mark.parametrize(
    ("wsl", "present", "expected"),
    [
        (
            False,
            [WIN_ROOT / "System32" / "nvidia-smi.exe", NVSMI],
            WIN_ROOT / "System32" / "nvidia-smi.exe",
        ),
        (False, [NVSMI], NVSMI),
        (True, [Path("/usr/lib/wsl/lib/nvidia-smi")], Path("/usr/lib/wsl/lib/nvidia-smi")),
    ],
)
def test_nvidia_smi_candidates(
    runner: FakeRunner, wsl: bool, present: list[Path], expected: Path
) -> None:
    runner.responses[expected.stem] = runner.responses["nvidia-smi"]
    collector = live(runner, wsl=wsl, exists=only(POWERSHELL, *present))
    collector.sensors(None)
    smi = [c for c in runner.calls if "nvidia-smi" in c.argv[0]]
    assert [c.argv[0] for c in smi] == [str(expected)]


def test_native_nvsmi_is_on_the_windows_drive(
    runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    # %PROGRAMFILES% must not steer it (32-bit Python sees "Program Files (x86)").
    monkeypatch.setenv("PROGRAMFILES", "D:\\Elsewhere")
    root = Path("E:/Windows")
    powershell = str(root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
    expected = "E:\\Program Files\\NVIDIA Corporation\\NVSMI\\nvidia-smi.exe"
    runner.responses[Path(expected).stem] = runner.responses["nvidia-smi"]
    collector = live(runner, windows_root=root, exists=only(powershell, expected))
    collector.sensors(None)
    assert [c.argv[0] for c in runner.calls if "nvidia-smi" in c.argv[0]] == [expected]


def test_nvidia_smi_absent_is_unavailable(runner: FakeRunner) -> None:
    snapshot = live(runner, exists=only(POWERSHELL, "/usr/bin/nvidia-smi")).sensors(None)[0]
    assert NVIDIA_SMI_MISSING in snapshot.unavailable


@pytest.mark.skipif(sys.platform == "win32", reason="WSL/Linux detection")
def test_find_windows_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for drive in ("c", "d", "e"):
        (tmp_path / drive).mkdir()
    monkeypatch.setattr(ps, "running_under_wsl", lambda: True)

    def windows_on(*drives: str) -> ps.Exists:
        return lambda p: any(str(p).startswith(str(tmp_path / d / "Windows")) for d in drives)

    assert find_windows_root(windows_on("d", "e"), tmp_path) == tmp_path / "d" / "Windows"
    assert find_windows_root(windows_on("c", "d"), tmp_path) == tmp_path / "c" / "Windows"
    assert find_windows_root(windows_on(), tmp_path) is None
    monkeypatch.setattr(ps, "running_under_wsl", lambda: False)
    assert find_windows_root(windows_on("c"), tmp_path) is None


def test_minimal_environment_native(runner: FakeRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMP", "C:\\Temp")
    monkeypatch.setenv("SECRET_TOKEN", "x")
    live(runner).system_info()
    env = runner.calls[-1].env
    assert env is not None
    assert set(env) <= {"SystemRoot", "windir", "SystemDrive", "TEMP", "TMP", "PATH"}
    assert env["SystemRoot"] == str(WIN_ROOT)
    assert all(part.startswith(str(WIN_ROOT)) for part in env["PATH"].split(";"))


def test_minimal_environment_wsl(runner: FakeRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WSL_INTEROP", "/run/WSL/1_interop")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.setenv("WSLENV", "")
    monkeypatch.setenv("SECRET_TOKEN", "x")
    live(runner, wsl=True).system_info()
    assert len(runner.calls) == 2  # wslpath, then powershell.exe
    for call in runner.calls:
        assert call.env == {
            "PATH": "/usr/bin:/bin",
            "WSL_INTEROP": "/run/WSL/1_interop",
            "WSL_DISTRO_NAME": "Ubuntu",
            "WSLENV": "",
        }


def _python(code: str) -> list[str]:
    return [sys.executable, "-I", "-c", code]


def test_run_process_round_trip() -> None:
    proc = run_process(
        _python("import sys; d=sys.stdin.read(); print(d.upper()); print('e', file=sys.stderr)"),
        "hello",
        30,
    )
    assert proc.returncode == 0 and proc.stdout.strip() == "HELLO" and proc.stderr.strip() == "e"


def test_run_process_caps_stdout() -> None:
    code = "import sys\nwhile True: sys.stdout.write('x' * 65536)"
    with pytest.raises(CollectorError, match="output exceeded 1 MB"):
        run_process(_python(code), None, 30, max_stdout=1024 * 1024)


def test_run_process_timeout_kills_child() -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        run_process(_python("import time; time.sleep(30)"), None, 0.5)


def test_run_process_passes_env() -> None:
    proc = run_process(
        _python("import os; print(sorted(k for k in os.environ if k.startswith('PCDIAG')))"),
        None,
        30,
        {"PCDIAG_ONLY": "1"},
    )
    assert proc.stdout.strip() == "['PCDIAG_ONLY']"


def test_dxdiag_timeout_triggers_token_cleanup(runner: FakeRunner) -> None:
    runner.responses["dxdiag.ps1"] = subprocess.TimeoutExpired(["powershell.exe"], 120)
    runner.responses["dxdiag_cleanup.ps1"] = '{"ok": true, "data": {"stopped": 1}}'
    with pytest.raises(CollectorError, match="timed out"):
        live(runner).dxdiag()
    token = runner.stdin_for("dxdiag.ps1")["token"]
    assert re.fullmatch(r"[0-9a-f]{16}", token)
    assert runner.stdin_for("dxdiag_cleanup.ps1") == {"token": token}


def test_dxdiag_scripts_validate_token_and_cap_size() -> None:
    main = (PS_DIR / "dxdiag.ps1").read_text(encoding="utf-8")
    cleanup = (PS_DIR / "dxdiag_cleanup.ps1").read_text(encoding="utf-8")
    for text in (main, cleanup):
        assert "'^[0-9a-f]{16}$'" in text
    assert "$MaxBytes = 10MB" in main and "Stop-Process -Id $proc.Id" in main
    assert "('\"' + $path + '\"')" in main
    lhm = (PS_DIR / "lhm_http.ps1").read_text(encoding="utf-8")
    assert "$limit = 16MB" in lhm and "ReadToEnd()" not in lhm.split("GetResponse()")[1]


def test_invocation_documents_bypass_rationale() -> None:
    source = Path(ps.__file__).read_text(encoding="utf-8")
    assert "-ExecutionPolicy Bypass is process-scoped" in source
