"""FastAPI 应用：报告提交与判读 API。

- POST /reports                          提交（或重传）某站点某采样日的完整日报
- GET  /reports/{station}/{date}         读取现行（或 ?revision= 指定）报告
- GET  /reports/{station}/{date}/revisions  列出该日全部已提交修订
- GET  /interpretation                   七日窗口判读
"""

from __future__ import annotations

import datetime as dt
import math
import os
import sqlite3
from typing import Annotated, Any, Optional

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse

from .db import Database, rollback_quietly
from .logic import (
    STATUS_ZH,
    WINDOW_DAYS,
    canonical_content,
    content_hash,
    interpret_window,
)
from .models import Name, NonNegFinite, ReportSubmission, SubmitResponse

DESCRIPTION = """\
流域监测报告提交与判读 API。

**提交规则**
- 实验室按站点+采样日提交包含全部污染物的完整日报，修订号递增（正整数）。
- 更高修订**完整替换**当天报告，删除的项目不沿用旧值。
- 同修订同内容重传：幂等复用（返回 200 与原 report_id）。
- 旧修订、或同修订但内容不同：拒绝（409）。
- 提交整体落库或整体回滚，并发/重试不会留下半份报告；历史修订始终可读取。

**判读规则**
- 窗口为结束日（含）向前连续 7 天，取**同一已提交时点**各日的现行日报。
- 缺日或缺项目 → `insufficient_data`（资料不足）。
- “未检出”按半开区间 [0, 检出限) 参与均值；实测值即点值。
- 均值必定 ≤ 限值 → `compliant`（达标）；必定 > 限值 → `exceeds`（超标）；
  其余 → `pending_review`（待复核）。响应含所用修订号。
"""


def _item_json(row: sqlite3.Row) -> dict[str, Any]:
    if row["type"] == "value":
        return {"type": "value", "pollutant": row["pollutant"], "value": row["value"]}
    return {
        "type": "nd",
        "pollutant": row["pollutant"],
        "detection_limit": row["detection_limit"],
    }


def _json_safe(obj: Any) -> Any:
    """把校验错误中的非有限浮点（inf/nan 输入值）转为字符串，保证可 JSON 序列化。"""
    if isinstance(obj, float) and not math.isfinite(obj):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def create_app(db_path: Optional[str] = None) -> FastAPI:
    db = Database(db_path or os.environ.get("DB_PATH", "app.db"))
    app = FastAPI(
        title="流域监测报告提交与判读 API",
        version="1.0.0",
        description=DESCRIPTION,
    )
    app.state.db = db

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=jsonable_encoder({"detail": _json_safe(exc.errors())}),
        )

    # ---------------- 系统 ----------------

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/docs")

    @app.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # ---------------- 报告提交 ----------------

    @app.post(
        "/reports",
        status_code=201,
        response_model=SubmitResponse,
        tags=["reports"],
        responses={
            200: {"description": "同修订同内容重传，幂等复用"},
            409: {"description": "旧修订或同修订异内容，拒收"},
            422: {"description": "请求体校验失败"},
            503: {"description": "数据库繁忙，可重试"},
        },
    )
    def submit_report(payload: ReportSubmission, response: Response) -> SubmitResponse:
        items = [it.model_dump() for it in payload.items]
        canon = canonical_content(items)
        digest = content_hash(items)
        date_s = payload.sampling_date.isoformat()

        conn = db.connect()
        try:
            # BEGIN IMMEDIATE 立即取得写锁：检查+插入在同一写事务内串行执行
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id, revision, content_hash FROM reports "
                "WHERE station=? AND sampling_date=? ORDER BY revision DESC LIMIT 1",
                (payload.station, date_s),
            ).fetchone()

            if row is not None and payload.revision < row["revision"]:
                conn.execute("ROLLBACK")
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "stale_revision",
                        "message": f"revision {payload.revision} 低于当前修订 {row['revision']}，拒收",
                        "current_revision": row["revision"],
                    },
                )

            if row is not None and payload.revision == row["revision"]:
                if row["content_hash"] == digest:
                    conn.execute("COMMIT")  # 幂等重传：复用已有结果
                    response.status_code = 200
                    return SubmitResponse(
                        report_id=row["id"],
                        station=payload.station,
                        sampling_date=date_s,
                        revision=payload.revision,
                        status="reused",
                        superseded_revision=None,
                    )
                conn.execute("ROLLBACK")
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "revision_content_mismatch",
                        "message": f"修订 {payload.revision} 已存在且内容不同，拒收",
                        "current_revision": row["revision"],
                    },
                )

            now = dt.datetime.now(dt.timezone.utc).isoformat()
            cur = conn.execute(
                "INSERT INTO reports (station, sampling_date, revision, content_hash, "
                "content_json, submitted_at) VALUES (?,?,?,?,?,?)",
                (payload.station, date_s, payload.revision, digest, canon, now),
            )
            report_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO report_items (report_id, pollutant, type, value, "
                "detection_limit) VALUES (?,?,?,?,?)",
                [
                    (
                        report_id,
                        it["pollutant"],
                        it["type"],
                        it.get("value"),
                        it.get("detection_limit"),
                    )
                    for it in items
                ],
            )
            conn.execute("COMMIT")
            return SubmitResponse(
                report_id=report_id,
                station=payload.station,
                sampling_date=date_s,
                revision=payload.revision,
                status="created",
                superseded_revision=row["revision"] if row is not None else None,
            )
        except HTTPException:
            raise
        except sqlite3.OperationalError as exc:
            rollback_quietly(conn)
            if "locked" in str(exc).lower():
                raise HTTPException(
                    status_code=503,
                    detail={"error": "busy", "message": "数据库繁忙，请重试"},
                ) from exc
            raise
        except Exception:
            rollback_quietly(conn)
            raise
        finally:
            conn.close()

    # ---------------- 报告读取 ----------------

    @app.get("/reports/{station}/{sampling_date}", tags=["reports"])
    def get_report(
        station: Name,
        sampling_date: dt.date,
        revision: Annotated[Optional[int], Query(ge=1, description="缺省读取现行（最高）修订")] = None,
    ) -> dict[str, Any]:
        conn = db.connect()
        try:
            if revision is None:
                row = conn.execute(
                    "SELECT * FROM reports WHERE station=? AND sampling_date=? "
                    "ORDER BY revision DESC LIMIT 1",
                    (station, sampling_date.isoformat()),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM reports WHERE station=? AND sampling_date=? AND revision=?",
                    (station, sampling_date.isoformat(), revision),
                ).fetchone()
            if row is None:
                raise HTTPException(
                    status_code=404,
                    detail={"error": "not_found", "message": "报告不存在"},
                )
            items = conn.execute(
                "SELECT pollutant, type, value, detection_limit FROM report_items "
                "WHERE report_id=? ORDER BY pollutant",
                (row["id"],),
            ).fetchall()
            current = conn.execute(
                "SELECT MAX(revision) FROM reports WHERE station=? AND sampling_date=?",
                (station, sampling_date.isoformat()),
            ).fetchone()[0]
            return {
                "report_id": row["id"],
                "station": row["station"],
                "sampling_date": row["sampling_date"],
                "revision": row["revision"],
                "is_current": row["revision"] == current,
                "submitted_at": row["submitted_at"],
                "items": [_item_json(r) for r in items],
            }
        finally:
            conn.close()

    @app.get("/reports/{station}/{sampling_date}/revisions", tags=["reports"])
    def list_revisions(station: Name, sampling_date: dt.date) -> dict[str, Any]:
        conn = db.connect()
        try:
            rows = conn.execute(
                "SELECT id, revision, submitted_at FROM reports "
                "WHERE station=? AND sampling_date=? ORDER BY revision",
                (station, sampling_date.isoformat()),
            ).fetchall()
            if not rows:
                raise HTTPException(
                    status_code=404,
                    detail={"error": "not_found", "message": "报告不存在"},
                )
            current = max(r["revision"] for r in rows)
            return {
                "station": station,
                "sampling_date": sampling_date.isoformat(),
                "revisions": [
                    {
                        "report_id": r["id"],
                        "revision": r["revision"],
                        "submitted_at": r["submitted_at"],
                        "is_current": r["revision"] == current,
                    }
                    for r in rows
                ],
            }
        finally:
            conn.close()

    # ---------------- 判读 ----------------

    @app.get("/interpretation", tags=["interpretation"])
    def get_interpretation(
        station: Annotated[Name, Query(description="站点")],
        end_date: Annotated[dt.date, Query(description="窗口结束日（含），向前连续 7 天")],
        pollutant: Annotated[Name, Query(description="污染物")],
        limit: Annotated[NonNegFinite, Query(description="非负限值 mg/L")],
    ) -> dict[str, Any]:
        conn = db.connect()
        try:
            # 单事务读取：七份现行日报取自同一已提交时点（快照）
            conn.execute("BEGIN")
            snapshot = conn.execute("SELECT COALESCE(MAX(id),0) FROM reports").fetchone()[0]
            days: list[dict[str, Any]] = []
            for offset in range(WINDOW_DAYS - 1, -1, -1):
                d = end_date - dt.timedelta(days=offset)
                row = conn.execute(
                    "SELECT id, revision FROM reports "
                    "WHERE station=? AND sampling_date=? AND id<=? "
                    "ORDER BY revision DESC LIMIT 1",
                    (station, d.isoformat(), snapshot),
                ).fetchone()
                if row is None:
                    days.append(
                        {"date": d.isoformat(), "revision": None, "report_id": None, "items": None}
                    )
                    continue
                rows = conn.execute(
                    "SELECT pollutant, type, value, detection_limit FROM report_items "
                    "WHERE report_id=?",
                    (row["id"],),
                ).fetchall()
                items = {
                    r["pollutant"]: {
                        "type": r["type"],
                        "value": r["value"],
                        "detection_limit": r["detection_limit"],
                    }
                    for r in rows
                }
                days.append(
                    {
                        "date": d.isoformat(),
                        "revision": row["revision"],
                        "report_id": row["id"],
                        "items": items,
                    }
                )
            conn.execute("COMMIT")
        except Exception:
            rollback_quietly(conn)
            raise
        finally:
            conn.close()

        result = interpret_window(days, pollutant, limit)
        return {
            "station": station,
            "pollutant": pollutant,
            "end_date": end_date.isoformat(),
            "window_start": (end_date - dt.timedelta(days=WINDOW_DAYS - 1)).isoformat(),
            "window_days": WINDOW_DAYS,
            "limit": limit,
            "status": result["status"],
            "status_zh": STATUS_ZH[result["status"]],
            "mean": result["mean"],
            "revisions": result["revisions"],
            "missing_dates": result["missing_dates"],
            "missing_pollutant_dates": result["missing_pollutant_dates"],
            "snapshot_seq": snapshot,
        }

    return app


app = create_app()
