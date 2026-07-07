"""Tests for UTF-8 stream reconfiguration on Windows (_force_utf8_output)."""

from __future__ import annotations

import pytest

from audible_deals.cli import _force_utf8_output


class _FakeStream:
    def __init__(self, raises=None):
        self._raises = raises
        self.calls = []

    def reconfigure(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises


def test_reconfigures_both_streams_on_windows(monkeypatch):
    out, err = _FakeStream(), _FakeStream()
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr("sys.stderr", err)

    _force_utf8_output()

    assert out.calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert err.calls == [{"encoding": "utf-8", "errors": "replace"}]


def test_noop_off_windows(monkeypatch):
    out = _FakeStream()
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr("sys.stdout", out)

    _force_utf8_output()

    assert out.calls == []


@pytest.mark.parametrize("exc", [AttributeError, ValueError])
def test_swallows_non_reconfigurable_streams(monkeypatch, exc):
    out = _FakeStream(raises=exc())
    err = _FakeStream(raises=exc())
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr("sys.stderr", err)

    _force_utf8_output()  # must not raise
