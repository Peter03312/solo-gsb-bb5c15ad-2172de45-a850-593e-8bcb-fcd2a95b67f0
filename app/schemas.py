"""HTTP 请求体的 Pydantic 模型与按事件类型的必填字段校验。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

EventType = Literal["transfer", "receive", "seal_check", "storage"]
CorrectionAction = Literal["void", "replace"]


class SampleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_code: str = Field(..., min_length=1, description="样本编号，业务唯一键")
    collected_at: datetime = Field(..., description="采集时间 ISO8601")
    collector: str = Field(..., min_length=1, description="采集人，即初始保管人")
    seal_no: str = Field(..., min_length=1, description="封签号")


class _EventBase(BaseModel):
    """事件公共字段与按类型必填校验（事件上报与纠错替换共用）。"""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(..., min_length=1, description="全局唯一事件号，同号重试幂等")
    event_type: EventType
    occurred_at: datetime = Field(..., description="事件发生时间 ISO8601")

    # transfer（移交）
    transfer_no: str | None = Field(None, description="移交单号（transfer 必填）")
    from_custodian: str | None = Field(None, description="移交发起/交出方（transfer 必填，须为当前保管人）")
    to_custodian: str | None = Field(None, description="接收方（transfer 必填）")

    # receive（签收）
    receiver: str | None = Field(None, description="实际签收人（receive 必填，须等于移交的接收方）")
    seal_no: str | None = Field(None, description="签收核对封签号（receive 必填，须与登记一致）")

    # seal_check（封签检查）
    checked_seal_no: str | None = Field(None, description="检查时读取的封签号（seal_check 必填）")
    seal_intact: bool | None = Field(None, description="封签是否完好，默认 true；false 判无效")

    # storage（实验室入库）
    storage_location: str | None = Field(None, description="入库库位（storage 建议填写）")
    custodian: str | None = Field(None, description="入库后保管人，默认实验室")

    note: str | None = Field(None, description="备注，参与幂等指纹")

    @model_validator(mode="after")
    def _check_required_fields(self) -> "_EventBase":
        required: dict[str, list[str]] = {
            "transfer": ["transfer_no", "from_custodian", "to_custodian"],
            "receive": ["transfer_no", "receiver", "seal_no"],
            "seal_check": ["checked_seal_no"],
            "storage": [],
        }
        missing = [f for f in required[self.event_type] if not getattr(self, f)]
        if missing:
            raise ValueError(f"事件类型 {self.event_type} 缺少必填字段: {', '.join(missing)}")
        return self


class EventIn(_EventBase):
    """事件上报：样本编号随事件给出。"""

    sample_code: str = Field(..., min_length=1, description="样本编号")


class ReplacementEventIn(_EventBase):
    """纠错替换产生的新事件：样本由纠错单固定（纠错不得跨样本），事件号须全局未占用。"""


class CorrectionIn(BaseModel):
    """作废/替换纠错单：核定前对当前有效事件进行纠错，全程留痕。"""

    model_config = ConfigDict(extra="forbid")

    correction_id: str = Field(..., min_length=1, description="全局唯一纠错号，同号同内容重试幂等")
    sample_code: str = Field(..., min_length=1, description="样本编号（纠错不得跨样本）")
    action: CorrectionAction = Field(..., description="void=作废 / replace=替换")
    target_event_id: str = Field(..., min_length=1, description="被纠错的当前有效事件号")
    operator: str = Field(..., min_length=1, description="操作人")
    reason: str = Field(..., min_length=1, description="纠错原因")
    new_event: ReplacementEventIn | None = Field(
        None, description="action=replace 时必填：完整的新事件内容（含未占用的新事件号）"
    )

    @model_validator(mode="after")
    def _check_action_fields(self) -> "CorrectionIn":
        if self.action == "replace":
            if self.new_event is None:
                raise ValueError("action=replace 时必须提供完整的新事件内容 new_event（含未占用的新事件号）")
            if self.new_event.event_id == self.target_event_id:
                raise ValueError("新事件号必须与被替换的事件号不同")
        elif self.new_event is not None:
            raise ValueError("action=void 时不得提供 new_event")
        return self


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approver: str = Field(..., min_length=1, description="核定操作人")
    note: str | None = Field(None, description="核定备注")
