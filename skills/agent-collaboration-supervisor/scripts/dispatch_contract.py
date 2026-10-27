"""Read-only execution-view resolution; no messaging, writes or identity enforcement."""
import argparse
import json
import re
from pathlib import Path


class ContractError(ValueError):
    pass


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def resolve_task_view(path, task_id, *, legacy_root=None, legacy_owner=None):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", task_id):
        raise ContractError("TASK identifier cannot select a path outside its registered task root")
    path = Path(path).resolve(strict=True)
    record = _read(path)
    if record.get("task_id") != task_id:
        raise ContractError("Task identity differs from the requested TASK")
    legacy_path = (Path(legacy_root) / "tasks" / (task_id + ".json")).resolve() if legacy_root else None
    if not record.get("ledger_owner") or not record.get("authority_pointer"):
        if path != legacy_path or not legacy_owner:
            raise ContractError("Missing canonical ledger fields outside verified legacy root")
        if record.get("ledger_owner") not in (None, legacy_owner):
            raise ContractError("Legacy writer differs from registered owner")
        record = dict(record, ledger_owner=legacy_owner, authority_pointer=str(path),
                      execution_owner=record.get("execution_owner") or record.get("owner_thread"))
    if Path(record["authority_pointer"]).resolve(strict=True) != path:
        raise ContractError("authority_pointer must locate this unique execution view")
    for key in ("ledger_owner", "execution_owner"):
        if not isinstance(record.get(key), str) or not record[key].strip():
            raise ContractError("Missing explicit " + key)
    if legacy_path and path != legacy_path and legacy_path.exists() and not record.get("handoff_ref"):
        raise ContractError("Existing legacy TASK requires explicit handoff before resolving a competing execution view")
    if path != legacy_path:
        source = Path(record.get("source_card", "")).resolve(strict=True)
        if source == path or _read(source).get("task_id") != task_id:
            raise ContractError("source_card must be distinct input for the same TASK")
    if record.get("handoff_ref"):
        handoff = _read(record["handoff_ref"])
        if not (legacy_path and legacy_path.is_file() and legacy_owner
                and _read(legacy_path).get("task_id") == task_id
                and handoff.get("task_id") == task_id
                and handoff.get("from_ledger_owner") == legacy_owner
                and handoff.get("to_ledger_owner") == record["ledger_owner"]
                and handoff.get("prior_writer_stopped") is True
                and handoff.get("accepted_by_new_owner") is True
                and Path(handoff.get("old_authority_pointer", "")).resolve() == legacy_path
                and Path(handoff.get("new_authority_pointer", "")).resolve() == path):
            raise ContractError("Explicit handoff lacks matching old/new writer and stop/accept facts")
    return record


def feedback_recipient(record, kind):
    if kind not in ("PROGRESS", "BLOCKED", "TEST_REQUEST", "TEST_RESULT"):
        raise ContractError("Product decisions and technical review use their own role routes")
    return record["ledger_owner"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-view", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--legacy-root")
    parser.add_argument("--legacy-owner")
    args = parser.parse_args()
    view = resolve_task_view(args.task_view, args.task_id, legacy_root=args.legacy_root,
                             legacy_owner=args.legacy_owner)
    print(json.dumps({"task_id": view["task_id"], "ledger_owner": view["ledger_owner"],
                      "execution_owner": view["execution_owner"],
                      "authority_pointer": view["authority_pointer"], "status": view.get("status"),
                      "execution_feedback_to": feedback_recipient(view, "PROGRESS")}, ensure_ascii=False))
