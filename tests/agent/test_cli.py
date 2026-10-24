from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from pc_diag_mcp import schema, server
from pc_diag_mcp.models import Diagnosis, Incident, RunStats
from tdr_agent import cli, triage
from tdr_agent.triage import TriageResult

from .conftest import diagnosis_dict, fenced

REPLAY = Path(__file__).resolve().parents[2] / "evals" / "scenarios" / "syn-026"
RUN = RunStats(
    config="skill_tools",
    model="claude-opus-5-5",
    input_tokens=12000,
    output_tokens=900,
    cache_read_tokens=40000,
    cost_usd=0.21,
    latency_ms=65000,
)


def syn_diagnosis() -> dict[str, Any]:
    d = diagnosis_dict(["gpu_driver_timeout", "gpu_thermal"], refs=("E3",))
    d["window"] = {"start": "2026-10-15T01:30:00Z", "end": "2026-10-15T02:00:00Z"}
    d["bug_report"]["occurred_at"] = "2026-10-15T01:43:08Z"
    return d


def ok_result(**kw: Any) -> TriageResult:
    d = syn_diagnosis()
    fields: dict[str, Any] = {
        "answer": fenced(d),
        "diagnosis": Diagnosis.model_validate(d),
        "parse_error": None,
        "run": RUN,
        "tool_calls": ["Skill", "mcp__pc-diag__system_info", "Bash"],
        "error": None,
    }
    fields.update(kw)
    return TriageResult(**fields)


class FakeTriage:
    """Stands in for ``run_triage``; records each call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = ok_result()

    async def __call__(self, question: str, **kw: Any) -> TriageResult:
        self.calls.append({"question": question, **kw})
        return self.result


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch) -> FakeTriage:
    fake = FakeTriage()
    monkeypatch.setattr(triage, "run_triage", fake)
    return fake


class FakeResponse(io.BytesIO):
    status = 201

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@pytest.fixture
def posts(monkeypatch: pytest.MonkeyPatch) -> list[urllib.request.Request]:
    sent: list[urllib.request.Request] = []

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> FakeResponse:
        sent.append(request)
        body = json.loads(request.data or b"{}")  # type: ignore[arg-type]
        return FakeResponse(json.dumps({"id": body.get("id"), "created": True}).encode())

    monkeypatch.setattr(cli, "urlopen", fake_urlopen)
    return sent


def test_ask_prints_readable_report(agent: FakeTriage, capsys: Any) -> None:
    code = cli.main(["ask", "Why did my screen go black at 9:43?", "--replay", str(REPLAY)])
    out = capsys.readouterr().out
    assert code == 0
    assert "Ranked causes" in out
    assert "1. gpu_driver_timeout (0.90)" in out
    assert "evidence: E3" in out
    assert "Next steps" in out
    assert "# Display driver reset during gameplay" in out
    assert "| OS build | 22631.4317 |" in out
    assert "- Display 4101 at 01:43:08Z" in out
    assert "claude-opus-5-5" in out and "12000 in" in out and "$0.2100" in out
    call = agent.calls[-1]
    assert call["question"] == "Why did my screen go black at 9:43?"
    assert call["replay_dir"] == REPLAY
    assert call["model"] == "claude-opus-5-5"
    assert call["effort"] == "medium"
    assert call["max_budget_usd"] == 1.0


def test_ask_passes_options(agent: FakeTriage) -> None:
    argv = ["ask", "q", "--model", "claude-x", "--effort", "high", "--budget", "0.25"]
    assert cli.main(argv) == 0
    call = agent.calls[-1]
    assert (call["model"], call["effort"], call["max_budget_usd"]) == ("claude-x", "high", 0.25)
    assert call["replay_dir"] is None


def test_ask_json(agent: FakeTriage, capsys: Any) -> None:
    assert cli.main(["ask", "q", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert TriageResult.model_validate(payload).diagnosis is not None


def test_ask_reports_errors_and_parse_failures(agent: FakeTriage, capsys: Any) -> None:
    agent.result = ok_result(
        answer="I think it was the GPU.",
        diagnosis=None,
        parse_error="no fenced ```json block found",
        error="run ended with error_max_budget_usd",
    )
    assert cli.main(["ask", "q"]) == 1
    out = capsys.readouterr().out
    assert "error_max_budget_usd" in out
    assert "no fenced" in out
    assert "I think it was the GPU." in out


def test_post_sends_incident(
    agent: FakeTriage, posts: list[urllib.request.Request], capsys: Any
) -> None:
    argv = ["ask", "black screen", "--replay", str(REPLAY), "--post", "http://127.0.0.1:4173/"]
    assert cli.main(argv) == 0
    (request,) = posts
    assert request.full_url == "http://127.0.0.1:4173/api/incidents"
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json"
    incident = Incident.model_validate_json(request.data or b"")  # type: ignore[arg-type]
    assert incident.question == "black screen"
    assert incident.run == RUN
    assert incident.system.os_build == "22631.4317"
    assert any(e.kind == "frametime" for e in incident.evidence)
    assert incident.id in capsys.readouterr().err


def test_post_skipped_without_diagnosis(
    agent: FakeTriage, posts: list[urllib.request.Request], capsys: Any
) -> None:
    agent.result = ok_result(diagnosis=None, parse_error="bad", answer="x")
    argv = ["ask", "q", "--replay", str(REPLAY), "--post", "http://127.0.0.1:4173"]
    assert cli.main(argv) == 1
    assert posts == []
    assert "not posted" in capsys.readouterr().err


def test_post_http_error_is_reported(
    agent: FakeTriage, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    def failing(request: urllib.request.Request, timeout: float) -> Any:
        raise urllib.error.HTTPError(
            request.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"error":"incident failed"}')
        )

    monkeypatch.setattr(cli, "urlopen", failing)
    argv = ["ask", "q", "--replay", str(REPLAY), "--post", "http://127.0.0.1:4173"]
    assert cli.main(argv) == 1
    assert "incident failed" in capsys.readouterr().err


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://x",
        "127.0.0.1:4173",
        "http://example.com:4173",  # plain http off loopback
        "http://10.0.0.5:4173",
        "http://127.0.0.1.evil.example:4173",
        "http://user:hunter2pass@127.0.0.1:4173",
        "https://hunter2pass@timeline.example",
        "http://127.0.0.1:99999",
        "https://",
    ],
)
def test_post_rejects_unsafe_urls(agent: FakeTriage, url: str, capsys: Any) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["ask", "q", "--post", url])
    assert exc.value.code == 2
    assert "hunter2pass" not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("url", "normalised"),
    [
        ("http://127.0.0.1:4173/", "http://127.0.0.1:4173"),
        ("http://localhost:4173", "http://localhost:4173"),
        ("http://[::1]:4173", "http://[::1]:4173"),
        ("https://timeline.example/base/?x=1#frag", "https://timeline.example/base"),
    ],
)
def test_post_accepts_https_and_loopback_http(url: str, normalised: str) -> None:
    assert cli._post_url(url) == normalised


def test_post_opener_follows_no_redirects_and_uses_no_proxy() -> None:
    handlers = cli._OPENER.handlers
    # ProxyHandler({}) replaces the default env-driven one and registers no proxy routes.
    proxies = [h for h in handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert all(not h.proxies for h in proxies)  # type: ignore[attr-defined]
    assert not any(
        hasattr(h, "http_open") and isinstance(h, urllib.request.ProxyHandler) for h in handlers
    )
    (redirect,) = [h for h in handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
    request = urllib.request.Request("http://127.0.0.1:4173/api/incidents")
    with pytest.raises(urllib.error.HTTPError, match="redirect not followed"):
        redirect.redirect_request(
            request, io.BytesIO(), 302, "Found", Message(), "http://evil.example/"
        )


@pytest.mark.parametrize("budget", ["inf", "nan", "0", "-1"])
def test_budget_must_be_finite_and_positive(agent: FakeTriage, budget: str) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["ask", "q", "--budget", budget])
    assert exc.value.code == 2


@pytest.mark.parametrize("as_json", [False, True])
def test_printed_output_is_redacted(
    agent: FakeTriage, capsys: Any, monkeypatch: pytest.MonkeyPatch, as_json: bool
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key-from-env-0123456789")
    agent.result = ok_result(
        answer="leaked sk-ant-api03-abcDEF_123 in the answer",
        diagnosis=None,
        parse_error="no fenced ```json block found",
        error="auth failed for key-from-env-0123456789",
    )
    cli.main(["ask", "q", *(["--json"] if as_json else [])])
    out = capsys.readouterr().out
    assert "sk-ant-api03" not in out
    assert "key-from-env-0123456789" not in out
    assert "[redacted]" in out


def test_schema_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str] | None] = []

    def fake(argv: list[str] | None = None) -> int:
        seen.append(argv)
        return 7

    monkeypatch.setattr(schema, "main", fake)
    assert cli.main(["schema", "--check"]) == 7
    assert cli.main(["schema"]) == 7
    assert seen == [["--check"], []]


def test_serve_runs_the_mcp_server(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[bool] = []
    monkeypatch.setattr(server, "main", lambda: ran.append(True))
    assert cli.main(["serve"]) == 0
    assert ran == [True]
