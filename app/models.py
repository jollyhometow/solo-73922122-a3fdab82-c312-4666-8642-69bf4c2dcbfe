"""请求/响应的 Pydantic 模型与校验规则。"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal, Optional, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

# 有限非负浓度 / 有限正检出限 / 非负限值
NonNegFinite = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
PosFinite = Annotated[float, Field(gt=0.0, allow_inf_nan=False)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
# SQLite INTEGER 上限
Revision = Annotated[int, Field(strict=True, ge=1, le=9223372036854775807)]

MAX_HANDOVER_SAMPLES = 30


class ValueItem(BaseModel):
    """实测浓度测值（有限非负，mg/L）。"""

    model_config = ConfigDict(extra="forbid")
    type: Literal["value"]
    pollutant: Name
    value: NonNegFinite


class NdItem(BaseModel):
    """“未检出”测值，须带有限正检出限（mg/L）。"""

    model_config = ConfigDict(extra="forbid")
    type: Literal["nd"]
    pollutant: Name
    detection_limit: PosFinite


Item = Annotated[Union[ValueItem, NdItem], Field(discriminator="type")]


class ReportSubmission(BaseModel):
    """实验室按站点+采样日提交的完整日报（含递增修订号）。"""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "station": "ST-01",
                    "sampling_date": "2026-11-10",
                    "revision": 1,
                    "items": [
                        {"type": "value", "pollutant": "COD", "value": 12.3},
                        {"type": "nd", "pollutant": "NH3-N", "detection_limit": 0.05},
                    ],
                }
            ]
        },
    )
    station: Name
    sampling_date: dt.date
    revision: Revision
    items: list[Item] = Field(min_length=1, max_length=1000)

    @field_validator("items")
    @classmethod
    def _unique_pollutants(cls, items: list[Item]) -> list[Item]:
        seen: set[str] = set()
        for it in items:
            if it.pollutant in seen:
                raise ValueError(f"duplicate pollutant {it.pollutant!r} in one report")
            seen.add(it.pollutant)
        return items


class SubmitResponse(BaseModel):
    report_id: int
    station: str
    sampling_date: str
    revision: int
    status: str  # created | reused
    superseded_revision: Optional[int] = None


# ---------------- 交接对账 ----------------

class HandoverSample(BaseModel):
    """一条现场采样交接记录（补正可乱序、可改站点/日期）。"""

    model_config = ConfigDict(extra="forbid")
    sample_id: Name
    station: Name
    sampling_date: dt.date
    pollutants: list[Name] = Field(
        min_length=1,
        max_length=1000,
        description="应检污染物集合（重复项按集合去重）",
    )
    replaces_sample_id: Optional[Name] = Field(
        default=None,
        description="可选：本样本替换的旧样本 ID；自指或互相替换按成环处理",
    )


class HandoverReconciliationRequest(BaseModel):
    """一批最多 30 条交接样本；补正可乱序。"""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "samples": [
                        {
                            "sample_id": "S-002",
                            "station": "ST-01",
                            "sampling_date": "2026-11-11",
                            "pollutants": ["COD", "NH3-N"],
                            "replaces_sample_id": "S-001",
                        },
                        {
                            "sample_id": "S-001",
                            "station": "ST-01",
                            "sampling_date": "2026-11-10",
                            "pollutants": ["COD"],
                        },
                    ]
                }
            ]
        },
    )
    samples: list[HandoverSample] = Field(
        min_length=1,
        max_length=MAX_HANDOVER_SAMPLES,
        description=f"交接样本，最多 {MAX_HANDOVER_SAMPLES} 条",
    )

    @field_validator("samples")
    @classmethod
    def _unique_sample_ids(cls, samples: list[HandoverSample]) -> list[HandoverSample]:
        seen: set[str] = set()
        for s in samples:
            if s.sample_id in seen:
                raise ValueError(f"duplicate sample_id {s.sample_id!r}")
            seen.add(s.sample_id)
        return samples


class ResolvedSample(BaseModel):
    sample_id: str
    station: str
    sampling_date: str
    expected_pollutants: list[str]
    status: str
    # warehouseable | missing_report | missing_items | extra_items
    # | pending_verification | superseded | handover_conflict
    report_id: Optional[int] = None
    revision: Optional[int] = None
    missing_pollutants: Optional[list[str]] = None
    extra_pollutants: Optional[list[str]] = None
    reasons: Optional[list[str]] = None


class WarehouseableReport(BaseModel):
    sample_id: str
    station: str
    sampling_date: str
    expected_pollutants: list[str]
    report_id: int
    revision: int


class MissingReport(BaseModel):
    sample_id: str
    station: str
    sampling_date: str
    expected_pollutants: list[str]


class MissingItems(BaseModel):
    sample_id: str
    station: str
    sampling_date: str
    expected_pollutants: list[str]
    report_id: int
    revision: int
    missing_pollutants: list[str]


class ExtraItems(BaseModel):
    sample_id: str
    station: str
    sampling_date: str
    expected_pollutants: list[str]
    report_id: int
    revision: int
    extra_pollutants: list[str]


class ConflictDetail(BaseModel):
    sample_id: str
    station: str
    sampling_date: str
    replaces_sample_id: Optional[str] = None
    reasons: list[str]
    reasons_zh: list[str]


class HandoverConflict(BaseModel):
    station: str
    sampling_date: str
    sample_ids: list[str]
    reasons: list[str]
    reasons_zh: list[str]
    details: list[ConflictDetail]


class PendingVerification(BaseModel):
    station: str
    sampling_date: str
    reason: str
    reason_zh: str
    report_exists: bool
    report_id: Optional[int] = None
    revision: Optional[int] = None
    vacated_sample_ids: list[str]
    final_sample_ids: list[str]


class HandoverReconciliationResponse(BaseModel):
    submitted_samples: int
    snapshot_seq: int
    resolved_samples: list[ResolvedSample]
    warehouseable_reports: list[WarehouseableReport]
    missing_reports: list[MissingReport]
    missing_items: list[MissingItems]
    extra_items: list[ExtraItems]
    handover_conflicts: list[HandoverConflict]
    pending_verification: list[PendingVerification]
