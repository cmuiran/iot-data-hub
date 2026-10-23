from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import openpyxl
import pytest

from backend.reports import _Hour, aggregate, periods
from hub.models import Sample
from tests.api.conftest import GATE_METER, PUMP_DRIVE, Backend

TEHRAN = ZoneInfo("Asia/Tehran")
IRST = timezone(timedelta(hours=3, minutes=30))


# --- pure functions -------------------------------------------------------------------------


def _h(hour: int, first: float, last: float, n: int = 6) -> _Hour:
    return _Hour(datetime(2026, 10, 1, hour, tzinfo=UTC), (first + last) / 2, first, last,
                 first, last, n)


def test_delta_with_counter_reset() -> None:
    hours = [_h(0, 100, 110), _h(1, 110, 125), _h(2, 3, 8)]   # meter exchanged in hour 2
    value, carry = aggregate(hours, "delta", None)
    assert value == pytest.approx(10 + 15 + 5) and carry == 8


def test_delta_continues_from_previous_period() -> None:
    value, _ = aggregate([_h(5, 210, 230)], "delta", previous_last=200)
    assert value == pytest.approx(30)          # includes the step between the periods


def test_avg_is_weighted_by_samples() -> None:
    hours = [_Hour(datetime(2026, 1, 1, tzinfo=UTC), 10, 10, 10, 10, 10, 1),
             _Hour(datetime(2026, 1, 1, 1, tzinfo=UTC), 20, 20, 20, 20, 20, 3)]
    assert aggregate(hours, "avg", None)[0] == pytest.approx(17.5)
    assert aggregate([], "avg", None) == (None, None)


def test_daily_periods_start_at_tehran_midnight() -> None:
    start = datetime(2026, 10, 1, tzinfo=IRST)
    days = periods(start, start + timedelta(days=3), "day", TEHRAN)
    assert [d.isoformat() for d in days] == ["2026-10-01T00:00:00+03:30",
                                             "2026-10-02T00:00:00+03:30",
                                             "2026-10-03T00:00:00+03:30"]
    months = periods(datetime(2026, 11, 15, tzinfo=IRST), datetime(2027, 2, 1, tzinfo=IRST),
                     "month", TEHRAN)
    assert [m.strftime("%Y-%m") for m in months] == ["2026-11", "2026-12", "2027-01"]


# --- through the API ------------------------------------------------------------------------

ENERGY = f"{GATE_METER}.voltage_l1"   # used as a counter here: values only go up
SPEED = f"{PUMP_DRIVE}.speed_setpoint"


async def _seed(b: Backend) -> datetime:
    """Two Tehran days, one sample per 30 min; the counter grows 1 per sample."""
    start = datetime(2026, 10, 1, tzinfo=IRST)
    samples = []
    for i in range(96):
        ts = start + timedelta(minutes=30 * i)
        samples.append(Sample(tag=ENERGY, value=1000.0 + i, timestamp=ts.astimezone(UTC)))
        samples.append(Sample(tag=SPEED, value=1000 + (i % 2) * 100, timestamp=ts.astimezone(UTC)))
    await b.store.write_samples(samples)
    return start


@pytest.mark.postgres
@pytest.mark.integration
async def test_daily_report_json_csv_xlsx(backend: Backend) -> None:
    await backend.user("viewer", ("viewer", None, None))
    h = await backend.headers("viewer")
    start = await _seed(backend)
    body = {"title": "Gate energy", "interval": "day", "start": start.isoformat(),
            "end": (start + timedelta(days=2)).isoformat(),
            "columns": [{"tag": ENERGY, "function": "delta", "label": "Energy"},
                        {"tag": SPEED, "function": "avg"},
                        {"tag": SPEED, "function": "max"}]}
    r = await backend.client.post("/api/reports", headers=h, json=body)
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["periods"] == ["2026-10-01T00:00:00+03:30", "2026-10-02T00:00:00+03:30"]
    energy, avg, mx = rep["columns"]
    assert energy["values"] == [pytest.approx(47.0), pytest.approx(48.0)]   # 47 steps, then 48
    assert energy["total"] == pytest.approx(95.0)
    assert avg["values"] == [pytest.approx(1050.0), pytest.approx(1050.0)]
    assert mx["values"] == [1100, 1100] and avg["total"] is None
    assert energy["unit"] == "V" and energy["label"] == "Energy"

    csv = await backend.client.post("/api/reports/export?format=csv", headers=h, json=body)
    assert csv.status_code == 200
    lines = csv.content.decode("utf-8-sig").splitlines()
    assert lines[0].startswith("period,Energy [V]") and lines[-1].startswith("total,95")

    xlsx = await backend.client.post("/api/reports/export?format=xlsx", headers=h, json=body)
    assert xlsx.status_code == 200
    ws = openpyxl.load_workbook(io.BytesIO(xlsx.content)).active
    rows = [r for r in ws.iter_rows(values_only=True) if any(v is not None for v in r)]
    assert rows[0][0] == "Gate energy"
    assert ("2026-10-01", 47.0, 1050.0, 1100) in [tuple(r[:4]) for r in rows]
    assert rows[-1][0] == "Total" and rows[-1][1] == pytest.approx(95.0)


@pytest.mark.postgres
@pytest.mark.integration
async def test_report_scope_and_validation(backend: Backend) -> None:
    await backend.user("view.gate", ("viewer", "kish", "gate"))
    h = await backend.headers("view.gate")
    start = datetime(2026, 10, 1, tzinfo=IRST).isoformat()
    end = datetime(2026, 10, 2, tzinfo=IRST).isoformat()
    other_area = await backend.client.post("/api/reports", headers=h, json={
        "start": start, "end": end, "columns": [{"tag": SPEED}]})
    assert other_area.status_code == 404
    not_numeric = await backend.client.post("/api/reports", headers=h, json={
        "start": start, "end": end, "columns": [{"tag": f"{GATE_METER}.breaker_closed"}]})
    assert not_numeric.status_code == 422 and not_numeric.json()["error"] == "not_numeric"
    naive = await backend.client.post("/api/reports", headers=h, json={
        "start": "2026-10-01T00:00:00", "end": end, "columns": [{"tag": ENERGY}]})
    assert naive.status_code == 422
    hourly = await backend.client.post("/api/reports", headers=h, json={
        "start": start, "end": end, "interval": "hour", "columns": [{"tag": ENERGY}]})
    assert hourly.status_code == 200 and len(hourly.json()["periods"]) == 24
