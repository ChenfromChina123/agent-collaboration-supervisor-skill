"""Read-only workflow decisions. Evidence assertions remain the owning Agent's duty.

No chat, timers, task-ledger writes, credentials or production operations occur here.
"""
from datetime import datetime, timezone


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timezone-aware timestamp required")
    return parsed.astimezone(timezone.utc)


def nonempty(record, keys):
    return [key for key in keys if not isinstance(record.get(key), str) or not record[key].strip()]


def finding_route(finding, existing):
    """Link an existing finding first; otherwise identify the next decision, not permission."""
    missing = nonempty(finding, ("finding_key", "reporter", "evidence_ref", "environment", "expected_baseline_ref"))
    if missing:
        return {"action": "verify", "missing": missing}
    matches = [t for t in existing if t.get("finding_key") == finding["finding_key"]]
    if len(matches) > 1:
        return {"action": "reconcile_duplicate_tickets"}
    if matches:
        if nonempty(matches[0], ("task_id", "authority_pointer", "ledger_owner")):
            return {"action": "verify_existing_ticket"}
        return {"action": "link_existing", "task_id": matches[0]["task_id"],
                "authority_pointer": matches[0]["authority_pointer"]}
    if finding.get("confirmed") is not True:
        return {"action": "verify"}
    gates = finding.get("decision_gates")
    gate_names = {"new_product_scope", "public_contract", "data_model", "permission_policy", "irreversible_action"}
    if not isinstance(gates, dict) or set(gates) != gate_names or any(type(v) is not bool for v in gates.values()):
        return {"action": "verify_decision_boundary"}
    required = [name for name in sorted(gate_names) if gates[name]]
    if required:
        return {"action": "human_decision", "gates": required}
    if finding.get("within_approved_baseline") is not True:
        return {"action": "verify_decision_boundary"}
    missing = nonempty(finding, ("authorization_ref", "allowed_scope", "acceptance_ref", "next_owner"))
    if missing:
        return {"action": "prepare_work_package", "missing": missing}
    # Production approval can block implementation there, never independent local engineering.
    return {"action": "ready_for_local_engineering", "production_authorized": False}


def intake_action(record, now):
    """5/10-minute operational targets; unknown clocks are never invented."""
    current = timestamp(now)
    if not record.get("ready_at"):
        return {"action": "verify_readiness_time"}
    ready = timestamp(record["ready_at"])
    times = [record.get(k) for k in ("claimed_at", "assigned_at", "developer_queued_at",
                                    "developer_claimed_at", "developer_action_at") if record.get(k)]
    parsed = [timestamp(v) for v in times]
    if ready > current or any(t < ready or t > current for t in parsed) or parsed != sorted(parsed):
        raise ValueError("Invalid workflow timestamp order")
    age = (current - ready).total_seconds()
    wait = record.get("wait")
    if wait:
        missing = nonempty(wait, ("owner", "condition", "evidence_ref", "recheck_at"))
        if missing:
            return {"action": "repair_wait_record", "missing": missing}
        recheck = timestamp(wait["recheck_at"])
        if recheck <= current:
            return {"action": "recheck_wait", "owner": wait["owner"]}
        return {"action": "known_wait", "owner": wait["owner"], "recheck_at": wait["recheck_at"]}
    if not record.get("claimed_at"):
        return {"action": "claim_intake", "overdue": age >= 300}
    if not record.get("assigned_at"):
        return {"action": "dispatch_or_record_specific_blocker", "overdue": age >= 600}
    if not record.get("developer_claimed_at"):
        return {"action": "ensure_recipient_scan_and_claim", "queued": bool(record.get("developer_queued_at")),
                "overdue": (current - timestamp(record["assigned_at"])).total_seconds() >= 300}
    if not record.get("developer_action_at"):
        return {"action": "verify_actual_developer_action"}
    return {"action": "track_execution"}


def closure(record):
    """Explicit delivery objectives avoid calling main a production repair."""
    scope = record.get("closure_scope")
    if scope not in {"engineering", "production"}:
        return {"closed": False, "missing": ["closure_scope"]}
    required = ["task_id", "authority_pointer", "ledger_owner", "verification_ref", "regression_ref"]
    if record.get("resolution") == "change":
        required += ["source_commit", "pre_main_commit", "review_ref", "main_commit"]
    elif record.get("resolution") in {"not_reproduced", "configuration_only"}:
        required += ["resolution_ref", "acceptance_ref"]
    else:
        required += ["resolution"]
    if scope == "production":
        required += ["user_scope_ref", "management_approval_ref", "release_execution_ref", "runtime_identity_ref",
                     "independent_reverification_ref"]
    missing = nonempty(record, required)
    return {"closed": not missing, "missing": missing, "scope": scope}
