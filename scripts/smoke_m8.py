"""用路由垫片在纯标准库下验证 create_app 的 m8 路由契约（不依赖 flask/pytest）。"""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".stub"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "smoke.db")
from app.main import create_app
from app.modules.m8_family import service
from app.config import MODULES

fails=[]
def check(n,c,extra=""):
    print(("PASS" if c else "FAIL"),n,extra);
    if not c: fails.append(n)

app=create_app(); client=app.test_client()

# m1–m8 模块均已注册蓝图
paths={r[0] for r in app.rules}
check("m8蓝图已注册", any(p.startswith("/m8_family") for p in paths))
check("m1–m8模块数=8", len(MODULES)==8 and MODULES[-1][0]=="m8_family")

# 页面（垫片返回占位 200）
check("复核页面200", client.get("/m8_family").status_code==200)
# 预览
r=client.get("/api/m8_family/preview"); check("预览200", r.status_code==200)
body=r.get_json(); check("预览落两档", body["tier_change"]["new_tier"]==2)
combo=service.sample_combo(); cid=combo["combo_id"]
# 建版（垫片不保留元组状态码，处理器首次创建确为 201，由 pytest 在真 Flask 下验证）
r=client.post("/api/m8_family/settlements", json={"combo":combo})
check("建版 v1", r.status_code in (200,201) and r.get_json()["version"]==1)
r=client.post("/api/m8_family/settlements", json={"combo":combo})
check("重跑幂等v1", r.status_code==200 and r.get_json()["version"]==1)
# 未激活禁止下载
r=client.get(f"/api/m8_family/settlements/{cid}/closing.csv")
check("未激活下载409", r.status_code==409 and r.get_json()["code"]=="STATE")
# 版本史
r=client.get(f"/api/m8_family/settlements/{cid}/versions")
check("版本史200", r.status_code==200 and len(r.get_json())==1)
# 激活
r=client.post(f"/api/m8_family/settlements/{cid}/versions/1/activate")
check("激活200 active", r.status_code==200 and r.get_json()["status"]=="active")
# 当前
r=client.get(f"/api/m8_family/settlements/{cid}")
check("当前版本200", r.status_code==200 and r.get_json()["version"]==1)
# 下载
r=client.get(f"/api/m8_family/settlements/{cid}/closing.csv")
txt=r.data.decode("utf-8-sig")
check("下载200含成员明细", r.status_code==200 and "成员明细" in txt and "档位依据" in txt)
# 结案
r=client.post(f"/api/m8_family/settlements/{cid}/close")
check("结案200 closed", r.status_code==200 and r.get_json()["status"]=="closed")
# 结案后建版被拒
r=client.post("/api/m8_family/settlements", json={"combo":combo})
check("结案后建版409", r.status_code==409 and r.get_json()["code"]=="CLOSED")
# 不存在组合
r=client.get("/api/m8_family/settlements/NOPE")
check("缺失组合404", r.status_code==404 and r.get_json()["code"]=="NOT_FOUND")

print("\n%s：%d 项失败"%("全部通过" if not fails else "存在失败",len(fails)))
sys.exit(1 if fails else 0)
