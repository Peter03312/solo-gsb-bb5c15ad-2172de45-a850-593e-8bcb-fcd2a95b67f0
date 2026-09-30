"""零依赖（仅标准库）的 HTTP API 调用示例：完整演示登记→移交→签收→检查→入库→查询→核定。

使用前：
    pip install 无额外要求（urllib 为 Python 标准库）
    先启动服务：docker compose up -d   或   python -m uvicorn app.main:app --port 8080
运行：
    python examples/call_api.py
"""
import json
import urllib.error
import urllib.request
import uuid

BASE = "http://127.0.0.1:8080/api/v1"


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def show(title: str, status: int, resp: dict, *keys: str) -> None:
    print(f"\n--- {title} [HTTP {status}] ---")
    out = resp
    for k in keys:
        out = out.get(k, {}) if isinstance(out, dict) else {}
    print(json.dumps(out, ensure_ascii=False, indent=2))


def main() -> None:
    # 每次运行使用独立编号，脚本可任意重复执行
    run_id = uuid.uuid4().hex[:8]
    code = f"API-DEMO-{run_id}"

    # 1) 样本登记：采集时间、采集人（初始保管人）、封签号
    s, r = call("POST", "/samples", {
        "sample_code": code,
        "collected_at": "2026-09-30T01:00:00Z",
        "collector": "王采集",
        "seal_no": "SEAL-DEMO-1",
    })
    show("样本登记", s, r, "evaluation")

    # 2) 移交：必须由当前保管人发起，指定移交单号与接收人
    s, r = call("POST", "/events", {
        "event_id": f"API-EVT-T1-{run_id}", "event_type": "transfer", "sample_code": code,
        "occurred_at": "2026-09-30T02:00:00Z",
        "transfer_no": "TR-DEMO-1", "from_custodian": "王采集", "to_custodian": "李运输",
    })
    show("移交（签收前=待补证）", s, r, "evaluation")

    # 3) 签收：单号/接收人/封签号三匹配，且晚于移交
    s, r = call("POST", "/events", {
        "event_id": f"API-EVT-R1-{run_id}", "event_type": "receive", "sample_code": code,
        "occurred_at": "2026-09-30T04:00:00Z",
        "transfer_no": "TR-DEMO-1", "receiver": "李运输", "seal_no": "SEAL-DEMO-1",
    })
    show("签收", s, r, "evaluation")

    # 4) 封签检查
    s, r = call("POST", "/events", {
        "event_id": f"API-EVT-C1-{run_id}", "event_type": "seal_check", "sample_code": code,
        "occurred_at": "2026-09-30T04:30:00Z",
        "checked_seal_no": "SEAL-DEMO-1", "seal_intact": True,
    })
    show("封签检查", s, r, "evaluation")

    # 5) 实验室入库（缺此节点结论为待补证）
    s, r = call("POST", "/events", {
        "event_id": f"API-EVT-S1-{run_id}", "event_type": "storage", "sample_code": code,
        "occurred_at": "2026-09-30T05:00:00Z",
        "custodian": "实验室", "storage_location": "冷库A-01",
    })
    show("实验室入库（此时有效）", s, r, "evaluation")

    # 6) 幂等重试：同事件号同内容 → 200
    s, r = call("POST", "/events", {
        "event_id": f"API-EVT-T1-{run_id}", "event_type": "transfer", "sample_code": code,
        "occurred_at": "2026-09-30T02:00:00Z",
        "transfer_no": "TR-DEMO-1", "from_custodian": "王采集", "to_custodian": "李运输",
    })
    show("同事件号同内容重试（幂等）", s, r, "idempotent")

    # 7) 冲突：同事件号异内容 → 409
    s, r = call("POST", "/events", {
        "event_id": f"API-EVT-T1-{run_id}", "event_type": "transfer", "sample_code": code,
        "occurred_at": "2026-09-30T02:30:00Z",  # 时间被改动
        "transfer_no": "TR-DEMO-1", "from_custodian": "王采集", "to_custodian": "李运输",
    })
    show("同事件号异内容（冲突）", s, r)

    # 8) 查询完整证据链（按发生时间排列）
    s, r = call("GET", f"/samples/{code}/chain")
    print(f"\n--- 证据链 [HTTP {s}] ---")
    print("结论:", r["status"], "| 当前保管人:", r["current_custodian"])
    for e in r["timeline"]:
        print(" ", e["occurred_at"], e["event_type"], e["event_id"])

    # 9) 核定入库（仅 valid 可核定；核定后冻结）
    s, r = call("POST", f"/samples/{code}/approve", {"approver": "质量主管", "note": "准予入库"})
    show("核定入库", s, r, "approval")

    # 10) 核定后再写事件 → 409 history_locked
    s, r = call("POST", "/events", {
        "event_id": f"API-EVT-LATE-{run_id}", "event_type": "seal_check", "sample_code": code,
        "occurred_at": "2026-09-30T06:00:00Z", "checked_seal_no": "SEAL-DEMO-1",
    })
    show("核定后改写（拒绝）", s, r)


if __name__ == "__main__":
    main()
