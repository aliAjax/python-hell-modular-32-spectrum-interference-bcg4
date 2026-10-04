import hashlib
import json


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def audit_hash(previous_hash, event):
    payload = canonical_json(event)
    return hashlib.sha256((previous_hash + payload).encode("utf-8")).hexdigest()
