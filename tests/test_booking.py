"""booking.py after M6: file I/O off the event loop (D8), failures raise
so the session's tool runner can turn them into ToolFailed events."""
import json

import pytest

import booking


async def test_save_booking_appends_jsonl(tmp_path, monkeypatch):
    store = tmp_path / "bookings.jsonl"
    monkeypatch.setattr(booking, "_STORE", store)

    await booking.save_booking("c1", "+911234567890", {"day": "Sunday", "time": "4pm"})
    await booking.save_booking("c1", "+911234567890", {"day": "Monday", "time": "11am"})

    records = [json.loads(line) for line in
               store.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2
    assert records[0]["call_id"] == "c1"
    assert records[0]["day"] == "Sunday"
    assert "ts" in records[0]


async def test_save_booking_raises_on_write_failure(tmp_path, monkeypatch):
    # A directory in place of the file makes the append fail — the error
    # must propagate (the tool runner owns the catch since M6).
    monkeypatch.setattr(booking, "_STORE", tmp_path)
    with pytest.raises(OSError):
        await booking.save_booking("c1", "caller", {"day": "Sunday"})
