"""Local read-only HTTP query API over the PelosiTracker SQLite database.

Serves already-ingested public STOCK Act disclosure data. The database is
opened read-only per request (mode=ro URI) and every JSON response —
including errors — carries the product disclaimer. Amount ranges are returned
as {"min_cents": int, "max_cents": int | null} objects; an open-ended maximum
stays null and is never collapsed to a midpoint or estimate.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

DISCLAIMER = (
    "PelosiTracker displays public STOCK Act disclosure data. Filings may lag "
    "trades by up to 45 days and report amount ranges, not exact values. "
    "Informational only — not investment advice."
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8642
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

_TRANSACTION_TYPES = ("buy", "sell", "exchange")

_TRADE_SELECT = """
    SELECT t.id, t.politician_id, p.full_name AS politician_name, p.chamber,
           t.ticker, t.asset_name, t.transaction_type,
           t.amount_min_cents, t.amount_max_cents,
           t.transaction_date, t.disclosure_date, t.owner, t.source_url
    FROM trades t
    JOIN politicians p ON p.id = t.politician_id
"""


class _BadRequest(ValueError):
    """Client error whose message is safe to return in a 400 body."""


def _days_between(transaction_date: str, disclosure_date: str) -> int | None:
    try:
        return (
            date.fromisoformat(disclosure_date) - date.fromisoformat(transaction_date)
        ).days
    except ValueError:
        return None


def _trade_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "politician_id": row["politician_id"],
        "politician_name": row["politician_name"],
        "chamber": row["chamber"],
        "ticker": row["ticker"],
        "asset_name": row["asset_name"],
        "transaction_type": row["transaction_type"],
        "amount": {
            "min_cents": row["amount_min_cents"],
            "max_cents": row["amount_max_cents"],
        },
        "transaction_date": row["transaction_date"],
        "disclosure_date": row["disclosure_date"],
        "days_to_disclosure": _days_between(
            row["transaction_date"], row["disclosure_date"]
        ),
        "owner": row["owner"],
        "source_url": row["source_url"],
    }


def _parse_non_negative_int(raw: str, name: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise _BadRequest(f"{name} must be an integer") from exc
    if value < 0:
        raise _BadRequest(f"{name} must not be negative")
    return value


def _query_trades(
    conn: sqlite3.Connection, params: dict[str, str]
) -> list[dict[str, Any]]:
    limit = min(
        _parse_non_negative_int(params.get("limit", str(DEFAULT_LIMIT)), "limit"),
        MAX_LIMIT,
    )
    offset = _parse_non_negative_int(params.get("offset", "0"), "offset")

    where: list[str] = []
    args: list[str | int] = []
    ticker = params.get("ticker")
    if ticker is not None:
        where.append("UPPER(t.ticker) = ?")
        args.append(ticker.upper())
    politician_id = params.get("politician_id")
    if politician_id is not None:
        where.append("t.politician_id = ?")
        args.append(_parse_non_negative_int(politician_id, "politician_id"))
    transaction_type = params.get("type")
    if transaction_type is not None:
        if transaction_type not in _TRANSACTION_TYPES:
            raise _BadRequest("type must be one of: buy, sell, exchange")
        where.append("t.transaction_type = ?")
        args.append(transaction_type)
    date_from = params.get("date_from")
    if date_from is not None:
        where.append("t.disclosure_date >= ?")
        args.append(date_from)
    date_to = params.get("date_to")
    if date_to is not None:
        where.append("t.disclosure_date <= ?")
        args.append(date_to)

    sql = _TRADE_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY t.disclosure_date DESC, t.id DESC LIMIT ? OFFSET ?"
    args.extend((limit, offset))
    return [_trade_payload(row) for row in conn.execute(sql, args)]


def _list_politicians(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT p.id, p.full_name, p.chamber, COUNT(t.id) AS trade_count
        FROM politicians p
        LEFT JOIN trades t ON t.politician_id = p.id
        GROUP BY p.id
        ORDER BY p.full_name ASC
        """
    )
    return [
        {
            "id": row["id"],
            "full_name": row["full_name"],
            "chamber": row["chamber"],
            "trade_count": row["trade_count"],
        }
        for row in rows
    ]


def _politician_detail(
    conn: sqlite3.Connection, politician_id: int
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, full_name, chamber FROM politicians WHERE id = ?",
        (politician_id,),
    ).fetchone()
    if row is None:
        return None
    agg = conn.execute(
        """
        SELECT COUNT(*) AS trade_count,
               COALESCE(SUM(amount_min_cents), 0) AS min_sum,
               SUM(amount_max_cents) AS max_sum,
               SUM(CASE WHEN amount_max_cents IS NULL THEN 1 ELSE 0 END) AS open_ended
        FROM trades WHERE politician_id = ?
        """,
        (politician_id,),
    ).fetchone()
    top_rows = conn.execute(
        """
        SELECT ticker, COUNT(*) AS n FROM trades
        WHERE politician_id = ? AND ticker IS NOT NULL
        GROUP BY ticker ORDER BY n DESC, ticker ASC LIMIT 5
        """,
        (politician_id,),
    ).fetchall()
    # Range integrity: if ANY trade has an open-ended maximum, the aggregate
    # maximum is unknowable and must stay null — never estimated.
    if agg["trade_count"] == 0:
        max_cents: int | None = 0
    elif agg["open_ended"]:
        max_cents = None
    else:
        max_cents = agg["max_sum"]
    return {
        "id": row["id"],
        "full_name": row["full_name"],
        "chamber": row["chamber"],
        "trade_count": agg["trade_count"],
        "top_tickers": [
            {"ticker": top["ticker"], "trades": top["n"]} for top in top_rows
        ],
        "total_volume": {"min_cents": agg["min_sum"], "max_cents": max_cents},
    }


class _ApiHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        try:
            status, payload, headers = self._handle_get()
        except _BadRequest as exc:
            status, payload, headers = 400, {"error": str(exc)}, {}
        except sqlite3.Error:
            # Never leak database/exception detail to clients.
            status, payload, headers = 500, {"error": "internal database error"}, {}
        self._send_json(status, payload, headers)

    def _handle_get(self) -> tuple[int, dict[str, Any], dict[str, str]]:
        parsed = urlsplit(self.path)
        segments = [segment for segment in parsed.path.split("/") if segment]
        params = {
            key: values[-1] for key, values in parse_qs(parsed.query).items()
        }
        if segments[:2] != ["api", "v1"]:
            return 404, {"error": "not found"}, {}
        route = segments[2:]
        with contextlib.closing(self._connect()) as conn:
            if route == ["trades"]:
                trades = _query_trades(conn, params)
                return 200, {"count": len(trades), "trades": trades}, {}
            if route == ["politicians"]:
                return 200, {"politicians": _list_politicians(conn)}, {}
            if len(route) == 2 and route[0] == "politicians":
                try:
                    politician_id = int(route[1])
                except ValueError:
                    return 404, {"error": "not found"}, {}
                detail = _politician_detail(conn, politician_id)
                if detail is None:
                    return (
                        404,
                        {"error": f"unknown politician id {politician_id}"},
                        {},
                    )
                return 200, detail, {}
            if len(route) == 3 and route[0] == "tickers" and route[2] == "trades":
                trades = _query_trades(conn, {**params, "ticker": route[1]})
                return 200, {"count": len(trades), "trades": trades}, {}
        return 404, {"error": "not found"}, {}

    def _connect(self) -> sqlite3.Connection:
        server = self.server
        assert isinstance(server, _ApiServer)
        conn = sqlite3.connect(server.db_uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _send_json(
        self,
        status: int,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps({"disclaimer": DISCLAIMER, **payload}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _reject_method(self) -> None:
        self._send_json(
            405,
            {"error": f"method {self.command} not allowed; GET only"},
            {"Allow": "GET"},
        )

    do_POST = _reject_method
    do_PUT = _reject_method
    do_PATCH = _reject_method
    do_DELETE = _reject_method
    do_HEAD = _reject_method
    do_OPTIONS = _reject_method

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass  # local read-only server; keep test/CLI output quiet


class _ApiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], db_path: str) -> None:
        # SQLite URI paths use forward slashes and percent-encoding, even on
        # Windows; mode=ro guarantees the API can never write the database.
        self.db_uri = f"file:{quote(Path(db_path).as_posix(), safe='/:')}?mode=ro"
        super().__init__(address, _ApiHandler)


def build_server(db_path: str, host: str, port: int) -> ThreadingHTTPServer:
    """Bind (but do not serve) the API server. Port 0 picks an ephemeral port."""
    return _ApiServer((host, port), db_path)


def serve(db_path: str, host: str | None = None, port: int | None = None) -> None:
    """Run the API server until interrupted.

    Host/port default from PT_API_HOST / PT_API_PORT when not passed.
    """
    if host is None:
        host = os.environ.get("PT_API_HOST", DEFAULT_HOST)
    if port is None:
        port = int(os.environ.get("PT_API_PORT", str(DEFAULT_PORT)))
    server = build_server(db_path, host, port)
    bound_port = server.server_address[1]
    print(f"serving on http://{host}:{bound_port}")
    print(DISCLAIMER)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
