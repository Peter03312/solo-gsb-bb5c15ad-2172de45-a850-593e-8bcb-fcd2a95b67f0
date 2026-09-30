"""HTTP 请求体的 Pydantic 模型与按事件类型的必填字段校验。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

EventType = Literal["transfer", "receive", "seal_check", "storage"]


class SampleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_code: str = Field(..., min_length=1, description="样本编号，业务唯一键")
    collected_at: datetime = Field(..., description="采集时间 ISO8601")
    collector: str = Field(..., min_length=1, description="采集人，即初始保管人")
    seal_no: str = Field(..., min_length=1, description="封签号")


class EventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(..., min_length=1, description="全局唯一事件号，同号重试幂等")
    event_type: EventType
    sample_code: str = Field(..., min_length=1, description="样本编号")
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
    def _check_required_fields(self) -> "EventIn":
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


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approver: str = Field(..., min_length=1, description="核定操作人")
    note: str | None = Field(None, description="核定备注")
