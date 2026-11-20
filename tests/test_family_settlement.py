"""m8 家庭组合结算：版本化、幂等、中断回滚与 HTTP 契约测试。"""
import json

import pytest

from app.errors import DomainError
from app.modules.m8_family import service
from app.modules.m8_family import store as fstore
from app.modules.m8_family.evidence import canonical, closing_csv


@pytest.fixture
def combo():
    return service.sample_combo()


# --------------------------------------------------------------------------
# 版本状态机与幂等
# --------------------------------------------------------------------------
def test_build_creates_staged_version(combo, tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "fam.db"))
    v = service.build(combo)
    assert v["version"] == 1 and v["status"] == fstore.STAGED
    assert v["idempotent_hit"] is False


def test_rerun_same_correction_is_idempotent_no_new_adjustment(combo, tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "fam.db"))
    first = service.build(combo)
    again = service.build(json.loads(canonical(combo)))
    assert again["version"] == first["version"] == 1
    assert again["idempotent_hit"] is True
    assert again["group"]["cash_delta_cents"] == first["group"]["cash_delta_cents"]
    assert len(service.versions(combo["combo_id"])) == 1


def test_activate_switches_current_and_supersedes_old(combo, tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "fam.db"))
    service.build(combo)
    active = service.review_activate(combo["combo_id"], 1)
    assert active["status"] == fstore.ACTIVE
    assert service.current(combo["combo_id"])["version"] == 1

    changed = json.loads(canonical(combo))
    changed["members"][1]["standard_cents"] = 930_000
    v2 = service.build(changed)
    assert v2["version"] == 2
    # 激活前当前仍指旧版
    assert service.current(combo["combo_id"])["version"] == 1
    service.review_activate(combo["combo_id"], 2)
    assert service.current(combo["combo_id"])["version"] == 2
    statuses = {v["version"]: v["status"] for v in service.versions(combo["combo_id"])}
    assert statuses[1] == fstore.SUPERSEDED and statuses[2] == fstore.ACTIVE


def test_cannot_activate_non_staged_version(combo, tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "fam.db"))
    service.build(combo)
    service.review_activate(combo["combo_id"], 1)
    with pytest.raises(DomainError) as e:
        service.review_activate(combo["combo_id"], 1)
    assert e.value.status == 409 and e.value.code == "STATE"


def test_interrupted_activation_rolls_back_to_old_version(combo, tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "fam.db"))
    service.build(combo)
    service.review_activate(combo["combo_id"], 1)
    changed = json.loads(canonical(combo))
    changed["members"][0]["standard_cents"] = 1_400_000
    v2 = service.build(changed)
    assert v2["version"] == 2

    real_connection = fstore.connection

    class GuardConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=()):
            if sql.lstrip().upper().startswith(("UPDATE", "INSERT")) and "family" in sql:
                raise RuntimeError("interrupted replacement")
            return self._inner.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *a):
            return self._inner.__exit__(*a)

    fstore.connection = lambda: GuardConn(real_connection())
    try:
        with pytest.raises(RuntimeError):
            service.review_activate(combo["combo_id"], 2)
    finally:
        fstore.connection = real_connection

    # 中断后当前指针仍指旧版，v2 仍停留在 staged
    assert service.current(combo["combo_id"])["version"] == 1
    v2row = next(v for v in service.versions(combo["combo_id"]) if v["version"] == 2)
    assert v2row["status"] == fstore.STAGED


def test_close_freezes_and_blocks_new_version(combo, tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "fam.db"))
    service.build(combo)
    service.review_activate(combo["combo_id"], 1)
    closed = service.close_settlement(combo["combo_id"])
    assert closed["status"] == fstore.CLOSED
    with pytest.raises(DomainError) as e:
        service.build(combo)
    assert e.value.code == "CLOSED" and e.value.status == 409


def test_download_requires_active_and_reads_same_result(combo, tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "fam.db"))
    service.build(combo)
    # staged 阶段禁止下载
    with pytest.raises(DomainError) as e:
        service.download(combo["combo_id"])
    assert e.value.status == 409
    service.review_activate(combo["combo_id"], 1)
    text, result = service.download(combo["combo_id"])
    assert result["version"] == 1
    assert "运行清单" in text and "成员明细" in text and "守恒校验" in text and "档位依据" in text
    # 结案文件与结果同源：对同一 result 重新生成内容一致
    assert text == closing_csv(result)


def test_snapshot_retains_membership_and_bill_basis(combo, tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "fam.db"))
    service.build(combo)
    snap = fstore.get_version(combo["combo_id"], 1)
    assert len(snap["members"]) == 3
    assert all("discount_rate" in m and "old_billed_cents" in m for m in snap["members"])
    # 流水表逐人留痕
    ledger = fstore.ledger(combo["combo_id"], 1)
    assert {row["member_id"] for row in ledger} == {m["member_id"] for m in combo["members"]}


def test_missing_combo_raises_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "fam.db"))
    with pytest.raises(DomainError) as e:
        service.current("NOPE")
    assert e.value.status == 404


# --------------------------------------------------------------------------
# HTTP 契约
# --------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "web.db"))
    from app.main import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_m8_page_and_preview(client):
    assert client.get("/m8_family").status_code == 200
    r = client.get("/api/m8_family/preview")
    assert r.status_code == 200
    body = r.get_json()
    assert body["tier_change"]["new_tier"] == 2
    assert len(body["members"]) == 3


def test_m8_build_activate_download_flow(client):
    combo = service.sample_combo()
    r = client.post("/api/m8_family/settlements", json={"combo": combo})
    assert r.status_code == 201 and r.get_json()["version"] == 1
    # 幂等：第二次同输入返回 200 且仍是 v1
    r2 = client.post("/api/m8_family/settlements", json={"combo": combo})
    assert r2.status_code == 200 and r2.get_json()["version"] == 1
    act = client.post(f"/api/m8_family/settlements/{combo['combo_id']}/versions/1/activate")
    assert act.status_code == 200 and act.get_json()["status"] == "active"
    dl = client.get(f"/api/m8_family/settlements/{combo['combo_id']}/closing.csv")
    assert dl.status_code == 200 and "成员明细" in dl.data.decode("utf-8-sig")


def test_m8_download_blocked_before_activation(client):
    combo = service.sample_combo()
    client.post("/api/m8_family/settlements", json={"combo": combo})
    dl = client.get(f"/api/m8_family/settlements/{combo['combo_id']}/closing.csv")
    assert dl.status_code == 409 and dl.get_json()["code"] == "STATE"


def test_m8_version_history_endpoint(client):
    combo = service.sample_combo()
    client.post("/api/m8_family/settlements", json={"combo": combo})
    r = client.get(f"/api/m8_family/settlements/{combo['combo_id']}/versions")
    assert r.status_code == 200 and len(r.get_json()) == 1
