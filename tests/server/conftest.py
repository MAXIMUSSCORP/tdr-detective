from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from pc_diag_mcp.collectors.powershell import PowerShellCollector

DATA = Path(__file__).parent / "data"


def sample(name: str) -> Any:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def envelope(data: Any) -> str:
    return json.dumps({"ok": True, "data": data})


@dataclass
class Call:
    argv: list[str]
    stdin: str | None
    timeout: float
    env: Mapping[str, str] | None = None


Response = str | subprocess.CompletedProcess[str] | BaseException | Callable[[Call], str]


@dataclass
class FakeRunner:
    """Stands in for subprocess.run; keyed by script file name or executable name."""

    responses: dict[str, Response] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)

    def __call__(
        self,
        argv: Sequence[str],
        stdin: str | None,
        timeout: float,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        call = Call(list(argv), stdin, timeout, env)
        self.calls.append(call)
        key = Path(argv[-1]).name if argv[0].endswith("powershell.exe") else Path(argv[0]).stem
        if Path(argv[0]).name == "wslpath":
            return subprocess.CompletedProcess(argv, 0, "\\\\wsl.localhost\\X" + argv[-1], "")
        response = self.responses[key]
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, subprocess.CompletedProcess):
            return response
        stdout = response(call) if callable(response) else response
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    def stdin_for(self, script: str) -> Any:
        for call in self.calls:
            if Path(call.argv[-1]).name == script:
                return json.loads(call.stdin or "null")
        raise AssertionError(f"{script} was not run")


WIN_ROOT = Path("/win/Windows")
POWERSHELL = str(WIN_ROOT / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def exists_all(path: Path) -> bool:
    return True


def live(fake: FakeRunner, **kwargs: Any) -> PowerShellCollector:
    """A collector wired to the fake runner, with every pinned executable "present"."""
    options: dict[str, Any] = {
        "runner": fake,
        "http_get": no_lhm,
        "wsl": False,
        "windows_root": WIN_ROOT,
        "exists": exists_all,
    }
    options.update(kwargs)
    return PowerShellCollector(**options)


def no_lhm(url: str, timeout: float) -> bytes:
    raise ConnectionRefusedError("refused")


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner(
        responses={
            "get_events.ps1": envelope(sample("events_raw.json")),
            "process_snapshot.ps1": envelope(sample("process_snapshot_raw.json")),
            "sensors_cim.ps1": envelope(sample("sensors_cim_raw.json")),
            "drivers.ps1": envelope(sample("drivers_raw.json")),
            "system_info.ps1": envelope(sample("system_info_raw.json")),
            "dxdiag.ps1": envelope(sample("dxdiag_raw.json")),
            "identity.ps1": envelope({"computer_name": "HOST", "user_name": "someone"}),
            "nvidia-smi": sample("nvidia_smi_raw.json")["stdout"],
            "lhm_http.ps1": json.dumps(
                {"ok": False, "kind": "unavailable", "error": "Unable to connect"}
            ),
        }
    )


def has_powershell() -> bool:
    return shutil.which("powershell.exe") is not None
