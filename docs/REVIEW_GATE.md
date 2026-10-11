# Signed exact-SHA review gate

The review gate replaces a "required approving review" that a single maintainer identity cannot
satisfy with a **required commit status**. The status turns green only for a signed, time-limited
receipt bound to repository, pull-request number and the exact head SHA. A new commit produces a
new SHA and can never inherit an old receipt.

This document describes the mechanism. Applying it to a repository changes branch protection and
must be authorised by the repository owner; nothing in this package changes GitHub settings on its
own.

## Names and namespace

A deployment chooses one namespace (default `githubbot`, set with `--namespace`). Everything else
is derived from it:

| Name | Value |
|---|---|
| status context | `<namespace>/review-gate` |
| SSH signature namespace | `<namespace>-review-gate` |
| `repository_dispatch` event type | `<namespace>-review-gate` |
| principal for `model_review` | `<namespace>-reviewer` |
| principal for `owner_override` | `<namespace>-owner` |

The receipt schema `githubbot-core.review-gate-receipt/v1` does not depend on the namespace.

## Trust boundaries

The privileged workflow runs on `pull_request_target`, but only executes the verifier checked out
from the **default branch**. It does not check out the pull-request branch or a fork, installs no
pull-request dependencies and runs no pull-request code. This boundary is covered by a regression
test (`tests/test_review_gate.py`).

A receipt contains:

- `kind`: `model_review` or `owner_override`
- repository, PR number and the exact 40-character head SHA
- `approve` or `reject`, finding counts and the checks that were run
- SHA-256 of the detailed review artifact
- issue and expiry time (at most 24 hours)
- a random nonce
- for an owner override additionally `decision_id` and `reason`

The canonical JSON bytes are signed with OpenSSH (Ed25519 recommended). `model_review` accepts only
the reviewer principal, `owner_override` only the owner principal. A positive model review with a
critical or high finding is invalid.

The required-check configuration pins the status context to the GitHub Actions app
(ID `15368`), so a personal token cannot satisfy the gate by posting a status with the same name.
Anyone with write access to the default branch, the Actions variables, branch-protection
administration or a private signing key remains a high-trust person outside this gate.

## Key separation is mandatory

The reviewer key must not be reachable by the process that authored the pull request. If author
token and reviewer private key live in the same automation context, the signature is cosmetic and
the gate is **not** effective.

Use two separate keys; only the public keys go into an `allowed_signers` file:

```text
<namespace>-reviewer ssh-ed25519 AAAA... reviewer
<namespace>-owner ssh-ed25519 AAAA... owner
```

Store its base64 content as the repository variable `REVIEW_GATE_ALLOWED_SIGNERS_B64`. Private
keys, local paths, tokens and receipt artifacts are never committed.

## Installing the workflow

```bash
githubbot-core gate-scaffold --dest path/to/checkout --namespace <namespace>          # preview
githubbot-core gate-scaffold --dest path/to/checkout --namespace <namespace> --apply  # write
```

This writes `.github/workflows/review-gate.yml` and the standalone, standard-library-only verifier
`.github/review-gate/review_gate.py`. Existing files are never overwritten. Because the gate does
not exist on the default branch before this change is merged, the bootstrap merge needs a one-off,
explicitly authorised decision by the repository owner.

## Local flow

All commands are read-only towards GitHub unless `--apply` is given.

```bash
githubbot-core review-gate --namespace <ns> create --kind model_review --repo owner/repo --pr 6 \
  --head-sha <current-head-sha> --verdict approve --reviewer <reviewer-id> \
  --artifact review.md --check pytest:green --check ruff:clean --output receipt.json

githubbot-core review-gate --namespace <ns> sign --receipt receipt.json --key <reviewer-key> --output receipt.sig

# verify signature and live PR head, dispatch nothing
githubbot-core review-gate --namespace <ns> dispatch --receipt receipt.json --signature receipt.sig \
  --allowed-signers allowed_signers

# after authorisation: trigger repository_dispatch
githubbot-core review-gate --namespace <ns> dispatch --receipt receipt.json --signature receipt.sig \
  --allowed-signers allowed_signers --apply
```

An owner override uses `--kind owner_override --verdict approve --decision-id ... --reason ...` and
is never presented as an independent model review.

## Branch protection plan and rollback

`githubbot-core review-gate protection-plan --current current.json --output-dir plan/` turns the
current branch-protection GET response into two immutable files: `rollback.json` (restorable PUT
body of the current state) and `desired.json` (pull request still required, zero required
approvals, the gate as required status pinned to the Actions app with `strict: true`, enforced for
admins, no force pushes or deletions, conversation resolution on). The tool refuses to overwrite an
existing plan. Apply `desired.json` only after explicit authorisation; on any problem, restore
`rollback.json` first and read the protection back before touching the workflow.

## Acceptance checks

- A valid reviewer receipt for the current SHA sets exactly one green status.
- A signed `reject` sets exactly one red status.
- Wrong principal, invalid signature, expired receipt, other repository, other PR number or an old
  SHA never produce success.
- A new commit is back to pending.
- An explicitly signed owner override works and names decision and reason in the receipt.
- The workflow demonstrably never runs pull-request code with the privileged token.

## References

- [Events `pull_request_target` and `repository_dispatch`](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)
- [Update branch protection](https://docs.github.com/en/rest/branches/branch-protection#update-branch-protection)
- [Create a commit status](https://docs.github.com/en/rest/commits/statuses#create-a-commit-status)
