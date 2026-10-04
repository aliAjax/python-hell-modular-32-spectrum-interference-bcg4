import math
from datetime import datetime, timezone

from .domain import DomainError

ENTITY_TYPE = "spectrum_interference"
INITIAL_STATUS = "pending"
CREATE_ROLES = {"analyst", "monitor"}
SOURCE_ROLES = {"analyst", "monitor", "field_operator"}
ACTION_ROLES = {
    "assess": {"analyst", "monitor"},
    "locate": {"field_operator", "analyst"},
    "authorize_suspend": {"coordinator", "regulator"},
    "suspend": {"coordinator", "regulator"},
    "coordinate": {"coordinator"},
    "resolve": {"coordinator", "regulator"},
    "correct_measurement": {"analyst", "monitor"},
    "cancel": {"coordinator"},
}
ENFORCE_REGION = True
REGION_SENSITIVE_ACTIONS = {"authorize_suspend", "suspend", "coordinate", "resolve", "cancel"}
ACTION_REQUIRES_VERSION = {"authorize_suspend", "suspend", "coordinate", "resolve", "cancel"}
# 推进处置链路的动作必须锚定已确认的观测来源
BASIS_REQUIRED_ACTIONS = {"locate", "authorize_suspend", "suspend", "coordinate", "resolve"}
TERMINAL_STATUS = {"resolved", "cancelled"}


def assess(payload):
    strength = float(payload.get("strength_dbm", -120))
    bandwidth = max(float(payload.get("bandwidth_mhz", 0.1)), 0.001)
    impact = strength + 10.0 * math.log10(bandwidth * 1000.0)
    if impact >= -37:
        level = "critical"
    elif impact >= -50:
        level = "high"
    elif impact >= -65:
        level = "medium"
    else:
        level = "low"
    score = round(max(0.0, min(100.0, 100.0 + impact)), 2)
    return {"score": score, "level": level, "impact_value": round(impact, 2)}


def _need_status(item, allowed):
    if item["status"] not in allowed:
        raise DomainError("invalid_state", "当前状态 %s 不允许执行该操作" % item["status"])


def _text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def _parse_ts(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _need_basis(current):
    basis = current.get("basis")
    if not basis:
        raise DomainError(
            "basis_unconfirmed",
            "当前依据未确认：旧事件没有来源记录，不能提交该动作；请先补录观测来源",
            409,
            {"reason": "no_confirmed_source", "hint": "先 POST /api/items/<id>/sources 补录观测来源"},
        )
    return basis


def _basis_snapshot(source, strength, bandwidth, frequency):
    """来源被提升为当前依据时的固化快照，source_id 由仓储层在入库后回填。"""
    return {
        "source_id": None,
        "source_type": source["source_type"],
        "external_id": source["external_id"],
        "observed_at": source["observed_at"],
        "strength_dbm": strength,
        "bandwidth_mhz": bandwidth,
        "frequency_mhz": frequency,
    }


def consider_source(item, source, now):
    """判定一条观测来源是成为当前依据，还是仅进入历史。

    返回 {promoted, reason, changed_fields, new_status, new_payload, basis}
    """
    current = dict(item["payload"])
    basis = current.get("basis")
    # 已结案/已取消的事件不再让更晚来源改写依据，新观测只进历史
    later_than_basis = basis is None or _parse_ts(source["observed_at"]) > _parse_ts(basis["observed_at"])
    promoted = later_than_basis and item["status"] not in TERMINAL_STATUS

    if not promoted:
        # 更晚的当前依据已存在（或事件已终态）：晚到来源只进历史，不影响事件参数与状态
        return {
            "promoted": False,
            "reason": "terminal_history_only" if basis and item["status"] in TERMINAL_STATUS
                      else "late_source_history_only",
            "changed_fields": [],
            "new_status": item["status"],
            "new_payload": current,
            "basis": None,
            "current_basis_source_id": basis["source_id"] if basis else None,
        }

    reason = "first_source_confirmed" if basis is None else "later_observation"
    new_strength = source["strength_dbm"]
    new_bandwidth = source.get("bandwidth_mhz")
    if new_bandwidth is None:
        new_bandwidth = current.get("bandwidth_mhz")
    new_frequency = source.get("frequency_mhz")
    if new_frequency is None:
        new_frequency = current.get("frequency_mhz")

    new_basis = _basis_snapshot(source, new_strength, new_bandwidth, new_frequency)
    history = list(current.get("basis_history", []))
    if basis is not None:
        history.append(basis)

    current["basis_history"] = history
    current["basis"] = new_basis
    current["strength_dbm"] = new_strength
    if new_bandwidth is not None:
        current["bandwidth_mhz"] = new_bandwidth
    if new_frequency is not None:
        current["frequency_mhz"] = new_frequency
    if "assessment" in current:
        current["assessment"] = assess(current)
    elif basis is not None or reason == "first_source_confirmed":
        # 依据一旦确认，评估始终基于当前依据参数
        current["assessment"] = assess(current)

    changed_fields = []
    if basis is not None:
        if new_strength != basis["strength_dbm"]:
            changed_fields.append("strength_dbm")
        if new_bandwidth != basis["bandwidth_mhz"]:
            changed_fields.append("bandwidth_mhz")

    new_status = item["status"]
    # 协调确认后，更晚来源改动强度或带宽：作废未执行的停用授权和待结案，
    # 已执行的停用与已发出的协调记录保留原依据。
    if item["status"] == "coordinating" and changed_fields:
        pending_auth = current.get("authorization")
        if pending_auth and pending_auth.get("status") == "issued":
            current.setdefault("voided_authorizations", []).append(
                dict(
                    pending_auth,
                    status="voided",
                    void_reason="basis_changed_after_coordination",
                    voided_at=now,
                    changed_fields=list(changed_fields),
                )
            )
            current["authorization"] = None
        coordination = current.get("coordination")
        if coordination:
            current.setdefault("coordination_history", []).append(
                dict(
                    coordination,
                    status="superseded",
                    superseded_at=now,
                    changed_fields=list(changed_fields),
                )
            )
            current["coordination"] = None
            current.pop("coordination_agreement", None)
            current.pop("coordination_note", None)
        current["reopening"] = {
            "reason": "basis_changed_after_coordination",
            "changed_fields": list(changed_fields),
            "at": now,
            "previous_basis": basis,
        }
        new_status = "reopened"

    return {
        "promoted": True,
        "reason": reason,
        "changed_fields": changed_fields,
        "new_status": new_status,
        "new_payload": current,
        "basis": new_basis,
    }


def apply_action(item, action, payload, actor, role, now=""):
    status = item["status"]
    current = dict(item["payload"])

    if action == "assess":
        _need_status(item, {"pending", "assessed"})
        current["assessment"] = assess(current)
        return "assessed", current, {"assessment": current["assessment"]}

    if action == "correct_measurement":
        _need_status(item, {"pending", "assessed", "located"})
        try:
            strength = float(payload["strength_dbm"])
        except (KeyError, TypeError, ValueError):
            raise DomainError("field_required", "strength_dbm 不能为空")
        revision = {
            "old_strength_dbm": current.get("strength_dbm"),
            "new_strength_dbm": strength,
            "reason": _text(payload, "reason"),
            "actor": actor,
        }
        current.setdefault("measurement_revisions", []).append(revision)
        current["strength_dbm"] = strength
        current["assessment"] = assess(current)
        return status, current, {"revision": revision}

    if action == "locate":
        _need_status(item, {"assessed", "located", "reopened"})
        location = _text(payload, "location")
        confidence = float(payload.get("confidence", 0))
        if confidence < 0.6:
            raise DomainError("low_location_confidence", "定位置信度低于0.6，不能进入处置", 409)
        basis = _need_basis(current)
        current["location"] = {"label": location, "confidence": confidence, "basis_source_id": basis["source_id"]}
        return "located", current, {"location": current["location"], "basis": basis}

    if action == "authorize_suspend":
        _need_status(item, {"located", "reopened", "coordinating"})
        basis = _need_basis(current)
        code = _text(payload, "authorization_code")
        if not code.startswith("REG-"):
            raise DomainError("invalid_authorization", "停用授权编号无效", 403)
        pending = current.get("authorization")
        if pending and pending.get("status") == "issued":
            raise DomainError("authorization_pending", "已有未执行的停用授权，不能重复签发", 409)
        authorization = {
            "authorization_code": code,
            "status": "issued",
            "basis": dict(basis),
            "issued_at": now,
            "issued_by": actor,
        }
        current["authorization"] = authorization
        return status, current, {"authorization": authorization, "basis": basis}

    if action == "suspend":
        _need_status(item, {"located", "reopened"})
        basis = _need_basis(current)
        code = _text(payload, "authorization_code")
        if not code.startswith("REG-"):
            raise DomainError("invalid_authorization", "停用授权编号无效", 403)
        pending = current.get("authorization")
        voided_codes = {a["authorization_code"] for a in current.get("voided_authorizations", [])}
        if pending is not None:
            if pending.get("status") != "issued":
                raise DomainError("invalid_authorization", "停用授权状态异常，不能执行", 403)
            if pending["authorization_code"] != code:
                raise DomainError("invalid_authorization", "授权编号与已签发的停用授权不符", 403)
            executed = dict(
                pending,
                status="executed",
                executed_at=now,
                executed_by=actor,
                basis=dict(basis),
            )
        elif code in voided_codes:
            raise DomainError("invalid_authorization", "停用授权已被作废，不能执行，请重新签发", 403)
        else:
            # 未预先签发时，持合规编号直接执行，执行依据锚定当前来源
            executed = {
                "authorization_code": code,
                "status": "executed",
                "issued_inline": True,
                "basis": dict(basis),
                "executed_at": now,
                "executed_by": actor,
            }
        current["authorization"] = None
        current.setdefault("suspensions", []).append(executed)
        current["suspend_authorization"] = code
        current.pop("reopening", None)
        return "suspended", current, {"authorization": executed, "basis": basis}

    if action == "coordinate":
        _need_status(item, {"suspended"})
        basis = _need_basis(current)
        agreement = _text(payload, "coordination_agreement")
        coordination = {
            "agreement": agreement,
            "note": payload.get("note", ""),
            "basis": dict(basis),
            "coordinated_at": now,
            "coordinated_by": actor,
        }
        current["coordination"] = coordination
        current["coordination_agreement"] = agreement
        current["coordination_note"] = payload.get("note", "")
        return "coordinating", current, {"coordination": coordination, "basis": basis}

    if action == "resolve":
        _need_status(item, {"coordinating"})
        basis = _need_basis(current)
        if not payload.get("measurement_cleared"):
            raise DomainError("interference_present", "干扰尚未消除，不能结案", 409)
        resolution = {
            "evidence": _text(payload, "evidence"),
            "cleared": True,
            "basis": dict(basis),
            "resolved_at": now,
            "resolved_by": actor,
        }
        current["resolution"] = resolution
        return "resolved", current, {"resolution": resolution, "basis": basis}

    if action == "cancel":
        _need_status(item, {"pending", "assessed"})
        reason = _text(payload, "reason")
        current["cancellation"] = {"reason": reason, "actor": actor, "at": now}
        return "cancelled", current, {"reason": reason}

    raise DomainError("unknown_action", "不支持的操作")
