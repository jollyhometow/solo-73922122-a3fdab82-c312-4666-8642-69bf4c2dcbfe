# 流域监测报告提交与判读 API

实验室按站点、采样日提交包含各污染物的完整日报（带递增修订号），系统按
“同一已提交时点的七份现行日报”对连续七天平均浓度做限值判读，辅助决定
是否复采。技术栈：FastAPI + SQLite（WAL），交付可启动的 Docker Compose
环境，Swagger UI（`/docs`）可直接操作提交与判读。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8000/docs
```

本地开发（无需 Docker）：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest                      # 运行测试
uvicorn app.main:app --reload   # http://localhost:8000/docs
```

## 数据与单位

- 测值单位 mg/L，两种形态：
  - `{"type": "value", "pollutant": "COD", "value": 12.3}` —— 有限非负浓度；
  - `{"type": "nd", "pollutant": "NH3-N", "detection_limit": 0.05}` —— “未检出”，
    带有限正检出限。
- 修订号为正整数，同一（站点, 采样日）内递增，允许跳号。

## API

### `POST /reports` —— 提交日报

```json
{
  "station": "ST-01",
  "sampling_date": "2026-11-10",
  "revision": 1,
  "items": [
    {"type": "value", "pollutant": "COD", "value": 12.3},
    {"type": "nd", "pollutant": "NH3-N", "detection_limit": 0.05}
  ]
}
```

| 情形 | 结果 |
| --- | --- |
| 修订号高于当前最高修订 | `201` 完整替换当天报告（删除的项目不沿用旧值） |
| 同修订、同内容重传 | `200` 幂等复用，返回原 `report_id` |
| 修订号低于当前最高修订 | `409 stale_revision` |
| 同修订、内容不同 | `409 revision_content_mismatch` |

提交整体落库或整体回滚：并发提交与失败重试不会留下半份报告；历史修订
永不删除，始终可读取。

### `GET /reports/{station}/{sampling_date}[?revision=N]` —— 读取报告

缺省返回现行（最高修订）报告；指定 `revision` 读取历史修订。
`GET /reports/{station}/{sampling_date}/revisions` 列出全部修订。

### `GET /interpretation` —— 判读

参数：`station`、`end_date`（窗口结束日，含）、`pollutant`、`limit`（非负限值）。

```bash
curl 'http://localhost:8000/interpretation?station=ST-01&end_date=2026-11-14&pollutant=COD&limit=10'
```

判读规则（窗口为结束日向前连续 7 天，取同一已提交时点各日现行日报）：

- 缺日或当天报告缺该污染物 → `insufficient_data`（资料不足）；
- “未检出”按半开区间 `[0, 检出限)` 参与均值，实测值即点值；
- 均值必定不高于限值（区间上界 ≤ 限值）→ `compliant`（达标）；
- 均值必定高于限值（区间下界 > 限值）→ `exceeds`（超标）；
- 其余（可能区间跨过限值）→ `pending_review`（待复核）。

响应示例：

```json
{
  "station": "ST-01", "pollutant": "COD",
  "end_date": "2026-11-14", "window_start": "2026-11-08", "window_days": 7,
  "limit": 10.0,
  "status": "compliant", "status_zh": "达标",
  "mean": {"lower": 8.1, "upper": 9.4, "upper_exclusive": true},
  "revisions": [{"date": "2026-11-08", "revision": 2, "report_id": 3}, "..."],
  "missing_dates": [], "missing_pollutant_dates": [],
  "snapshot_seq": 42
}
```

`revisions` 即判读所用的各日修订号；`snapshot_seq` 为判读使用的已提交
时点（报告提交顺序号），均值上下界用精确有理数计算，边界比较无舍入歧义。

## 并发与一致性设计

- SQLite WAL + `busy_timeout`：读写不互斥，写事务串行；
- 提交在 `BEGIN IMMEDIATE` 事务内完成“检查当前修订 → 写入报告与项目”，
  并发下检查与写入不会交错；`(station, sampling_date, revision)` 唯一约束
  兜底，同修订并发重传只有一份落库，其余幂等复用；
- 判读在单个读事务内完成，七份日报来自同一已提交时点，不受并发提交影响；
- 历史修订只增不改，已提交报告始终可读取。

## 目录结构

```
app/
  main.py    # FastAPI 应用与端点
  models.py  # 请求/响应模型与校验
  logic.py   # 内容规范化哈希与判读（纯函数）
  db.py      # SQLite 连接、schema、事务约定
tests/       # pytest 端到端测试（含并发用例）
Dockerfile / docker-compose.yml
```
