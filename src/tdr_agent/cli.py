"""``tdr-detective``: ask a triage question, regenerate the schema, or serve the MCP server.

tdr-detective ask "Why did my screen go black at 9:42?" [--replay DIR] [--json]
                  [--model M] [--effort E] [--budget USD] [--post http://127.0.0.1:4173]
tdr-detective schema [--check]
tdr-detective serve
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from pc_diag_mcp.models import BugReport, Diagnosis, Incident, RunStats

EFFORTS = ("low", "medium", "high", "xhigh", "max")
POST_TIMEOUT_S = 15.0


def _http_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise argparse.ArgumentTypeError(f"{value!r} is not an http(s) URL")
    return value.rstrip("/")


def bug_report_markdown(report: BugReport) -> str:
    def cell(text: str) -> str:
        return text.replace("|", "\\|").replace("\n", " ")

    rows = [
        ("Severity", report.severity),
        ("Occurred", f"{report.occurred_at:%Y-%m-%d %H:%M:%S} UTC"),
        ("OS build", report.os_build),
        ("GPU", report.gpu),
        ("Driver", report.driver_version),
    ]
    lines = [f"# {report.title}", "", report.summary, "", "| Field | Value |", "|---|---|"]
    lines += [f"| {name} | {cell(value)} |" for name, value in rows]
    lines += ["", "## Steps to reproduce", ""]
    lines += [f"{n}. {step}" for n, step in enumerate(report.steps_to_reproduce, start=1)]
    lines += ["", "## Expected", "", report.expected, "", "## Actual", "", report.actual]
    lines += ["", "## Evidence", ""] + [f"- {item}" for item in report.evidence]
    return "\n".join(lines)


def _run_line(run: RunStats, tool_calls: Sequence[str]) -> str:
    cost = f"${run.cost_usd:.4f}" if run.cost_usd is not None else "cost n/a"
    return (
        f"{run.model}: {run.input_tokens} in / {run.output_tokens} out / "
        f"{run.cache_read_tokens} cache-read tokens, {cost}, {run.latency_ms / 1000:.1f} s, "
        f"{len(tool_calls)} tool calls"
    )


def render_report(diagnosis: Diagnosis) -> str:
    w = diagnosis.window
    lines = [f"Window: {w.start:%Y-%m-%d %H:%M:%S} to {w.end:%Y-%m-%d %H:%M:%S} UTC", ""]
    lines.append("Ranked causes")
    for n, cause in enumerate(diagnosis.causes, start=1):
        lines.append(f"  {n}. {cause.category.value} ({cause.confidence:.2f}): {cause.rationale}")
        if cause.evidence_refs:
            lines.append(f"     evidence: {', '.join(cause.evidence_refs)}")
    lines += ["", "Next steps"]
    lines += [f"  {n}. {step}" for n, step in enumerate(diagnosis.next_steps, start=1)]
    lines += ["", "Bug report", "", bug_report_markdown(diagnosis.bug_report)]
    return "\n".join(lines)


def post_incident(base_url: str, incident: Incident) -> str:
    request = Request(  # noqa: S310 - the scheme is checked to be http(s) by _http_url
        f"{base_url}/api/incidents",
        data=incident.model_dump_json().encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=POST_TIMEOUT_S) as response:  # noqa: S310
        body = json.loads(response.read() or b"{}")
    return f"posted incident {incident.id} ({'created' if body.get('created') else 'updated'})"


def _ask(args: argparse.Namespace) -> int:
    from tdr_agent import triage  # needs the 'agent' extra
    from tdr_agent.evidence import build_incident

    result = asyncio.run(
        triage.run_triage(
            args.question,
            replay_dir=args.replay,
            model=args.model,
            effort=args.effort,
            max_budget_usd=args.budget,
        )
    )
    if args.json:
        print(result.model_dump_json(indent=2))
    else:
        if result.diagnosis is not None:
            print(render_report(result.diagnosis))
        else:
            print(f"No valid diagnosis: {result.parse_error}\n\nRaw answer:\n{result.answer}")
        print(f"\nRun: {_run_line(result.run, result.tool_calls)}")
        if result.error:
            print(f"Error: {result.error}")

    code = 0 if result.diagnosis is not None and result.error is None else 1
    if args.post:
        if result.diagnosis is None:
            print("incident not posted: the run produced no valid diagnosis", file=sys.stderr)
            return 1
        from pc_diag_mcp.collectors import Collector, PowerShellCollector, ReplayCollector

        collector: Collector = (
            ReplayCollector(args.replay) if args.replay is not None else PowerShellCollector()
        )
        incident = build_incident(
            args.question, result.diagnosis, collector, run=result.run, replay_dir=args.replay
        )
        try:
            print(post_incident(args.post, incident), file=sys.stderr)
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:500].decode("utf-8", "replace")
            print(f"posting failed: HTTP {exc.code}: {detail}", file=sys.stderr)
            return 1
        except (urllib.error.URLError, OSError) as exc:
            print(f"posting failed: {exc}", file=sys.stderr)
            return 1
    return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tdr-detective",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ask = sub.add_parser("ask", help="triage one question with the agent")
    ask.add_argument("question")
    ask.add_argument("--replay", type=Path, help="replay directory instead of the live host")
    ask.add_argument("--model", default="claude-opus-5-5", help="full model id")
    ask.add_argument("--effort", default="medium", choices=EFFORTS)
    ask.add_argument("--budget", type=float, default=1.0, help="max spend in USD")
    ask.add_argument("--json", action="store_true", help="print the TriageResult as JSON")
    ask.add_argument(
        "--post", type=_http_url, metavar="URL", help="timeline API base URL to POST to"
    )

    schema = sub.add_parser("schema", help="regenerate schema/incident.schema.json")
    schema.add_argument("--check", action="store_true", help="verify instead of writing")

    sub.add_parser("serve", help="run the pc-diag MCP server on stdio")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "ask":
        return _ask(args)
    if args.command == "schema":
        from pc_diag_mcp import schema

        return schema.main(["--check"] if args.check else [])
    from pc_diag_mcp import server

    server.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
