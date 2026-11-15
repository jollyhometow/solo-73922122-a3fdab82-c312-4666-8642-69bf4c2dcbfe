"""请求/响应的 Pydantic 模型与校验规则。"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

# 有限非负浓度 / 有限正检出限 / 非负限值
NonNegFinite = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
PosFinite = Annotated[float, Field(gt=0.0, allow_inf_nan=False)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
# SQLite INTEGER 上限
Revision = Annotated[int, Field(strict=True, ge=1, le=9223372036854775807)]


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
