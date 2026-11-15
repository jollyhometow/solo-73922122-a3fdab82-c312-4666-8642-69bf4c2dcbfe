"""FastAPI 应用：报告提交、判读与交接对账 API。

- POST /reports                          提交（或重传）某站点某采样日的完整日报
- GET  /reports/{station}/{date}         读取现行（或 ?revision= 指定）报告
- GET  /reports/{station}/{date}/revisions  列出该日全部已提交修订
- GET  /interpretation                   七日窗口判读
- POST /handover/reconcile               现场采样交接补正对账
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
from .handover import reconcile
from .logic import (
    STATUS_ZH,
    WINDOW_DAYS,
    canonical_content,
    content_hash,
    interpret_window,
)
from .models import (
    HandoverReconciliationRequest,
    HandoverReconciliationResponse,
    Name,
    NonNegFinite,
    ReportSubmission,
    SubmitResponse,
)

DESCRIPTION = """\
流域监测报告提交、判读与交接对账 API。

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

**交接对账**
- 提交最多 30 条样本记录（样本 ID、站点、采样日、应检污染物、可选被替换样本 ID），
  补正可乱序，也可改站点或采样日。
- 沿替换关系确定每个站日的最终样本，与**同一已提交快照**中的现行完整日报比对应检项目：
  项目完全一致 → `warehouseable_reports`（可入仓日报 ID + 修订号）；
  无现行报告 → `missing_reports`（缺报）；缺项 → `missing_items`；多项 → `extra_items`。
- 缺前驱、成环、分叉、同站日多个最终样本 → 相关站日列入 `handover_conflicts`。
- 跨站日补正让出的旧位置即使有报告也列入 `pending_verification`（待核对，不得入仓）。
- 对账在单个读事务内取同一快照（`snapshot_seq`），并发补报/修订不会混用不同时点的日报。
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
        title="流域监测报告提交、判读与交接对账 API",
        version="1.1.0",
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

    # ---------------- 交接对账 ----------------

    @app.post(
        "/handover/reconcile",
        response_model=HandoverReconciliationResponse,
        tags=["handover"],
        summary="交接补正对账",
        responses={
            200: {"description": "对账结果（含可入仓、缺报、缺项、多项、冲突与待核对）"},
            422: {"description": "请求体校验失败（样本数 1..30、样本 ID 唯一等）"},
            503: {"description": "数据库繁忙，可重试"},
        },
    )
    def reconcile_handover(
        payload: HandoverReconciliationRequest,
    ) -> HandoverReconciliationResponse:
        """按替换关系确定每个站日的最终样本，与同一快照的现行日报比对。

        - 补正可乱序提交，也可修改站点或采样日；
        - 缺前驱、成环、分叉、同站日多个最终样本 → 相关站日列为交接冲突；
        - 跨站日补正让出的旧位置即使有报告也列为待核对，不得入仓；
        - 整批报告在单个读事务内取自同一已提交时点（snapshot_seq）。
        """
        samples = [
            {
                "sample_id": s.sample_id,
                "station": s.station,
                "sampling_date": s.sampling_date.isoformat(),
                "pollutants": [p for p in s.pollutants],
                "replaces_sample_id": s.replaces_sample_id,
            }
            for s in payload.samples
        ]
        wanted_days = sorted(
            {(s["station"], s["sampling_date"]) for s in samples}
        )

        conn = db.connect()
        try:
            # 单读事务：MAX(id) 固定快照时点，之后各站日现行日报均来自同一时点
            conn.execute("BEGIN")
            snapshot = conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM reports"
            ).fetchone()[0]
            reports: dict[tuple[str, str], dict[str, Any]] = {}
            for station, day in wanted_days:
                row = conn.execute(
                    "SELECT id, revision FROM reports "
                    "WHERE station=? AND sampling_date=? AND id<=? "
                    "ORDER BY revision DESC LIMIT 1",
                    (station, day, snapshot),
                ).fetchone()
                if row is None:
                    continue
                items = conn.execute(
                    "SELECT pollutant FROM report_items WHERE report_id=?",
                    (row["id"],),
                ).fetchall()
                reports[(station, day)] = {
                    "report_id": row["id"],
                    "revision": row["revision"],
                    "pollutants": {r["pollutant"] for r in items},
                }
            conn.execute("COMMIT")
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

        result = reconcile(samples, reports)
        return HandoverReconciliationResponse(snapshot_seq=snapshot, **result)

    return app


app = create_app()
