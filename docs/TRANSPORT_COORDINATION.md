# Host-local transport coordination

## Scope and authority

This is a cooperative, single-host implementation, not a distributed lock. All
participating processes must run as the same OS user and use the same local state
directory. The production default is the OS-local application state directory;
explicit `Coordinator(root=...)` injection is for trusted embedding and isolated
tests. Different roots, users, hosts, unwrapped gh/Git clients and custom transports
are outside the guarantee. Do not place this state on a cloud-synced/network
filesystem. No service, daemon, normative registry or credential is created.
A deployed instance still needs an explicit multi-host execution decision. Until
then this capability must not be advertised as solving multi-host concurrency.

`github-com-actions.lock` serializes whole applied actions. The transport lock
serializes individual gh requests and the typed Git push/readback pair. Locks
use persistent inodes and OS advisory locking (Windows/POSIX), with bounded
acquisition and automatic kernel release after a process dies. Never unlink a
lock inode to recover. Ordering is actions -> transport -> rate-state; each rate-state read/check/write
transaction acquires its own innermost lock, including action preflight outside
the transport lock. The state lock is released before any later transport call
and never acquires an outer lock. This prevents a stale preflight snapshot from
overwriting a concurrent policy/rate HOLD. A state-lock failure after a request
was attempted is reported as unconfirmed, never as nothing sent. In particular, an enclosing wrapper
must not hold the transport lock while waiting for a Core child.

## Cooldowns and policy HOLD

The shared GitHub.com state spans accounts, so choosing another account does not
bypass a cooldown. Requests are never automatically retried. Before a mutation,
a one-second interval is required; a caller arriving too early receives HOLD
rather than a sleeping/retrying mutation. Reads can continue during ordinary
mutation spacing, but all requests stop during a rate cooldown or policy HOLD.
`gh api --include` provides response headers; pagination is performed page by
page, with validated GitHub API links and no partial result on failure. Body,
error and an immutable header snapshot belong to one response; a concurrent
read on the same transport cannot replace another response's pagination link.

HTTP 429, remaining=0 on 403, or recognized CLI rate/secondary/abuse errors record
a persistent cooldown. Retry-After (seconds or HTTP date) and reset epoch are
respected with at least 60 seconds of conservative waiting. Invalid rate metadata
and non-rate 403 become persistent operator HOLD. This is not proof that every
403 is a rate error. Response bodies are not searched for rate keywords. Raw
errors/tokens are not persisted by the coordinator. Unbound metadata errors do
not trigger account fallback. There is no automatic account switch in Core.
An expired cooldown permits a new explicitly requested operation; it does not
replay a previous write. No automatic policy-HOLD reset is provided in this unit.

## Durable request uncertainty

Before **every supported request**, including GET and typed Git push/readback,
Core persists and reads back one shared request-inflight marker while holding
transport -> rate-state locks. If the marker cannot be durably written, nothing
is sent. The marker contains a random request ID and fixed schema/status only,
never a token, URL or body. It has no automatic expiry.

Only completed transport status/header handling and confirmed rate-state
persistence permit marker removal. A timeout, process death, malformed transport
response, failed HOLD persistence or failed marker cleanup leaves uncertainty
HOLD across process restarts. A fresh process must not send even a GET merely
because the original OS lock has become available. Ordinary action preflight and
Git use the same marker check. This takes precedence over automatic action
readback; application-level readback remains possible only when transport state
is confirmed. Parsed application/body errors still use the action journal.

Recovery is explicit and offline: after operator inspection, trusted embedding
may call `Coordinator.settle_request_as_policy_hold(exact_request_id)`. Under
transport -> rate-state locks this persists and verifies policy HOLD before
consuming that exact marker. It performs no network request and does **not**
permit further requests. A wrong ID, failed write or unreadable state preserves
HOLD; no automatic deletion, TTL reset or fallback account is used. Releasing a
policy HOLD requires a separate reviewed operator decision; this unit provides
no automatic unblock API. If storage remains unwritable, requests remain blocked,
and the storage/authority problem must be repaired before recovery can complete.
This is a durability prerequisite, not a claim that recovery is impossible.

## Durable unknown-write journal

`actions.apply` preserves permission, actor, canonical target, head, review and
expected-plan gates. Authority is rechecked inside the actions lock. Immediately
before an effect can be submitted, a local atomic/fsync journal records only
hashed action ID, authority-bound plan digest and `inflight`. No body, token or
repository name is stored there. Success persists `applied`; transport failure,
readback failure or executor exception after submission remains `unknown`.
A crash may leave `inflight`. Both states prohibit another write, even without
an optional JSONL receipt store. Failure before any possible effect creates no
unknown journal. The optional JSONL receipt remains an output, not the durable
source of truth for unknown effects.

A subsequent call with the exact authority-bound plan can only reconcile by
reads. Reviews require one exact account/head/event/body match plus the unchanged
open PR head. Merges require the exact original head, merged state and a valid
merge commit SHA. This confirms desired state, not attribution of the effect to
this process. All other effects (comments, labels, dispatch, ready, close, PR
creation, raw API and Git pushes) remain HOLD when unknown. No generic reset or
blind retry is offered: unsupported recovery needs a separately reviewed,
action-specific readback. A changed plan/scope/actor cannot reuse an unknown
intent. Applied action IDs are durably deduplicated across invocations.

Journal or rate-state corruption, links, a backward wall clock and lock timeout
fail closed. State files must be protected by the embedding OS account. This is
not a security boundary against a malicious process with that account's write
rights. Persistent media loss/deletion or a different state root voids durability.
POSIX directory fsync is used; Windows uses file fsync plus atomic replace and
does not claim power-loss guarantees beyond the OS/filesystem contract.

## Verification

Tests use synthetic data and subprocesses, never GitHub credentials or network
writes. They cover overlapping clients, duplicate actions, process death after
an effect, durable unknown HOLD, exact/ambiguous reconciliation, rate headers,
expiry, mutation spacing, pagination failure and damaged state. Existing typed
semantic gates remain tested; their sandbox state is isolated per test.

## Deutsch: Betriebsgrenzen

Die Umsetzung koordiniert kooperierende Prozesse desselben OS-Benutzers auf
einem Host mit gemeinsamem lokalem Zustand. Sie bietet keine Mehrhostgarantie.
Dateisperren werden niemals zur Wiederherstellung gelöscht. Bei unbestätigtem
Schreibausgang oder fehlgeschlagener HOLD-Speicherung gilt dauerhaft HOLD; ein Neustart ist keine Freigabe. Nur die
beschriebenen eindeutigen Review-/Merge-Leseprüfungen können den Zustand als
bestätigt kennzeichnen. Für andere Aktionen bleibt eine gesonderte geprüfte
Wiederaufnahme erforderlich. Rate-HOLD erlaubt keinen Kontowechsel zur Umgehung.
Mehrhostbetrieb, privater Wrapper-Anschluss und Runtime-Rollout bleiben getrennte
Aufgaben. Diese Sourceänderung installiert oder veröffentlicht nichts.
