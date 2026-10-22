#!/usr/bin/env python3
"""One-shot read-only GitHub inventory; never runs agents or applies fixes."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def query(args, *, run=subprocess.run):
    result = run(
        ["gh", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        # Do not forward arbitrary CLI stderr (may contain credential configuration).
        return {"ok": False, "exit_code": result.returncode, "error": "GitHub-Abfrage fehlgeschlagen"}
    try:
        return {"ok": True, "data": json.loads(result.stdout)}
    except ValueError:
        return {"ok": False, "error": "Ungültige JSON-Antwort"}


def probe(root, *, memberships=False, account="", run=subprocess.run):
    config = json.loads((root / "config/orgs.json").read_text(encoding="utf-8-sig"))
    orgs = [
        x["name"]
        for x in config["organizations"]
        if x.get("enabled", True) and x.get("type", "organization") == "organization"
    ]
    owners = [*orgs, *[x["name"] for x in config.get("personal_namespaces", [])]]
    results = {}
    for kind in ("prs", "issues"):
        results[kind] = query(
            [
                "search",
                kind,
                "--owner",
                ",".join(owners),
                "--state",
                "open",
                "--limit",
                "1000",
                "--json",
                "number,title,repository,url",
            ],
            run=run,
        )
        if results[kind].get("ok") and len(results[kind]["data"]) >= 1000:
            results[kind]["coverage"] = "possibly-truncated"
    if memberships:
        account = account or config.get("multi_bot", {}).get("merge_account", "")
        if not account or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", account):
            raise ValueError("Mitgliedschaftsprüfung braucht --account oder multi_bot.merge_account")
        results["bot_membership"] = {org: query(["api", f"orgs/{org}/memberships/{account}"], run=run) for org in orgs}
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--memberships", action="store_true")
    parser.add_argument(
        "--account", default=os.environ.get("MULTIBOT_MERGE_ACCOUNT", ""), help="Configured merge account to inspect"
    )
    parser.add_argument("--output", type=Path, help="Optional local evidence JSON")
    args = parser.parse_args(argv)
    try:
        result = probe(args.root, memberships=args.memberships, account=args.account)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            summary = {
                kind: {"ok": result[kind]["ok"], "count": len(result[kind].get("data", []))}
                for kind in ("prs", "issues")
            }
            summary["bot_membership"] = {
                org: {
                    "ok": row["ok"],
                    "state": row.get("data", {}).get("state"),
                    "role": row.get("data", {}).get("role"),
                }
                for org, row in result.get("bot_membership", {}).items()
            }
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        # Membership 404/403 is unknown, never a false 'not member'.
        return 0 if result["prs"]["ok"] and result["issues"]["ok"] else 1
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print(f"[HOLD] Probe unvollständig ({type(exc).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
