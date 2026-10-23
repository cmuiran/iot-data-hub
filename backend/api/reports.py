"""/api/reports: period reports (JSON, CSV, Excel). See backend/reports.py."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from backend.deps import ServicesDep, require
from backend.errors import Invalid, NotFound
from backend.rbac import Principal, split_path
from backend.reports import ReportRequest, build_report, to_csv, to_xlsx
from hub.models import NUMERIC_TYPES

router = APIRouter(prefix="/api/reports", tags=["reports"])
Reader = Annotated[Principal, Depends(require("data.read"))]

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


async def _report(svc: ServicesDep, p: Principal, body: ReportRequest) -> dict[str, Any]:
    units: dict[str, str | None] = {}
    for col in body.columns:
        found = svc.hub.tags.get(col.tag)
        site, area, _, _ = split_path(col.tag) if found else (None, None, None, None)
        if found is None or not p.can("data.read", site, area):
            raise NotFound(f"no such tag {col.tag}")
        if found[1].data_type not in NUMERIC_TYPES:
            raise Invalid(f"{col.tag} is not numeric; reports need numbers", code="not_numeric")
        units[col.tag] = found[1].unit
    if (body.end - body.start).days > 3 * 366:
        raise Invalid("at most three years per report", code="invalid_range")
    try:
        return await build_report(svc.store, body, svc.settings.timezone, units)
    except ValueError as exc:
        raise Invalid(str(exc), code="invalid_range") from exc


@router.post("")
async def report(body: ReportRequest, svc: ServicesDep, p: Reader) -> dict[str, Any]:
    return await _report(svc, p, body)


@router.post("/export")
async def export(body: ReportRequest, svc: ServicesDep, p: Reader,
                 format: Annotated[Literal["csv", "xlsx"], Query()] = "xlsx") -> Response:
    data = await _report(svc, p, body)
    stamp = body.start.strftime("%Y%m%d")
    if format == "csv":
        return Response(to_csv(data).encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition":
                                 f'attachment; filename="report-{stamp}.csv"'})
    return Response(to_xlsx(data), media_type=_XLSX,
                    headers={"Content-Disposition": f'attachment; filename="report-{stamp}.xlsx"'})
