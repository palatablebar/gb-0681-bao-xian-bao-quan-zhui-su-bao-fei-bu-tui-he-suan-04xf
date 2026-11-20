"""家庭组合结算 m8 的 HTTP 入口。

与 m1–m7 的单保单流水线相互独立：本模块围绕“一个家庭组合”的版本化结算，
提供试算、建版（幂等）、复核激活、结案冻结、版本史与结案文件下载。
"""

from flask import Blueprint, Response, jsonify, render_template, request

from app.config import MODULES, SHORT, TITLE
from app.errors import DomainError
from app.modules.m8_family import service
from app.modules.m8_family.evidence import closing_csv

bp = Blueprint("m8_family", __name__)

STATUS_TEXT = {"staged": "待复核", "active": "生效中", "superseded": "已替换", "closed": "已结案"}


def _combo_from_request():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        raise DomainError("PAYLOAD", "请求必须是对象")
    combo = data.get("combo")
    if combo is None:
        combo = service.sample_combo()
    if not isinstance(combo, dict) or "members" not in combo:
        raise DomainError("PAYLOAD", "组合输入必须是含 members 的对象")
    return combo


@bp.route("/api/m8_family/preview", methods=["GET", "POST"])
def preview():
    combo = _combo_from_request() if request.method == "POST" else service.sample_combo()
    return jsonify(service.preview(combo))


@bp.post("/api/m8_family/settlements")
def build():
    """建版（变更已采信 → 待复核 staged）。同指纹重跑幂等，不新增补退。"""
    combo = _combo_from_request()
    version = service.build(combo)
    return jsonify(version), 200 if version.get("idempotent_hit") else 201


@bp.post("/api/m8_family/settlements/<combo_id>/versions/<int:version>/activate")
def activate(combo_id, version):
    return jsonify(service.review_activate(combo_id, version))


@bp.post("/api/m8_family/settlements/<combo_id>/close")
def close(combo_id):
    return jsonify(service.close_settlement(combo_id))


@bp.get("/api/m8_family/settlements/<combo_id>/versions")
def versions(combo_id):
    return jsonify(service.versions(combo_id))


@bp.get("/api/m8_family/settlements/<combo_id>")
def current(combo_id):
    return jsonify(service.current(combo_id))


@bp.get("/api/m8_family/settlements/<combo_id>/closing.csv")
def download(combo_id):
    text, result = service.download(combo_id)
    return Response(
        text,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=family_{combo_id}_v{result['version']}.csv"},
    )


@bp.get("/m8_family")
def screen():
    """复核界面：核对每位成员的补退、实收与折扣依据。"""
    preview_data = service.preview(service.sample_combo())
    current_data = None
    version_rows = []
    try:
        current_data = service.current(preview_data["combo_id"])
        version_rows = service.versions(preview_data["combo_id"])
    except DomainError as error:
        if error.code != "NOT_FOUND":
            raise
    # 与 base.html 模板变量对齐
    run = {
        "decision": STATUS_TEXT.get(current_data["status"], "未建版") if current_data else "未建版",
        "review_version": current_data["version"] if current_data else 0,
        "run_id": preview_data["combo_id"],
    }
    return render_template(
        "m8_family.html",
        title=TITLE,
        short=SHORT,
        modules=MODULES,
        current=8,
        data=preview_data,
        current_data=current_data,
        version_rows=version_rows,
        status_text=STATUS_TEXT,
        run=run,
        history=[],
    )
