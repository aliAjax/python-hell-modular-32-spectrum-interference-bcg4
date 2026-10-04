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
    "suspend": {"coordinator", "regulator"},
    "coordinate": {"coordinator"},
    "resolve": {"coordinator", "regulator"},
    "correct_measurement": {"analyst", "monitor"},
    "cancel": {"coordinator"},
}
ENFORCE_REGION = True
REGION_SENSITIVE_ACTIONS = {"suspend", "coordinate", "resolve", "cancel"}
ACTION_REQUIRES_VERSION = {"suspend", "coordinate", "resolve", "cancel"}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def make_basis(source_id, observed_at, strength_dbm, bandwidth_mhz):
    return {
        "source_id": source_id,
        "observed_at": observed_at,
        "strength_dbm": strength_dbm,
        "bandwidth_mhz": bandwidth_mhz,
        "confirmed": True,
    }


def basis_confirmed(payload):
    return payload.get("current_basis") is not None


def basis_snapshot(payload):
    return payload.get("current_basis")


def _basis_params_changed(new_basis, old_basis):
    if old_basis is None:
        return True
    if float(new_basis["strength_dbm"]) != float(old_basis["strength_dbm"]):
        return True
    new_bw = new_basis.get("bandwidth_mhz")
    old_bw = old_basis.get("bandwidth_mhz")
    if new_bw is not None and old_bw is None:
        return True
    if new_bw is not None and old_bw is not None and float(new_bw) != float(old_bw):
        return True
    return False


def apply_basis_impact(payload, new_basis, old_basis, status):
    """协调确认后新来源改动强度/带宽时，作废未执行的停用授权和待结案。

    已发出（已结案）的授权保留原依据，不回溯。返回 (new_payload, new_status, events)。
    """
    events = []
    new_status = None
    if not _basis_params_changed(new_basis, old_basis):
        return payload, new_status, events
    if status == "coordinating":
        for auth in payload.get("suspend_authorizations", []):
            if auth.get("status") == "issued":
                auth["status"] = "voided"
                auth["voided_reason"] = "basis_changed"
                events.append({
                    "type": "authorization_voided",
                    "code": auth.get("code"),
                    "basis_source_id": auth.get("basis_source_id"),
                    "note": "未执行的停用授权作废，原依据保留",
                })
        payload["suspend_authorization"] = None
        if "coordination_agreement" in payload:
            payload["coordination_agreement_voided"] = payload.pop("coordination_agreement")
        payload.pop("coordination_note", None)
        payload.pop("coordination_basis_source_id", None)
        payload.pop("coordination_basis_snapshot", None)
        payload["coordination_voided_reason"] = "basis_changed"
        new_status = "located"
        events.append({
            "type": "coordination_voided",
            "reason": "basis_changed",
            "note": "待结案作废，退回定位后按新依据重新停用协调",
        })
    elif status == "resolved":
        events.append({
            "type": "basis_changed_after_closure",
            "note": "已结案，停用授权与结论保留原依据",
        })
    return payload, new_status, events


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


def apply_action(item, action, payload, actor, role):
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
        _need_status(item, {"assessed", "located"})
        location = _text(payload, "location")
        confidence = float(payload.get("confidence", 0))
        if confidence < 0.6:
            raise DomainError("low_location_confidence", "定位置信度低于0.6，不能进入处置", 409)
        current["location"] = {"label": location, "confidence": confidence}
        return "located", current, {"location": current["location"]}

    if action == "suspend":
        _need_status(item, {"located", "suspended"})
        if not basis_confirmed(current):
            raise DomainError("basis_unconfirmed", "该事件没有观测来源记录，依据未确认，不能停用授权", 409)
        authorization = _text(payload, "authorization_code")
        if not authorization.startswith("REG-"):
            raise DomainError("invalid_authorization", "停用授权编号无效", 403)
        basis = basis_snapshot(current)
        current["suspend_authorization"] = authorization
        current.setdefault("suspend_authorizations", []).append({
            "code": authorization,
            "basis_source_id": basis["source_id"],
            "strength_dbm": basis["strength_dbm"],
            "bandwidth_mhz": basis["bandwidth_mhz"],
            "status": "issued",
            "issued_at": _now_iso(),
            "actor": actor,
        })
        return "suspended", current, {"authorization_code": authorization, "basis_source_id": basis["source_id"]}

    if action == "coordinate":
        _need_status(item, {"suspended"})
        if not basis_confirmed(current):
            raise DomainError("basis_unconfirmed", "该事件没有观测来源记录，依据未确认，不能协调确认", 409)
        agreement = _text(payload, "coordination_agreement")
        basis = basis_snapshot(current)
        current["coordination_agreement"] = agreement
        current["coordination_note"] = payload.get("note", "")
        current["coordination_basis_source_id"] = basis["source_id"]
        current["coordination_basis_snapshot"] = {
            "strength_dbm": basis["strength_dbm"],
            "bandwidth_mhz": basis["bandwidth_mhz"],
        }
        return "coordinating", current, {"coordination_agreement": agreement, "basis_source_id": basis["source_id"]}

    if action == "resolve":
        _need_status(item, {"coordinating"})
        if not basis_confirmed(current):
            raise DomainError("basis_unconfirmed", "该事件没有观测来源记录，依据未确认，不能结案", 409)
        if not payload.get("measurement_cleared"):
            raise DomainError("interference_present", "干扰尚未消除，不能结案", 409)
        basis = basis_snapshot(current)
        current["resolution"] = {
            "evidence": _text(payload, "evidence"),
            "cleared": True,
            "basis_source_id": basis["source_id"],
        }
        return "resolved", current, {
            "evidence": current["resolution"]["evidence"],
            "basis_source_id": basis["source_id"],
        }

    if action == "cancel":
        _need_status(item, {"pending", "assessed"})
        reason = _text(payload, "reason")
        current["cancellation"] = {"reason": reason, "actor": actor}
        return "cancelled", current, {"reason": reason}

    raise DomainError("unknown_action", "不支持的操作")
