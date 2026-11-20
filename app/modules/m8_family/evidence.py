"""组合结算的确定性标识、快照冻结与结案文件。

* canonical：把输入组合按键名排序做规范化 JSON，确保与字典顺序无关；
* fingerprint：对“规范化输入 + 规则版本”取 SHA256，作为组合结果的唯一指纹。
  同一更正（相同成员关系、原账单、追溯期、规则版本）重跑得到同一指纹与同一
  组合结果，因此不会新增任何补退——幂等由指纹而非运行次数保证。
* 成员关系与原账单在结算建立时即冻结进快照；一旦源记录丢失，仍可凭快照完整
  解释整组结算（每条成员结论都带档位、费率与新旧账单依据）。
"""

import copy
import hashlib
import io
import csv
import json


def canonical(obj):
    """规范化 JSON 字符串（排序键、无空白、非 ASCII 保留），用于确定性哈希。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(combo, rule_version):
    payload = canonical({"combo": combo, "rule_version": rule_version})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def freeze_input(combo):
    """深拷贝输入，冻结为不可再被外部改动的快照依据。"""
    return copy.deepcopy(combo)


def result_hash(result):
    """对裁决结果本身取指纹，用于版本替换时的完整性比对。"""
    return hashlib.sha256(canonical(result).encode("utf-8")).hexdigest()


def closing_csv(result):
    """生成结案 CSV（单份文件，读取同一组合结果）。

    记录类型：运行清单 / 组合汇总 / 成员明细 / 守恒校验 / 档位依据。
    复核界面下载与接口下载走同一函数、同一 result，保证“下载的结案文件读取
    同一组合结果”。带 UTF-8 BOM，便于 Excel 直接打开。
    """
    out = io.StringIO(newline="")
    w = csv.writer(out)
    rid = result.get("combo_id", "preview")
    w.writerow(["记录类型", "组合", "成员ID", "成员", "项目", "方向/状态", "金额(元)", "依据"])

    w.writerow(["运行清单", rid, "", "", "rule_version", result.get("rule_version", ""), "",
                json.dumps({"period": result["period"], "tier_change": result["tier_change"],
                            "result_hash": result.get("result_hash", "")},
                           ensure_ascii=False, sort_keys=True)])

    g = result["group"]
    w.writerow(["组合汇总", rid, "", "全组", "标准保费", "", g["standard_yuan"], "减保后现值"])
    w.writerow(["组合汇总", rid, "", "全组", "新组应收", f"{result['tier_change']['new_tier_name']} {result['tier_change']['rate']}",
                g["receivable_yuan"], "组规模统一折算"])
    w.writerow(["组合汇总", rid, "", "全组", "原组应收", "", g["old_billed_yuan"], "原账单合计"])
    w.writerow(["组合汇总", rid, "", "全组", "账面差额",
                "退" if g["book_delta_cents"] < 0 else ("补" if g["book_delta_cents"] > 0 else "平"),
                g["book_delta_yuan"], "新应收-原应收"])
    w.writerow(["组合汇总", rid, "", "全组", "现金补退",
                "退" if g["cash_delta_cents"] < 0 else ("补" if g["cash_delta_cents"] > 0 else "平"),
                g["cash_delta_yuan"], "仅变动成员"])
    w.writerow(["组合汇总", rid, "", "全组", "折扣挂账", "非现金", g["hold_delta_yuan"], "账单有效成员差额"])
    w.writerow(["组合汇总", rid, "", "全组", "实收结转", "", g["paid_yuan"], "原实收"])

    for m in result["members"]:
        a = m["amounts"]
        basis = (f"原{m['old_tier']}档→新{m['new_tier']}档，费率{m['discount_rate']}；"
                 f"原账单{a['old_billed']}，新应收{a['new_receivable']}")
        w.writerow(["成员明细", rid, m["member_id"], m["name"], "账面补退",
                    a["book_delta_sign"], a["book_delta"], basis])
        w.writerow(["成员明细", rid, m["member_id"], m["name"], "现金补退",
                    a["cash_delta_sign"], a["cash_delta"],
                    "账单重开" if m["reopen_bill"] else "账单保持有效"])
        w.writerow(["成员明细", rid, m["member_id"], m["name"], "折扣挂账",
                    "非现金", a["hold_delta"], "不重开账单" if not m["reopen_bill"] else "—"])
        w.writerow(["成员明细", rid, m["member_id"], m["name"], "实收",
                    "", a["paid"], "原实收结转"])
        w.writerow(["成员明细", rid, m["member_id"], m["name"], "去向",
                    m["disposition"], "", m["disposition_text"]])

    c = result["conservation"]
    w.writerow(["守恒校验", rid, "", "", "应收守恒", str(c["receivable_conserved"]), "", "各人新应收之和=组应收"])
    w.writerow(["守恒校验", rid, "", "", "差额守恒", str(c["delta_conserved"]), "", "账面差额=现金+挂账"])
    w.writerow(["守恒校验", rid, "", "", "组口径守恒", str(c["group_conserved"]), "", "新组应收-原组应收=组账面差额"])

    for t in result["basis"]["tier_table"]:
        w.writerow(["档位依据", rid, "", "", f"{t['tier']}档/{t['name']}",
                    f"费率{t['rate']}", t["threshold_yuan"], f"组标准保费≥{t['threshold_yuan']}入档"])

    return "﻿" + out.getvalue()
