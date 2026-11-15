"""纯函数对账逻辑的单元测试（不经 HTTP）。"""

from app.handover import (
    BRANCH,
    CHAIN_IN_CONFLICT,
    CYCLE,
    DAY_IN_CONFLICT,
    MISSING_PREDECESSOR,
    MULTIPLE_FINALS,
    PENDING_VACATED,
    reconcile,
)

A = "2026-11-10"
B = "2026-11-11"


def s(sid, station, day, pollutants=("COD",), replaces=None):
    return {
        "sample_id": sid,
        "station": station,
        "sampling_date": day,
        "pollutants": list(pollutants),
        "replaces_sample_id": replaces,
    }


def reports(*entries):
    out = {}
    for i, (station, day, pollutants) in enumerate(entries, start=1):
        out[(station, day)] = {
            "report_id": i,
            "revision": 1,
            "pollutants": set(pollutants),
        }
    return out


def statuses(result):
    return {r["sample_id"]: r["status"] for r in result["resolved_samples"]}


def test_empty_samples():
    result = reconcile([], reports())
    assert result["submitted_samples"] == 0
    assert result["warehouseable_reports"] == []
    assert result["handover_conflicts"] == []
    assert result["pending_verification"] == []


def test_simple_warehouseable():
    result = reconcile([s("S1", "ST", A)], reports(("ST", A, {"COD"})))
    assert statuses(result) == {"S1": "warehouseable"}
    assert result["warehouseable_reports"][0]["report_id"] == 1


def test_items_are_a_set_comparison():
    result = reconcile(
        [s("S1", "ST", A, ["COD", "COD"])], reports(("ST", A, {"COD"}))
    )
    assert result["warehouseable_reports"][0]["expected_pollutants"] == ["COD"]


def test_order_independence_and_stable_output():
    samples = [s("S2", "ST2", B, replaces="S1"), s("S1", "ST1", A)]
    reps = reports(("ST1", A, {"COD"}), ("ST2", B, {"COD"}))
    a = reconcile(samples, reps)
    b = reconcile(list(reversed(samples)), reps)
    assert a == b


def test_missing_predecessor_propagates_downstream():
    samples = [
        s("S1", "ST1", A, replaces="GONE"),
        s("S2", "ST2", B, replaces="S1"),
    ]
    result = reconcile(samples, reports(("ST2", B, {"COD"})))
    assert statuses(result) == {
        "S1": "handover_conflict",
        "S2": "handover_conflict",
    }
    assert {r for x in result["resolved_samples"] for r in x["reasons"]} == {
        MISSING_PREDECESSOR
    }
    assert {(c["station"], c["sampling_date"]) for c in result["handover_conflicts"]} == {
        ("ST1", A),
        ("ST2", B),
    }


def test_final_sample_with_its_own_conflict_not_counted_as_multiple_finals():
    # S1 缺前驱、S2 正常，且二者同站日且都是“无后继”样本。
    # 该站日已因缺前驱成为冲突，不再额外记为“多个最终样本”。
    result = reconcile(
        [s("S1", "ST1", A, replaces="GONE"), s("S2", "ST1", A)],
        reports(("ST1", A, {"COD"})),
    )
    reasons = {(x["sample_id"]): x["reasons"] for x in result["resolved_samples"]}
    assert reasons["S1"] == [MISSING_PREDECESSOR]
    assert reasons["S2"] == [DAY_IN_CONFLICT]


def test_missing_predecessor_days():
    samples = [s("S1", "ST1", A, replaces="GONE")]
    result = reconcile(samples, reports())
    assert [(c["station"], c["sampling_date"], c["reasons"]) for c in result["handover_conflicts"]] == [
        ("ST1", A, [MISSING_PREDECESSOR])
    ]


def test_self_loop_is_cycle():
    result = reconcile([s("S1", "ST1", A, replaces="S1")], reports())
    assert result["handover_conflicts"][0]["reasons"] == [CYCLE]
    assert statuses(result) == {"S1": "handover_conflict"}


def test_branch_marks_all_branches():
    samples = [
        s("S0", "ST0", A),
        s("S1", "ST1", A, replaces="S0"),
        s("S2", "ST2", B, replaces="S0"),
    ]
    result = reconcile(samples, reports(("ST1", A, {"COD"}), ("ST2", B, {"COD"})))
    assert {r for x in result["resolved_samples"] for r in x["reasons"]} == {BRANCH}
    assert {(c["station"], c["sampling_date"]) for c in result["handover_conflicts"]} == {
        ("ST0", A),
        ("ST1", A),
        ("ST2", B),
    }


def test_same_day_two_finals():
    result = reconcile(
        [s("S1", "ST1", A), s("S2", "ST1", A)], reports(("ST1", A, {"COD"}))
    )
    assert result["handover_conflicts"][0]["reasons"] == [MULTIPLE_FINALS]
    assert all(v == "handover_conflict" for v in statuses(result).values())


def test_conflict_station_day_marks_extra_sample():
    samples = [
        s("S1", "ST1", A),
        s("S2", "ST1", A, replaces="S1"),  # 同站日替换链本身合法
        s("S3", "ST1", A),                 # 与 S2 构成同站日多个最终样本
    ]
    result = reconcile(samples, reports(("ST1", A, {"COD"})))
    assert MULTIPLE_FINALS in result["handover_conflicts"][0]["reasons"]
    # S1 被 S2 替换，但 S2 自身冲突 → S1 同样无法确定最终样本
    assert statuses(result)["S1"] == "handover_conflict"


def test_superseded_chain_ok_same_day():
    result = reconcile(
        [s("S1", "ST1", A), s("S2", "ST1", A, replaces="S1")],
        reports(("ST1", A, {"COD"})),
    )
    assert statuses(result) == {"S1": "superseded", "S2": "warehouseable"}
    assert result["pending_verification"] == []


def test_cross_day_chain_vacates_every_old_position():
    samples = [
        s("S1", "ST1", A),
        s("S2", "ST2", B, replaces="S1"),
        s("S3", "ST3", "2026-11-12", replaces="S2"),
    ]
    result = reconcile(
        samples,
        reports(
            ("ST1", A, {"COD"}),
            ("ST2", B, {"COD"}),
            ("ST3", "2026-11-12", {"COD"}),
        ),
    )
    assert statuses(result)["S3"] == "warehouseable"
    assert [w["sample_id"] for w in result["warehouseable_reports"]] == ["S3"]
    assert [(p["station"], p["sampling_date"]) for p in result["pending_verification"]] == [
        ("ST1", A),
        ("ST2", B),
    ]
    assert all(p["reason"] == PENDING_VACATED for p in result["pending_verification"])
    assert all(p["report_exists"] is True for p in result["pending_verification"])


def test_pending_position_not_reported_as_discrepancy():
    result = reconcile(
        [s("S1", "ST1", A), s("S2", "ST1", B, replaces="S1")],
        reports(("ST1", A, {"X"}), ("ST1", B, {"COD"})),
    )
    assert result["missing_items"] == []
    assert result["extra_items"] == []
    assert result["warehouseable_reports"][0]["sampling_date"] == B


def test_conflict_day_excludes_pending():
    # 前驱在 ST1/A 让出旧位置，但补正链分叉 → ST1/A 是冲突而非待核对
    result = reconcile(
        [
            s("S1", "ST1", A),
            s("S2", "ST2", B, replaces="S1"),
            s("S3", "ST3", "2026-11-12", replaces="S1"),
        ],
        reports(),
    )
    assert {(c["station"], c["sampling_date"]) for c in result["handover_conflicts"]} == {
        ("ST1", A),
        ("ST2", B),
        ("ST3", "2026-11-12"),
    }
    assert result["pending_verification"] == []


def test_day_in_conflict_reason_used_for_collateral_samples():
    samples = [
        # S1 缺前驱 → ST1/A 成为冲突站日
        s("S1", "ST1", A, replaces="GONE"),
        # S2 位于 ST1/A，但已被 ST2/B 的 S3 替换（本身不是最终样本）→ 附带冲突
        s("S2", "ST1", A),
        s("S3", "ST2", B, replaces="S2"),
    ]
    result = reconcile(samples, reports())
    resolved = {x["sample_id"]: x for x in result["resolved_samples"]}
    assert MISSING_PREDECESSOR in resolved["S1"]["reasons"]
    assert DAY_IN_CONFLICT in resolved["S2"]["reasons"]
    assert resolved["S3"]["status"] == "handover_conflict"
    assert CHAIN_IN_CONFLICT in resolved["S3"]["reasons"]
