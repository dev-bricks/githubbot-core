"""Command line interface ``githubbot-core``.

Every command can print a JSON envelope (``--json``)::

    {"schema": "githubbot-core.result/v1", "command": ..., "ok": bool,
     "mode": "read" | "preview" | "apply", "data": ..., "errors": [...]}

Commands that could write default to ``preview``. Nothing is written without
``--apply``, and even then only actions permitted by the configuration.

Exit codes: 0 ok, 1 runtime error (for example GitHub not reachable),
2 configuration or usage error, 3 plugin not available.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from importlib import resources
from pathlib import Path
from typing import Any, Optional

from . import __version__, actions, plugins
from .config import ConfigError, load_config
from .credentials import CredentialUnavailable, resolve_token
from .duplicates import DuplicateDetector
from .health import RepoHealthCollector, generate_health_report
from .pr_review import PRReviewer
from .review_gate import DEFAULT_NAMESPACE, ReceiptValidationError, gate_names
from .stars import StarsFetcher, generate_stars_report
from .traffic import TrafficFetcher, generate_traffic_report
from .transport import GhCliTransport
from .triage import GitHubIssue, IssueClassifier, IssueTriage

RESULT_SCHEMA = "githubbot-core.result/v1"
EXIT_OK, EXIT_RUNTIME, EXIT_USAGE, EXIT_PLUGIN = 0, 1, 2, 3


def _emit(args, command: str, ok: bool, mode: str, data: Any, errors: list[str], text: str = "") -> None:
    if args.json:
        envelope = {"schema": RESULT_SCHEMA, "command": command, "ok": ok, "mode": mode,
                    "data": data, "errors": errors}
        print(json.dumps(envelope, ensure_ascii=False, indent=2, default=str))
        return
    if text:
        print(text)
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)


def _read_issues(path: Path) -> list[GitHubIssue]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    issues = []
    for item in raw:
        if "user" in item or "created_at" in item:
            issues.append(GitHubIssue.from_rest_json(item))
        else:
            issues.append(GitHubIssue.from_gh_json(item))
    return issues


def _plan_output(args, command: str, plan: list, config: dict, transport, extra: Optional[dict] = None) -> int:
    """Shared preview/apply step for every command that produces actions."""
    if args.apply:
        result = actions.apply(plan, transport, config,
                               receipt_store=Path(args.receipts) if args.receipts else None,
                               expected_plan_sha256=args.expect_plan)
        failed = [r for r in result["receipts"] if r["status"] == "failed"]
        errors = ([result["error"]] if result.get("error") else []) + [f"{r['id'][:12]}: {r['detail']}" for r in failed]
        data = {**(extra or {}), **result}
        lines = [f"apply: {r['status']:<18} {r['kind']:<18} {r['detail']}" for r in result["receipts"]]
        _emit(args, command, not errors, "apply", data, errors, "\n".join(lines) or "nothing to apply")
        return EXIT_RUNTIME if errors else EXIT_OK
    result = actions.preview(plan, config)
    data = {**(extra or {}), **result}
    lines = [f"preview: {'ok    ' if a['permitted'] else 'DENIED'} {a['kind']:<18} {a['reason']}"
             + (f"  ({a['denied_reason']})" if a["denied_reason"] else "") for a in result["actions"]]
    lines.append(f"plan_sha256 {result['plan_sha256']} -- nothing written; rerun with --apply to execute")
    _emit(args, command, True, "preview", data, [], "\n".join(lines))
    return EXIT_OK


def cmd_info(args, config, transport) -> int:
    data = {"package": "githubbot-core", "version": __version__, "result_schema": RESULT_SCHEMA,
            "spi_version": plugins.SPI_VERSION, "plugin_group": plugins.ENTRY_POINT_GROUP,
            "plugins": plugins.list_plugins()}
    text = f"githubbot-core {__version__} (SPI v{plugins.SPI_VERSION}, {len(data['plugins'])} plugin(s))"
    _emit(args, "info", True, "read", data, [], text)
    return EXIT_OK


def cmd_doctor(args, config, transport) -> int:
    try:
        _, provider = resolve_token()
        credential = {"available": True, "provider": provider}
    except CredentialUnavailable as exc:
        credential = {"available": False, "detail": str(exc)}
    data = {"gh_cli": shutil.which("gh") is not None, "credential": credential,
            "permissions": config.get("permissions", {}), "plugins": plugins.list_plugins()}
    text = (f"gh CLI: {'found' if data['gh_cli'] else 'missing'}\n"
            f"credential: {credential.get('provider') or 'not available'}\n"
            f"enabled actions: {', '.join(data['permissions'].get('enabled', [])) or 'none'}")
    _emit(args, "doctor", True, "read", data, [], text)
    return EXIT_OK


def cmd_plugins(args, config, transport) -> int:
    listing = plugins.list_plugins()
    text = "\n".join(f"{p['name']}: {'available' if p['available'] else p['error']}" for p in listing) \
        or "no plugins installed"
    _emit(args, "plugins", True, "read", listing, [], text)
    return EXIT_OK


def template_sha256(schema: str, template_id: str, blueprint: dict) -> str:
    """sha256 over canonical JSON of exactly {schema, id, blueprint}: sort_keys, separators (",", ":"),
    ensure_ascii=False, UTF-8. Excludes version, capability, permissions and _meta, so a consumer can
    recompute it from the printed `data` alone."""
    material = {"schema": schema, "id": template_id, "blueprint": blueprint}
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def cmd_system_agent(args, config, transport) -> int:
    template = json.loads(resources.files("githubbot_core").joinpath("system_agent.json").read_text(encoding="utf-8"))
    schema = template["_meta"]["schema"]
    data = {"schema": schema, "id": template["id"], "version": __version__,
            "sha256": template_sha256(schema, template["id"], template["blueprint"]),
            "capability": {"package": "githubbot-core", "version": __version__, "spi_version": plugins.SPI_VERSION},
            "blueprint": template["blueprint"], "permissions": config.get("permissions", {})}
    text = f"system agent {data['id']} {data['version']} sha256 {data['sha256']}"
    _emit(args, "system-agent", True, "read", data, [], text)
    return EXIT_OK


def cmd_classify(args, config, transport) -> int:
    classifier = IssueClassifier(config)
    results = [classifier.classify(issue) for issue in _read_issues(Path(args.input))]
    data = [{"number": r.issue.number, "labels_to_add": r.labels_to_add, "comment_type": r.comment_type,
             "escalate": r.escalate, "escalate_reason": r.escalate_reason} for r in results]
    text = "\n".join(f"#{d['number']}: {', '.join(d['labels_to_add']) or '-'}"
                     + (" [escalate]" if d["escalate"] else "") for d in data)
    _emit(args, "classify", True, "read", data, [], text)
    return EXIT_OK


def cmd_duplicates(args, config, transport) -> int:
    candidates = DuplicateDetector(threshold=args.threshold).find_duplicates(_read_issues(Path(args.input)))
    data = [{"issue": c.issue_number, "original": c.original_number, "similarity": round(c.similarity, 4)}
            for c in candidates]
    _emit(args, "duplicates", True, "read", data, [], "\n".join(str(c) for c in candidates) or "no duplicates")
    return EXIT_OK


def cmd_triage(args, config, transport) -> int:
    triage = IssueTriage(config, args.repo, transport)
    issues, error = triage.fetch_issues(limit=args.limit)
    if error:
        _emit(args, "triage", False, "read", None, [error])
        return EXIT_RUNTIME
    results = triage.classify_all(issues)
    plan = triage.plan(results)
    extra = {"repo": args.repo, "issues": len(issues),
             "escalations": [r.issue.number for r in triage.get_escalations(results)]}
    if not args.json:
        print(triage.generate_report(results))
    return _plan_output(args, "triage", plan, config, transport, extra)


def cmd_review_pr(args, config, transport) -> int:
    reviewer = PRReviewer(config, args.repo, transport, policy_plugins=plugins.available_plugins())
    pr, error = reviewer.fetch(args.pr)
    if error:
        _emit(args, "review-pr", False, "read", None, [error])
        return EXIT_RUNTIME
    result = reviewer.review(pr)
    data = {"repo": args.repo, "pr": pr.number, "head_sha": pr.head_sha, "escalate": result.escalate,
            "escalate_reason": result.escalate_reason, "summary": result.summary,
            "checks": [{"name": n, "passed": p, "detail": d} for n, p, d in result.policy_check_results]}
    _emit(args, "review-pr", True, "read", data, [], result.summary)
    return EXIT_OK


def cmd_health(args, config, transport) -> int:
    health_config = config.get("health", {})
    summaries = RepoHealthCollector(health_config, transport).fetch_all(args.repo)
    errors = [e for s in summaries for e in s.errors]
    report = generate_health_report(
        summaries,
        window_days=int(health_config.get("failed_runs_window_days", 14)),
        max_dependabot_alerts_per_repo=int(health_config.get("max_dependabot_alerts_per_repo", 10)),
        max_failed_runs_per_repo=int(health_config.get("max_failed_runs_per_repo", 5)),
    )
    data = [{"repo": s.repo, "dependabot": s.dependabot_count, "failed_runs": s.failed_runs_count,
             "open_issues": s.open_issues_count, "open_prs": s.open_pull_requests_count,
             "code_scanning": s.code_scanning_count, "secret_scanning": s.secret_scanning_count,
             "errors": s.errors} for s in summaries]
    _emit(args, "health", not errors, "read", data, errors, report)
    return EXIT_RUNTIME if errors else EXIT_OK


def cmd_stars(args, config, transport) -> int:
    summaries = StarsFetcher(transport).fetch_all(args.repo)
    errors = [f"{s.repo}: {s.error}" for s in summaries if s.error]
    data = [{"repo": s.repo, "stars": s.stars, "error": s.error} for s in summaries]
    _emit(args, "stars", not errors, "read", data, errors, generate_stars_report(summaries))
    return EXIT_RUNTIME if errors else EXIT_OK


def cmd_traffic(args, config, transport) -> int:
    summaries = TrafficFetcher(transport).fetch_all(args.repo)
    errors = [f"{s.repo}: {s.error}" for s in summaries if s.error]
    data = [{"repo": s.repo, "views": s.views_total, "views_unique": s.views_unique,
             "clones": s.clones_total, "clones_unique": s.clones_unique, "error": s.error} for s in summaries]
    _emit(args, "traffic", not errors, "read", data, errors, generate_traffic_report(summaries))
    return EXIT_RUNTIME if errors else EXIT_OK


def cmd_run(args, config, transport) -> int:
    plan = plugins.run_plan(args.plugin, config, transport, args.plugin_args)
    return _plan_output(args, "run", plan, config, transport, {"plugin": args.plugin})


def cmd_gate_scaffold(args, config, transport) -> int:
    names = gate_names(args.namespace)
    package = resources.files("githubbot_core")
    workflow = package.joinpath("templates/review-gate.yml").read_text(encoding="utf-8")
    files = {
        ".github/workflows/review-gate.yml": workflow.replace("__GATE_NAMESPACE__", args.namespace),
        ".github/review-gate/review_gate.py": package.joinpath("review_gate.py").read_text(encoding="utf-8"),
    }
    dest = Path(args.dest)
    existing = [rel for rel in files if (dest / rel).exists()]
    data = {"dest": str(dest), "namespace": args.namespace, "names": names, "files": sorted(files),
            "existing": existing}
    if not args.apply:
        _emit(args, "gate-scaffold", True, "preview", data, [],
              "would write:\n  " + "\n  ".join(sorted(files)) + "\nnothing written; rerun with --apply")
        return EXIT_OK
    if existing:
        _emit(args, "gate-scaffold", False, "apply", data, [f"refusing to overwrite {', '.join(existing)}"])
        return EXIT_USAGE
    for rel, content in files.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
    _emit(args, "gate-scaffold", True, "apply", data, [], "wrote:\n  " + "\n  ".join(sorted(files)))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="githubbot-core", description="Neutral GitHub maintenance core.")
    parser.add_argument("--config", action="append", default=[], help="extra config layer (JSON), repeatable")
    parser.add_argument("--json", action="store_true", help="print the JSON result envelope")
    parser.add_argument("--version", action="version", version=f"githubbot-core {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="version, contract and plugin overview").set_defaults(func=cmd_info)
    sub.add_parser("doctor", help="check gh CLI, credentials and permissions").set_defaults(func=cmd_doctor)
    sub.add_parser("plugins", help="list installed plugins").set_defaults(func=cmd_plugins)
    sub.add_parser("system-agent", help="versioned system agent template of this package").set_defaults(
        func=cmd_system_agent)

    p = sub.add_parser("classify", help="classify issues from a JSON file (offline)")
    p.add_argument("--input", required=True)
    p.set_defaults(func=cmd_classify)

    p = sub.add_parser("duplicates", help="find duplicate issues in a JSON file (offline)")
    p.add_argument("--input", required=True)
    p.add_argument("--threshold", type=float, default=0.8)
    p.set_defaults(func=cmd_duplicates)

    def add_apply_flags(p):
        p.add_argument("--apply", action="store_true", help="execute the plan (default: preview only)")
        p.add_argument("--receipts", help="JSON-lines receipt store; already applied actions are skipped")
        p.add_argument("--expect-plan", help="refuse to apply unless the plan_sha256 matches this preview")

    p = sub.add_parser("triage", help="classify open issues and plan labels/comments")
    p.add_argument("--repo", required=True)
    p.add_argument("--limit", type=int, default=50)
    add_apply_flags(p)
    p.set_defaults(func=cmd_triage)

    p = sub.add_parser("review-pr", help="summarise a pull request and run plugin policy checks")
    p.add_argument("--repo", required=True)
    p.add_argument("--pr", required=True, type=int)
    p.set_defaults(func=cmd_review_pr)

    for name, func in (("health", cmd_health), ("stars", cmd_stars), ("traffic", cmd_traffic)):
        p = sub.add_parser(name, help=f"read-only {name} report")
        p.add_argument("--repo", required=True, action="append", help="owner/name, repeatable")
        p.set_defaults(func=func)

    p = sub.add_parser("run", help="preview or apply the action plan of a plugin")
    p.add_argument("plugin")
    p.add_argument("--plugin-arg", dest="plugin_args", action="append", default=[],
                   help="argument passed to the plugin, repeatable")
    add_apply_flags(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("gate-scaffold", help="copy the review-gate workflow and verifier into a repository")
    p.add_argument("--dest", required=True, help="root of the target repository checkout")
    p.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_gate_scaffold)

    sub.add_parser("review-gate", help="signed exact-SHA receipts (see: review-gate --help)", add_help=False)
    return parser


def _subcommand_index(argv: list[str]) -> Optional[int]:
    i = 0
    while i < len(argv):
        if argv[i] == "--config":
            i += 2
        elif argv[i].startswith("-"):
            i += 1
        else:
            return i
    return None


def main(argv: Optional[list[str]] = None, transport=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    index = _subcommand_index(argv)
    if index is not None and argv[index] == "review-gate":
        from . import review_gate

        return review_gate.main(argv[index + 1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "json"):
        args.json = False
    try:
        config = load_config(args.config)
        return args.func(args, config, transport or GhCliTransport())
    except ConfigError as exc:
        _emit(args, args.command, False, "read", None, [f"config: {exc}"])
        return EXIT_USAGE
    except plugins.PluginNotAvailable as exc:
        _emit(args, args.command, False, "read", {"error": "plugin_not_available"}, [str(exc)])
        return EXIT_PLUGIN
    except (ReceiptValidationError, ValueError, OSError) as exc:
        _emit(args, args.command, False, "read", None, [str(exc)])
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
