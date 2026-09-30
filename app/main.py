"""FastAPI 应用：样本交接链核验 HTTP API。"""
import json
import sqlite3
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import config
from .db import db_session, init_db, row_to_dict
from .schemas import ApproveIn, CorrectionIn, EventIn, SampleIn
from .service import (
    DomainError,
    apply_correction,
    approve_storage,
    chain_history,
    create_event,
    evaluate,
    get_sample_or_404,
    register_sample,
)

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if config.SEED_ON_START:
        # 仅当库中尚无样本时写入演示数据，重启不重复
        from .seed import seed_demo

        with db_session() as conn:
            count = conn.execute("SELECT COUNT(*) FROM sample").fetchone()[0]
        if count == 0:
            seed_demo()
    yield


app = FastAPI(
    title="样本交接链核验服务",
    description="环境检测机构样本保管链（Chain of Custody）登记、事件核验、纠错（作废/替换）与入库核定",
    version="1.1.0",
    lifespan=lifespan,
)


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}})


@app.exception_handler(sqlite3.OperationalError)
async def sqlite_lock_error_handler(request: Request, exc: sqlite3.OperationalError) -> JSONResponse:
    """写锁等待超时等并发冲突兜底：请求已整体回滚，链不变，可安全重试。"""
    return JSONResponse(
        status_code=409,
        content={"error": {"code": "concurrent_modification", "message": "并发写入冲突，本次操作未生效，请重试", "details": {"reason": str(exc)}}},
    )


def _summary(result: dict[str, Any], sample: dict[str, Any]) -> dict[str, Any]:
    return {
        "sample_code": result["sample_code"],
        "status": result["status"],
        "reasons": result["reasons"],
        "current_custodian": result["current_custodian"],
        "pending_transfer": result["pending_transfer"],
        "has_storage": result["has_storage"],
        "event_count": result["event_count"],
        "collected_at": sample["collected_at"],
        "collector": sample["collector"],
        "seal_no": sample["seal_no"],
        "approved_storage_at": sample["approved_storage_at"],
        "approved_by": sample["approved_by"],
    }


# --------------------------------------------------------------------------- 基本信息

@app.get("/", tags=["meta"])
def root() -> dict[str, Any]:
    return {
        "service": "样本交接链核验服务",
        "docs": "/docs",
        "health": "/health",
        "api_prefix": API_PREFIX,
        "status_values": {"valid": "有效", "pending": "待补证", "invalid": "无效"},
    }


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


# --------------------------------------------------------------------------- 样本登记

@app.post(f"{API_PREFIX}/samples", tags=["samples"], status_code=201)
def register_sample_api(data: SampleIn) -> JSONResponse:
    with db_session(immediate=True) as conn:
        sample, idempotent = register_sample(conn, data)
        result = evaluate(conn, sample)
        body = {
            "sample": {k: sample[k] for k in ("sample_code", "collected_at", "collector", "seal_no", "created_at")},
            "evaluation": _summary(result, dict(sample)),
            "idempotent": idempotent,
        }
    return JSONResponse(status_code=200 if idempotent else 201, content=body)


@app.get(f"{API_PREFIX}/samples", tags=["samples"])
def list_samples_api() -> dict[str, Any]:
    with db_session() as conn:
        samples = conn.execute("SELECT * FROM sample ORDER BY id").fetchall()
        items = [_summary(evaluate(conn, s), dict(s)) for s in samples]
    return {"total": len(items), "items": items}


@app.get(f"{API_PREFIX}/samples/{{sample_code}}", tags=["samples"])
def get_sample_api(sample_code: str) -> dict[str, Any]:
    with db_session() as conn:
        sample = get_sample_or_404(conn, sample_code)
        result = evaluate(conn, sample)
        return {"sample": row_to_dict(sample), "evaluation": _summary(result, dict(sample))}


@app.get(f"{API_PREFIX}/samples/{{sample_code}}/chain", tags=["samples"])
def get_chain_api(sample_code: str) -> dict[str, Any]:
    """完整证据链：核验只纳入有效事件；被作废/替换的事件与纠错记录留痕可追溯。"""
    with db_session() as conn:
        sample = get_sample_or_404(conn, sample_code)
        result = evaluate(conn, sample)
        timeline, corrections = chain_history(conn, sample)
        return {
            "sample": {k: sample[k] for k in ("sample_code", "collected_at", "collector", "seal_no")},
            "status": result["status"],
            "reasons": result["reasons"],
            "current_custodian": result["current_custodian"],
            "pending_transfer": result["pending_transfer"],
            "has_storage": result["has_storage"],
            "approved_storage_at": sample["approved_storage_at"],
            "timeline": timeline,
            "corrections": corrections,
        }


# --------------------------------------------------------------------------- 事件上报

@app.post(f"{API_PREFIX}/events", tags=["events"], status_code=201)
def create_event_api(data: EventIn) -> JSONResponse:
    """移交 / 签收 / 封签检查 / 入库 统一上报入口。

    同 event_id 同内容重试 → 200 幂等返回；同 event_id 异内容 → 409 冲突。
    事件号全局唯一且永久占用（含被作废/替换的历史事件）。
    """
    with db_session(immediate=True) as conn:
        row, idempotent = create_event(conn, data)
        sample = conn.execute("SELECT * FROM sample WHERE id = ?", (row["sample_id"],)).fetchone()
        result = evaluate(conn, sample)
        body = {
            "event": {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "sample_code": data.sample_code,
                "occurred_at": row["occurred_at"],
                "status": row["status"],
                "payload": json.loads(row["payload_json"]),
                "recorded_at": row["created_at"],
            },
            "evaluation": _summary(result, dict(sample)),
            "idempotent": idempotent,
        }
    return JSONResponse(status_code=200 if idempotent else 201, content=body)


# --------------------------------------------------------------------------- 事件纠错（作废/替换）

@app.post(f"{API_PREFIX}/corrections", tags=["corrections"], status_code=201)
def create_correction_api(data: CorrectionIn) -> JSONResponse:
    """核定前对当前有效事件作废（void）或替换（replace），全程留痕。

    同纠错号同内容重试 → 200 幂等返回；同纠错号异内容 → 409 冲突；
    目标事件须属于该样本且为当前有效版本；核定入库后拒绝纠错。
    """
    with db_session(immediate=True) as conn:
        row, idempotent = apply_correction(conn, data)
        sample = get_sample_or_404(conn, data.sample_code)
        result = evaluate(conn, sample)
        body = {
            "correction": {
                "correction_id": row["correction_id"],
                "sample_code": data.sample_code,
                "action": row["action"],
                "target_event_id": row["target_event_id"],
                "new_event_id": row["new_event_id"],
                "operator": row["operator"],
                "reason": row["reason"],
                "created_at": row["created_at"],
            },
            "evaluation": _summary(result, dict(sample)),
            "idempotent": idempotent,
        }
    return JSONResponse(status_code=200 if idempotent else 201, content=body)


# --------------------------------------------------------------------------- 入库核定

@app.post(f"{API_PREFIX}/samples/{{sample_code}}/approve", tags=["approval"])
def approve_api(sample_code: str, data: ApproveIn) -> dict[str, Any]:
    """仅有效链可核定；与纠错并发时，核定只基于最终提交的有效链。"""
    with db_session(immediate=True) as conn:
        result = approve_storage(conn, sample_code, data)
        sample = get_sample_or_404(conn, sample_code)
        return {"evaluation": _summary(result, dict(sample)), "approval": result["approval"]}
