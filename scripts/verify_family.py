"""标准库自检：不依赖 flask/pytest，验证 m8 裁决、幂等、版本回滚、守恒、结案文件。"""
import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.modules.m8_family import service, rules
from app.modules.m8_family import store as fstore
from app.modules.m8_family.evidence import closing_csv, canonical
from app.errors import DomainError

failures = []
def check(name, cond, extra=""):
    print(("PASS" if cond else "FAIL"), name, extra)
    if not cond: failures.append(name)

tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(tmp, "fam.db")

combo = service.sample_combo()
preview = service.preview(combo)

# 1) 档位：减保后组标准 30000 元 -> 两档 9 折
check("档位降为两档", preview["tier_change"]["new_tier"] == 2 and preview["tier_change"]["dropped"])
check("组标准保费30000", preview["group"]["standard_cents"] == 3_000_000, preview["group"]["standard_yuan"])
check("组应收27000", preview["group"]["receivable_cents"] == 2_700_000, preview["group"]["receivable_yuan"])

by = {m["member_id"]: m for m in preview["members"]}
# 张某：新应收 13500，原 15600，现金退 2100
check("张某新应收13500", by["M-zhang"]["new_receivable_cents"] == 1_350_000)
check("张某现金退费2100", by["M-zhang"]["cash_delta_cents"] == -210_000, str(by["M-zhang"]["cash_delta_cents"]))
check("张某去向REFUND", by["M-zhang"]["disposition"] == rules.DISP_REFUND)
check("张某账单重开", by["M-zhang"]["reopen_bill"] is True)
# 李某：新应收 8100，+900 挂账，现金 0，账单有效
check("李某新应收8100", by["M-li"]["new_receivable_cents"] == 810_000)
check("李某挂账+900", by["M-li"]["hold_delta_cents"] == 90_000)
check("李某现金0", by["M-li"]["cash_delta_cents"] == 0)
check("李某去向HOLD", by["M-li"]["disposition"] == rules.DISP_HOLD)
check("李某账单保持有效", by["M-li"]["reopen_bill"] is False)
# 王某：新应收 5400，+600 挂账
check("王某新应收5400", by["M-wang"]["new_receivable_cents"] == 540_000)
check("王某挂账+600", by["M-wang"]["hold_delta_cents"] == 60_000)

# 2) 守恒（追溯期 3 个月，金额为整段合计，单位：元）
#   张某现金退 2100（每月）；李某挂账 +900、王某挂账 +600；
#   组账面 = -2100 + 900 + 600 = -600；现金 -2100 + 挂账 +1500 = -600。
c = preview["conservation"]
check("应收守恒", c["receivable_conserved"])
check("差额守恒 账面=现金+挂账", c["delta_conserved"],
      f"book={c['sum_book_delta_cents']} cash={c['sum_cash_delta_cents']} hold={c['sum_hold_delta_cents']}")
check("组口径守恒", c["group_conserved"])
check("组账面-600", c["sum_book_delta_cents"] == -60_000, str(c["sum_book_delta_cents"]))
check("组现金-2100", c["sum_cash_delta_cents"] == -210_000, str(c["sum_cash_delta_cents"]))
check("组挂账+1500", c["sum_hold_delta_cents"] == 150_000)
# 各人新应收之和 = 27000
check("成员新应收合计27000", sum(m["new_receivable_cents"] for m in preview["members"]) == 2_700_000)

# 3) 确定性：同输入两次裁决结果指纹一致
p2 = service.preview(json.loads(canonical(combo)))  # 经序列化往返，打乱无关
check("裁决确定性指纹一致", preview["result_hash"] == p2["result_hash"])

# 4) 建版幂等：同一更正重跑不新增版本/补退
v1 = service.build(combo)
v1b = service.build(json.loads(canonical(combo)))
check("首版v1", v1["version"] == 1 and v1["idempotent_hit"] is False)
check("重跑幂等命中v1", v1b["version"] == 1 and v1b["idempotent_hit"] is True)
check("重跑补退不变", v1b["group"]["cash_delta_cents"] == v1["group"]["cash_delta_cents"])
check("版本数仍为1", len(service.versions(combo["combo_id"])) == 1)

# 5) 复核激活：staged -> active，指针切换
cur = service.review_activate(combo["combo_id"], 1)
check("v1激活生效", cur["status"] == "active" and cur["version"] == 1)

# 6) 新版替换：改输入产生 v2(staged)，激活后旧版 superseded；中断回滚
combo2 = json.loads(canonical(combo))
combo2["members"][1]["standard_cents"] = 930_000  # 李某标准保费微调
v2 = service.build(combo2)
check("更正产生v2", v2["version"] == 2 and v2["idempotent_hit"] is False)
check("当前仍指v1(未激活前)", service.current(combo["combo_id"])["version"] == 1)
cur2 = service.review_activate(combo["combo_id"], 2)
check("激活后当前指v2", cur2["version"] == 2 and cur2["status"] == "active")
v1row = [v for v in service.versions(combo["combo_id"]) if v["version"] == 1][0]
check("旧版v1置superseded", v1row["status"] == "superseded")

# 7) 激活非staged版本应被拒绝（状态保护）
try:
    service.review_activate(combo["combo_id"], 1); check("重复激活拒绝", False)
except DomainError as e: check("重复激活拒绝", e.status == 409 and e.code == "STATE")

# 8) 中断回滚：让激活事务内的写入 SQL 抛错，with 上下文回滚，指针仍指旧版
combo3 = json.loads(canonical(combo)); combo3["members"][0]["standard_cents"] = 1_400_000
v3 = service.build(combo3)
check("v3已staged", v3["version"] == 3)
real_connection = fstore.connection
class Boom(Exception): pass
class GuardConn:
    """包装连接：仅写入 family 表时引爆，SELECT 与 commit/rollback 透传。"""
    def __init__(self, inner): self._inner = inner
    def execute(self, sql, params=()):
        if sql.lstrip().upper().startswith(("UPDATE", "INSERT")) and "family" in sql:
            raise Boom("interrupted")
        return self._inner.execute(sql, params)
    def __getattr__(self, name): return getattr(self._inner, name)
    def __enter__(self):
        self._inner.__enter__(); return self
    def __exit__(self, *a): return self._inner.__exit__(*a)
fstore.connection = lambda: GuardConn(real_connection())
try:
    try:
        service.review_activate(combo["combo_id"], 3); check("中断回滚", False)
    except Boom:
        pass
finally:
    fstore.connection = real_connection
check("中断后当前仍指v2", service.current(combo["combo_id"])["version"] == 2)
v3row = [v for v in service.versions(combo["combo_id"]) if v["version"] == 3][0]
check("v3仍为staged未生效", v3row["status"] == "staged")

# 9) 结案冻结与下载同一组合结果
service.review_activate(combo["combo_id"], 3)  # 正常激活 v3
closed = service.close_settlement(combo["combo_id"])
check("结案closed", closed["status"] == "closed")
text, dl = service.download(combo["combo_id"])
check("下载读取当前版本v3", dl["version"] == 3)
check("结案CSV含运行清单", "运行清单" in text and "成员明细" in text and "守恒校验" in text)
check("结案CSV含档位依据", "档位依据" in text)
# 已结案不能再开新版本
try:
    service.build(combo)
    check("结案后禁开新版", False)
except DomainError as e:
    check("结案后禁开新版", e.code == "CLOSED" and e.status == 409, f"{e.code}/{e.status}")

# 10) 快照留存：成员关系与原账单随版本保存，源丢失可解释
snap = fstore.get_version(combo["combo_id"], 1)
check("快照保留成员关系", len(json.loads(json.dumps(snap))) and "members" in snap)
check("快照含折扣依据", all("discount_rate" in m for m in snap["members"]))

# 11) 输入校验：触发成员不属于组合 / 成员重复
bad = json.loads(canonical(combo)); bad["trigger_id"] = "NOPE"
try:
    rules.settle(bad); check("触发成员归属校验", False)
except ValueError: check("触发成员归属校验", True)
bad2 = json.loads(canonical(combo)); bad2["members"][1]["member_id"] = bad2["members"][0]["member_id"]
try:
    rules.settle(bad2); check("成员重复校验", False)
except ValueError: check("成员重复校验", True)

# 12) 最大余额法守恒（含全零权重）
for total, weights in [(100,[33,33,34]),(7,[1,1,1]),(10,[0,0,0]),(0,[5,5]),(12345,[7,13,19,23])]:
    alloc = rules.largest_remainder(weights, total)
    check(f"LRM守恒 total={total}", sum(alloc) == total and len(alloc) == len(weights), str(alloc))

print("\n%s：%d 项失败" % ("全部通过" if not failures else "存在失败", len(failures)))
sys.exit(1 if failures else 0)
