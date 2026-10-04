from . import domain, rules
from .audit import canonical_json
from .domain import ConflictError, DomainError
from .repository import REPLAY_MARKER, now_iso


class Service:
    def __init__(self, repository):
        self.repository = repository

    # ---- 幂等：同一请求编号只入账一次；写入失败按该编号重试 ----
    def _fingerprint(self, value):
        return canonical_json(value)

    def _materialize(self, outcome, loader):
        if isinstance(outcome, dict) and outcome.get(REPLAY_MARKER):
            return True, loader(outcome["result"])
        return False, outcome

    def create_item(self, payload, actor, role, region=None, request_id=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        fingerprint = self._fingerprint(normalized) if request_id else None
        outcome = self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role,
            request_id, fingerprint,
        )
        _, result = self._materialize(outcome, lambda desc: self.get_item(desc["item_id"]))
        result["idempotent_replay"] = isinstance(outcome, dict) and outcome.get(REPLAY_MARKER, False)
        return result

    def add_source(self, item_id, payload, actor, role, region=None, request_id=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)

        source_type = normalized.pop("source_type")
        external_id = normalized.pop("external_id")
        observed_at = normalized.pop("observed_at")
        fingerprint = self._fingerprint({
            "item_id": item_id, "source_type": source_type, "external_id": external_id,
            "observed_at": observed_at, "source": normalized,
        }) if request_id else None
        if request_id is not None:
            existing = self.repository.lookup_request(request_id)
            if existing is not None and existing["status"] == "completed":
                if existing["fingerprint"] != fingerprint:
                    raise ConflictError("idempotency_key_conflict",
                                        "同一请求编号提交了不同内容，请更换请求编号",
                                        {"request_id": request_id, "stored_scope": existing["scope"]})
                desc = existing["result_payload"]
                return {
                    "source_id": desc["source_id"],
                    "item_id": desc["item_id"],
                    "promoted": desc.get("promoted", False),
                    "version": desc.get("version"),
                    "idempotent_replay": True,
                }

        promotion = rules.consider_source(item, {
            "source_type": source_type,
            "external_id": external_id,
            "observed_at": observed_at,
            **normalized,
        }, now_iso())

        outcome = self.repository.add_source(
            item_id, source_type, external_id, normalized, observed_at, actor, role,
            promotion, request_id, fingerprint,
        )
        replayed, result = self._materialize(
            outcome,
            lambda desc: {
                "source_id": desc["source_id"],
                "item_id": desc["item_id"],
                "promoted": desc.get("promoted", False),
                "version": desc.get("version"),
                "source": self.repository.get_source(desc["source_id"]),
                "item": self.get_item(desc["item_id"]),
            },
        )
        if replayed:
            result["idempotent_replay"] = True
            return result
        return {
            "source_id": result["source_id"],
            "item_id": item_id,
            "promoted": result["promoted"],
            "promotion": result["promotion"],
            "version": result["version"],
            "item": self.get_item(item_id),
        }

    def _conflict_details(self, item_id, action, payload, expected_version):
        latest = self.repository.get_item(item_id)
        current = latest["payload"]
        current_basis = current.get("basis")
        conflicting = {}

        for key in ("strength_dbm", "bandwidth_mhz", "frequency_mhz"):
            if key in payload and payload.get(key) != current.get(key):
                conflicting[key] = {"submitted": payload.get(key), "current": current.get(key)}
        if "location" in payload:
            current_label = (current.get("location") or {}).get("label")
            if payload.get("location") != current_label:
                conflicting["location"] = {"submitted": payload.get("location"), "current": current_label}
        if "coordination_agreement" in payload and payload.get("coordination_agreement") != current.get("coordination_agreement"):
            conflicting["coordination_agreement"] = {
                "submitted": payload.get("coordination_agreement"),
                "current": current.get("coordination_agreement"),
            }
        if "evidence" in payload:
            current_evidence = (current.get("resolution") or {}).get("evidence")
            if payload.get("evidence") != current_evidence:
                conflicting["evidence"] = {"submitted": payload.get("evidence"), "current": current_evidence}
        if "authorization_code" in payload:
            pending = current.get("authorization") or {}
            effective_code = pending.get("authorization_code") or current.get("suspend_authorization")
            if payload.get("authorization_code") != effective_code:
                conflicting["authorization_code"] = {
                    "submitted": payload.get("authorization_code"),
                    "current": effective_code,
                }
        submitted_basis = payload.get("basis_source_id")
        if submitted_basis is not None and current_basis and submitted_basis != current_basis.get("source_id"):
            conflicting["basis_source_id"] = {"submitted": submitted_basis,
                                              "current": current_basis.get("source_id")}
        return {
            "action": action,
            "expected_version": expected_version,
            "current_version": latest["version"],
            "current_status": latest["status"],
            "current_basis": current_basis,
            "conflicting_fields": conflicting,
            "instruction": "后到请求：请基于最新版本和冲突项重做",
        }

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None, request_id=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)

        # expected_version 不进指纹：按同一请求编号带新版本重试应命中同一条台账
        fingerprint = self._fingerprint({
            "item_id": item_id, "action": action, "payload": payload,
        }) if request_id else None
        if request_id is not None:
            existing = self.repository.lookup_request(request_id)
            if existing is not None and existing["status"] == "completed":
                if existing["fingerprint"] != fingerprint:
                    raise ConflictError("idempotency_key_conflict",
                                        "同一请求编号提交了不同内容，请更换请求编号",
                                        {"request_id": request_id, "stored_scope": existing["scope"]})
                result = self.get_item(existing["item_id"])
                result["idempotent_replay"] = True
                return result

        new_status, new_payload, event_payload = rules.apply_action(
            item, action, payload, actor, role, now_iso()
        )
        try:
            outcome = self.repository.apply_action(
                item_id, action, actor, role, new_status, new_payload, event_payload,
                expected_version, request_id, fingerprint,
            )
        except ConflictError as exc:
            if exc.code == "version_conflict":
                raise ConflictError("version_conflict", str(exc),
                                    self._conflict_details(item_id, action, payload, expected_version))
            raise
        if isinstance(outcome, dict) and outcome.get(REPLAY_MARKER):
            result = self.get_item(outcome["result"]["item_id"])
            result["idempotent_replay"] = True
            return result
        result = self.get_item(item_id)
        result["idempotent_replay"] = False
        return result

    # ---- 凭据链：事件 -> 观测来源（当前依据）-> 协调协议 -> 停用授权/结案 ----
    def credential_chain(self, item_id):
        item = self.repository.get_item(item_id)
        payload = item["payload"]
        basis = payload.get("basis")
        sources = self.repository.list_sources(item_id)
        historical = [
            {
                "source_id": s["id"],
                "source_type": s["source_type"],
                "external_id": s["external_id"],
                "observed_at": s["observed_at"],
                "role": ("current_basis" if basis and s["id"] == basis.get("source_id")
                         else "history"),
            }
            for s in sources
        ]
        chain = {
            "item_id": item_id,
            "status": item["status"],
            "basis_confirmed": basis is not None,
            "current_basis": basis,
            "basis_history": payload.get("basis_history", []),
            "observations": historical,
            "authorization": payload.get("authorization"),
            "voided_authorizations": payload.get("voided_authorizations", []),
            "suspensions": payload.get("suspensions", []),
            "coordination": payload.get("coordination"),
            "coordination_history": payload.get("coordination_history", []),
            "resolution": payload.get("resolution"),
            "reopening": payload.get("reopening"),
        }
        if not basis:
            chain["invalidation"] = {
                "reason": "no_confirmed_source",
                "message": "旧事件没有来源记录，按未确认处理：先补录观测来源，处置动作才会生效",
            }
        return chain

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        item["basis_confirmed"] = item["payload"].get("basis") is not None
        item["credential_chain"] = self.credential_chain(item_id)
        return item

    def list_items(self, status=None):
        items = self.repository.list_items(status)
        for item in items:
            item["basis_confirmed"] = item["payload"].get("basis") is not None
        return items

    def state(self):
        summary = self.repository.state_summary()
        summary["items"] = self.list_items()
        return summary
