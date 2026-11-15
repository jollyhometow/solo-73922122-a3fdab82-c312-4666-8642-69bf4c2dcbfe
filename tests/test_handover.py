"""交接对账 API 的端到端测试：替换链、项目比对、冲突、待核对与并发快照。"""

import datetime as dt
import itertools
import threading

import pytest
from fastapi.testclient import TestClient

from app.handover import reconcile
from app.main import create_app

DAY_A = "2026-11-10"
DAY_B = "2026-11-11"


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def val(pollutant, value=1.0):
    return {"type": "value", "pollutant": pollutant, "value": value}


def submit(client, station, date, revision, pollutants, value=1.0):
    return client.post(
        "/reports",
        json={
            "station": station,
            "sampling_date": str(date),
            "revision": revision,
            "items": [val(p, value) for p in pollutants],
        },
    )


def sample(sid, station, date, pollutants, replaces=None):
    body = {
        "sample_id": sid,
        "station": station,
        "sampling_date": str(date),
        "pollutants": pollutants,
    }
    if replaces is not None:
        body["replaces_sample_id"] = replaces
    return body


def reconcile_call(client, samples):
    return client.post("/handover/reconcile", json={"samples": samples})


def by_id(items):
    return {x["sample_id"]: x for x in items}


def conflict_days(body):
    return {(c["station"], c["sampling_date"]) for c in body["handover_conflicts"]}


def pending_days(body):
    return {(p["station"], p["sampling_date"]) for p in body["pending_verification"]}


# ---------------- 基本比对 ----------------


def test_warehouseable_returns_report_id_and_revision(client):
    assert submit(client, "ST-01", DAY_A, 1, ["COD", "NH3-N"]).status_code == 201
    r = reconcile_call(
        client, [sample("S1", "ST-01", DAY_A, ["NH3-N", "COD"])]
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["submitted_samples"] == 1
    assert len(body["warehouseable_reports"]) == 1
    entry = body["warehouseable_reports"][0]
    assert entry["sample_id"] == "S1"
    assert entry["station"] == "ST-01"
    assert entry["sampling_date"] == DAY_A
    assert entry["revision"] == 1
    assert entry["expected_pollutants"] == ["COD", "NH3-N"]
    report = client.get(f"/reports/ST-01/{DAY_A}").json()
    assert entry["report_id"] == report["report_id"]
    assert by_id(body["resolved_samples"])["S1"]["status"] == "warehouseable"
    assert body["handover_conflicts"] == []
    assert body["pending_verification"] == []
    assert isinstance(body["snapshot_seq"], int)


def test_warehouseable_uses_current_revision(client):
    submit(client, "ST-01", DAY_A, 1, ["COD"])
    submit(client, "ST-01", DAY_A, 2, ["COD"])
    body = reconcile_call(client, [sample("S1", "ST-01", DAY_A, ["COD"])]).json()
    entry = body["warehouseable_reports"][0]
    assert entry["revision"] == 2
    current = client.get(f"/reports/ST-01/{DAY_A}").json()
    assert entry["report_id"] == current["report_id"]


def test_missing_report(client):
    body = reconcile_call(client, [sample("S1", "ST-01", DAY_A, ["COD"])]).json()
    assert body["warehouseable_reports"] == []
    assert body["missing_reports"] == [
        {
            "sample_id": "S1",
            "station": "ST-01",
            "sampling_date": DAY_A,
            "expected_pollutants": ["COD"],
        }
    ]
    assert by_id(body["resolved_samples"])["S1"]["status"] == "missing_report"


def test_missing_items(client):
    submit(client, "ST-01", DAY_A, 1, ["COD"])
    body = reconcile_call(
        client, [sample("S1", "ST-01", DAY_A, ["COD", "NH3-N"])]
    ).json()
    assert body["warehouseable_reports"] == []
    assert body["missing_reports"] == []
    assert len(body["missing_items"]) == 1
    entry = body["missing_items"][0]
    assert entry["missing_pollutants"] == ["NH3-N"]
    assert entry["revision"] == 1
    assert by_id(body["resolved_samples"])["S1"]["status"] == "missing_items"


def test_extra_items(client):
    submit(client, "ST-01", DAY_A, 1, ["COD", "NH3-N", "TP"])
    body = reconcile_call(client, [sample("S1", "ST-01", DAY_A, ["COD"])]).json()
    assert body["warehouseable_reports"] == []
    assert body["missing_items"] == []
    assert len(body["extra_items"]) == 1
    assert body["extra_items"][0]["extra_pollutants"] == ["NH3-N", "TP"]
    assert by_id(body["resolved_samples"])["S1"]["status"] == "extra_items"


def test_missing_and_extra_reported_together(client):
    submit(client, "ST-01", DAY_A, 1, ["COD", "TP"])
    body = reconcile_call(
        client, [sample("S1", "ST-01", DAY_A, ["COD", "NH3-N"])]
    ).json()
    assert body["missing_items"][0]["missing_pollutants"] == ["NH3-N"]
    assert body["extra_items"][0]["extra_pollutants"] == ["TP"]
    assert body["warehouseable_reports"] == []


def test_duplicate_pollutants_in_handover_are_deduplicated(client):
    submit(client, "ST-01", DAY_A, 1, ["COD"])
    body = reconcile_call(
        client, [sample("S1", "ST-01", DAY_A, ["COD", "COD"])]
    ).json()
    assert body["warehouseable_reports"][0]["expected_pollutants"] == ["COD"]


def test_multiple_samples_resolved_independently(client):
    submit(client, "ST-01", DAY_A, 1, ["COD"])
    submit(client, "ST-02", DAY_B, 1, ["COD"])
    body = reconcile_call(
        client,
        [
            sample("S1", "ST-01", DAY_A, ["COD"]),
            sample("S2", "ST-02", DAY_B, ["COD"]),
            sample("S3", "ST-03", DAY_A, ["COD"]),  # 无报告
        ],
    ).json()
    assert {x["sample_id"] for x in body["warehouseable_reports"]} == {"S1", "S2"}
    assert [x["sample_id"] for x in body["missing_reports"]] == ["S3"]


def test_submission_order_does_not_matter(client):
    submit(client, "ST-01", DAY_A, 1, ["COD"])
    submit(client, "ST-01", DAY_B, 1, ["COD", "NH3-N"])
    samples = [
        sample("S2", "ST-01", DAY_B, ["COD", "NH3-N"], replaces="S1"),
        sample("S1", "ST-01", DAY_A, ["COD"]),
    ]
    outputs = []
    for perm in itertools.permutations(samples):
        r = reconcile_call(client, list(perm))
        assert r.status_code == 200
        body = r.json()
        # 响应不依赖提交顺序（resolved/列表均已排序）
        outputs.append(body)
    for body in outputs[1:]:
        assert body == outputs[0]


# ---------------- 替换链 / 跨站日补正 ----------------


def test_same_day_replacement_keeps_day_warehouseable(client):
    submit(client, "ST-01", DAY_A, 1, ["COD", "NH3-N"])
    body = reconcile_call(
        client,
        [
            sample("S1", "ST-01", DAY_A, ["COD"]),
            sample("S2", "ST-01", DAY_A, ["COD", "NH3-N"], replaces="S1"),
        ],
    ).json()
    resolved = by_id(body["resolved_samples"])
    assert resolved["S1"]["status"] == "superseded"
    assert resolved["S2"]["status"] == "warehouseable"
    assert len(body["warehouseable_reports"]) == 1
    assert body["pending_verification"] == []


def test_cross_station_day_correction_vacates_old_position(client):
    # 旧位置与补正后的位置都有完整报告：只有新位置可入仓，旧位置必须待核对
    submit(client, "ST-01", DAY_A, 1, ["COD"])
    submit(client, "ST-02", DAY_B, 1, ["COD"])
    body = reconcile_call(
        client,
        [
            sample("S1", "ST-01", DAY_A, ["COD"]),
            sample("S2", "ST-02", DAY_B, ["COD"], replaces="S1"),
        ],
    ).json()

    assert len(body["warehouseable_reports"]) == 1
    assert body["warehouseable_reports"][0]["sample_id"] == "S2"
    assert body["warehouseable_reports"][0]["station"] == "ST-02"
    assert pending_days(body) == {("ST-01", DAY_A)}
    pending = body["pending_verification"][0]
    assert pending["report_exists"] is True
    assert pending["revision"] == 1
    assert pending["vacated_sample_ids"] == ["S1"]
    assert pending["final_sample_ids"] == []

    # 旧位置即使项目完全匹配也不得出现在可入仓或差异清单里
    assert all(w["station"] != "ST-01" for w in body["warehouseable_reports"])
    assert body["missing_items"] == []
    assert body["extra_items"] == []
    assert by_id(body["resolved_samples"])["S1"]["status"] == "superseded"


def test_cross_station_day_correction_without_old_report(client):
    submit(client, "ST-02", DAY_B, 1, ["COD"])
    body = reconcile_call(
        client,
        [
            sample("S1", "ST-01", DAY_A, ["COD"]),
            sample("S2", "ST-02", DAY_B, ["COD"], replaces="S1"),
        ],
    ).json()
    assert len(body["warehouseable_reports"]) == 1
    pending = body["pending_verification"][0]
    assert pending["report_exists"] is False
    assert pending["report_id"] is None
    assert pending["vacated_sample_ids"] == ["S1"]


def test_station_change_only_also_vacates_old_station(client):
    submit(client, "NEW", DAY_A, 1, ["COD"])
    body = reconcile_call(
        client,
        [
            sample("S1", "OLD", DAY_A, ["COD"]),
            sample("S2", "NEW", DAY_A, ["COD"], replaces="S1"),
        ],
    ).json()
    assert pending_days(body) == {("OLD", DAY_A)}
    assert body["warehouseable_reports"][0]["station"] == "NEW"


def test_multi_hop_chain_leaves_intermediate_positions_pending(client):
    submit(client, "ST-03", "2026-11-12", 1, ["COD"])
    body = reconcile_call(
        client,
        [
            sample("S1", "ST-01", DAY_A, ["COD"]),
            sample("S2", "ST-02", DAY_B, ["COD"], replaces="S1"),
            sample("S3", "ST-03", "2026-11-12", ["COD"], replaces="S2"),
        ],
    ).json()
    assert pending_days(body) == {("ST-01", DAY_A), ("ST-02", DAY_B)}
    assert {w["sample_id"] for w in body["warehouseable_reports"]} == {"S3"}
    assert by_id(body["resolved_samples"])["S2"]["status"] == "superseded"


# ---------------- 冲突 ----------------


def test_missing_predecessor_conflict(client):
    submit(client, "ST-01", DAY_A, 1, ["COD"])
    body = reconcile_call(
        client, [sample("S1", "ST-01", DAY_A, ["COD"], replaces="GONE")]
    ).json()
    assert conflict_days(body) == {("ST-01", DAY_A)}
    assert body["handover_conflicts"][0]["reasons"] == ["missing_predecessor"]
    assert body["handover_conflicts"][0]["reasons_zh"] == ["缺前驱"]
    assert body["warehouseable_reports"] == []


def test_cycle_conflict(client):
    submit(client, "ST-01", DAY_A, 1, ["COD"])
    submit(client, "ST-02", DAY_B, 1, ["COD"])
    body = reconcile_call(
        client,
        [
            sample("S1", "ST-01", DAY_A, ["COD"], replaces="S2"),
            sample("S2", "ST-02", DAY_B, ["COD"], replaces="S1"),
        ],
    ).json()
    assert conflict_days(body) == {("ST-01", DAY_A), ("ST-02", DAY_B)}
    assert body["handover_conflicts"][0]["reasons"] == ["cycle"]
    assert body["warehouseable_reports"] == []


def test_self_cycle_conflict(client):
    submit(client, "ST-01", DAY_A, 1, ["COD"])
    body = reconcile_call(
        client, [sample("S1", "ST-01", DAY_A, ["COD"], replaces="S1")]
    ).json()
    assert conflict_days(body) == {("ST-01", DAY_A)}
    assert body["handover_conflicts"][0]["reasons"] == ["cycle"]
    assert body["warehouseable_reports"] == []


def test_branch_conflict(client):
    submit(client, "ST-01", DAY_A, 1, ["COD"])
    submit(client, "ST-02", DAY_B, 1, ["COD"])
    body = reconcile_call(
        client,
        [
            sample("S1", "ST-01", DAY_A, ["COD"]),
            sample("S2a", "ST-02", DAY_B, ["COD"], replaces="S1"),
            sample("S2b", "ST-03", DAY_A, ["COD"], replaces="S1"),
        ],
    ).json()
    assert conflict_days(body) == {
        ("ST-01", DAY_A),
        ("ST-02", DAY_B),
        ("ST-03", DAY_A),
    }
    for c in body["handover_conflicts"]:
        assert "branch" in c["reasons"]
    assert body["warehouseable_reports"] == []


def test_multiple_final_samples_same_station_day_conflict(client):
    submit(client, "ST-01", DAY_A, 1, ["COD"])
    body = reconcile_call(
        client,
        [
            sample("S1", "ST-01", DAY_A, ["COD"]),
            sample("S2", "ST-01", DAY_A, ["COD"]),
        ],
    ).json()
    assert conflict_days(body) == {("ST-01", DAY_A)}
    assert body["handover_conflicts"][0]["reasons"] == ["multiple_final_samples"]
    assert body["handover_conflicts"][0]["reasons_zh"] == ["同站日有多个最终样本"]
    assert body["warehouseable_reports"] == []


def test_conflict_day_does_not_block_other_days(client):
    submit(client, "ST-01", DAY_A, 1, ["COD"])
    submit(client, "ST-02", DAY_B, 1, ["COD"])
    body = reconcile_call(
        client,
        [
            sample("BAD", "ST-01", DAY_A, ["COD"], replaces="GONE"),
            sample("OK", "ST-02", DAY_B, ["COD"]),
        ],
    ).json()
    assert conflict_days(body) == {("ST-01", DAY_A)}
    assert [w["sample_id"] for w in body["warehouseable_reports"]] == ["OK"]


def test_conflict_wins_over_stale_pending_position(client):
    submit(client, "ST-01", DAY_A, 1, ["COD"])
    body = reconcile_call(
        client,
        [
            sample("S1", "ST-01", DAY_A, ["COD"]),
            # 前驱在本批中，但补正链自身分叉 → 相关站日只列冲突
            sample("S2", "ST-02", DAY_B, ["COD"], replaces="S1"),
            sample("S3", "ST-03", "2026-11-12", ["COD"], replaces="S1"),
        ],
    ).json()
    assert ("ST-01", DAY_A) in conflict_days(body)
    assert ("ST-01", DAY_A) not in pending_days(body)
    assert body["warehouseable_reports"] == []


# ---------------- 校验 ----------------


def test_empty_batch_rejected(client):
    r = reconcile_call(client, [])
    assert r.status_code == 422


def test_max_30_samples_allowed(client):
    samples = [
        sample(f"S{i}", "ST-01", DAY_A, ["COD"]) for i in range(30)
    ]
    r = reconcile_call(client, samples)
    assert r.status_code == 200, r.text
    assert r.json()["submitted_samples"] == 30


def test_more_than_30_samples_rejected(client):
    samples = [
        sample(f"S{i}", "ST-01", DAY_A, ["COD"]) for i in range(31)
    ]
    r = reconcile_call(client, samples)
    assert r.status_code == 422


def test_duplicate_sample_id_rejected(client):
    r = reconcile_call(
        client,
        [
            sample("S1", "ST-01", DAY_A, ["COD"]),
            sample("S1", "ST-02", DAY_B, ["COD"]),
        ],
    )
    assert r.status_code == 422


@pytest.mark.parametrize(
    "bad_sample",
    [
        {"sample_id": "S1", "station": "ST-01", "sampling_date": DAY_A, "pollutants": []},
        {"sample_id": "", "station": "ST-01", "sampling_date": DAY_A, "pollutants": ["COD"]},
        {"sample_id": "S1", "sampling_date": DAY_A, "pollutants": ["COD"]},
        {"sample_id": "S1", "station": "ST-01", "sampling_date": "not-a-date", "pollutants": ["COD"]},
        {"sample_id": "S1", "station": "ST-01", "sampling_date": DAY_A, "pollutants": ["  "]},
        {
            "sample_id": "S1",
            "station": "ST-01",
            "sampling_date": DAY_A,
            "pollutants": ["COD"],
            "unexpected": 1,
        },
    ],
)
def test_invalid_samples_rejected(client, bad_sample):
    r = reconcile_call(client, [bad_sample])
    assert r.status_code == 422, r.text


# ---------------- 并发快照一致性 ----------------


def _insert_report_pair(conn, revision):
    """在单个写事务内为两个站日各写入同修订号的报告（测试快照隔离）。"""
    import json

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    conn.execute("BEGIN IMMEDIATE")
    for station, day in (("ST-A", DAY_A), ("ST-B", DAY_B)):
        content = json.dumps([[["COD", "value", "1.0"]]])
        cur = conn.execute(
            "INSERT INTO reports (station, sampling_date, revision, content_hash, "
            "content_json, submitted_at) VALUES (?,?,?,?,?,?)",
            (station, day, revision, f"h-{station}-{revision}", content, now),
        )
        conn.execute(
            "INSERT INTO report_items (report_id, pollutant, type, value, "
            "detection_limit) VALUES (?,?,?,?,?)",
            (cur.lastrowid, "COD", "value", 1.0, None),
        )
    conn.execute("COMMIT")


def test_reconcile_never_mixes_snapshots_across_station_days(client):
    samples = [
        sample("SA", "ST-A", DAY_A, ["COD"]),
        sample("SB", "ST-B", DAY_B, ["COD"]),
    ]
    db = client.app.state.db

    # 初始版本 1，保证第一次对账就有报告
    conn = db.connect()
    try:
        _insert_report_pair(conn, 1)
    finally:
        conn.close()

    errors = []
    stop = threading.Event()

    def writer():
        c = db.connect()
        try:
            for rev in range(2, 60):
                _insert_report_pair(c, rev)
        finally:
            c.close()
            stop.set()

    def reader():
        while not stop.is_set():
            r = reconcile_call(client, samples)
            if r.status_code != 200:
                errors.append(f"status {r.status_code}: {r.text}")
                continue
            body = r.json()
            resolved = by_id(body["resolved_samples"])
            ra = resolved["SA"].get("revision")
            rb = resolved["SB"].get("revision")
            if ra != rb:
                errors.append(f"torn snapshot: {ra} vs {rb} (seq {body['snapshot_seq']})")
            for w in body["warehouseable_reports"] + body["missing_items"] + body["extra_items"]:
                if w["report_id"] > body["snapshot_seq"]:
                    errors.append(
                        f"report {w['report_id']} newer than snapshot {body['snapshot_seq']}"
                    )

    t = threading.Thread(target=writer)
    readers = [threading.Thread(target=reader) for _ in range(3)]
    for th in readers:
        th.start()
    t.start()
    t.join()
    for th in readers:
        th.join()

    assert errors == []


def test_pure_reconcile_is_deterministic():
    samples = [
        {
            "sample_id": "S1",
            "station": "ST-01",
            "sampling_date": DAY_A,
            "pollutants": ["COD"],
            "replaces_sample_id": None,
        }
    ]
    reports = {("ST-01", DAY_A): {"report_id": 7, "revision": 3, "pollutants": {"COD"}}}
    first = reconcile(samples, reports)
    second = reconcile(list(reversed(samples)), reports)
    assert first == second
