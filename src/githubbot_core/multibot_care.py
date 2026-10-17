#!/usr/bin/env python3
"""Host-local care ledger, conservative leases and oldest-first selection."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import socket
import sys
import uuid

UTC = timezone.utc
MAX_TTL = 86400


def timestamp(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Zeitstempel ohne Zeitzone")
    return parsed.astimezone(UTC)


def host_name(value=None):
    value = value or os.environ.get("COMPUTERNAME") or socket.gethostname()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value) or value in (".", ".."):
        raise ValueError("Ungültiger Hostname")
    return value


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def targets(root):
    registry = load_json(root / "config/repo_registry.json")["repos"]
    config = load_json(root / "config/orgs.json")
    owners = {x["name"] for x in config["organizations"] if x.get("enabled", True)}
    owners.update(x["name"] for x in config.get("personal_namespaces", []))
    result = {}
    for repo, entry in registry.items():
        if repo.split("/")[0] not in owners:
            continue
        gh = entry.get("github") or {}
        visibility = entry.get("visibility_override") or gh.get("visibility")
        if visibility != "public" or gh.get("archived") is not False or gh.get("fork") is not False:
            continue
        result[repo] = entry
    # Profile candidates are explicit but unknown visibility is never assumed public.
    return result


def events(root):
    rows = []
    log = root / "_log"
    paths = sorted((log / "hosts").glob("*/CARE-LOG*.jsonl"))
    paths += sorted((log / "archive").glob("*/CARE-LOG*.jsonl"))
    paths += sorted((log / "conflicts").glob("*/*CARE-LOG*.jsonl"))
    for path in paths:
        for index, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if row["event"] not in {"claim", "care"}:
                    raise ValueError("Unbekanntes Ereignis")
                if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", row["repo"]):
                    raise ValueError("Ungültiges Repo")
                timestamp(row["at"])
                host_name(row["host"])
                if not row.get("claim_id"):
                    raise ValueError("Claim-ID fehlt")
                if row["event"] == "claim":
                    expires = timestamp(row["expires_at"])
                    duration = (expires - timestamp(row["at"])).total_seconds()
                    if not 0 < duration <= MAX_TTL:
                        raise ValueError("Ungültige TTL")
                elif row.get("result") not in {"ok", "failed", "skipped"}:
                    raise ValueError("Ungültiges Ergebnis")
                rows.append(row)
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"Unlesbarer Care-Beleg {path}:{index}: {exc}") from exc
    return rows


def active_claims(rows, now):
    # Only a matching owner may close a lease. Foreign-host receipts cannot release it.
    closed = {(r["claim_id"], r["host"], r["repo"]) for r in rows if r["event"] == "care"}
    return [
        r
        for r in rows
        if r["event"] == "claim"
        and (r["claim_id"], r["host"], r["repo"]) not in closed
        and timestamp(r["expires_at"]) > now
    ]


def legacy_dates(root, candidates, host):
    dates = {}
    # Historical run directories often contain only repo-name, so ambiguous names are ignored.
    names = {}
    for repo in candidates:
        names.setdefault(repo.split("/")[1].lower(), []).append(repo)
    logs = root / "logs"
    for path in logs.iterdir() if logs.exists() else []:
        match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})-(.+?)-(?:full-|surface-)?care", path.name)
        if match and path.is_dir():
            repos = names.get(match[2].lower(), [])
            if len(repos) == 1:
                at = timestamp(match[1] + "T00:00:00+00:00")
                dates[repos[0]] = max(dates.get(repos[0], at), at)
    for repo, entry in candidates.items():
        # Foreign host paths are not valid paths on this machine.
        for clone in entry.get("hosts", {}).get(host, {}).get("clones", []):
            path = Path(clone["path"]) / "_after-care/LOG.md"
            if path.is_file():
                for match in re.finditer(
                    r"^## (\d{4}-\d{2}-\d{2})\s+[^\n]*(?:full|surface)", path.read_text(encoding="utf-8-sig"), re.M
                ):
                    at = timestamp(match[1] + "T00:00:00+00:00")
                    dates[repo] = max(dates.get(repo, at), at)
    return dates


def overview(root, *, now=None, host=None):
    now = now or datetime.now(UTC)
    host = host_name(host)
    candidates = targets(root)
    rows = events(root)
    dates = legacy_dates(root, candidates, host)
    for row in rows:
        if row["event"] == "care" and row["result"] == "ok":
            at = timestamp(row["at"])
            if at > now:
                raise ValueError("Care-Beleg liegt in der Zukunft")
            dates[row["repo"]] = max(dates.get(row["repo"], at), at)
    claimed = {r["repo"] for r in active_claims(rows, now)}
    return sorted(
        [
            {"repo": repo, "last_care": dates[repo].isoformat() if repo in dates else None, "claimed": repo in claimed}
            for repo in candidates
        ],
        key=lambda r: (r["last_care"] is not None, r["last_care"] or "", r["repo"]),
    )


def append_event(root, host, row):
    path = root / "_log/hosts" / host / "CARE-LOG.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive per-host writer guard; stale guards are deliberately not auto-deleted.
    guard = path.with_suffix(".writer")
    fd = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.close(fd)
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        guard.unlink()


def claim(root, repo, *, host=None, ttl=3600, now=None):
    host = host_name(host)
    now = now or datetime.now(UTC)
    if not 1 <= ttl <= MAX_TTL:
        raise ValueError("TTL muss 1 bis 86400 Sekunden betragen")
    if repo not in targets(root):
        raise ValueError("Ziel ist kein bekanntes eigenes öffentliches Care-Repo")
    if any(r["repo"] == repo for r in active_claims(events(root), now)):
        raise ValueError("Ziel bereits beansprucht")
    row = {
        "event": "claim",
        "repo": repo,
        "host": host,
        "at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=ttl)).isoformat(),
        "claim_id": uuid.uuid4().hex,
    }
    append_event(root, host, row)
    # Detect a second claim visible after append. OneDrive replication still requires project locks.
    if len([r for r in active_claims(events(root), now) if r["repo"] == repo]) != 1:
        raise ValueError("Konkurrierender Claim; Projekt nicht bearbeiten")
    return row


def record(root, claim_id, *, host=None, kind="surface", model="", result="ok", evidence="", now=None):
    host = host_name(host)
    now = now or datetime.now(UTC)
    leases = [r for r in active_claims(events(root), now) if r["claim_id"] == claim_id]
    if len(leases) != 1 or leases[0]["host"] != host:
        raise ValueError("Eigener aktiver Claim fehlt")
    if any(r["repo"] == leases[0]["repo"] and r["claim_id"] != claim_id for r in active_claims(events(root), now)):
        raise ValueError("Konkurrierender Claim")
    if kind not in {"full", "surface"} or result not in {"ok", "failed", "skipped"}:
        raise ValueError("Ungültige Art oder Ergebnis")
    if result == "ok" and (not model.strip() or not evidence.strip()):
        raise ValueError("Erfolgsbeleg braucht Modell und PR/Commit/Artefakt")
    row = {
        "event": "care",
        "repo": leases[0]["repo"],
        "host": host,
        "at": now.isoformat(),
        "claim_id": claim_id,
        "kind": kind,
        "model": model,
        "result": result,
        "evidence": evidence,
    }
    append_event(root, host, row)
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--host", default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("next")
    commands.add_parser("overview")
    c = commands.add_parser("claim")
    c.add_argument("--repo", required=True)
    c.add_argument("--ttl", type=int, default=3600)
    r = commands.add_parser("record")
    r.add_argument("--claim-id", required=True)
    r.add_argument("--kind", choices=("full", "surface"), default="surface")
    r.add_argument("--model", default="")
    r.add_argument("--result", choices=("ok", "failed", "skipped"), default="ok")
    r.add_argument("--evidence", default="")
    args = parser.parse_args(argv)
    try:
        if args.command in {"next", "overview"}:
            rows = overview(args.root, host=args.host)
            result = next((r for r in rows if not r["claimed"]), None) if args.command == "next" else rows
        elif args.command == "claim":
            result = claim(args.root, args.repo, host=args.host, ttl=args.ttl)
        else:
            result = record(
                args.root,
                args.claim_id,
                host=args.host,
                kind=args.kind,
                model=args.model,
                result=args.result,
                evidence=args.evidence,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"[HOLD] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
