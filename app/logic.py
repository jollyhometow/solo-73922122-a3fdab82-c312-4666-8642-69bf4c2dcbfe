"""判读与内容规范化逻辑（纯函数，便于测试）。

判读规则：
- 窗口为结束日（含）向前连续 7 天；
- 每个测值对均值的贡献是一个区间：实测浓度 v → [v, v]；
  “未检出”（nd）→ 半开区间 [0, 检出限)；
- 七天均值的可能区间为 [mean_lo, mean_hi]（含 nd 时上界为开）；
- mean_hi <= 限值  → 均值必定不高于限值 → 达标 compliant；
- mean_lo >  限值  → 均值必定高于限值   → 超标 exceeds；
- 其余（区间跨过限值）              → 待复核 pending_review；
- 缺日或缺项目                     → 资料不足 insufficient_data。

均值上下界用 Fraction 精确计算（按提交数值的十进制语义），
避免 0.1 这类十进制数在边界比较时的舍入歧义。
"""

from __future__ import annotations

import hashlib
import json
from fractions import Fraction
from typing import Any, Iterable, Optional

STATUS_COMPLIANT = "compliant"              # 达标
STATUS_EXCEEDS = "exceeds"                  # 超标
STATUS_REVIEW = "pending_review"            # 待复核
STATUS_INSUFFICIENT = "insufficient_data"   # 资料不足

STATUS_ZH = {
    STATUS_COMPLIANT: "达标",
    STATUS_EXCEEDS: "超标",
    STATUS_REVIEW: "待复核",
    STATUS_INSUFFICIENT: "资料不足",
}

WINDOW_DAYS = 7


def _num_repr(x: float) -> str:
    x = float(x)
    if x == 0:
        x = 0.0  # 归一化 -0.0
    return repr(x)


def canonical_content(items: Iterable[dict[str, Any]]) -> str:
    """报告内容的规范化表示：按污染物名排序，数值用最短往返表示。

    用于同修订重传的内容比对（顺序无关、数值等价即相同）。
    """
    norm = []
    for it in items:
        if it["type"] == "value":
            norm.append([it["pollutant"], "value", _num_repr(it["value"])])
        else:
            norm.append([it["pollutant"], "nd", _num_repr(it["detection_limit"])])
    norm.sort(key=lambda e: e[0])
    return json.dumps(norm, ensure_ascii=False, separators=(",", ":"))


def content_hash(items: Iterable[dict[str, Any]]) -> str:
    return hashlib.sha256(canonical_content(items).encode("utf-8")).hexdigest()


def _frac(x: float) -> Fraction:
    """把测值/限值按用户提交的十进制语义转为精确有理数。

    用 repr（最短往返十进制）而非二进制精确值：0.07 视为 7/100，
    使 0.07/7 == 0.01 这类边界比较符合十进制直觉且无舍入歧义。
    """
    return Fraction(repr(float(x)))


def interpret_window(
    days: list[dict[str, Any]],
    pollutant: str,
    limit: float,
) -> dict[str, Any]:
    """对同一已提交时点的七份现行日报做判读。

    days: 按日期升序的 7 个元素，每个为
        {"date": str, "revision": int|None, "report_id": int|None,
         "items": {pollutant: {"type","value","detection_limit"}} | None}
    """
    revisions: list[dict[str, Any]] = []
    missing_dates: list[str] = []
    missing_pollutant_dates: list[str] = []
    lo = Fraction(0)
    hi = Fraction(0)
    any_nd = False

    for day in days:
        if day["report_id"] is None:
            missing_dates.append(day["date"])
            continue
        revisions.append(
            {"date": day["date"], "revision": day["revision"], "report_id": day["report_id"]}
        )
        item: Optional[dict[str, Any]] = day["items"].get(pollutant)
        if item is None:
            missing_pollutant_dates.append(day["date"])
            continue
        if item["type"] == "value":
            v = _frac(item["value"])
            lo += v
            hi += v
        else:  # nd: [0, detection_limit)
            hi += _frac(item["detection_limit"])
            any_nd = True

    result: dict[str, Any] = {
        "revisions": revisions,
        "missing_dates": missing_dates,
        "missing_pollutant_dates": missing_pollutant_dates,
    }

    if missing_dates or missing_pollutant_dates:
        result["status"] = STATUS_INSUFFICIENT
        result["mean"] = None
        return result

    mean_lo = lo / WINDOW_DAYS
    mean_hi = hi / WINDOW_DAYS
    lim = _frac(limit)
    if mean_hi <= lim:
        status = STATUS_COMPLIANT
    elif mean_lo > lim:
        status = STATUS_EXCEEDS
    else:
        status = STATUS_REVIEW

    result["status"] = status
    result["mean"] = {
        "lower": float(mean_lo),
        "upper": float(mean_hi),
        "upper_exclusive": any_nd,
    }
    return result
