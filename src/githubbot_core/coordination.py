"""Host-local, cooperative coordination. Never a distributed lock or a retry engine."""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
import tempfile
from contextlib import contextmanager
from pathlib import Path

MUTATION_INTERVAL = 1.0


class Hold(RuntimeError):
    """No network effect is permitted until the reported condition is resolved."""


def default_root() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            raise Hold("coordination local state directory unavailable")
        return Path(base) / "githubbot-core" / "coordination"
    return Path.home() / ".local" / "state" / "githubbot-core" / "coordination"


def _regular(path: Path) -> None:
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise Hold("coordination links are not permitted")


class Coordinator:
    def __init__(self, root=None, *, clock=time.time, timeout=5.0):
        self.root = Path(root) if root is not None else default_root()
        self.clock, self.timeout = clock, timeout

    def prepare(self):
        if not self.root.is_absolute() or str(self.root).startswith(("\\\\", "//")):
            raise Hold("coordination directory must be absolute and host-local")
        for part in (self.root, *self.root.parents):
            _regular(part)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextmanager
    def lock(self, name):
        self.prepare()
        path = self.root / (name + ".lock")
        _regular(path)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "r+b", buffering=0) as f:
            if os.fstat(f.fileno()).st_size == 0:
                f.write(b"0")
            deadline = time.monotonic() + self.timeout
            acquired = False
            try:
                while not acquired:
                    try:
                        if os.name == "nt":
                            import msvcrt

                            f.seek(0)
                            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise Hold("coordination busy; no request sent") from None
                        time.sleep(0.02)
                yield
            finally:
                if acquired:
                    if os.name == "nt":
                        import msvcrt

                        f.seek(0)
                        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        # Never unlink a lock inode: waiters may still hold it open.

    def load(self, name):
        path = self.root / (name + ".json")
        _regular(path)
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or not value:
                raise ValueError
            return value
        except (ValueError, OSError):
            raise Hold("coordination state unreadable; no request sent") from None

    def save(self, name, value):
        path = self.root / (name + ".json")
        _regular(path)
        fd, temporary = tempfile.mkstemp(prefix=".state-", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(value, f, allow_nan=False, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, path)
            if os.name != "nt":
                directory = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def check_rate(self, mutation=False):
        state = self.load("github-com-rate")
        now = self.clock()
        if state and (
            state.get("schema") != "githubbot-core.rate/v1"
            or not set(state) <= {"schema", "observed", "not_before", "last_mutation", "policy_hold"}
        ):
            raise Hold("coordination rate schema invalid")
        for key in ("observed", "not_before", "last_mutation"):
            value = state.get(key, 0)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise Hold("coordination rate state invalid")
        if now < state.get("observed", 0):
            raise Hold("coordination clock moved backwards")
        if state.get("policy_hold"):
            raise Hold("GitHub policy HOLD; operator inspection required")
        deadline = state.get("not_before", 0)
        if mutation and "last_mutation" in state:
            deadline = max(deadline, state["last_mutation"] + MUTATION_INTERVAL)
        if now < deadline:
            raise Hold("GitHub cooldown HOLD until " + str(deadline))
        state["schema"] = "githubbot-core.rate/v1"
        state["observed"] = now
        if mutation:
            state["last_mutation"] = now
        self.save("github-com-rate", state)

    def rate_hold(self, *, seconds=None, policy=False):
        state = self.load("github-com-rate")
        now = self.clock()
        state["observed"] = max(now, state.get("observed", 0))
        if policy:
            state["policy_hold"] = True
        else:
            state["not_before"] = max(state.get("not_before", 0), now + max(60, seconds or 0))
        self.save("github-com-rate", state)

    @staticmethod
    def action_key(action_id):
        return "action-" + hashlib.sha256(action_id.encode("ascii")).hexdigest()
