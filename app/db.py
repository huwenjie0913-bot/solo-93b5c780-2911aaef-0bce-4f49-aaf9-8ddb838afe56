"""sqlite3 连接、建表脚本与基础读写辅助。

所有实体（原料资料、单位表、标签规则、配方）一旦提交即不可变：
相同内容重复提交会幂等返回；相同版本但内容变化返回 409。
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 原料营养/过敏原资料版本（一个 release 下可批量提交多个原料）
CREATE TABLE IF NOT EXISTS ingredient_release (
    id                INTEGER PRIMARY KEY,
    release_version   TEXT NOT NULL UNIQUE,
    body_hash         TEXT NOT NULL,
    body              TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

-- 单个原料（属于某个 release）
CREATE TABLE IF NOT EXISTS ingredient (
    id                INTEGER PRIMARY KEY,
    release_version   TEXT NOT NULL REFERENCES ingredient_release(release_version),
    code              TEXT NOT NULL,
    name              TEXT,
    category          TEXT NOT NULL DEFAULT 'ingredient',
    nutrition         TEXT NOT NULL,   -- 营养素 -> 每 basis_amount/basis_unit 的数值
    basis_amount      REAL NOT NULL,
    basis_unit        TEXT NOT NULL,
    allergens         TEXT NOT NULL,   -- {allergen: contains|may_contain|free}
    UNIQUE(release_version, code)
);

-- 单位换算表版本
CREATE TABLE IF NOT EXISTS unit_version (
    id                INTEGER PRIMARY KEY,
    version           TEXT NOT NULL UNIQUE,
    body_hash         TEXT NOT NULL,
    body              TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS unit_conversion (
    id                INTEGER PRIMARY KEY,
    unit_version      TEXT NOT NULL REFERENCES unit_version(version),
    from_unit         TEXT NOT NULL,
    to_unit           TEXT NOT NULL,
    factor            REAL NOT NULL,    -- 1 from_unit = factor to_unit
    UNIQUE(unit_version, from_unit, to_unit)
);

-- 标签规则版本（必报营养素、每日参考值 NRV、舍入位数）
CREATE TABLE IF NOT EXISTS label_rule (
    id                INTEGER PRIMARY KEY,
    version           TEXT NOT NULL UNIQUE,
    body_hash         TEXT NOT NULL,
    body              TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

-- 配方版本
CREATE TABLE IF NOT EXISTS recipe (
    id                INTEGER PRIMARY KEY,
    code              TEXT NOT NULL,
    version           TEXT NOT NULL,
    body_hash         TEXT NOT NULL,
    name              TEXT,
    yield_qty         REAL NOT NULL,
    yield_unit        TEXT NOT NULL,
    servings          REAL NOT NULL,
    components        TEXT NOT NULL,    -- [{kind, code, version?, qty, unit}]
    created_at        TEXT NOT NULL,
    UNIQUE(code, version)
);

-- 计算记录：以请求指纹为缓存键
CREATE TABLE IF NOT EXISTS computation (
    id                INTEGER PRIMARY KEY,
    fingerprint       TEXT NOT NULL UNIQUE,
    request           TEXT NOT NULL,
    result            TEXT NOT NULL,
    export_doc        TEXT NOT NULL,
    recipe_code       TEXT NOT NULL,
    recipe_version    TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    hits              INTEGER NOT NULL DEFAULT 0
);

-- 计算涉及的配方（含嵌套展开链），用于按配方追溯历史
CREATE TABLE IF NOT EXISTS computation_recipe (
    id                INTEGER PRIMARY KEY,
    computation_id    INTEGER NOT NULL REFERENCES computation(id),
    recipe_code       TEXT NOT NULL,
    recipe_version    TEXT NOT NULL,
    depth             INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ingredient_code ON ingredient(code);
CREATE INDEX IF NOT EXISTS idx_comp_recipe ON computation_recipe(recipe_code, recipe_version);
CREATE INDEX IF NOT EXISTS idx_comp_root ON computation(recipe_code, recipe_version, created_at);
"""


def utc_now_iso():
    """UTC 时间，秒级 ISO8601（Z 结尾）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def get_db_path():
    return os.environ.get("LABEL_DB_PATH", os.path.join(os.getcwd(), "labels.db"))


def get_conn():
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn=None):
    own = conn is None
    conn = conn or get_conn()
    conn.executescript(SCHEMA)
    # 旧库轻量迁移：ingredient.category（原料/添加剂）
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(ingredient)").fetchall()}
    if "category" not in cols:
        conn.execute(
            "ALTER TABLE ingredient ADD COLUMN category TEXT NOT NULL"
            " DEFAULT 'ingredient'"
        )
    conn.commit()
    if own:
        conn.close()


def init_app(app):
    @app.teardown_appcontext
    def _close(_exc):
        pass  # 各请求自行管理连接；此处保留扩展点


def row_to_dict(row):
    return dict(row) if row is not None else None


def loads(s):
    return json.loads(s)
