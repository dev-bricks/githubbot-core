from __future__ import annotations

import pytest


class FakeTransport:
    """In-memory transport: canned GET answers, every write is recorded."""

    def __init__(self, gets: dict | None = None, texts: dict | None = None, fail_writes: bool = False):
        self.gets = gets or {}
        self.texts = texts or {}
        self.fail_writes = fail_writes
        self.writes: list[tuple[str, str, object]] = []

    def get(self, endpoint):
        if endpoint in self.gets:
            return self.gets[endpoint], None
        return None, f"404 {endpoint}"

    def get_paginated(self, endpoint):
        data, error = self.get(endpoint)
        return (data or []), error

    def get_text(self, endpoint, accept):
        return self.texts.get(endpoint, ""), None

    def request(self, method, endpoint, body=None):
        self.writes.append((method, endpoint, body))
        return (None, "HTTP 500") if self.fail_writes else ({"ok": True}, None)


@pytest.fixture
def fake_transport():
    return FakeTransport
