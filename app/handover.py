"""现场采样交接单补正的对账逻辑（纯函数，便于测试）。

输入是一批最多 30 条的样本记录（可乱序提交）：

- ``sample_id``            样本 ID（批内唯一）
- ``station`` / ``sampling_date``  该样本**当前**所在的站点与采样日
- ``pollutants``           应检污染物集合
- ``replaces_sample_id``   可选：本样本替换的旧样本 ID

替换关系构成一张有向图（边方向：被替换样本 → 替换样本）。沿该方向，
没有被任何样本替换的样本即所在站日的**最终样本**。随后把最终样本与
同一已提交快照里的现行完整日报比对项目集合：

- ``warehouseable_reports`` 应检项目与报告项目完全一致、可入仓的日报（ID + 修订号）
- ``missing_reports``       该站日没有现行报告（缺报）
- ``missing_items``         报告缺少应检污染物（缺项）
- ``extra_items``           报告多出交接单未要求的污染物（多项）
- ``handover_conflicts``    交接冲突：缺前驱、成环、分叉、同站日多个最终样本；
                           冲突样本所在站日，以及同一替换链/站日上的其他样本一并列出
- ``pending_verification``  跨站日补正留下的旧位置：即使有报告也待核对，不得入仓

一致性约定：所有日报都来自调用方给定的同一已提交快照；本模块不读数据库。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Optional

# 交接冲突原因（机器可读）
MISSING_PREDECESSOR = "missing_predecessor"      # 缺前驱
CYCLE = "cycle"                                  # 成环
BRANCH = "branch"                                # 分叉
MULTIPLE_FINALS = "multiple_final_samples"       # 同站日多个最终样本
DAY_IN_CONFLICT = "station_day_in_conflict"      # 同站日另有冲突样本
CHAIN_IN_CONFLICT = "replacement_chain_in_conflict"  # 替换链上有冲突样本

REASON_ZH = {
    MISSING_PREDECESSOR: "缺前驱",
    CYCLE: "成环",
    BRANCH: "分叉",
    MULTIPLE_FINALS: "同站日有多个最终样本",
    DAY_IN_CONFLICT: "同站日存在其他交接冲突",
    CHAIN_IN_CONFLICT: "替换链上存在交接冲突",
}

PENDING_VACATED = "vacated_by_cross_station_day_correction"
PENDING_ZH = {PENDING_VACATED: "跨站日补正留下的旧位置"}


def _day(sample: dict[str, Any]) -> tuple[str, str]:
    return (sample["station"], sample["sampling_date"])


def _mark_descendants(
    children: dict[str, list[str]],
    root: str,
    reasons: dict[str, set[str]],
    reason: str,
) -> None:
    """沿“被替换 → 替换”方向把冲突标记传播到 root 的所有后代。"""
    stack = list(children.get(root, []))
    seen: set[str] = set()
    while stack:
        sid = stack.pop()
        if sid in seen:
            continue
        seen.add(sid)
        reasons[sid].add(reason)
        stack.extend(children.get(sid, []))


def _find_cycles(
    sample_ids: list[str], parent: dict[str, Optional[str]]
) -> set[str]:
    """在“每个样本至多一个前驱”的函数图上找所有环。

    三色迭代（0=未访问, 1=当前路径, 2=已完成）。只有仍在当前路径上的节点
    才构成环；处理完的路径统一染成完成色，后续遍历不再重复。自环（样本
    替换自己）同样会被识别为环。
    """
    state: dict[str, int] = {sid: 0 for sid in sample_ids}
    cycle_nodes: set[str] = set()

    for start in sample_ids:
        if state[start] != 0:
            continue
        path: list[str] = []
        cur: Optional[str] = start
        while cur is not None and state[cur] == 0:
            state[cur] = 1
            path.append(cur)
            cur = parent.get(cur)
        if cur is not None and state[cur] == 1:
            cycle_nodes.update(path[path.index(cur) :])
        for sid in path:
            state[sid] = 2
    return cycle_nodes


def reconcile(
    samples: list[dict[str, Any]],
    reports: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    """对一批交接样本与同一快照中的现行日报做对账。

    samples: [{"sample_id", "station", "sampling_date", "pollutants",
               "replaces_sample_id": str|None}]
    reports: {(station, sampling_date): {"report_id", "revision", "pollutants": set}}
    """
    by_id = {s["sample_id"]: s for s in samples}
    sample_ids = sorted(by_id)

    # ---- 替换关系：parent（前驱）与 children（后继） ----
    parent: dict[str, Optional[str]] = {}
    children: dict[str, list[str]] = defaultdict(list)
    missing_predecessor: set[str] = set()
    for sid in sample_ids:
        rid = by_id[sid].get("replaces_sample_id")
        if rid is None:
            parent[sid] = None
        elif rid in by_id:
            parent[sid] = rid
            children[rid].append(sid)
        else:
            # 前驱不在本批：无法沿链回溯，按缺前驱处理
            parent[sid] = None
            missing_predecessor.add(sid)
    for kids in children.values():
        kids.sort()

    reasons: dict[str, set[str]] = defaultdict(set)

    # 1) 缺前驱：该样本及其全部后代都无法确定最终样本
    for sid in sorted(missing_predecessor):
        reasons[sid].add(MISSING_PREDECESSOR)
        _mark_descendants(children, sid, reasons, MISSING_PREDECESSOR)

    # 2) 成环：环上样本及其全部后代
    cycle_nodes = _find_cycles(sample_ids, parent)
    for sid in sorted(cycle_nodes):
        reasons[sid].add(CYCLE)
    for sid in sorted(cycle_nodes):
        _mark_descendants(children, sid, reasons, CYCLE)

    # 3) 分叉：一个样本被多个样本替换，后继分支无法判定
    for sid in sorted(children):
        kids = children[sid]
        if len(kids) > 1:
            reasons[sid].add(BRANCH)
            for kid in kids:
                reasons[kid].add(BRANCH)
                _mark_descendants(children, kid, reasons, BRANCH)

    # 4) 同站日多个最终样本：最终样本 = 没有任何样本替换它（无后继）。
    #    只统计“可解析”的最终样本；本身已因缺前驱/成环/分叉无法解析的样本
    #    已让该站日成为冲突，不重复计入“多个最终样本”。
    finals = [sid for sid in sample_ids if not children.get(sid)]
    candidates = [sid for sid in finals if not reasons.get(sid)]
    finals_by_day: dict[tuple[str, str], list[str]] = defaultdict(list)
    for sid in finals:
        finals_by_day[_day(by_id[sid])].append(sid)
    candidates_by_day: dict[tuple[str, str], list[str]] = defaultdict(list)
    for sid in candidates:
        candidates_by_day[_day(by_id[sid])].append(sid)
    for day, sids in sorted(candidates_by_day.items()):
        if len(sids) > 1:
            for sid in sids:
                reasons[sid].add(MULTIPLE_FINALS)

    # 5) 收敛传播：冲突一旦出现在替换链的任一环上，整条链都无法确定最终样本。
    #    - 沿替换边双向传播（前驱与后继同样受影响）；
    #    - 冲突样本所在站日上的其他样本也视为冲突（station_day_in_conflict）；
    #    直到不再变化为止。
    neighbors: dict[str, set[str]] = {sid: set() for sid in sample_ids}
    for sid in sample_ids:
        rid = parent.get(sid)
        if rid is not None:
            neighbors[sid].add(rid)
            neighbors[rid].add(sid)
    by_day: dict[tuple[str, str], list[str]] = defaultdict(list)
    for sid in sample_ids:
        by_day[_day(by_id[sid])].append(sid)

    changed = True
    while changed:
        changed = False
        for sid in sample_ids:
            if not reasons.get(sid):
                continue
            for nb in neighbors[sid]:
                if not reasons.get(nb):
                    reasons[nb].add(CHAIN_IN_CONFLICT)
                    changed = True
            for other in by_day[_day(by_id[sid])]:
                if not reasons.get(other):
                    reasons[other].add(DAY_IN_CONFLICT)
                    changed = True

    # 6) 跨站日补正让出的旧位置：前驱所在站日与替换样本所在站日不同。
    #    即使旧位置有报告也必须待核对；若旧位置本身已是交接冲突，以冲突为准。
    conflict_days: dict[tuple[str, str], set[str]] = defaultdict(set)
    for sid in sorted(reasons):
        conflict_days[_day(by_id[sid])].add(sid)

    vacated: dict[tuple[str, str], set[str]] = defaultdict(set)
    for sid in sample_ids:
        rid = parent.get(sid)
        if rid is None:
            continue
        old_day, new_day = _day(by_id[rid]), _day(by_id[sid])
        if old_day != new_day:
            vacated[old_day].add(rid)
    pending_days = {day for day in vacated if day not in conflict_days}

    # ---- 与快照中的现行日报比对 ----
    warehouseable: list[dict[str, Any]] = []
    missing_reports: list[dict[str, Any]] = []
    missing_items: list[dict[str, Any]] = []
    extra_items: list[dict[str, Any]] = []
    comparison: dict[str, dict[str, Any]] = {}

    eligible = [
        sid
        for sid in candidates
        if sid not in reasons and _day(by_id[sid]) not in pending_days
    ]
    for sid in sorted(eligible, key=lambda s: (*_day(by_id[s]), s)):
        sample = by_id[sid]
        expected = sorted(set(sample["pollutants"]))
        base = {
            "sample_id": sid,
            "station": sample["station"],
            "sampling_date": sample["sampling_date"],
            "expected_pollutants": expected,
        }
        report = reports.get(_day(sample))
        if report is None:
            missing_reports.append(base)
            comparison[sid] = {**base, "status": "missing_report"}
            continue

        actual = set(report["pollutants"])
        lack = sorted(set(expected) - actual)
        extra = sorted(actual - set(expected))
        with_report = {
            **base,
            "report_id": report["report_id"],
            "revision": report["revision"],
        }
        if lack:
            missing_items.append({**with_report, "missing_pollutants": lack})
            comparison[sid] = {
                **with_report,
                "status": "missing_items",
                "missing_pollutants": lack,
            }
            if extra:
                extra_items.append({**with_report, "extra_pollutants": extra})
        elif extra:
            extra_items.append({**with_report, "extra_pollutants": extra})
            comparison[sid] = {
                **with_report,
                "status": "extra_items",
                "extra_pollutants": extra,
            }
        else:
            warehouseable.append(with_report)
            comparison[sid] = {**with_report, "status": "warehouseable"}

    # ---- 逐样本解析（每个提交样本都有明确去向） ----
    # 没有冲突且没有后继的样本即最终样本；最终样本若在待核对的旧位置上，
    # 依然会走到下方比对，但该位置不得入仓（见 eligible 过滤）。
    resolved: list[dict[str, Any]] = []
    for sid in sample_ids:
        sample = by_id[sid]
        if sid in reasons:
            resolved.append(
                {
                    "sample_id": sid,
                    "station": sample["station"],
                    "sampling_date": sample["sampling_date"],
                    "expected_pollutants": sorted(set(sample["pollutants"])),
                    "status": "handover_conflict",
                    "reasons": sorted(reasons[sid]),
                }
            )
        elif children.get(sid):
            resolved.append(
                {
                    "sample_id": sid,
                    "station": sample["station"],
                    "sampling_date": sample["sampling_date"],
                    "expected_pollutants": sorted(set(sample["pollutants"])),
                    "status": "superseded",
                }
            )
        elif _day(sample) in pending_days:
            report = reports.get(_day(sample))
            resolved.append(
                {
                    "sample_id": sid,
                    "station": sample["station"],
                    "sampling_date": sample["sampling_date"],
                    "expected_pollutants": sorted(set(sample["pollutants"])),
                    "status": "pending_verification",
                    "report_id": report["report_id"] if report else None,
                    "revision": report["revision"] if report else None,
                }
            )
        else:
            resolved.append(comparison[sid])
    resolved.sort(key=lambda e: (e["station"], e["sampling_date"], e["sample_id"]))

    # ---- 待核对的旧位置（即使有报告也不得入仓） ----
    pending_verification: list[dict[str, Any]] = []
    for day in sorted(pending_days):
        report = reports.get(day)
        pending_verification.append(
            {
                "station": day[0],
                "sampling_date": day[1],
                "reason": PENDING_VACATED,
                "reason_zh": PENDING_ZH[PENDING_VACATED],
                "report_exists": report is not None,
                "report_id": report["report_id"] if report else None,
                "revision": report["revision"] if report else None,
                "vacated_sample_ids": sorted(vacated[day]),
                "final_sample_ids": sorted(
                    sid for sid in finals_by_day.get(day, []) if sid not in reasons
                ),
            }
        )

    # ---- 交接冲突汇总（按站日） ----
    handover_conflicts: list[dict[str, Any]] = []
    for day in sorted(conflict_days):
        sids = sorted(conflict_days[day])
        day_reasons = sorted({r for sid in sids for r in reasons.get(sid, set())})
        handover_conflicts.append(
            {
                "station": day[0],
                "sampling_date": day[1],
                "sample_ids": sids,
                "reasons": day_reasons,
                "reasons_zh": [REASON_ZH[r] for r in day_reasons],
                "details": [
                    {
                        "sample_id": sid,
                        "station": by_id[sid]["station"],
                        "sampling_date": by_id[sid]["sampling_date"],
                        "replaces_sample_id": by_id[sid].get("replaces_sample_id"),
                        "reasons": sorted(reasons.get(sid, set())),
                        "reasons_zh": [
                            REASON_ZH[r] for r in sorted(reasons.get(sid, set()))
                        ],
                    }
                    for sid in sids
                ],
            }
        )

    warehouseable.sort(key=lambda e: (e["station"], e["sampling_date"]))
    missing_reports.sort(key=lambda e: (e["station"], e["sampling_date"]))
    missing_items.sort(key=lambda e: (e["station"], e["sampling_date"]))
    extra_items.sort(key=lambda e: (e["station"], e["sampling_date"]))

    return {
        "submitted_samples": len(samples),
        "resolved_samples": resolved,
        "warehouseable_reports": warehouseable,
        "missing_reports": missing_reports,
        "missing_items": missing_items,
        "extra_items": extra_items,
        "handover_conflicts": handover_conflicts,
        "pending_verification": pending_verification,
    }
