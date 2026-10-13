#!/usr/bin/env python3
"""Read local JSONL traces without importing the backend or needing its dependencies."""

import argparse
import heapq
import json
import os
from pathlib import Path


def records(directory):
    for path in sorted(Path(directory).glob("*.jsonl"), reverse=True):
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    yield json.loads(line)
                except ValueError:
                    print(f"Warning: unreadable record in {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dir", default=os.environ.get("AGENT_TRACE_DIR", "data/agent_traces")
    )
    commands = parser.add_subparsers(dest="command", required=True)
    recent = commands.add_parser("list")
    recent.add_argument("--limit", type=int, default=20)
    show = commands.add_parser("show")
    show.add_argument("trace_id")
    show.add_argument(
        "--json", action="store_true", help="Print complete persisted record"
    )
    args = parser.parse_args()
    if args.command == "list":
        rows = heapq.nlargest(
            max(0, args.limit),
            records(args.dir),
            key=lambda row: row.get("timestamp", ""),
        )
        for row in rows:
            outcome = row.get("outcome", {})
            print(
                f"{row.get('trace_id')} {row.get('timestamp')} {row.get('model', {}).get('resolved')} {outcome.get('status')} {outcome.get('duration_ms')}ms"
            )
        return
    row = next(
        (row for row in records(args.dir) if row.get("trace_id") == args.trace_id), None
    )
    if row is None:
        parser.exit(1, "Trace not found\n")
    if args.json:
        print(json.dumps(row, indent=2, ensure_ascii=False))
        return
    print(
        f"Trace {row['trace_id']} — schema {row.get('schema_version')}\n{row.get('timestamp')}"
    )
    print(json.dumps(row.get("outcome"), indent=2, ensure_ascii=False))
    for round_ in sorted(row.get("rounds", []), key=lambda r: r["round"]):
        print(
            f"\nRound {round_['round']} — {round_.get('model')} — {round_.get('duration_ms')}ms — {round_.get('decision')}"
        )
        print("Response:", json.dumps(round_.get("model_response"), ensure_ascii=False))
        print(
            "Parsing:", json.dumps(round_.get("parse_diagnostics"), ensure_ascii=False)
        )
        for call in round_.get("tool_calls", []):
            print("Tool:", json.dumps(call, ensure_ascii=False))
    for call in row.get("preparation_tools", []):
        print("Preparation tool:", json.dumps(call, ensure_ascii=False))


if __name__ == "__main__":
    main()
