"""m8 家庭组合结算：规则引擎、守恒与确定性测试。"""
import pytest

from app.modules.m8_family import rules, service
from app.modules.m8_family.evidence import canonical


@pytest.fixture
def combo():
    return service.sample_combo()


@pytest.fixture
def result(combo):
    return service.preview(combo)


def test_tier_drops_from_three_to_two(result):
    # 减保后组标准保费 30000 元（<32000 且 ≥20000）落入两档 9 折
    assert result["tier_change"]["old_tier"] == 3
    assert result["tier_change"]["new_tier"] == 2
    assert result["tier_change"]["dropped"] is True
    assert result["group"]["standard_cents"] == 3_000_000


def test_group_receivable_uses_new_tier(result):
    # 30000 * (1-0.10) = 27000
    assert result["group"]["receivable_cents"] == 2_700_000


def test_trigger_member_cash_refund_and_reopen(result):
    zhang = next(m for m in result["members"] if m["member_id"] == "M-zhang")
    assert zhang["new_receivable_cents"] == 1_350_000      # 15000 * 0.9
    assert zhang["old_billed_cents"] == 1_560_000          # 19500 * 0.8
    assert zhang["cash_delta_cents"] == -210_000           # 现金退 2100
    assert zhang["book_delta_cents"] == -210_000
    assert zhang["hold_delta_cents"] == 0
    assert zhang["reopen_bill"] is True
    assert zhang["disposition"] == rules.DISP_REFUND


def test_other_members_hold_no_cash_bill_kept(result):
    li = next(m for m in result["members"] if m["member_id"] == "M-li")
    wang = next(m for m in result["members"] if m["member_id"] == "M-wang")
    # 李某 9000*0.9=8100，较原账单 7200 增 900，全额挂账、现金 0、账单有效
    assert li["new_receivable_cents"] == 810_000
    assert li["hold_delta_cents"] == 90_000
    assert li["cash_delta_cents"] == 0
    assert li["reopen_bill"] is False
    assert li["disposition"] == rules.DISP_HOLD
    # 王某 6000*0.9=5400，较原 4800 增 600 挂账
    assert wang["new_receivable_cents"] == 540_000
    assert wang["hold_delta_cents"] == 60_000
    assert wang["cash_delta_cents"] == 0


def test_receivable_conservation(result):
    total = sum(m["new_receivable_cents"] for m in result["members"])
    assert total == result["group"]["receivable_cents"]
    assert result["conservation"]["receivable_conserved"] is True


def test_delta_conservation_book_equals_cash_plus_hold(result):
    c = result["conservation"]
    assert c["delta_conserved"] is True
    assert c["sum_book_delta_cents"] == c["sum_cash_delta_cents"] + c["sum_hold_delta_cents"]
    # -2100（张某现金退） + 900 + 600（挂账） = -600
    assert c["sum_book_delta_cents"] == -60_000
    assert c["sum_cash_delta_cents"] == -210_000
    assert c["sum_hold_delta_cents"] == 150_000
    assert c["group_conserved"] is True


def test_deterministic_identical_fingerprint(combo, result):
    again = service.preview(canonical_roundtrip(combo))
    assert again["result_hash"] == result["result_hash"]
    assert again["fingerprint"] == result["fingerprint"]


def test_invalid_trigger_member_rejected(combo):
    combo["trigger_id"] = "NOT-IN-GROUP"
    with pytest.raises(ValueError):
        rules.settle(combo)


def test_duplicate_member_rejected(combo):
    combo["members"][1]["member_id"] = combo["members"][0]["member_id"]
    with pytest.raises(ValueError):
        rules.settle(combo)


def test_empty_members_rejected():
    with pytest.raises(ValueError):
        rules.settle({"combo_id": "x", "period": {"start": "2026-08-01", "end": "2026-11-01"},
                      "trigger_id": "a", "old_tier": 3, "members": []})


@pytest.mark.parametrize("total,weights", [
    (100, [33, 33, 34]), (7, [1, 1, 1]), (10, [0, 0, 0]), (0, [5, 5]), (12345, [7, 13, 19, 23]),
])
def test_largest_remainder_conserves(total, weights):
    alloc = rules.largest_remainder(weights, total)
    assert sum(alloc) == total
    assert len(alloc) == len(weights)
    assert all(a >= 0 for a in alloc)


def test_all_members_have_explicit_disposition(result):
    # 完成后每位成员都有明确去向
    assert len(result["members"]) == 3
    assert all(m["disposition"] in rules.DISPOSITION_TEXT for m in result["members"])
    assert all(m["disposition_text"] for m in result["members"])


def test_no_downgrade_trigger_refund_other_even():
    # 减保后仍在同档（不降级）：触发成员退费，另一成员无差额 HOLD_EVEN
    combo = {
        "combo_id": "T1", "period": {"start": "2026-08-01", "end": "2026-11-01"},
        "trigger_id": "a", "old_tier": 2,
        "members": [
            {"member_id": "a", "name": "A", "standard_cents": 2_200_000, "old_standard_cents": 2_400_000,
             "old_billed_cents": 2_160_000, "paid_cents": 2_160_000, "bill_mailed": True},
            {"member_id": "b", "name": "B", "standard_cents": 800_000, "old_standard_cents": 800_000,
             "old_billed_cents": 720_000, "paid_cents": 720_000, "bill_mailed": True}]}
    r = rules.settle(combo)
    assert r["tier_change"]["dropped"] is False
    a, b = r["members"]
    assert a["disposition"] == rules.DISP_REFUND and a["cash_delta_cents"] < 0
    assert b["disposition"] == rules.DISP_HOLD_EVEN and b["cash_delta_cents"] == 0 and b["hold_delta_cents"] == 0
    assert r["conservation"]["delta_conserved"] is True


def test_downgrade_but_trigger_owes_supplement():
    # 档位下降，但触发成员重算后新应收高于原账单 -> SUPPLEMENT，现金向成员补收
    combo = {
        "combo_id": "T2", "period": {"start": "2026-08-01", "end": "2026-11-01"},
        "trigger_id": "a", "old_tier": 3,
        "members": [
            {"member_id": "a", "name": "A", "standard_cents": 2_000_000, "old_standard_cents": 3_000_000,
             "old_billed_cents": 1_000_000, "paid_cents": 1_000_000, "bill_mailed": True},
            {"member_id": "b", "name": "B", "standard_cents": 500_000, "old_standard_cents": 500_000,
             "old_billed_cents": 400_000, "paid_cents": 400_000, "bill_mailed": True}]}
    r = rules.settle(combo)
    assert r["tier_change"]["new_tier"] == 2
    a, b = r["members"]
    assert a["disposition"] == rules.DISP_SUPPLEMENT and a["cash_delta_cents"] == 800_000
    assert b["disposition"] == rules.DISP_HOLD and b["cash_delta_cents"] == 0 and b["hold_delta_cents"] == 50_000
    assert r["conservation"]["delta_conserved"] is True
    assert r["conservation"]["group_conserved"] is True


def canonical_roundtrip(obj):
    import json
    return json.loads(canonical(obj))
