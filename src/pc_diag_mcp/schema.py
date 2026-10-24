"""Export the Incident JSON Schema consumed by the timeline API.

python -m pc_diag_mcp.schema           # write schema/incident.schema.json
python -m pc_diag_mcp.schema --check   # exit 1 if the committed file is stale
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pc_diag_mcp.models import Incident

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema" / "incident.schema.json"


def render() -> str:
    return json.dumps(Incident.model_json_schema(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify instead of writing")
    args = parser.parse_args(argv)

    expected = render()
    if args.check:
        current = SCHEMA_PATH.read_text(encoding="utf-8") if SCHEMA_PATH.exists() else ""
        if current != expected:
            print(f"{SCHEMA_PATH} is stale; run: python -m pc_diag_mcp.schema", file=sys.stderr)
            return 1
        return 0

    SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCHEMA_PATH.write_text(expected, encoding="utf-8")
    print(f"wrote {SCHEMA_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
