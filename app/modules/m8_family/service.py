"""家庭组合结算服务层：编排规则引擎、指纹、版本与复核结案。

状态流转（每个组合）
--------------------
变更已采信（build 出待复核 staged 版本，整组金额待复核）
   └─ 复核通过 activate → active（每位成员都有明确去向，当前生效）
        └─ close 结案冻结 → closed；下载结案文件读取该冻结的同一组合结果

同一更正重跑：指纹相同 → 命中既有版本，不新增补退。
替换中断：activate 在单事务内切换指针，失败回滚，当前仍指旧版。
"""

from datetime import datetime, timezone

from app.config import FAMILY_RULE_VERSION
from app.modules.m8_family import store as fstore
from app.modules.m8_family.evidence import (
    canonical,
    closing_csv,
    fingerprint,
    freeze_input,
    result_hash,
)
from app.modules.m8_family.rules import settle


def _now():
    return datetime.now(timezone.utc).isoformat()


def build(combo, rule_version=FAMILY_RULE_VERSION):
    """对一组已采信变更执行裁决，落为待复核版本（幂等）。

    入参 combo 见 rules.settle。返回 (version_data, created)。
    冻结输入快照后再裁决，保证成员关系与原账单不被后续改动影响。
    """
    snapshot = freeze_input(combo)
    snapshot["rule_version"] = rule_version
    fp = fingerprint(snapshot, rule_version)
    result = settle(snapshot)
    result["rule_version"] = rule_version
    result["result_hash"] = result_hash(result)
    staged = fstore.stage(
        snapshot["combo_id"], fp, result["result_hash"], snapshot, result, _now()
    )
    return staged


def review_activate(combo_id, version):
    """复核通过指定 staged 版本，原子切换为当前生效版本。"""
    return fstore.activate(combo_id, version, _now())


def close_settlement(combo_id):
    return fstore.close(combo_id, _now())


def current(combo_id):
    return fstore.get_current(combo_id)


def versions(combo_id):
    return fstore.list_versions(combo_id)


def download(combo_id):
    """结案下载：读取当前生效（或已结案）版本的同一组合结果生成 CSV。

    尚处 staged（无生效指针）时返回 409 STATE，提示先复核；组合完全不存在
    才是 404 NOT_FOUND。
    """
    from app.errors import DomainError

    versions_ = fstore.list_versions(combo_id)
    if not versions_:
        raise DomainError("NOT_FOUND", "组合结算不存在", 404)
    try:
        result = fstore.get_current(combo_id)
    except DomainError as error:
        if error.code == "NOT_FOUND":
            raise DomainError("STATE", "版本尚未复核通过，不能下载结案文件", 409)
        raise
    if result["status"] not in ("active", "closed"):
        raise DomainError("STATE", "版本尚未复核通过，不能下载结案文件", 409)
    return closing_csv(result), result


def preview(combo):
    """不落库的试算（复核界面在提交前预览同一套规则的结果）。"""
    snapshot = freeze_input(combo)
    snapshot["rule_version"] = FAMILY_RULE_VERSION
    result = settle(snapshot)
    result["rule_version"] = FAMILY_RULE_VERSION
    result["fingerprint"] = fingerprint(snapshot, FAMILY_RULE_VERSION)
    result["result_hash"] = result_hash(result)
    return result


# ---------------------------------------------------------------------------
# 内置家庭样例：三口之家，成员共用折扣档；张某追溯减保导致三档→两档。
# 追溯期 2026-08-01 ~ 2026-11-01（3 个自然月），金额单位：分。
# ---------------------------------------------------------------------------
def sample_combo():
    return {
        "combo_id": "FAM-2026-0001",
        "period": {"start": "2026-08-01", "end": "2026-11-01"},
        "trigger_id": "M-zhang",
        "old_tier": 3,
        "members": [
            {
                "member_id": "M-zhang", "name": "张某",
                # 减保后标准保费 5000/月；减保前 6500/月
                "standard_cents": 1_500_000,
                "old_standard_cents": 1_950_000,
                # 原按三档 8 折：19500*0.8=15600
                "old_billed_cents": 1_560_000,
                "paid_cents": 1_560_000,
                "bill_mailed": True,
            },
            {
                "member_id": "M-li", "name": "李某",
                "standard_cents": 900_000,
                "old_standard_cents": 900_000,
                # 9000*0.8=7200
                "old_billed_cents": 720_000,
                "paid_cents": 720_000,
                "bill_mailed": True,
            },
            {
                "member_id": "M-wang", "name": "王某",
                "standard_cents": 600_000,
                "old_standard_cents": 600_000,
                # 6000*0.8=4800
                "old_billed_cents": 480_000,
                "paid_cents": 480_000,
                "bill_mailed": True,
            },
        ],
    }
