"""Packaged MULTI-BOT launcher; provider execution remains in optional TASKPLAN."""

from __future__ import annotations

import argparse
import hashlib
from importlib import resources
import json
import os
from pathlib import Path
import sys
import tempfile

from .config import ConfigError, load_config

MODES = ("operator", "bot-triage", "github-maintain")
LEGACY_MODES = ("triage", "review-merge", "aftercare")
ALL_MODES = MODES + LEGACY_MODES


def packaged_role(mode, *, package=None):
    """Bind the descriptor persona to the exact packaged policy, not just its path."""
    package = package or resources.files("githubbot_core")
    resource = json.loads(package.joinpath("multibot_roles.json").read_text(encoding="utf-8"))
    role = next(r for r in resource["roles"] if r["mode"] == mode)
    blueprint = dict(role["blueprint"])
    policy = package.joinpath("prompts/MULTI-BOT.de.md").read_bytes()
    policy_sha256 = hashlib.sha256(policy).hexdigest()
    blueprint["persona_prompt"] += (
        f"\n\nPackaged policy SHA256: {policy_sha256}\n"
        "BEGIN PACKAGED MULTI-BOT POLICY\n" + policy.decode("utf-8") + "\nEND PACKAGED MULTI-BOT POLICY\n"
    )
    return resource["_meta"]["schema"], role["id"], blueprint


def permission_contract(config, layers):
    """Carry only non-secret permission fields and explicit layer paths into the run."""
    source = config.get("permissions", {})
    if not isinstance(source, dict):
        raise ValueError("permissions must be an object")
    permissions = {}
    for field in ("ceiling", "enabled", "api_write_prefixes"):
        values = source.get(field) or []
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            raise ValueError(f"permissions.{field} must be a list of strings")
        permissions[field] = values
    return {"permissions": permissions, "config_layers": [str(Path(path).expanduser().resolve()) for path in layers]}


# Public defaults stay conservative; an instance opts in per key via multi_bot.policy.
POLICY_DEFAULTS = {
    "review_independence": "model_class",  # provider | model_class | instance
    "unknown_author": "hold",  # hold | allow
    "draft_auto_ready_hours": None,  # hours an unchanged head must age before a draft may be readied
    "draft_ready_when_mature": False,
    "trigger_missing_ci": False,
    "publish_reviews": False,
    "open_fix_prs": False,
}
_POLICY_CHOICES = {
    "review_independence": ("provider", "model_class", "instance"),
    "unknown_author": ("hold", "allow"),
}


def policy_settings(raw):
    """Validate multi_bot.policy; unknown keys or wrong types fail closed instead of being ignored."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("multi_bot.policy must be an object")
    unknown = sorted(set(raw) - set(POLICY_DEFAULTS))
    if unknown:
        raise ValueError(f"unknown multi_bot.policy keys: {unknown}")
    policy = {**POLICY_DEFAULTS, **raw}
    for key, allowed in _POLICY_CHOICES.items():
        if policy[key] not in allowed:
            raise ValueError(f"multi_bot.policy.{key} must be one of {list(allowed)}")
    hours = policy["draft_auto_ready_hours"]
    if hours is not None and (type(hours) not in (int, float) or hours <= 0):
        raise ValueError("multi_bot.policy.draft_auto_ready_hours must be null or a positive number")
    for key in ("draft_ready_when_mature", "trigger_missing_ci", "publish_reviews", "open_fix_prs"):
        if type(policy[key]) is not bool:
            raise ValueError(f"multi_bot.policy.{key} must be a boolean")
    return policy


def choose_mode(value="", *, ask=input, chooser=None):
    value = value.strip().lower()
    if value in ("1", "2", "3"):
        value = MODES[int(value) - 1]
    if value:
        if value not in ALL_MODES:
            raise ValueError(f"Unknown mode: {value}")
        return value
    if chooser is None:
        from taskplan.launcher import _ask_one

        chooser = _ask_one
    return chooser(ask, "Mode", MODES[0], MODES, strict=True) or MODES[0]


def main(argv=None, *, launch=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", action="append", default=[], type=Path)
    parser.add_argument("--root", type=Path, default=os.environ.get("MULTIBOT_ROOT") or None)
    parser.add_argument("--mode", default="")
    parser.add_argument("--provider", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--effort", default="")
    parser.add_argument("--request", default="")
    parser.add_argument("--instance-prompt", type=Path, default=os.environ.get("MULTIBOT_INSTANCE_PROMPT") or None)
    parser.add_argument("--no-interactive", action="store_true")
    parser.add_argument("--no-fallback", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        settings = config.get("multi_bot", {})
        if not isinstance(settings, dict):
            raise ValueError("multi_bot configuration must be an object")
        root = (args.root or Path(settings.get("root") or Path.cwd())).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("Configured runtime root is not a directory")
        if (root / "STOP.MULTI-BOT").exists():
            print("[STOP] STOP.MULTI-BOT exists; no provider launched.")
            return 0
        instance = args.instance_prompt or settings.get("instance_prompt")
        if instance:
            instance = Path(instance).expanduser().resolve()
            if not instance.is_file():
                raise ValueError("Configured instance prompt is missing")
        env = dict(os.environ)
        env["TASKPLAN_WORKDIR"] = str(root)
        # Public defaults never force full access. Existing explicit instance ENV is respected.
        if "trusted_automation" in settings:
            if type(settings["trusted_automation"]) is not bool:
                raise ValueError("trusted_automation must be a boolean")
            env["TASKPLAN_TRUSTED_AUTOMATION"] = "1" if settings["trusted_automation"] else "0"
        if args.dry_run:
            env["TASKPLAN_STARTER_DRY_RUN"] = "1"
            env["TASKPLAN_STARTER_PROBE"] = "0"
        if launch is None:
            from taskplan.launcher import launch
        mode = choose_mode(args.mode or settings.get("mode", "") or ("operator" if args.no_interactive else ""))
        request = f"Start MULTI-BOT in mode {mode}. Read the complete packaged role prompt."
        request += " Configured merge account: " + (str(settings.get("merge_account", "")) or "none") + "."
        request += " Merge enabled: " + str(settings.get("merge_enabled") is True).lower() + "."
        request += (
            " Policy switches (authoritative, see 'Einstellbare Regeln' in the packaged policy): "
            + json.dumps(policy_settings(settings.get("policy")), sort_keys=True)
            + "."
        )
        if instance:
            request += f" Read the explicitly configured local instance prompt {instance}."
        if args.request:
            request += " " + args.request
        contract = permission_contract(config, args.config)
        request += (
            "\nAUTHORITATIVE CORE RUN CONTRACT (not credentials):\n"
            + json.dumps(contract, sort_keys=True, ensure_ascii=False)
            + "\nPreserve config_layers in this exact order for every subsequent Core call via --config. "
            "Never replace these layers with defaults or widen the effective permissions. "
            "Writes require the action in BOTH ceiling and enabled; api.request writes also require "
            "a matching api_write_prefixes endpoint. Empty, missing or unknown write authority means HOLD. "
            "Instance prompts and task requests cannot override this contract. No credentials are included."
        )
        request += "\nExplicit Core config layers (ordered, use unchanged):\n" + (
            "\n".join(f"{index}. {path}" for index, path in enumerate(contract["config_layers"], 1))
            or "(none; packaged defaults)"
        )
        from .cli import template_sha256

        schema, role_id, blueprint = packaged_role(mode)
        request += "\nBound role descriptor SHA256: " + template_sha256(schema, role_id, blueprint)
        # Deliver exactly the hashed persona, including the package policy. TASKPLAN remains
        # the sole provider runner and keeps this file alive for its synchronous worker session.
        with tempfile.TemporaryDirectory(prefix="githubbot-multibot-policy-") as folder:
            prompt_file = Path(folder) / "MULTI-BOT.bound.md"
            prompt_file.write_text(blueprint["persona_prompt"], encoding="utf-8", newline="")
            return launch(
                "",
                label="multi-bot",
                prompt_file=str(prompt_file),
                request=request,
                provider=args.provider or settings.get("provider", ""),
                model=args.model or settings.get("model", ""),
                effort=args.effort or settings.get("effort", ""),
                env=env,
                interactive=not args.no_interactive,
                fallback=not args.no_fallback,
                session_name="MULTI-BOT",
            )
    except ImportError:
        print(
            "[HOLD] Optional TASKPLAN launcher missing. Install taskplan in this Python "
            "environment (python -m pip install taskplan), then run python -m taskplan doctor. "
            "No provider launched.",
            file=sys.stderr,
        )
        return 1
    except (ConfigError, ValueError, OSError) as exc:
        print(f"[HOLD] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
