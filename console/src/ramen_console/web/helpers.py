import csv
import io
import json

from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from ..accounts import Accounts
from ..backup import Backups
from ..services import Services


def svc(request: Request) -> Services:
    return request.app.state.services


def accounts(request: Request) -> Accounts:
    return request.app.state.accounts


def backups(request: Request) -> Backups:
    return request.app.state.backups


def is_hx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def respond(request: Request, data, status: int = 200, hx_html: str | None = None) -> Response:
    if is_hx(request):
        if hx_html is not None:
            return Response(hx_html, status, media_type="text/html")
        return Response("ok", status, headers={"HX-Refresh": "true"})
    return JSONResponse(data, status)


def _csv_safe(v):
    """Neutralise spreadsheet formula injection (=, +, -, @, tab, CR at the start of a cell)."""
    return f"'{v}" if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def tabular(rows: list[dict], fmt: str | None, name: str) -> Response:
    if fmt == "csv":
        buf = io.StringIO()
        cols = sorted({k for r in rows for k in r})
        w = csv.DictWriter(buf, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: _csv_safe(json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items()})
        return PlainTextResponse(
            buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}.csv"'}
        )
    if fmt == "json":
        return JSONResponse(rows, headers={"Content-Disposition": f'attachment; filename="{name}.json"'})
    return JSONResponse(rows)
