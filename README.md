# 流域监测报告提交、判读与交接对账 API

实验室按站点、采样日提交包含各污染物的完整日报（带递增修订号），系统按
“同一已提交时点的七份现行日报”对连续七天平均浓度做限值判读，辅助决定
是否复采；现场采样交接单的补正记录可批量提交对账，确定哪些实验室日报
可以入仓。技术栈：FastAPI + SQLite（WAL），交付可启动的 Docker Compose
环境，Swagger UI（`/docs`）可直接操作提交、判读与交接对账。

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

### `POST /handover/reconcile` —— 现场采样交接补正对账

提交最多 30 条样本记录（补正可乱序，也可改站点或采样日），沿替换关系确定
每个站日的最终样本，与同一已提交快照中的现行完整日报比对项目集合：

```json
{
  "samples": [
    {
      "sample_id": "S-002",
      "station": "ST-01",
      "sampling_date": "2026-11-11",
      "pollutants": ["COD", "NH3-N"],
      "replaces_sample_id": "S-001"
    },
    {
      "sample_id": "S-001",
      "station": "ST-01",
      "sampling_date": "2026-11-10",
      "pollutants": ["COD"]
    }
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `sample_id` | 样本 ID，批内唯一 |
| `station` / `sampling_date` | 该样本**当前**所在站点与采样日（补正后） |
| `pollutants` | 应检污染物集合，重复项自动去重 |
| `replaces_sample_id` | 可选：本样本替换的旧样本 ID |

替换关系是一张有向图（被替换样本 → 替换样本）。每个站日**没有被任何样本替换**
的样本即最终样本。响应字段：

- `warehouseable_reports`：可入仓的日报，含 `report_id`、`revision`、站日与
  应检污染物（应检项目与现行报告项目完全一致）；
- `missing_reports`：该站日没有现行报告（缺报）；
- `missing_items`：现行报告缺少应检污染物（缺项，附 `missing_pollutants`）；
- `extra_items`：现行报告多出交接单未要求的污染物（多项，附 `extra_pollutants`）；
- `handover_conflicts`：交接冲突站日，`reasons` 为
  `missing_predecessor`（缺前驱）、`cycle`（成环）、`branch`（分叉）、
  `multiple_final_samples`（同站日多个最终样本）、
  `station_day_in_conflict`（同站日另有冲突样本）、
  `replacement_chain_in_conflict`（替换链上有冲突样本）；
- `pending_verification`：跨站日补正让出的旧位置。即使该位置有报告，
  `report_exists` 为 `true`、`report_id`/`revision` 照常返回，但**不得入仓**；
- `resolved_samples`：每个提交样本的去向（`warehouseable`、`missing_report`、
  `missing_items`、`extra_items`、`pending_verification`、`superseded`、
  `handover_conflict`）。

冲突站日上的样本、被其替换的样本、以及同一替换链上的样本一律不在可入仓
清单中；冲突以站日为单位汇总。所有日报在同一读事务内取自同一已提交时点，
响应含 `snapshot_seq`；并发补报或修订不会让一次对账混用不同时点的日报。

## 并发与一致性设计

- SQLite WAL + `busy_timeout`：读写不互斥，写事务串行；
- 提交在 `BEGIN IMMEDIATE` 事务内完成“检查当前修订 → 写入报告与项目”，
  并发下检查与写入不会交错；`(station, sampling_date, revision)` 唯一约束
  兜底，同修订并发重传只有一份落库，其余幂等复用；
- 判读在单个读事务内完成，七份日报来自同一已提交时点，不受并发提交影响；
- 交接对账同样在单个读事务内完成：先取 `MAX(id)` 作为快照时点（报告提交
  顺序号），再以 `id <= snapshot_seq` 取各站日现行修订，因此并发补报/修订
  不会混入快照；
- 历史修订只增不改，已提交报告始终可读取。

## 目录结构

```
app/
  main.py     # FastAPI 应用与端点
  models.py   # 请求/响应模型与校验
  logic.py    # 内容规范化哈希与判读（纯函数）
  handover.py # 交接补正对账（替换图、冲突、待核对，纯函数）
  db.py       # SQLite 连接、schema、事务约定
tests/        # pytest 端到端测试（含并发与快照一致性用例）
Dockerfile / docker-compose.yml
```
