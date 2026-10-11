"""Layered configuration: packaged default -> profile -> local host config.

Ordinary keys are deep-merged, later layers win. Permission ceilings are
different: a later layer may only narrow them. A layer that tries to widen a
ceiling is rejected with :class:`ConfigError` instead of being applied
silently, and ``permissions.enabled`` must stay inside the final ceiling.
"""

from __future__ import annotations

import copy
import json
from importlib import resources
from pathlib import Path
from typing import Any, Iterable, Optional

# Dotted keys whose value is a list that later layers may only narrow.
NARROW_ONLY = ("permissions.ceiling", "permissions.api_write_prefixes")


class ConfigError(ValueError):
    """Invalid or unsafe configuration."""


def default_config() -> dict:
    text = resources.files("githubbot_core").joinpath("default_config.json").read_text(encoding="utf-8")
    return json.loads(text)


def _get(data: dict, dotted: str) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _set(data: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _deep_merge(base: dict, layer: dict) -> dict:
    for key, value in layer.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def merge_layers(layers: Iterable[tuple[str, dict]]) -> dict:
    """Merge ``(name, data)`` layers in order. The first layer is the base."""
    merged: dict = {}
    narrowed: dict[str, Optional[list]] = {key: None for key in NARROW_ONLY}
    for name, layer in layers:
        if not isinstance(layer, dict):
            raise ConfigError(f"{name}: configuration must be a JSON object")
        for key in NARROW_ONLY:
            value = _get(layer, key)
            if value is None:
                continue
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ConfigError(f"{name}: {key} must be a list of strings")
            current = narrowed[key]
            if current is not None:
                widened = [v for v in value if v not in current]
                if widened:
                    raise ConfigError(f"{name}: {key} may only be narrowed, not widened by {widened}")
            narrowed[key] = list(value)
        _deep_merge(merged, layer)
    for key, value in narrowed.items():
        if value is not None:
            _set(merged, key, value)
    validate(merged)
    return merged


def validate(config: dict) -> None:
    ceiling = _get(config, "permissions.ceiling") or []
    enabled = _get(config, "permissions.enabled") or []
    outside = [kind for kind in enabled if kind not in ceiling]
    if outside:
        raise ConfigError(f"permissions.enabled exceeds permissions.ceiling: {outside}")


def load_config(paths: Iterable[Path] = ()) -> dict:
    layers: list[tuple[str, dict]] = [("default", default_config())]
    for path in paths:
        path = Path(path)
        try:
            layers.append((str(path), json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"{path}: cannot read configuration ({exc})") from exc
    return merge_layers(layers)
