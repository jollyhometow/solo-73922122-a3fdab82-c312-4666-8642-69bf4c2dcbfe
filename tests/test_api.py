"""端到端 API 测试：提交、修订语义、判读规则、并发与原子性。"""

import datetime as dt
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

END = dt.date(2026, 11, 14)  # 判读结束日


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def val(pollutant, value):
    return {"type": "value", "pollutant": pollutant, "value": value}


def nd(pollutant, detection_limit):
    return {"type": "nd", "pollutant": pollutant, "detection_limit": detection_limit}


def submit(client, station, date, revision, items):
    return client.post(
        "/reports",
        json={
            "station": station,
            "sampling_date": str(date),
            "revision": revision,
            "items": items,
        },
    )


def fill_week(client, station, end, pollutant, week, revision=1):
    """week: 7 个元素（最早→最新），float 表示实测值，("nd", dl) 表示未检出。"""
    assert len(week) == 7
    for i, entry in enumerate(week):
        day = end - dt.timedelta(days=6 - i)
        item = nd(pollutant, entry[1]) if isinstance(entry, tuple) else val(pollutant, entry)
        r = submit(client, station, day, revision, [item])
        assert r.status_code == 201, r.text


def interpret(client, station, end, pollutant, limit):
    return client.get(
        "/interpretation",
        params={
            "station": station,
            "end_date": str(end),
            "pollutant": pollutant,
            "limit": limit,
        },
    )


# ---------------- 提交与修订语义 ----------------


def test_submit_and_read_current(client):
    r = submit(client, "S1", "2026-11-10", 1, [val("COD", 12.3), nd("NH3-N", 0.05)])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "created"
    assert body["revision"] == 1

    g = client.get("/reports/S1/2026-11-10")
    assert g.status_code == 200
    data = g.json()
    assert data["is_current"] is True
    assert data["items"] == [
        {"type": "value", "pollutant": "COD", "value": 12.3},
        {"type": "nd", "pollutant": "NH3-N", "detection_limit": 0.05},
    ]


def test_same_revision_same_content_reused(client):
    payload = [val("COD", 1.5), nd("NH3-N", 0.05)]
    r1 = submit(client, "S1", "2026-11-10", 1, payload)
    assert r1.status_code == 201
    # 项目顺序不同、数值写法等价，仍视为同内容
    r2 = submit(client, "S1", "2026-11-10", 1, [nd("NH3-N", 0.05), val("COD", 1.50)])
    assert r2.status_code == 200
    assert r2.json()["status"] == "reused"
    assert r2.json()["report_id"] == r1.json()["report_id"]

    revs = client.get("/reports/S1/2026-11-10/revisions").json()["revisions"]
    assert len(revs) == 1  # 未产生新记录


def test_same_revision_different_content_rejected(client):
    submit(client, "S1", "2026-11-10", 1, [val("COD", 1.5)])
    r = submit(client, "S1", "2026-11-10", 1, [val("COD", 2.0)])
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "revision_content_mismatch"
    # 原报告未被破坏
    g = client.get("/reports/S1/2026-11-10").json()
    assert g["items"] == [{"type": "value", "pollutant": "COD", "value": 1.5}]


def test_stale_revision_rejected(client):
    submit(client, "S1", "2026-11-10", 3, [val("COD", 1.0)])
    r = submit(client, "S1", "2026-11-10", 2, [val("COD", 9.9)])
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "stale_revision"
    # 同内容的旧修订同样拒收
    r2 = submit(client, "S1", "2026-11-10", 1, [val("COD", 1.0)])
    assert r2.status_code == 409


def test_higher_revision_replaces_fully_and_old_stays_readable(client):
    submit(client, "S1", "2026-11-10", 1, [val("COD", 1.0), val("NH3-N", 2.0)])
    r = submit(client, "S1", "2026-11-10", 2, [val("COD", 3.0)])
    assert r.status_code == 201
    assert r.json()["superseded_revision"] == 1

    cur = client.get("/reports/S1/2026-11-10").json()
    assert cur["revision"] == 2
    assert cur["items"] == [{"type": "value", "pollutant": "COD", "value": 3.0}]
    # 删除的项目不得沿用旧值：现行报告里已无 NH3-N
    assert all(i["pollutant"] != "NH3-N" for i in cur["items"])

    # 已提交的旧修订仍可读取
    old = client.get("/reports/S1/2026-11-10", params={"revision": 1})
    assert old.status_code == 200
    assert old.json()["is_current"] is False
    assert {i["pollutant"] for i in old.json()["items"]} == {"COD", "NH3-N"}

    revs = client.get("/reports/S1/2026-11-10/revisions").json()["revisions"]
    assert [r["revision"] for r in revs] == [1, 2]


def test_revision_gaps_allowed(client):
    submit(client, "S1", "2026-11-10", 1, [val("COD", 1.0)])
    r = submit(client, "S1", "2026-11-10", 5, [val("COD", 2.0)])
    assert r.status_code == 201
    assert client.get("/reports/S1/2026-11-10").json()["revision"] == 5


# ---------------- 判读规则 ----------------


def test_interpret_compliant_and_revisions_returned(client):
    fill_week(client, "S1", END, "COD", [10.0] * 7)
    r = interpret(client, "S1", END, "COD", 10.0)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "compliant"
    assert body["status_zh"] == "达标"
    assert body["mean"] == {"lower": 10.0, "upper": 10.0, "upper_exclusive": False}
    assert len(body["revisions"]) == 7
    assert body["revisions"][0]["date"] == "2026-11-08"
    assert body["revisions"][-1]["date"] == "2026-11-14"
    assert all(rv["revision"] == 1 for rv in body["revisions"])
    assert body["window_start"] == "2026-11-08"


def test_interpret_exceeds(client):
    fill_week(client, "S1", END, "COD", [10.0] * 6 + [11.0])
    body = interpret(client, "S1", END, "COD", 10.0).json()
    assert body["status"] == "exceeds"
    assert body["status_zh"] == "超标"


def test_interpret_pending_review_when_interval_straddles(client):
    # 6 天实测 9.0，1 天未检出(检出限 20)：均值 ∈ [7.714..., 10.571...) 跨过 10
    week = [9.0] * 6 + [("nd", 20.0)]
    fill_week(client, "S1", END, "COD", week)
    body = interpret(client, "S1", END, "COD", 10.0).json()
    assert body["status"] == "pending_review"
    assert body["status_zh"] == "待复核"
    assert body["mean"]["upper_exclusive"] is True
    assert body["mean"]["lower"] < 10.0 < body["mean"]["upper"]


def test_nd_upper_bound_inclusive_for_compliance(client):
    # 未检出按 [0, 检出限) 参与：均值上界恰等于限值时仍必定不高于限值 → 达标
    week = [0.0] * 6 + [("nd", 0.07)]  # mean_hi = 0.01
    fill_week(client, "S1", END, "COD", week)
    body = interpret(client, "S1", END, "COD", 0.01).json()
    assert body["status"] == "compliant"
    assert body["mean"]["upper"] == pytest.approx(0.01)

    # 检出限略大则上界超过限值 → 待复核
    fill_week(client, "S2", END, "COD", [0.0] * 6 + [("nd", 0.0700001)])
    body2 = interpret(client, "S2", END, "COD", 0.01).json()
    assert body2["status"] == "pending_review"


def test_all_nd_against_zero_limit_is_pending(client):
    fill_week(client, "S1", END, "COD", [("nd", 0.05)] * 7)
    body = interpret(client, "S1", END, "COD", 0.0).json()
    assert body["status"] == "pending_review"
    assert body["mean"]["lower"] == 0.0


def test_all_zero_against_zero_limit_is_compliant(client):
    fill_week(client, "S1", END, "COD", [0.0] * 7)
    body = interpret(client, "S1", END, "COD", 0.0).json()
    assert body["status"] == "compliant"


def test_mean_lower_above_limit_is_exceeds_with_nd(client):
    # 6 天 11.0 + 1 天 nd(30.0)：均值 ∈ [9.428..., 13.714...) 跨过 10 → 待复核
    fill_week(client, "S1", END, "COD", [11.0] * 6 + [("nd", 30.0)])
    assert interpret(client, "S1", END, "COD", 10.0).json()["status"] == "pending_review"
    # 限值 9.0：均值下界 9.428... > 9 → 必定超标
    assert interpret(client, "S1", END, "COD", 9.0).json()["status"] == "exceeds"
    # 6 天 11.0 + 1 天 nd(0.05)：均值上界 66.05/7 ≈ 9.44 ≤ 10 → 必定达标
    fill_week(client, "S2", END, "COD", [11.0] * 6 + [("nd", 0.05)])
    assert interpret(client, "S2", END, "COD", 10.0).json()["status"] == "compliant"


def test_insufficient_when_missing_day(client):
    fill_week(client, "S1", END, "COD", [1.0] * 7)
    # 删不掉已提交报告，换一个只有 6 天数据的站点
    for i in range(6):
        day = END - dt.timedelta(days=6 - i)
        submit(client, "S2", day, 1, [val("COD", 1.0)])
    body = interpret(client, "S2", END, "COD", 5.0).json()
    assert body["status"] == "insufficient_data"
    assert body["status_zh"] == "资料不足"
    assert body["missing_dates"] == ["2026-11-14"]
    assert body["mean"] is None
    assert len(body["revisions"]) == 6


def test_insufficient_when_missing_pollutant(client):
    for i in range(7):
        day = END - dt.timedelta(days=6 - i)
        items = [val("COD", 1.0)] if i < 3 else [val("NH3-N", 1.0)]
        submit(client, "S1", day, 1, items)
    body = interpret(client, "S1", END, "COD", 5.0).json()
    assert body["status"] == "insufficient_data"
    assert body["missing_pollutant_dates"] == ["2026-11-11", "2026-11-12", "2026-11-13", "2026-11-14"]


def test_interpret_uses_current_revision_only(client):
    # 第 1 天 rev1 含 COD=100（会超标），rev2 删除 COD → 资料不足而非沿用旧值
    fill_week(client, "S1", END, "COD", [1.0] * 7)
    first_day = END - dt.timedelta(days=6)
    submit(client, "S1", first_day, 2, [val("NH3-N", 0.5)])
    body = interpret(client, "S1", END, "COD", 5.0).json()
    assert body["status"] == "insufficient_data"
    assert body["missing_pollutant_dates"] == [str(first_day)]
    revs = {r["date"]: r["revision"] for r in body["revisions"]}
    assert revs[str(first_day)] == 2

    # 第 1 天 rev3 重新加入 COD=0.5 → 用最新修订判读
    submit(client, "S1", first_day, 3, [val("COD", 0.5)])
    body2 = interpret(client, "S1", END, "COD", 5.0).json()
    assert body2["status"] == "compliant"
    revs2 = {r["date"]: r["revision"] for r in body2["revisions"]}
    assert revs2[str(first_day)] == 3


def test_interpret_unknown_station(client):
    body = interpret(client, "NOPE", END, "COD", 1.0).json()
    assert body["status"] == "insufficient_data"
    assert len(body["missing_dates"]) == 7
    assert body["revisions"] == []


# ---------------- 校验 ----------------


@pytest.mark.parametrize(
    "item",
    [
        {"type": "value", "pollutant": "COD", "value": -0.1},          # 负浓度
        {"type": "value", "pollutant": "COD", "value": "abc"},         # 非数值
        {"type": "value", "pollutant": "COD"},                          # 缺 value
        {"type": "value", "pollutant": "COD", "value": 1.0, "detection_limit": 0.5},  # 多余字段
        {"type": "nd", "pollutant": "COD"},                             # 缺检出限
        {"type": "nd", "pollutant": "COD", "detection_limit": 0},       # 检出限须为正
        {"type": "nd", "pollutant": "COD", "detection_limit": -1},
        {"type": "nd", "pollutant": "COD", "value": 1.0, "detection_limit": 0.5},
        {"type": "other", "pollutant": "COD"},
        {"type": "value", "pollutant": "", "value": 1.0},               # 空污染物名
    ],
)
def test_invalid_items_rejected(client, item):
    r = submit(client, "S1", "2026-11-10", 1, [item])
    assert r.status_code == 422, r.text


def test_non_finite_values_rejected(client):
    r = client.post(
        "/reports",
        content=(
            b'{"station":"S1","sampling_date":"2026-11-10","revision":1,'
            b'"items":[{"type":"value","pollutant":"COD","value":Infinity}]}'
        ),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422


def test_duplicate_pollutant_rejected(client):
    r = submit(client, "S1", "2026-11-10", 1, [val("COD", 1.0), val("COD", 2.0)])
    assert r.status_code == 422


@pytest.mark.parametrize("revision", [0, -1, 1.5, "2", True])
def test_invalid_revision_rejected(client, revision):
    r = client.post(
        "/reports",
        json={
            "station": "S1",
            "sampling_date": "2026-11-10",
            "revision": revision,
            "items": [val("COD", 1.0)],
        },
    )
    assert r.status_code == 422, r.text


def test_empty_items_rejected(client):
    r = submit(client, "S1", "2026-11-10", 1, [])
    assert r.status_code == 422


@pytest.mark.parametrize("limit", [-0.5, "abc"])
def test_invalid_limit_rejected(client, limit):
    r = interpret(client, "S1", END, "COD", limit)
    assert r.status_code == 422, r.text


def test_failed_submission_leaves_nothing(client):
    submit(client, "S1", "2026-11-10", 1, [val("COD", 1.0)])
    # 一次被拒的提交不应改变已存数据，也不产生半成品
    submit(client, "S1", "2026-11-10", 1, [val("COD", 9.9)])  # 409
    submit(client, "S1", "2026-11-10", 0, [val("COD", 9.9)])  # 422
    g = client.get("/reports/S1/2026-11-10").json()
    assert g["revision"] == 1
    assert g["items"] == [{"type": "value", "pollutant": "COD", "value": 1.0}]
    revs = client.get("/reports/S1/2026-11-10/revisions").json()["revisions"]
    assert len(revs) == 1


# ---------------- 并发 ----------------


def test_concurrent_identical_submissions(client):
    payload = {
        "station": "S1",
        "sampling_date": "2026-11-10",
        "revision": 1,
        "items": [val("COD", 1.0), nd("NH3-N", 0.05)],
    }
    results = []

    def worker():
        c = TestClient(client.app)
        r = c.post("/reports", json=payload)
        results.append((r.status_code, r.json()))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    codes = sorted(code for code, _ in results)
    assert codes.count(201) == 1
    assert codes.count(200) == 7
    ids = {body["report_id"] for _, body in results}
    assert len(ids) == 1  # 全部复用同一份报告
    revs = client.get("/reports/S1/2026-11-10/revisions").json()["revisions"]
    assert len(revs) == 1


def test_concurrent_increasing_revisions(client):
    def worker(rev):
        c = TestClient(client.app)
        r = c.post(
            "/reports",
            json={
                "station": "S1",
                "sampling_date": "2026-11-10",
                "revision": rev,
                "items": [val("COD", float(rev)), nd("NH3-N", 0.05)],
            },
        )
        assert r.status_code in (201, 409), r.text
        return r.status_code

    threads = [threading.Thread(target=worker, args=(rev,)) for rev in range(1, 9)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 最终现行报告完整可读，项目无缺失、无混杂
    g = client.get("/reports/S1/2026-11-10")
    assert g.status_code == 200
    data = g.json()
    assert data["revision"] == 8
    assert sorted(i["pollutant"] for i in data["items"]) == ["COD", "NH3-N"]
    cod = next(i for i in data["items"] if i["pollutant"] == "COD")
    assert cod["value"] == 8.0  # 与最高修订一致，而非半份或混杂


def test_concurrent_submit_and_interpret(client):
    fill_week(client, "S1", END, "COD", [1.0] * 7)
    errors = []

    def submitter():
        c = TestClient(client.app)
        for rev in range(2, 12):
            c.post(
                "/reports",
                json={
                    "station": "S1",
                    "sampling_date": str(END),
                    "revision": rev,
                    "items": [val("COD", 1.0)],
                },
            )

    def reader():
        c = TestClient(client.app)
        for _ in range(30):
            r = c.get(
                "/interpretation",
                params={
                    "station": "S1",
                    "end_date": str(END),
                    "pollutant": "COD",
                    "limit": 5.0,
                },
            )
            if r.status_code != 200 or r.json()["status"] != "compliant":
                errors.append(r.text)

    threads = [threading.Thread(target=submitter)] + [
        threading.Thread(target=reader) for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    # 判读始终基于同一时点的七份现行日报：最终所用修订号一致
    body = interpret(client, "S1", END, "COD", 5.0).json()
    assert [r["revision"] for r in body["revisions"]] == [1] * 6 + [11]
