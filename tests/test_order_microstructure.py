"""Tests for bot.order_microstructure (Session 15).

All tests use mocks for the Kalshi client — verification of real-order behavior
is deferred until PAPER_MODE=False per spec.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import pytest

from bot import order_microstructure as om


@pytest.fixture(autouse=True)
def _reset_pending(monkeypatch):
    """Each test starts with an empty _PENDING dict."""
    monkeypatch.setattr(om, "_PENDING", {})


def test_append_record_writes_one_line_jsonl(tmp_path, monkeypatch):
    f = tmp_path / "om.jsonl"
    monkeypatch.setattr(om, "MICROSTRUCTURE_FILE", f)
    om._append_record({"ts_placed": "2026-04-25T00:00:00+00:00", "ticker": "KX1"})
    lines = f.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["ticker"] == "KX1"
