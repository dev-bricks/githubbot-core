"""Packaged MULTI-BOT launcher; provider execution remains in optional TASKPLAN."""

from __future__ import annotations

import argparse
from importlib import resources
import os
from pathlib import Path
import sys

from .config import ConfigError, load_config

MODES = ("triage", "review-merge", "aftercare")


def choose_mode(value="", *, ask=input, chooser=None):
    value = value.strip().lower()
    if value in ("1", "2", "3"):
        value = MODES[int(value) - 1]
    if value:
        if value not in MODES:
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
        mode = choose_mode(args.mode or settings.get("mode", "") or ("triage" if args.no_interactive else ""))
        request = f"Start MULTI-BOT in mode {mode}. Read the complete packaged role prompt."
        request += " Configured merge account: " + (str(settings.get("merge_account", "")) or "none") + "."
        request += " Merge enabled: " + str(settings.get("merge_enabled") is True).lower() + "."
        if instance:
            request += f" Read the explicitly configured local instance prompt {instance}."
        if args.request:
            request += " " + args.request
        prompt = resources.files("githubbot_core").joinpath("prompts/MULTI-BOT.de.md")
        with resources.as_file(prompt) as prompt_file:
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
