# githubbot-core

<p align="center"><img src="assets/banner.png" width="100%" alt="githubbot-core banner"></p>

**English** | [Deutsch](README_de.md)

Neutral, installable core for GitHub repository maintenance: issue triage, duplicate detection,
pull-request summaries, read-only health/traffic/star reports, a preview/apply contract for every
write, a plugin SPI and signed exact-SHA review receipts. Pure Python standard library, no runtime
dependencies.

> Status: alpha (0.2.2). The CLI, the JSON result envelope and SPI version 1 are the contract;
> everything else may still change.

## Features

- **Triage** – classifies open issues with configurable keyword criteria and plans labels and
  auto comments (with an invisible marker so a comment is never posted twice).
- **Duplicates** – Jaccard similarity on word tokens, offline on a JSON file.
- **Pull-request summary** – size check and summary; project-specific policy checks come from
  plugins only.
- **Reports** – Dependabot / Actions / issues / PRs / code- and secret-scanning health, traffic
  (14 days) and star rankings. Read-only.
- **Preview / apply** – nothing is written unless you pass `--apply`. Every write is an `Action`,
  checked against a permission ceiling, and leaves a receipt. With a receipt store, repeated runs
  skip actions that were already applied.
- **Plugin SPI** – extensions propose actions or add pull-request checks through an entry point.
  A missing extension is reported as "not available"; the core keeps working.
- **Credential adapter** – tokens come from `GH_TOKEN` / `GITHUB_TOKEN` or `gh auth token` at
  runtime. Nothing is stored or bundled.
- **Review gate** – signed (OpenSSH), time-limited receipts bound to repository, PR number and the
  exact head SHA, verified by a GitHub Actions workflow that never executes pull-request code.

## Installation

Requirements: Python 3.10+, the [GitHub CLI](https://cli.github.com/) (`gh auth login`) for
commands that talk to GitHub, OpenSSH `ssh-keygen` for review-gate signatures.

```bash
pip install git+https://github.com/dev-bricks/githubbot-core.git
githubbot-core --version
githubbot-core doctor
```

From a checkout: `pip install .` (development: `pip install -e ".[dev]"`).

## Usage

```bash
# Read-only reports
githubbot-core health --repo owner/repo --repo owner/other
githubbot-core traffic --repo owner/repo
githubbot-core stars --repo owner/repo

# Triage: preview (default) -> apply the very same plan
githubbot-core triage --repo owner/repo
githubbot-core --json triage --repo owner/repo            # note plan_sha256
githubbot-core triage --repo owner/repo --apply --expect-plan <plan_sha256> --receipts receipts.jsonl

# Offline helpers (JSON file in gh or REST issue format)
githubbot-core classify --input issues.json
githubbot-core duplicates --input issues.json --threshold 0.8

# Pull-request summary (+ checks from installed plugins)
githubbot-core review-pr --repo owner/repo --pr 12

# Plugins and system agent template
githubbot-core plugins
githubbot-core --json system-agent
githubbot-core run <plugin> [--plugin-arg ...] [--apply]
```

`python -m githubbot_core ...` works the same way.

### JSON contract

With `--json` every command prints one envelope:

```json
{"schema": "githubbot-core.result/v1", "command": "triage", "ok": true,
 "mode": "preview", "data": {"plan_sha256": "…", "actions": [], "writes": 0}, "errors": []}
```

`mode` is `read`, `preview` or `apply`. Exit codes: `0` ok, `1` runtime error (for example GitHub
not reachable or a write failed), `2` configuration or usage error, `3` plugin not available.

The Python API is the package itself: `githubbot_core.triage.IssueTriage`,
`githubbot_core.actions.preview/apply`, `githubbot_core.health.RepoHealthCollector`,
`githubbot_core.plugins`, `githubbot_core.review_gate`, `githubbot_core.credentials`.

### System agent template

`githubbot-core --json system-agent` prints the neutral agent template shipped with the package
(schema `githubbot-core.system-agent/v1`): `version` (package version), `sha256` over the canonical JSON of
`{schema, id, blueprint}` (sorted keys, compact separators, UTF-8; verifiable from the printed output), `capability`, the effective `permissions` and a `blueprint` with the field names of the
agent-studio blueprint payload (persona, role, contractus, governance, skills). The template
exists only where the package is installed. Consumers bind an agent to `version` + `sha256` and must
not overwrite an existing agent configuration when a newer template appears.

## Configuration and permissions

The packaged defaults (`githubbot_core/default_config.json`) are extended with `--config file.json`
layers, applied in order (for example a profile, then a host-local file). Ordinary keys are merged,
later layers win. Permission ceilings are different: a later layer may only **narrow**
`permissions.ceiling` and `permissions.api_write_prefixes`; an attempt to widen them is an error,
not a silent override. `permissions.enabled` must stay inside the ceiling.

| Action kind | Default | Executes |
|---|---|---|
| `issue.add_labels` | enabled | `POST /repos/{repo}/issues/{n}/labels` |
| `issue.comment` | enabled | `POST /repos/{repo}/issues/{n}/comments` |
| `api.request` | disabled | any write method on an endpoint that starts with one of `permissions.api_write_prefixes` |
| `pr.review` | disabled | verifies bot identity, open non-draft PR and exact head; `POST …/pulls/{n}/reviews` with `commit_id` and review event, then reads the formal review back; also requires `publish_reviews` and a configured enabled organization in `<runtime-root>/config/orgs.json` (`multi_bot.root`, default: current directory); global and organization `exclude_writes` are enforced, and a missing or invalid policy denies the review |
| `pr.merge` | disabled | re-reads the PR (open, exact `sha`, default base, not draft, `mergeable_state` clean, configured merge account), `PUT …/pulls/{n}/merge` with `sha`, reads `merged` back |
| `pr.ready` | disabled | re-reads the draft at the exact `sha`, GraphQL `markPullRequestReadyForReview`, reads back |
| `actions.dispatch` | disabled | `POST …/actions/workflows/{workflow}/dispatches` for an active workflow |
| `actions.rerun` | disabled | `POST …/actions/runs/{id}/rerun` for a completed, non-fork run of the exact `sha` |
| `pr.create` | disabled | `POST …/pulls` from a bot-prefixed branch onto the default branch |
| `git.push_branch` | disabled | `git push origin <sha>:refs/heads/<branch>` without force, bot prefix only (`typed_actions.push_branch_prefix`, default `bot/`), never the default branch, verified with `ls-remote` |

The seven typed actions sit in the default ceiling but are never enabled by default. Each one also
needs its `multi_bot` switch (`merge_enabled`, `policy.publish_reviews`, `policy.draft_*`,
`policy.trigger_missing_ci`, `policy.open_fix_prs`); a permission alone opens nothing. One action at a time:

```bash
githubbot-core --config instance.json act pr.merge --params '{"repo": "o/r", "number": 7, "sha": "<40-hex>"}'
githubbot-core --config instance.json act pr.merge --params '…' --apply --expect-plan <plan_sha256>
```

```json
{"permissions": {"enabled": ["issue.add_labels", "issue.comment", "api.request"],
                 "api_write_prefixes": ["user/starred/"]}}
```

## Writing a plugin (SPI v1)

Declare an entry point in the group `githubbot_core.plugins` that points to a zero-argument
factory:

```toml
[project.entry-points."githubbot_core.plugins"]
my-extension = "my_extension:create"
```

```python
from githubbot_core.actions import Action
from githubbot_core.plugins import SPI_VERSION


class MyExtension:
    name = "my-extension"
    spi_version = SPI_VERSION

    def describe(self):
        return {"version": "1.0.0", "capabilities": ["plan"]}

    def plan(self, ctx):  # ctx.config, ctx.transport (read-only), ctx.args
        data, error = ctx.transport.get("repos/owner/repo")
        return [Action("api.request", {"method": "PUT", "endpoint": "user/starred/owner/repo"}, reason="example")]

    def pr_policy_checks(self, pr, files, diff):  # optional
        return [("no_large_binaries", True, "OK")]


def create():
    return MyExtension()
```

Plugins never write themselves: the core previews or applies their plan under the configured
permissions. During planning they only get a read-only transport.

## Review gate

`githubbot-core gate-scaffold --dest <checkout> [--namespace name]` previews, and with `--apply`
writes, `.github/workflows/review-gate.yml` plus the standalone verifier
`.github/review-gate/review_gate.py` into a target repository. Receipts are created, signed,
verified and dispatched with `githubbot-core review-gate create|sign|verify|dispatch`. Details,
trust boundaries and rollback: [docs/REVIEW_GATE.md](docs/REVIEW_GATE.md).

## Development

```bash
pip install -e ".[dev]"
python -m pytest            # unit and contract tests
ruff check .
ruff format --check .
GITHUBBOT_CORE_CONSUMER_TEST=1 python -m pytest tests/test_consumer_install.py   # fresh-venv install test
```

## Provenance

Written by Lukas Geiger with AI assistance (code generation and review by large language models),
extracted from a private maintenance tool and rebuilt as a new tree without that tool's history.
No third-party code is included; see [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).

## License

[MIT](LICENSE)


## Optional MULTI-BOT role

`python -m githubbot_core multi-bot` delegates mode/provider/model/effort selection to the
optional TASKPLAN launcher. The visible menu is **1 operator** (default), **2 bot-triage** (PR and Issue triage, independent PR reviews), and **3 github-maintain** (fixes and maintenance). Operator handles fresh PRs, then Issues, then fixes, policies/gates/privacy and care. A selector call chooses one candidate; the agent continues through the requested scope using refreshed lists. Preview alone is not completion. Merges remain opt-in through `effective_enabled` and all exact-head review/CI/identity/branch-protection gates. Initial policy preference and evidenced policy/care alternation are a documented heuristic; urgent policies precede routine care. Missing TASKPLAN produces an installation hint
and starts no provider. `--dry-run` enables TASKPLAN's no-probe dry run.

Wheels and sdists ship `githubbot_core/prompts/MULTI-BOT.de.md`, neutral starters under
`githubbot_core/starters/windows` and `starters/posix`, and the care/probe helper modules.
`githubbot-core --json multi-bot-role --mode operator` exports one
role using the existing strict descriptor schema. `system-agent` remains unchanged;
role definitions live in the separate packaged `multibot_roles.json` resource. Runtime root defaults to the current directory. Merge is
disabled and full access is not forced by public defaults. Explicit `--config` layers may
set `multi_bot.root`, `instance_prompt`, `merge_account`, `merge_enabled`, and
`trusted_automation`. The configured action permission ceiling remains binding.

The starter never searches hidden credential paths. Accounts, authentication, slots and
host roots belong to private instance configuration. `multi-bot-care --root PATH next`
and `multi-bot-probe --root PATH` require explicit registry/org/host-log data and fail
closed when absent. Private runtime/extensions remain separate consumers; no background
service or executable automatic merge is installed.

Named legacy flags `triage`, `review-merge`, and `aftercare` retain their original limited behavior and are absent from the new three-choice menu. Choosing a role never enables merges or widens permissions.

`githubbot-core --json multi-bot-select --input SNAPSHOT.json --mode operator` makes a deterministic read-only decision from an existing snapshot. It creates no queue, work item, persistent state, or agent. The snapshot contains `candidates`, optional `processed` and `lastMaintenance`. Candidates require `id`, an exact lowercase 40-character commit `revision`, `status` (`ready`, `blocked`, `done`), `category` (`prs`, `issues`, `fixes`, `policies`, `care`), and optional boolean `urgent`. Operator prioritizes PRs, Issues, then fixes; `bot-triage` handles PRs and Issues, while `github-maintain` handles fixes and maintenance. Processed records carry the same identity/category, blocked/done status and a nonempty `evidence` reference; the caller verifies the actual evidence. `lastMaintenance` points via `{id, revision}` to an evidenced successful policy/care completion. Selection and repeated empty checks never advance it. Unknown values are rejected. Exact processed heads are skipped until new evidence; equal-priority candidates sort by ID and SHA.

The neutral ASCII Windows starter is titled MULTI-BOT COMPANION, explains foreground operation and pauses after success, failure or STOP. Only explicit `--dry-run`, `TASKPLAN_STARTER_DRY_RUN=1` or `MULTIBOT_NO_PAUSE=1` skips the pause. It installs nothing and opens no extra window.
