"""SQLite 数据访问层。

设计要点：
- 每请求一条连接，``isolation_level=None``（自动提交），事务用显式
  ``BEGIN IMMEDIATE`` / ``COMMIT`` / ``ROLLBACK`` 控制；
- WAL 模式 + busy_timeout：读不阻塞写，写事务串行化，并发提交安全；
- 报告与其测点项目在同一事务内写入，失败整体回滚，不会留下半份报告；
- 旧修订行永不删除，已提交报告始终可读取。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,  -- 提交顺序号（判读快照时点）
    station       TEXT    NOT NULL,
    sampling_date TEXT    NOT NULL,                   -- ISO YYYY-MM-DD
    revision      INTEGER NOT NULL,
    content_hash  TEXT    NOT NULL,
    content_json  TEXT    NOT NULL,                   -- 规范化后的报告内容
    submitted_at  TEXT    NOT NULL,                   -- UTC ISO 时间戳
    UNIQUE (station, sampling_date, revision)
);
CREATE TABLE IF NOT EXISTS report_items (
    report_id        INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    pollutant        TEXT    NOT NULL,
    type             TEXT    NOT NULL CHECK (type IN ('value', 'nd')),
    value            REAL,                            -- type='value' 时的浓度
    detection_limit  REAL,                            -- type='nd' 时的检出限
    UNIQUE (report_id, pollutant)
);
CREATE INDEX IF NOT EXISTS idx_reports_station_date ON reports (station, sampling_date);
CREATE INDEX IF NOT EXISTS idx_items_report ON report_items (report_id);
"""

PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=10000",
)


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        for pragma in PRAGMAS:
            conn.execute(pragma)
        return conn


def rollback_quietly(conn: sqlite3.Connection) -> None:
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass
