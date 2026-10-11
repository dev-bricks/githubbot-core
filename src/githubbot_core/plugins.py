"""Plugin SPI (service provider interface), version 1.

A plugin is any installed package that declares an entry point in the group
``githubbot_core.plugins``. The entry point names a zero-argument factory that
returns an object with:

* ``name`` (str) and ``spi_version`` (int, must equal :data:`SPI_VERSION`)
* ``describe() -> dict``: at least ``version`` and ``capabilities``
* optional ``plan(ctx: PluginContext) -> list[Action]``: propose writes.
  Plugins never write themselves; the core previews or applies the plan under
  its permission ceiling. During planning the plugin gets a read-only transport.
* optional ``pr_policy_checks(pr, files, diff) -> list[(name, passed, detail)]``:
  extra pull-request checks added to ``review-pr``.

A missing, unreadable or incompatible plugin is reported as "not available";
the core keeps working without it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import metadata
from typing import Any, Callable, Optional

from .actions import Action
from .transport import ReadOnlyTransport

SPI_VERSION = 1
ENTRY_POINT_GROUP = "githubbot_core.plugins"

_REGISTERED: dict[str, Callable[[], Any]] = {}


class PluginNotAvailable(LookupError):
    """The requested plugin is not installed, not accessible or incompatible."""


@dataclass
class PluginContext:
    config: dict
    transport: ReadOnlyTransport
    args: list[str] = field(default_factory=list)


def register(name: str, factory: Callable[[], Any]) -> None:
    """Register a plugin factory in-process (embedding applications, tests)."""
    _REGISTERED[name] = factory


def unregister(name: str) -> None:
    _REGISTERED.pop(name, None)


def _factories() -> dict[str, Callable[[], Any]]:
    found: dict[str, Callable[[], Any]] = {}
    for ep in metadata.entry_points(group=ENTRY_POINT_GROUP):
        found[ep.name] = ep.load
    found.update({name: (lambda f=f: f) for name, f in _REGISTERED.items()})
    return found


def _instantiate(name: str, loader: Callable[[], Any]) -> Any:
    try:
        factory = loader()
        plugin = factory()
    except Exception as exc:  # import errors, missing private access, broken factories
        raise PluginNotAvailable(f"plugin {name!r} is not available: {type(exc).__name__}: {exc}") from None
    if getattr(plugin, "spi_version", None) != SPI_VERSION:
        raise PluginNotAvailable(
            f"plugin {name!r} is not available: SPI version {getattr(plugin, 'spi_version', None)!r}, "
            f"core expects {SPI_VERSION}"
        )
    return plugin


def load(name: str) -> Any:
    loader = _factories().get(name)
    if loader is None:
        raise PluginNotAvailable(f"plugin {name!r} is not installed or not accessible")
    return _instantiate(name, loader)


def list_plugins() -> list[dict]:
    result = []
    for name, loader in sorted(_factories().items()):
        try:
            plugin = _instantiate(name, loader)
            info = dict(plugin.describe())
            result.append(
                {
                    "name": name,
                    "available": True,
                    **info,
                    "hooks": [h for h in ("plan", "pr_policy_checks") if callable(getattr(plugin, h, None))],
                }
            )
        except PluginNotAvailable as exc:
            result.append({"name": name, "available": False, "error": str(exc)})
    return result


def available_plugins() -> list[Any]:
    plugins = []
    for name, loader in sorted(_factories().items()):
        try:
            plugins.append(_instantiate(name, loader))
        except PluginNotAvailable:
            continue
    return plugins


def run_plan(name: str, config: dict, transport, args: Optional[list[str]] = None) -> list:
    plugin = load(name)
    plan = getattr(plugin, "plan", None)
    if not callable(plan):
        raise PluginNotAvailable(f"plugin {name!r} does not provide a plan hook")
    actions = list(plan(PluginContext(config=config, transport=ReadOnlyTransport(transport), args=list(args or []))))
    for action in actions:
        if not isinstance(action, Action):
            raise TypeError(f"plugin {name!r} returned {type(action).__name__}, expected Action")
    return [Action(a.kind, a.params, a.reason, source=name) for a in actions]
