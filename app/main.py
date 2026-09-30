"""FastAPI 应用：样本交接链核验 HTTP API。"""
import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import config
from .db import db_session, init_db, row_to_dict
from .schemas import ApproveIn, EventIn, SampleIn
from .service import DomainError, approve_storage, create_event, evaluate, get_sample_or_404, register_sample

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
    description="环境检测机构样本保管链（Chain of Custody）登记、事件核验与入库核定",
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}})


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
    with db_session() as conn:
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
    """完整证据链：按时间排列的事件、当前保管人、结论与原因。"""
    with db_session() as conn:
        sample = get_sample_or_404(conn, sample_code)
        result = evaluate(conn, sample)
        return {
            "sample": {k: sample[k] for k in ("sample_code", "collected_at", "collector", "seal_no")},
            "status": result["status"],
            "reasons": result["reasons"],
            "current_custodian": result["current_custodian"],
            "pending_transfer": result["pending_transfer"],
            "has_storage": result["has_storage"],
            "approved_storage_at": sample["approved_storage_at"],
            "timeline": result["timeline"],
        }


# --------------------------------------------------------------------------- 事件上报

@app.post(f"{API_PREFIX}/events", tags=["events"], status_code=201)
def create_event_api(data: EventIn) -> JSONResponse:
    """移交 / 签收 / 封签检查 / 入库 统一上报入口。

    同 event_id 同内容重试 → 200 幂等返回；同 event_id 异内容 → 409 冲突。
    """
    with db_session() as conn:
        row, idempotent = create_event(conn, data)
        sample = conn.execute("SELECT * FROM sample WHERE id = ?", (row["sample_id"],)).fetchone()
        result = evaluate(conn, sample)
        body = {
            "event": {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "sample_code": data.sample_code,
                "occurred_at": row["occurred_at"],
                "payload": json.loads(row["payload_json"]),
                "recorded_at": row["created_at"],
            },
            "evaluation": _summary(result, dict(sample)),
            "idempotent": idempotent,
        }
    return JSONResponse(status_code=200 if idempotent else 201, content=body)


# --------------------------------------------------------------------------- 入库核定

@app.post(f"{API_PREFIX}/samples/{{sample_code}}/approve", tags=["approval"])
def approve_api(sample_code: str, data: ApproveIn) -> dict[str, Any]:
    with db_session() as conn:
        result = approve_storage(conn, sample_code, data)
        sample = get_sample_or_404(conn, sample_code)
        return {"evaluation": _summary(result, dict(sample)), "approval": result["approval"]}
