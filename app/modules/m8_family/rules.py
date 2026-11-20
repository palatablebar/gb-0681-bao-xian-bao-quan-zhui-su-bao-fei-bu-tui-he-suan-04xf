"""家庭组合结算的纯规则引擎（无 IO、无数据库）。

裁决目标
========
一份家庭保单的成员共用一个保费折扣档。一名成员追溯减保后，整组在追溯期的
标准保费规模下降，折扣档随之变化（例如三档→两档）。本模块对“一个组合键 +
一段追溯期 + 一组成员关系与原账单”给出**唯一**的组合结算结果。

两个并列诉求的裁决
------------------
1. 同一折扣档成员的“同期应收”必须一致：所有成员都按新档费率重新计算应收，
   账面层面逐人可核、组内守恒（各人应收按相同折扣率折算，组合计无歧义）。
2. 财务只对“真正变动的成员”发生现金补退，不重开其他成员已寄出的账单：
   非变动成员的已寄账单保持有效，其新旧应收之差不产生现金收付，按
   “折扣挂账（非现金）”入册，并在结案文件中与现金补退分列。

因此每位成员有两条并行结论：
  * 应收账（book）：新档应收、原账单应收、应收差额（正=应补/负=应退），
    保证同档成员口径一致；
  * 现金账（cash）：实际补退。仅当成员账单被本更正“重开”（变动成员）时，
    现金补退=应收差额；账单保持有效的成员，现金补退恒为 0，差额挂账。

所有金额一律以“分”的整数运算，杜绝浮点尾差；档内分摊采用最大余额法，
保证分摊回的各人应收之和严格等于按组规模折算后的组应收。
"""

from decimal import ROUND_HALF_UP, Decimal

# ---------------------------------------------------------------------------
# 折扣档定义（规则版本见 app/config.py 的 FAMILY_RULE_VERSION）
#
# 档位按“追溯期内全组标准保费合计（分）”从高到低判定，命中第一个满足
# threshold 的档位。threshold 为进入该档的组标准保费下限；档位越高，门槛
# 越高、折扣率越大。样例：减保前组标准 34500 元（≥32000）在三档 8 折，
# 张某追溯减保后组标准降到 30000 元（<32000 且 ≥20000），落入两档 9 折。
# rate 为该档折扣率（给予的折扣比例），实缴比例 = 1 - rate。
# ---------------------------------------------------------------------------
TIERS = [
    {"tier": 3, "name": "三档", "threshold": 3_200_000, "rate": Decimal("0.20")},
    {"tier": 2, "name": "两档", "threshold": 2_000_000, "rate": Decimal("0.10")},
    {"tier": 1, "name": "一档", "threshold": 0, "rate": Decimal("0.00")},
]

# 成员去向（disposition）码
DISP_REFUND = "REFUND"            # 变动成员：退费，账单重开
DISP_SUPPLEMENT = "SUPPLEMENT"    # 变动成员：补缴，账单重开
DISP_EVEN = "EVEN"                # 变动成员：账面持平，无现金
DISP_HOLD = "HOLD"                # 非变动成员：账单有效，差额挂账（非现金）
DISP_HOLD_EVEN = "HOLD_EVEN"      # 非变动成员：账单有效且无差额

DISPOSITION_TEXT = {
    DISP_REFUND: "退费（重开账单）",
    DISP_SUPPLEMENT: "补缴（重开账单）",
    DISP_EVEN: "持平（重开账单）",
    DISP_HOLD: "折扣挂账（账单有效）",
    DISP_HOLD_EVEN: "无差额（账单有效）",
}


def yuan(cents):
    """分 -> 元字符串，保留两位。"""
    return f"{Decimal(cents) / 100:.2f}"


def pick_tier(group_standard_cents):
    """按组标准保费规模唯一选定折扣档。"""
    for spec in TIERS:
        if group_standard_cents >= spec["threshold"]:
            return spec
    return TIERS[-1]


def largest_remainder(weights, total_cents):
    """把 total_cents 按 weights 比例分摊为整数分，最大余额法守恒。

    返回与 weights 等长的整数列表，其和严格等于 total_cents。
    权重为 0 的位置分得 0；若全为 0 则按序号把总额逐分派发，保证可重复。
    """
    n = len(weights)
    out = [0] * n
    if total_cents == 0:
        return out
    wsum = sum(weights)
    if wsum == 0:
        # 无可按比例分摊的依据：确定性地逐分派发，保证唯一且守恒。
        for i in range(total_cents):
            out[i % n] += 1
        return out
    floors = [total_cents * w // wsum for w in weights]
    residual = total_cents - sum(floors)
    # 余额大者优先；余额相同按序号，保证结果与字典顺序无关、确定性。
    order = sorted(range(n), key=lambda i: (-(total_cents * weights[i] - floors[i] * wsum), i))
    for i in order[:residual]:
        floors[i] += 1
    return floors


def _round_cents_from_yuan(amount_yuan):
    """Decimal 金额（元）四舍五入到分，返回整数分。"""
    return int(amount_yuan.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)


def settle(combo):
    """对一个组合结算输入执行裁决，返回唯一的组合结算结果（纯数据）。

    入参 combo 结构：
      combo_id      组合键（家庭/批次标识）
      period        追溯期 {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}
      trigger_id    触发本次更正的成员 id
      old_tier      原折扣档档级（整数）
      members       成员列表，每项：
        member_id, name,
        standard_cents   追溯期标准保费（分，减保后的现值）
        old_standard_cents 减保前标准保费（分）
        old_billed_cents 原账单“应收”（分，已寄出口径）
        paid_cents       原账单实收（分）
        bill_mailed      账单是否已寄出（bool）

    输出为可 JSON 序列化的 dict（金额同时给分与元），含：
      header（组合级）、members（逐人结论）、守恒校验、依据快照。
    """
    members = combo["members"]
    if not members:
        raise ValueError("组合至少需要一名成员")
    ids = [m["member_id"] for m in members]
    if len(set(ids)) != len(ids):
        raise ValueError("成员关系重复：member_id 必须唯一")
    if combo["trigger_id"] not in ids:
        raise ValueError("触发成员必须属于该组合")

    # 1) 组规模（减保后现值）→ 唯一新档
    group_standard = sum(int(m["standard_cents"]) for m in members)
    new_tier = pick_tier(group_standard)
    new_rate = new_tier["rate"]

    # 2) 组应收（按组规模统一折算，唯一确定）。入参为整数分，先转元折算再回分。
    group_receivable = _round_cents_from_yuan(
        Decimal(group_standard) / 100 * (1 - new_rate)
    )

    # 3) 档内把组应收按各人标准保费占比分摊回成员 → 同档成员口径一致、组内守恒
    weights = [int(m["standard_cents"]) for m in members]
    allocated = largest_remainder(weights, group_receivable)

    lines = []
    sum_book_delta = 0
    sum_cash = 0
    sum_hold = 0
    sum_paid = 0
    for m, new_recv in zip(members, allocated):
        old_billed = int(m["old_billed_cents"])
        paid = int(m["paid_cents"])
        is_trigger = m["member_id"] == combo["trigger_id"]

        # 账面应收差额：新档应收 - 原账单应收。负=应退，正=应补。
        book_delta = new_recv - old_billed

        # 账单是否重开：仅触发更正的变动成员重开；其余成员账单保持有效。
        reopen = is_trigger
        if reopen:
            cash_delta = book_delta     # 变动成员：应收差额即现金补退
            hold_delta = 0
            if cash_delta < 0:
                disposition = DISP_REFUND
            elif cash_delta > 0:
                disposition = DISP_SUPPLEMENT
            else:
                disposition = DISP_EVEN
        else:
            cash_delta = 0              # 不动现金、不重开账单
            hold_delta = book_delta    # 差额全部折扣挂账（非现金）
            disposition = DISP_HOLD if hold_delta != 0 else DISP_HOLD_EVEN

        # 实收口径：变动成员按新应收重开账单，实收结转原实收；
        # 非变动成员账单有效，实收维持原值。
        effective_paid = paid
        # 重开账单后的账户余额：正=欠缴，负=溢缴（待退）
        balance_after = new_recv - effective_paid if reopen else old_billed - effective_paid

        sum_book_delta += book_delta
        sum_cash += cash_delta
        sum_hold += hold_delta
        sum_paid += effective_paid

        lines.append({
            "member_id": m["member_id"],
            "name": m["name"],
            "trigger": is_trigger,
            "bill_mailed": bool(m["bill_mailed"]),
            "reopen_bill": reopen,
            "standard_cents": int(m["standard_cents"]),
            "old_standard_cents": int(m["old_standard_cents"]),
            "old_tier": int(combo["old_tier"]),
            "new_tier": new_tier["tier"],
            "discount_rate": str(new_rate),
            "old_billed_cents": old_billed,
            "new_receivable_cents": new_recv,
            "paid_cents": effective_paid,
            "book_delta_cents": book_delta,
            "cash_delta_cents": cash_delta,
            "hold_delta_cents": hold_delta,
            "balance_after_cents": balance_after,
            "disposition": disposition,
            "disposition_text": DISPOSITION_TEXT[disposition],
            # 元口径，便于界面与结案文件直接展示
            "amounts": {
                "standard": yuan(int(m["standard_cents"])),
                "old_billed": yuan(old_billed),
                "new_receivable": yuan(new_recv),
                "paid": yuan(effective_paid),
                "book_delta": yuan(abs(book_delta)),
                "cash_delta": yuan(abs(cash_delta)),
                "hold_delta": yuan(abs(hold_delta)),
                "balance_after": yuan(abs(balance_after)),
                "book_delta_sign": "退" if book_delta < 0 else ("补" if book_delta > 0 else "平"),
                "cash_delta_sign": "退" if cash_delta < 0 else ("补" if cash_delta > 0 else "平"),
            },
        })

    old_group_billed = sum(int(m["old_billed_cents"]) for m in members)
    conservation = {
        # 应收守恒：各人新应收之和 == 组应收
        "receivable_conserved": sum(l["new_receivable_cents"] for l in lines) == group_receivable,
        # 差额守恒：账面应收差额之和 == 现金补退之和 + 折扣挂账之和
        "delta_conserved": sum_book_delta == (sum_cash + sum_hold),
        # 组口径：新组应收 - 原组应收 == 组账面差额
        "group_conserved": (group_receivable - old_group_billed) == sum_book_delta,
        "sum_book_delta_cents": sum_book_delta,
        "sum_cash_delta_cents": sum_cash,
        "sum_hold_delta_cents": sum_hold,
    }

    return {
        "combo_id": combo["combo_id"],
        "period": combo["period"],
        "trigger_id": combo["trigger_id"],
        "rule_version": combo.get("rule_version", ""),
        "tier_change": {
            "old_tier": int(combo["old_tier"]),
            "new_tier": new_tier["tier"],
            "old_tier_name": next((t["name"] for t in TIERS if t["tier"] == int(combo["old_tier"])), str(combo["old_tier"])),
            "new_tier_name": new_tier["name"],
            "rate": str(new_rate),
            "dropped": new_tier["tier"] < int(combo["old_tier"]),
        },
        "group": {
            "standard_cents": group_standard,
            "standard_yuan": yuan(group_standard),
            "receivable_cents": group_receivable,
            "receivable_yuan": yuan(group_receivable),
            "old_billed_cents": old_group_billed,
            "old_billed_yuan": yuan(old_group_billed),
            "book_delta_cents": group_receivable - old_group_billed,
            "book_delta_yuan": yuan(abs(group_receivable - old_group_billed)),
            "cash_delta_cents": sum_cash,
            "cash_delta_yuan": yuan(abs(sum_cash)),
            "hold_delta_cents": sum_hold,
            "hold_delta_yuan": yuan(abs(sum_hold)),
            "paid_cents": sum_paid,
            "paid_yuan": yuan(sum_paid),
        },
        "members": lines,
        "conservation": conservation,
        "basis": {
            "tier_table": [
                {"tier": t["tier"], "name": t["name"], "threshold_cents": t["threshold"],
                 "threshold_yuan": yuan(t["threshold"]), "rate": str(t["rate"])}
                for t in TIERS
            ],
            "allocation": "largest_remainder",
            "currency_unit": "cent",
        },
    }
