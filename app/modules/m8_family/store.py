"""家庭组合结算的持久化：版本化、事务替换、中断回滚。

表设计
------
family_settlements   每个组合一行“当前指针”（当前生效版本号）
family_versions      每个组合的每个不可变版本一行（状态机 + 完整快照 + 指纹）
family_ledger        逐成员、逐版本的补退/挂账流水（复核与结案的逐人依据）

关键保证
--------
* 幂等：同一更正指纹在同一组合下只对应一个版本，重跑直接返回既有版本，
  不会新增补退。
* 版本替换原子化：新版先以 staged 写入，复核通过后在**单事务**内把当前指针
  从旧版切到新版并将旧版置 superseded；任何中途失败都整体回滚，当前指针仍
  指向旧版（“替换中断时恢复旧版”）。
* 已结案（closed）版本冻结，不可再被替换。
* 成员关系与原账单随版本快照整体留存；源记录丢失也能凭快照解释整组结算。
"""

import json
import os
import sqlite3
from pathlib import Path

from app.errors import DomainError

# 版本状态机：staged（已算待复核）→ active（复核通过、当前生效）
#             → superseded（被新版替换）；closed 为 active 后的结案冻结态。
STAGED, ACTIVE, SUPERSEDED, CLOSED = "staged", "active", "superseded", "closed"


def connection():
    path = Path(os.environ.get("DB_PATH", "data/records.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("""CREATE TABLE IF NOT EXISTS family_settlements(
        combo_id TEXT PRIMARY KEY,
        current_version INTEGER NOT NULL,
        updated_at TEXT NOT NULL DEFAULT '')""")
    db.execute("""CREATE TABLE IF NOT EXISTS family_versions(
        combo_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        fingerprint TEXT NOT NULL,
        result_hash TEXT NOT NULL,
        status TEXT NOT NULL,
        input_snapshot TEXT NOT NULL,
        result TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT '',
        reviewed_at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(combo_id, version))""")
    db.execute("""CREATE TABLE IF NOT EXISTS family_ledger(
        combo_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        member_id TEXT NOT NULL,
        disposition TEXT NOT NULL,
        book_delta_cents INTEGER NOT NULL,
        cash_delta_cents INTEGER NOT NULL,
        hold_delta_cents INTEGER NOT NULL,
        PRIMARY KEY(combo_id, version, member_id))""")
    return db


def _row_to_version(row):
    data = json.loads(row["result"])
    data["version"] = row["version"]
    data["status"] = row["status"]
    data["fingerprint"] = row["fingerprint"]
    data["result_hash"] = row["result_hash"]
    return data


def get_current(combo_id):
    """取组合当前生效版本；不存在抛 NOT_FOUND。"""
    with connection() as db:
        ptr = db.execute("SELECT current_version FROM family_settlements WHERE combo_id=?",
                         (combo_id,)).fetchone()
        if not ptr:
            raise DomainError("NOT_FOUND", "组合结算不存在", 404)
        row = db.execute("SELECT * FROM family_versions WHERE combo_id=? AND version=?",
                         (combo_id, ptr["current_version"])).fetchone()
    return _row_to_version(row)


def get_version(combo_id, version):
    with connection() as db:
        row = db.execute("SELECT * FROM family_versions WHERE combo_id=? AND version=?",
                         (combo_id, version)).fetchone()
    if not row:
        raise DomainError("NOT_FOUND", "组合版本不存在", 404)
    return _row_to_version(row)


def list_versions(combo_id):
    with connection() as db:
        rows = db.execute("""SELECT combo_id,version,status,fingerprint,result_hash,created_at,reviewed_at
                             FROM family_versions WHERE combo_id=? ORDER BY version""",
                          (combo_id,)).fetchall()
    return [dict(r) for r in rows]


def find_by_fingerprint(combo_id, fingerprint):
    """同一更正指纹是否已生成过版本（幂等命中）。"""
    with connection() as db:
        row = db.execute("""SELECT * FROM family_versions WHERE combo_id=? AND fingerprint=?
                            ORDER BY version LIMIT 1""", (combo_id, fingerprint)).fetchone()
    return _row_to_version(row) if row else None


def stage(combo_id, fingerprint, result_hash, combo_input, result, now=""):
    """写入一个待复核版本。

    幂等：同指纹已存在则直接返回既有版本，不新增版本、不新增补退。
    否则版本号 = 当前最大版本 + 1（首个版本为 1），状态 staged。
    已结案（closed）组合在幂等判断之前即拒绝，确保冻结态不可再开版本。
    """
    with connection() as db:
        # 已结案组合不允许再开新版本（优先于幂等命中，冻结不可绕过）
        ptr = db.execute("SELECT current_version FROM family_settlements WHERE combo_id=?",
                         (combo_id,)).fetchone()
        if ptr:
            cur = db.execute("SELECT status FROM family_versions WHERE combo_id=? AND version=?",
                             (combo_id, ptr["current_version"])).fetchone()
            if cur and cur["status"] == CLOSED:
                raise DomainError("CLOSED", "组合已结案冻结，不能再开新版本", 409)

    existing = find_by_fingerprint(combo_id, fingerprint)
    if existing:
        existing["idempotent_hit"] = True
        return existing

    with connection() as db:
        row = db.execute("SELECT COALESCE(MAX(version),0) AS v FROM family_versions WHERE combo_id=?",
                         (combo_id,)).fetchone()
        next_version = row["v"] + 1
        db.execute("""INSERT INTO family_versions(combo_id,version,fingerprint,result_hash,
                     status,input_snapshot,result,created_at)
                     VALUES(?,?,?,?,?,?,?,?)""",
                   (combo_id, next_version, fingerprint, result_hash, STAGED,
                    json.dumps(combo_input, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False), now))
        for m in result["members"]:
            db.execute("""INSERT INTO family_ledger(combo_id,version,member_id,disposition,
                         book_delta_cents,cash_delta_cents,hold_delta_cents)
                         VALUES(?,?,?,?,?,?,?)""",
                       (combo_id, next_version, m["member_id"], m["disposition"],
                        m["book_delta_cents"], m["cash_delta_cents"], m["hold_delta_cents"]))
    out = get_version(combo_id, next_version)
    out["idempotent_hit"] = False
    return out


def activate(combo_id, version, now=""):
    """复核通过：单事务把当前指针切到该版本，旧版置 superseded。

    * staged 版本才能激活；
    * 中断（任何异常）整体回滚，指针仍指旧版；
    * 首个版本激活时建立指针；之后切换保留旧版行用于追溯。
    """
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT status FROM family_versions WHERE combo_id=? AND version=?",
                             (combo_id, version)).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "组合版本不存在", 404)
            if row["status"] != STAGED:
                raise DomainError("STATE", f"版本状态为 {row['status']}，不能复核激活", 409)
            ptr = db.execute("SELECT current_version FROM family_settlements WHERE combo_id=?",
                             (combo_id,)).fetchone()
            if ptr:
                db.execute("UPDATE family_versions SET status=? WHERE combo_id=? AND version=?",
                           (SUPERSEDED, combo_id, ptr["current_version"]))
            db.execute("UPDATE family_versions SET status=?, reviewed_at=? WHERE combo_id=? AND version=?",
                       (ACTIVE, now, combo_id, version))
            db.execute("""INSERT INTO family_settlements(combo_id,current_version,updated_at)
                          VALUES(?,?,?) ON CONFLICT(combo_id) DO UPDATE SET
                          current_version=excluded.current_version, updated_at=excluded.updated_at""",
                       (combo_id, version, now))
        except Exception:
            db.rollback()
            raise
        else:
            db.commit()
    return get_current(combo_id)


def close(combo_id, now=""):
    """结案冻结当前 active 版本。"""
    with connection() as db:
        ptr = db.execute("SELECT current_version FROM family_settlements WHERE combo_id=?",
                         (combo_id,)).fetchone()
        if not ptr:
            raise DomainError("NOT_FOUND", "组合结算不存在", 404)
        cur = db.execute("SELECT status FROM family_versions WHERE combo_id=? AND version=?",
                         (combo_id, ptr["current_version"])).fetchone()
        if cur["status"] != ACTIVE:
            raise DomainError("STATE", "仅生效版本可结案", 409)
        db.execute("UPDATE family_versions SET status=?, reviewed_at=? WHERE combo_id=? AND version=?",
                   (CLOSED, now, combo_id, ptr["current_version"]))
    return get_current(combo_id)


def ledger(combo_id, version):
    with connection() as db:
        rows = db.execute("""SELECT * FROM family_ledger WHERE combo_id=? AND version=?
                             ORDER BY member_id""", (combo_id, version)).fetchall()
    return [dict(r) for r in rows]
