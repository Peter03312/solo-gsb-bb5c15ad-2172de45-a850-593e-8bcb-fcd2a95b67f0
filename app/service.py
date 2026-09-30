"""领域服务：登记、事件写入（幂等/冲突）、纠错（作废/替换）、保管链重算与入库核定。

核验规则（与 README 一致）：
  * 移交必须由当前保管人发起；移交至签收期间不得再次移交；
  * 签收必须匹配移交单号、接收人、封签号，且发生在移交之后；重复签收无效；
  * 封签检查封签号不符或封签破损 → 无效；
  * 缺签收 / 缺入库 → 待补证；
  * 断链、重复签收、封签不符 → 无效，无效优先于待补证；
  * 乱序补录按发生时间排序后整体重算；
  * 纠错（核定前）：按样本编号 + 当前有效事件号作废/替换，附全局唯一纠错号、
    操作人、原因；替换须给出完整新事件与未占用新事件号；原事件与纠错关系永久
    留痕，核验只纳入有效（active）版本；纠错不得跨样本；同纠错号同内容幂等、
    异内容冲突；并发针对同一有效版本仅一次成功，失败不改链；核定后拒绝纠错；
  * 仅有效链可核定入库；核定后历史冻结（含纠错）。
"""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import config
from .schemas import ApproveIn, CorrectionIn, EventIn, SampleIn

LAB_CUSTODIAN = "实验室"

EVENT_TYPES = ("transfer", "receive", "seal_check", "storage")


class DomainError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


# --------------------------------------------------------------------------- 通用工具

def to_utc_iso(dt: datetime) -> str:
    """统一规范为 UTC ISO8601（秒精度、Z 结尾），无时区输入按 UTC 处理。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _fingerprint(obj: dict[str, Any]) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_sample_or_404(conn: sqlite3.Connection, sample_code: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM sample WHERE sample_code = ?", (sample_code,)).fetchone()
    if row is None:
        raise DomainError(404, "sample_not_found", f"样本不存在: {sample_code}")
    return row


def ensure_unlocked(sample: sqlite3.Row) -> None:
    if config.LOCK_AFTER_APPROVAL and sample["approved_storage_at"]:
        raise DomainError(
            409,
            "history_locked",
            f"样本 {sample['sample_code']} 已核定入库（{sample['approved_storage_at']}），历史记录冻结，禁止改写",
        )


# --------------------------------------------------------------------------- 样本登记

def register_sample(conn: sqlite3.Connection, data: SampleIn) -> tuple[sqlite3.Row, bool]:
    """返回 (样本行, 是否幂等命中已有记录)。"""
    canonical = {
        "sample_code": data.sample_code,
        "collected_at": to_utc_iso(data.collected_at),
        "collector": data.collector,
        "seal_no": data.seal_no,
    }
    h = _fingerprint(canonical)
    existing = conn.execute("SELECT * FROM sample WHERE sample_code = ?", (data.sample_code,)).fetchone()
    if existing is not None:
        if existing["content_hash"] != h:
            raise DomainError(
                409,
                "sample_conflict",
                f"样本编号 {data.sample_code} 已登记但内容不一致（同编号异内容冲突）",
                {"existing_content_hash": existing["content_hash"], "incoming_content_hash": h},
            )
        return existing, True

    from .db import utcnow_iso

    cur = conn.execute(
        "INSERT INTO sample (sample_code, collected_at, collector, seal_no, content_hash, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (data.sample_code, canonical["collected_at"], data.collector, data.seal_no, h, utcnow_iso()),
    )
    row = conn.execute("SELECT * FROM sample WHERE id = ?", (cur.lastrowid,)).fetchone()
    return row, False


# --------------------------------------------------------------------------- 事件写入

def _event_canonical(data: EventIn) -> tuple[dict[str, Any], dict[str, Any]]:
    """构造参与指纹的规范字典与落库 payload（字段集随事件类型固定）。"""
    occurred = to_utc_iso(data.occurred_at)
    payload: dict[str, Any]
    if data.event_type == "transfer":
        payload = {
            "transfer_no": data.transfer_no,
            "from_custodian": data.from_custodian,
            "to_custodian": data.to_custodian,
        }
    elif data.event_type == "receive":
        payload = {"transfer_no": data.transfer_no, "receiver": data.receiver, "seal_no": data.seal_no}
    elif data.event_type == "seal_check":
        payload = {"checked_seal_no": data.checked_seal_no, "seal_intact": bool(data.seal_intact)}
    else:  # storage
        payload = {"custodian": data.custodian or LAB_CUSTODIAN}
        if data.storage_location is not None:
            payload["storage_location"] = data.storage_location

    canonical: dict[str, Any] = {
        "event_id": data.event_id,
        "event_type": data.event_type,
        "sample_code": data.sample_code,
        "occurred_at": occurred,
        **payload,
    }
    if data.note is not None:
        canonical["note"] = data.note
        payload = {**payload, "note": data.note}
    return canonical, payload


def create_event(conn: sqlite3.Connection, data: EventIn) -> tuple[sqlite3.Row, bool]:
    """写入事件。返回 (事件行, 是否幂等重复提交)。

    同 event_id + 同内容 → 幂等返回；同 event_id + 异内容 → 409 冲突。
    """
    sample = get_sample_or_404(conn, data.sample_code)
    ensure_unlocked(sample)

    canonical, payload = _event_canonical(data)
    h = _fingerprint(canonical)
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    existing = conn.execute("SELECT * FROM custody_event WHERE event_id = ?", (data.event_id,)).fetchone()
    if existing is not None:
        if existing["content_hash"] != h:
            raise DomainError(
                409,
                "event_conflict",
                f"事件号 {data.event_id} 已存在但提交内容不一致（同事件号异内容冲突）",
                {
                    "event_id": data.event_id,
                    "existing": {
                        "event_type": existing["event_type"],
                        "occurred_at": existing["occurred_at"],
                        "content_hash": existing["content_hash"],
                    },
                    "incoming": {"event_type": data.event_type, "occurred_at": canonical["occurred_at"], "content_hash": h},
                },
            )
        return existing, True

    from .db import utcnow_iso

    try:
        cur = conn.execute(
            "INSERT INTO custody_event (sample_id, event_id, event_type, occurred_at, payload_json, content_hash, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (sample["id"], data.event_id, data.event_type, canonical["occurred_at"], payload_json, h, utcnow_iso()),
        )
    except sqlite3.IntegrityError:
        # 并发下唯一键竞争：重新读取后按幂等/冲突处理
        existing = conn.execute("SELECT * FROM custody_event WHERE event_id = ?", (data.event_id,)).fetchone()
        if existing is not None and existing["content_hash"] == h:
            return existing, True
        raise DomainError(409, "event_conflict", f"事件号 {data.event_id} 并发写入且内容冲突")

    row = conn.execute("SELECT * FROM custody_event WHERE id = ?", (cur.lastrowid,)).fetchone()
    return row, False


# --------------------------------------------------------------------------- 事件纠错（作废/替换）

def apply_correction(conn: sqlite3.Connection, data: CorrectionIn) -> tuple[sqlite3.Row, bool]:
    """作废/替换纠错。返回 (纠错记录行, 是否幂等重复提交)。

    规则：仅核定前可纠错；目标事件须属于该样本且为当前有效版本（不得跨样本）；
    替换须给出完整新事件与未占用的新事件号；同纠错号同内容幂等、异内容冲突；
    并发针对同一有效版本仅一次成功（原子状态翻转），任何失败整体回滚、链不变。
    调用方须以 immediate 事务进入（见 db_session），保证读改写串行。
    """
    sample = get_sample_or_404(conn, data.sample_code)
    ensure_unlocked(sample)  # 核定入库后拒绝纠错

    # 纠错内容指纹：同纠错号重试比对（含替换的完整新事件内容）
    canonical: dict[str, Any] = {
        "correction_id": data.correction_id,
        "sample_code": data.sample_code,
        "action": data.action,
        "target_event_id": data.target_event_id,
        "operator": data.operator,
        "reason": data.reason,
    }
    new_event_in: EventIn | None = None
    new_payload_json: str | None = None
    new_event_hash: str | None = None
    new_occurred_at: str | None = None
    if data.action == "replace" and data.new_event is not None:
        # 样本由纠错单固定，纠错不得跨样本
        new_event_in = EventIn(sample_code=data.sample_code, **data.new_event.model_dump())
        ev_canonical, ev_payload = _event_canonical(new_event_in)
        canonical["new_event"] = ev_canonical
        new_payload_json = json.dumps(ev_payload, ensure_ascii=False, separators=(",", ":"))
        new_event_hash = _fingerprint(ev_canonical)
        new_occurred_at = ev_canonical["occurred_at"]
    h = _fingerprint(canonical)

    existing = conn.execute("SELECT * FROM correction WHERE correction_id = ?", (data.correction_id,)).fetchone()
    if existing is not None:
        if existing["content_hash"] != h:
            raise DomainError(
                409,
                "correction_conflict",
                f"纠错号 {data.correction_id} 已提交但内容不一致（同纠错号异内容冲突）",
                {"existing_content_hash": existing["content_hash"], "incoming_content_hash": h},
            )
        return existing, True

    target = conn.execute("SELECT * FROM custody_event WHERE event_id = ?", (data.target_event_id,)).fetchone()
    if target is None:
        raise DomainError(404, "event_not_found", f"事件不存在: {data.target_event_id}",
                          {"target_event_id": data.target_event_id})
    if target["sample_id"] != sample["id"]:
        raise DomainError(
            409,
            "cross_sample_correction",
            f"纠错不得跨样本：事件 {data.target_event_id} 不属于样本 {data.sample_code}",
            {"target_event_id": data.target_event_id, "sample_code": data.sample_code},
        )
    if target["status"] != "active":
        raise DomainError(
            409,
            "event_not_active",
            f"事件 {data.target_event_id} 当前状态为 {target['status']}，仅当前有效版本可作废/替换",
            {"target_event_id": data.target_event_id, "status": target["status"],
             "correction_id": target["correction_id"]},
        )

    if new_event_in is not None:
        clash = conn.execute("SELECT id FROM custody_event WHERE event_id = ?", (new_event_in.event_id,)).fetchone()
        if clash is not None:
            raise DomainError(
                409,
                "event_conflict",
                f"替换的新事件号 {new_event_in.event_id} 已被占用（事件号全局唯一，含已作废/替换的历史号）",
                {"new_event_id": new_event_in.event_id},
            )

    from .db import utcnow_iso

    now = utcnow_iso()
    new_status = "replaced" if data.action == "replace" else "voided"

    # 原子翻转目标事件状态：并发针对同一有效版本只允许一次成功，失败者整体回滚、链不变
    cur = conn.execute(
        "UPDATE custody_event SET status = ?, correction_id = ? WHERE event_id = ? AND status = 'active'",
        (new_status, data.correction_id, data.target_event_id),
    )
    if cur.rowcount != 1:
        raise DomainError(
            409,
            "event_not_active",
            f"事件 {data.target_event_id} 已被并发纠错处理，本次操作未生效",
            {"target_event_id": data.target_event_id},
        )

    if new_event_in is not None:
        try:
            conn.execute(
                "INSERT INTO custody_event (sample_id, event_id, event_type, occurred_at, payload_json,"
                " content_hash, status, created_at) VALUES (?,?,?,?,?,?, 'active', ?)",
                (sample["id"], new_event_in.event_id, new_event_in.event_type, new_occurred_at,
                 new_payload_json, new_event_hash, now),
            )
        except sqlite3.IntegrityError:
            raise DomainError(409, "event_conflict", f"替换的新事件号 {new_event_in.event_id} 并发写入冲突")

    try:
        conn.execute(
            "INSERT INTO correction (correction_id, sample_id, action, target_event_id, new_event_id,"
            " operator, reason, content_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (data.correction_id, sample["id"], data.action, data.target_event_id,
             new_event_in.event_id if new_event_in else None, data.operator, data.reason, h, now),
        )
    except sqlite3.IntegrityError:
        # 同纠错号并发竞争（immediate 事务下不会出现，防御性兜底）：整体回滚，链不变
        raise DomainError(409, "correction_conflict", f"纠错号 {data.correction_id} 并发写入冲突")

    row = conn.execute("SELECT * FROM correction WHERE correction_id = ?", (data.correction_id,)).fetchone()
    return row, False


def chain_history(conn: sqlite3.Connection, sample: sqlite3.Row) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """链查询留痕：全部事件（含被作废/替换版本，带状态与纠错号标注）+ 纠错记录。"""
    rows = conn.execute(
        "SELECT * FROM custody_event WHERE sample_id = ? ORDER BY occurred_at ASC, id ASC",
        (sample["id"],),
    ).fetchall()
    timeline = [
        {
            "event_id": ev["event_id"],
            "event_type": ev["event_type"],
            "occurred_at": ev["occurred_at"],
            "recorded_at": ev["created_at"],
            "payload": json.loads(ev["payload_json"]),
            "status": ev["status"],                # active=参与核验；voided/replaced=仅留痕
            "correction_id": ev["correction_id"],  # 使其失效的纠错号
        }
        for ev in rows
    ]
    cors = conn.execute(
        "SELECT * FROM correction WHERE sample_id = ? ORDER BY id ASC", (sample["id"],)
    ).fetchall()
    corrections = [
        {
            "correction_id": c["correction_id"],
            "action": c["action"],
            "target_event_id": c["target_event_id"],
            "new_event_id": c["new_event_id"],
            "operator": c["operator"],
            "reason": c["reason"],
            "created_at": c["created_at"],
        }
        for c in cors
    ]
    return timeline, corrections


# --------------------------------------------------------------------------- 保管链重算

def evaluate(conn: sqlite3.Connection, sample: sqlite3.Row) -> dict[str, Any]:
    """按发生时间重算该样本的完整证据链，输出结论。

    只纳入当前有效（active）事件：被作废/替换的历史版本不参与核验，
    但仍在 custody_event 中留痕，可通过链查询追溯。
    """
    events = conn.execute(
        "SELECT * FROM custody_event WHERE sample_id = ? AND status = 'active'"
        " ORDER BY occurred_at ASC, id ASC",
        (sample["id"],),
    ).fetchall()

    current = sample["collector"]                       # 当前保管人（初始为采集人）
    open_transfer: dict[str, Any] | None = None         # 在途移交（已移交未签收）
    closed_transfer_nos: set[str] = set()               # 已正常签收的移交单
    last_receive_at: str | None = None
    stored = False
    storage_without_chain = False                       # 入库节点缺失前序签收（链不完整）
    invalid_reasons: list[str] = []
    timeline: list[dict[str, Any]] = []

    # 预索引全部移交单（含乱序补录），用于把"签收早于移交"与"单号根本不存在"区分开
    transfer_by_no: dict[str, dict[str, Any]] = {}
    for ev0 in events:
        if ev0["event_type"] == "transfer":
            transfer_by_no[json.loads(ev0["payload_json"])["transfer_no"]] = {
                "row": ev0,
                "payload": json.loads(ev0["payload_json"]),
            }

    for ev in events:
        p = json.loads(ev["payload_json"])
        ref = f"[事件 {ev['event_id']}]"
        timeline.append(
            {
                "event_id": ev["event_id"],
                "event_type": ev["event_type"],
                "occurred_at": ev["occurred_at"],
                "recorded_at": ev["created_at"],
                "payload": p,
            }
        )

        # 任何事件早于采集时间：记录无效（不可能在登记采集前发生保管动作）
        if ev["occurred_at"] < sample["collected_at"]:
            invalid_reasons.append(f"{ref} 事件发生时间 {ev['occurred_at']} 早于样本采集时间 {sample['collected_at']}")
            continue

        et = ev["event_type"]

        if et == "transfer":
            if open_transfer is not None:
                invalid_reasons.append(
                    f"{ref} 移交至签收期间再次移交：移交单 {open_transfer['transfer_no']}"
                    f"（{open_transfer['from_custodian']} → {open_transfer['to_custodian']}）尚未签收"
                )
                continue
            frm, to, no = p["from_custodian"], p["to_custodian"], p["transfer_no"]
            if frm != current:
                invalid_reasons.append(
                    f"{ref} 移交人 {frm} 不是当前保管人 {current}（移交单 {no}，保管人断链）"
                )
                continue
            open_transfer = {"transfer_no": no, "from_custodian": frm, "to_custodian": to, "occurred_at": ev["occurred_at"]}
            # 签收完成前保管人不变，仍为移交人

        elif et == "receive":
            no = p["transfer_no"]
            if no in closed_transfer_nos:
                invalid_reasons.append(f"{ref} 重复签收：移交单 {no} 已被签收")
                continue
            known = transfer_by_no.get(no)
            if open_transfer is None or open_transfer["transfer_no"] != no:
                if known is not None and ev["occurred_at"] <= known["row"]["occurred_at"]:
                    invalid_reasons.append(
                        f"{ref} 签收时间 {ev['occurred_at']} 不晚于移交时间 {known['row']['occurred_at']}（单号 {no}）"
                    )
                    # 该移交到达时在途状态仍可能被后续正确签收关闭，故此处不改在途状态
                    continue
                invalid_reasons.append(f"{ref} 签收无对应在途移交：单号 {no} 未移交或不匹配（保管人断链）")
                continue

            mismatch = False
            if p["receiver"] != open_transfer["to_custodian"]:
                invalid_reasons.append(
                    f"{ref} 签收接收人不匹配：签收人 {p['receiver']} ≠ 移交单 {no} 指定接收人 {open_transfer['to_custodian']}"
                )
                mismatch = True
            if p["seal_no"] != sample["seal_no"]:
                invalid_reasons.append(
                    f"{ref} 封签号不符：签收封签 {p['seal_no']} ≠ 登记封签 {sample['seal_no']}"
                )
                mismatch = True
            if ev["occurred_at"] <= open_transfer["occurred_at"]:
                invalid_reasons.append(
                    f"{ref} 签收时间 {ev['occurred_at']} 不晚于移交时间 {open_transfer['occurred_at']}（单号 {no}）"
                )
                mismatch = True
            if mismatch:
                # 在途移交保留：允许后续按发生时间补录正确签收
                continue

            closed_transfer_nos.add(no)
            current = p["receiver"]
            last_receive_at = ev["occurred_at"]
            open_transfer = None

        elif et == "seal_check":
            if p["checked_seal_no"] != sample["seal_no"]:
                invalid_reasons.append(
                    f"{ref} 封签检查封签号不符：检查封签 {p['checked_seal_no']} ≠ 登记封签 {sample['seal_no']}"
                )
            if not p.get("seal_intact", True):
                invalid_reasons.append(f"{ref} 封签检查发现封签破损/不完好")

        else:  # storage
            if last_receive_at is not None and ev["occurred_at"] < last_receive_at:
                invalid_reasons.append(
                    f"{ref} 入库时间 {ev['occurred_at']} 早于最终签收时间 {last_receive_at}（保管链不成立）"
                )
                continue
            if open_transfer is not None or last_receive_at is None:
                # 尚无已完成签收（或还有在途移交）：入库节点缺失前序保管链 → 待补证，保管人不变更
                storage_without_chain = True
                continue
            stored = True
            current = p.get("custodian") or LAB_CUSTODIAN

    # ------------------------------------------------------------ 汇总（无效优先）
    pending_reasons: list[str] = []
    if open_transfer is not None:
        pending_reasons.append(
            f"移交单 {open_transfer['transfer_no']} 已移交但缺少签收"
            f"（{open_transfer['from_custodian']} → {open_transfer['to_custodian']}，移交时间 {open_transfer['occurred_at']}）"
        )
    elif storage_without_chain:
        pending_reasons.append("存在入库记录但缺少此前完整的移交-签收链，入库不能成立")
    if not stored:
        pending_reasons.append("缺少实验室入库记录")

    if invalid_reasons:
        status = "invalid"
        reasons = invalid_reasons + [f"另有待补证：{r}" for r in pending_reasons]
    elif pending_reasons:
        status = "pending"
        reasons = pending_reasons
    else:
        status = "valid"
        reasons = [f"保管链完整：采集 {sample['collector']} 至当前保管人 {current}，封签一致，已入库"]

    return {
        "sample_code": sample["sample_code"],
        "status": status,
        "reasons": reasons,
        "current_custodian": current,
        "pending_transfer": open_transfer,
        "has_storage": stored,
        "approved_storage_at": sample["approved_storage_at"],
        "event_count": len(events),
        "timeline": timeline,
    }


# --------------------------------------------------------------------------- 入库核定

def approve_storage(conn: sqlite3.Connection, sample_code: str, data: ApproveIn) -> dict[str, Any]:
    sample = get_sample_or_404(conn, sample_code)
    result = evaluate(conn, sample)

    if sample["approved_storage_at"]:
        # 已核定：幂等返回，不允许借重复核定改写
        result["approval"] = {"approved": True, "approved_at": sample["approved_storage_at"], "approver": sample["approved_by"]}
        return result

    if result["status"] != "valid":
        raise DomainError(
            422,
            "approval_rejected",
            f"样本 {sample_code} 当前结论为 {result['status']}，仅有效（valid）保管链可核定入库",
            {"evaluation": result},
        )

    from .db import utcnow_iso

    approved_at = utcnow_iso()
    conn.execute(
        "UPDATE sample SET approved_storage_at = ?, approved_by = ?, approve_note = ? WHERE id = ?",
        (approved_at, data.approver, data.note, sample["id"]),
    )
    result["approved_storage_at"] = approved_at
    result["approval"] = {"approved": True, "approved_at": approved_at, "approver": data.approver, "note": data.note}
    return result
