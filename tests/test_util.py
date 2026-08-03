"""Credential redaction used before anything reaches a log or exception."""

from __future__ import annotations

import pytest

from aiosecspy.util import redact


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("http://cam:8000/++image?auth=dXNlcjpwYXNz&cameraNum=3", "dXNlcjpwYXNz"),
        ("GET /++image?cameraNum=3&AUTH=dXNlcjpwYXNz failed", "dXNlcjpwYXNz"),
        ("rtsp://user:hunter2@cam:8000/stream", "hunter2"),
        ("connect failed: password=hunter2", "hunter2"),
    ],
)
def test_secrets_are_removed(text, secret):
    cleaned = redact(text)
    assert secret not in cleaned
    assert "<redacted>" in cleaned


def test_ordinary_text_is_untouched():
    assert redact("++systemInfo failed: HTTP 500") == "++systemInfo failed: HTTP 500"


def test_other_query_parameters_survive():
    cleaned = redact("/++image?auth=abc123&cameraNum=3&width=640")
    assert "cameraNum=3" in cleaned
    assert "width=640" in cleaned
