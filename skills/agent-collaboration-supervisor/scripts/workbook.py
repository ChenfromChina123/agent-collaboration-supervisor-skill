#!/usr/bin/env python3
"""Local durable delivery and self-owned Agent workbooks. Standard library only.

Identity and authorization are project policy, not account isolation. No chat,
network, worker spawning, task execution, or automatic stale-lock removal.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import errno
from functools import wraps
import json
import os
from pathlib import Path
import re
import time
import uuid


class WorkbookError(RuntimeError):
    pass


class ProjectionWriteError(WorkbookError):
    def __init__(self, path, cause):
        super().__init__("Derived view could not be refreshed: " + str(path))
        self.path = str(path)
        self.cause = cause


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", value):
        raise WorkbookError("IDs must be 1-100 ASCII letters, digits, hyphens or underscores")
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", value, re.I):
        raise WorkbookError("Windows reserved device name is not a valid ID")
    return value


def publish(path, value):
    """Publish complete bytes atomically without overwriting an existing object."""
    data = encoded(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".pending-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
            return True
        except FileExistsError:
            if path.read_bytes() != data:
                raise WorkbookError("Existing immutable object has different content: " + path.name)
            return False
    finally:
        temporary.unlink(missing_ok=True)


def replace(path, data):
    temporary = path.with_name(".snapshot-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def replace_projection(path, data):
    """Skip identical derived bytes; bounded retries never apply to journals/registry."""
    try:
        if path.read_bytes() == data:
            return
    except FileNotFoundError:
        pass
    for attempt, delay in enumerate((0, 0.05, 0.15)):
        if delay:
            time.sleep(delay)
        try:
            replace(path, data)
            return
        except OSError as exc:
            denied = (getattr(exc, "winerror", None) in (5, 32, 33)
                      or exc.errno in (errno.EACCES, errno.EPERM))
            if not denied or attempt == 2:
                raise


def projection_outcome(method):
    @wraps(method)
    def operation(self, *args, **kwargs):
        self._projection_warnings = []
        self._committed_sequences = []
        return self._operation_result(method(self, *args, **kwargs))
    return operation


def validate_scoped_routes(config):
    seen = set()
    for grant in config.get("task_scoped_routes", []):
        gid = identifier(grant["grant_id"])
        if gid in seen:
            raise WorkbookError("Duplicate task-scoped route grant ID")
        seen.add(gid)
        pair = [grant["sender"], grant["recipient"]]
        if any(endpoint not in config["agents"] for endpoint in pair):
            raise WorkbookError("Scoped route endpoint is not registered")
        if pair in config["allowed_routes"]:
            raise WorkbookError("A task-scoped route cannot also have an unrestricted pair grant")
        if grant.get("status") not in ("active", "revoked"):
            raise WorkbookError("Scoped route must explicitly be active or revoked")
        if not isinstance(grant.get("authorization_ref"), str) or not grant["authorization_ref"].strip():
            raise WorkbookError("Scoped route requires an explicit authorization reference")
        for field in ("task_ids", "message_types", "work_item_ids"):
            values = grant.get(field)
            if (not isinstance(values, list) or not values or
                    any(not isinstance(v, str) or not v.strip() or "*" in v for v in values) or
                    len(values) != len(set(values))):
                raise WorkbookError("Scoped route needs exact nonempty unique " + field)
            if field == "work_item_ids":
                for value in values:
                    identifier(value)


def route_authorized(registry, message):
    pair = [message["sender"], message["recipient"]]
    if pair in registry["allowed_routes"]:
        return True
    return any(grant.get("status") == "active" and
               [grant["sender"], grant["recipient"]] == pair and
               message["task_id"] in grant["task_ids"] and
               message["type"] in grant["message_types"] and
               message["work_item_id"] in grant["work_item_ids"]
               for grant in registry.get("task_scoped_routes", []))


def configure(root, config, actor=None):
    manager = None
    root = Path(root).resolve()
    identifier(config["manager"])
    for registered_actor in config["agents"]:
        identifier(registered_actor)
    if config["manager"] not in config["agents"]:
        raise WorkbookError("Manager must be registered")
    for sender, recipient in config["allowed_routes"]:
        if sender not in config["agents"] or recipient not in config["agents"]:
            raise WorkbookError("Route endpoint is not registered")
    validate_scoped_routes(config)
    root.mkdir(parents=True, exist_ok=True)
    target = root / "registry.json"
    if target.exists() and read(target) != config:
        previous = read(target)
        if actor != previous["manager"] or actor != config["manager"]:
            raise WorkbookError("Only current registry manager may update configuration; no implicit owner change")
        manager = Workbook(root, actor)
        with manager._lock():
            if read(target) != previous:
                raise WorkbookError("Registry changed; reread before update")
            publish(root / "registry-history" / (uuid.uuid4().hex + ".json"), previous)
            replace(target, encoded(config))
            manager.registry = config
            manager._event("note", {"category": "system", "text": "Registry updated by its existing manager; previous version retained"})
    else:
        publish(target, config)
    result = {"root": str(root), "registered": len(config["agents"]), "enrolled": "each owner must run init"}
    return manager._operation_result(result) if manager is not None else result


class Workbook:
    def __init__(self, root, actor):
        self.root = Path(root).resolve()
        self.actor = identifier(actor)
        self.registry = read(self.root / "registry.json")
        if actor not in self.registry["agents"]:
            raise WorkbookError("Unregistered actor")
        self.directory = self.root / "agents" / actor
        self._safe(self.directory)
        self._projection_warnings = []
        self._committed_sequences = []

    def _operation_result(self, result):
        if not self._projection_warnings:
            return result
        return {**result,
                "operation_status": "committed_projection_pending" if self._committed_sequences else "projection_pending_no_new_event",
                "canonical_event_committed": bool(self._committed_sequences),
                "committed_event_sequences": list(self._committed_sequences),
                "projection_status": "pending",
                "projection_warnings": list(self._projection_warnings),
                "state_source": "immutable_events; state.json and Markdown may lag",
                "recovery_action": "Do not replay business/send/claim/transition. Read status/receipts from events; owner init can rebuild views later."}

    def _project(self, path, data):
        self._safe(path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            replace_projection(path, data)
        except OSError as exc:
            raise ProjectionWriteError(path, exc) from exc

    def _safe(self, path):
        if path.resolve() != path.absolute() or not path.resolve().is_relative_to(self.root):
            raise WorkbookError("Notebook path redirects outside its registered location")

    def _owned(self):
        owner = read(self.directory / "owner.json")
        if owner.get("actor") != self.actor or owner.get("directory") != str(self.directory):
            raise WorkbookError("Unknown notebook owner preserved")

    @contextmanager
    def _lock(self, require_owner=True):
        self._safe(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        lock = self.directory / ".writer.lock"
        token = uuid.uuid4().hex
        try:
            with lock.open("x", encoding="utf-8") as stream:
                json.dump({"actor": self.actor, "token": token, "pid": os.getpid(), "at": now()}, stream)
        except FileExistsError:
            raise WorkbookError("Writer lock exists; verify its owner/run; never remove by age")
        try:
            if require_owner:
                self._owned()
            yield
        finally:
            if read(lock).get("token") != token:
                raise WorkbookError("Writer lock ownership changed; preserved")
            lock.unlink()

    def _events(self):
        rows = []
        self._safe(self.directory / "events")
        for index, path in enumerate(sorted((self.directory / "events").rglob("*.json"), key=lambda p: p.name), 1):
            self._safe(path)
            event = read(path)
            if event.get("sequence") != index or event.get("actor") != self.actor:
                raise WorkbookError("Journal sequence/owner mismatch; preserve and investigate")
            rows.append(event)
        return rows

    def state(self):
        self._owned()
        state = {"actor": self.actor, "active_work_item": None, "items": {}, "queued": {}, "received": {}, "outbox": {}, "last_sequence": 0}
        for event in self._events():
            state["last_sequence"] = event["sequence"]
            data = event["data"]
            if event["kind"] == "queued":
                state["queued"][data["message_id"]] = {**data, "status": "queued"}
            elif event["kind"] == "claimed":
                state["received"][data["message_id"]] = "claimed"
                state["queued"][data["message_id"]]["status"] = "claimed"
                state["items"][data["work_item_id"]] = {**data, "status": "claimed"}
                state["active_work_item"] = data["work_item_id"]
            elif event["kind"] == "observed":
                state["received"][data["message_id"]] = data["disposition"]
                state["queued"][data["message_id"]]["status"] = data["disposition"]
            elif event["kind"] == "task_state":
                item = state["items"][data["work_item_id"]]
                item.update(status=data["status"], result=data.get("result"), updated_at=event["at"])
                state["queued"][item["message_id"]]["status"] = data["status"]
                if data["status"] in ("claimed", "in_progress"):
                    state["active_work_item"] = data["work_item_id"]
                elif state["active_work_item"] == data["work_item_id"]:
                    state["active_work_item"] = None
            elif event["kind"] == "dispatched":
                state["outbox"][data["message_id"]] = data
        return state

    def _date(self, timestamp):
        offset = self.registry.get("calendar_utc_offset_minutes", 0)
        if not isinstance(offset, int) or not -840 <= offset <= 840:
            raise WorkbookError("Invalid configured calendar UTC offset")
        return datetime.fromisoformat(timestamp).astimezone(timezone(timedelta(minutes=offset))).strftime("%Y-%m-%d")

    def _refresh(self):
        try:
            self._refresh_views()
        except ProjectionWriteError as exc:
            self._projection_warnings.append({"path": exc.path,
                                              "error": type(exc.cause).__name__, "errno": exc.cause.errno,
                                              "winerror": getattr(exc.cause, "winerror", None)})
        else:
            # A later refresh in the same scan may already have repaired every view.
            self._projection_warnings = []

    def _refresh_views(self):
        state = self.state()
        self._project(self.directory / "state.json", encoded(state))
        groups, task_groups = {}, {}
        for event in self._events():
            date = self._date(event["at"])
            category = event["data"].get("category") if event["kind"] == "note" else {"queued": "inbox", "dispatched": "dispatch", "claimed": "work", "task_state": "work"}.get(event["kind"], "system")
            identifier(category)
            groups.setdefault((date, category), []).append(event)
            work = event["data"].get("work_item_id")
            if work:
                identifier(work)
                task_groups.setdefault((work, date), []).append(event)
        dates = {}
        def pages(directory, prefix, events):
            self._safe(directory)
            paths = []
            for start in range(0, len(events), 50):
                page = directory / (prefix + f"{start // 50 + 1:03d}.md")
                lines = ["# 分类工作记录", "", "Owner: " + self.actor, "", "每页最多50条本人事件；原始事件为事实来源。", ""]
                for event in events[start:start + 50]:
                    lines += ["## " + str(event["sequence"]) + " · " + event["kind"], "", event["at"], "", "```json", json.dumps(event["data"], ensure_ascii=False, indent=2), "```", ""]
                self._project(page, "\n".join(lines).encode("utf-8"))
                paths.append(page)
            return paths
        for (date, category), events in sorted(groups.items()):
            directory = self.directory / "days" / date.replace("-", "/") / category
            paths = pages(directory, "part-", events)
            dates.setdefault(date, []).extend(paths)
        for (work, date), events in sorted(task_groups.items()):
            pages(self.directory / "tasks" / work, date + "-", events)
        for date, paths in dates.items():
            directory = self.directory / "days" / date.replace("-", "/")
            lines = ["# " + date + " 分类索引", "", "仅导航，本页不追加正文。", ""]
            lines += ["- [" + p.relative_to(directory).as_posix() + "](" + p.relative_to(directory).as_posix() + ")" for p in paths]
            self._project(directory / "INDEX.md", "\n".join(lines).encode("utf-8"))
        lines = ["# Agent 工作记录本索引", "", "Owner: " + self.actor, "", "当前工作: " + str(state["active_work_item"] or "none"), "", "仅显示最近20个日期入口；完整历史按 days/YYYY/MM/DD 分类，任务页在 tasks/WORK-ID/。此页不累计正文。", ""]
        lines += ["- [" + date + "](days/" + date.replace("-", "/") + "/INDEX.md)" for date in sorted(dates, reverse=True)[:20]]
        self._project(self.directory / "WORKLOG.md", "\n".join(lines).encode("utf-8"))

    def _event(self, kind, data):
        sequence = len(self._events()) + 1
        event = {"sequence": sequence, "actor": self.actor, "at": now(), "kind": kind, "data": data}
        destination = self.directory / "events" / self._date(event["at"]).replace("-", "/") / (f"{sequence:08d}.json")
        self._safe(destination.parent)
        publish(destination, event)
        self._committed_sequences.append(sequence)
        self._refresh()

    @projection_outcome
    def init(self):
        with self._lock(require_owner=False):
            owner = self.directory / "owner.json"
            if owner.exists():
                self._owned()
            else:
                publish(owner, {"actor": self.actor, "directory": str(self.directory), "created_at": now(), "single_writer": True})
            if not self._events():
                self._event("initialized", {"role": self.registry["agents"][self.actor]["role"]})
            else:
                self._refresh()
        return {"actor": self.actor, "directory": str(self.directory), "initialized": True}

    def _message(self, message):
        for key in ("message_id", "sender", "recipient", "work_item_id"):
            identifier(message[key])
        if not isinstance(message["requires_action"], bool):
            raise WorkbookError("requires_action must be boolean")
        for key in ("task_id", "type", "summary", "next_action", "authorization_ref", "created_at"):
            if not isinstance(message[key], str) or not message[key].strip():
                raise WorkbookError("Missing message field: " + key)
        stamp = datetime.fromisoformat(message["created_at"])
        if stamp.tzinfo is None:
            raise WorkbookError("created_at must include timezone")
        # A reused client must observe revocation at the next message operation.
        self.registry = read(self.root / "registry.json")
        if self.actor not in self.registry["agents"] or not route_authorized(self.registry, message):
            raise WorkbookError("Route is not authorized in registry")
        if len(encoded(message)) > 65536:
            raise WorkbookError("Use evidence pointers; message exceeds 64 KiB")
        return message

    @projection_outcome
    def send(self, message):
        self._message(message)
        if message["sender"] != self.actor:
            raise WorkbookError("Only sender may publish its outbox")
        if [message["sender"], message["recipient"]] not in self.registry["allowed_routes"]:
            raise WorkbookError("Task-scoped routes require AgentMail.send; do not publish a new legacy outbox message")
        with self._lock():
            self._safe(self.directory / "outbox")
            existing = list((self.directory / "outbox").rglob(message["message_id"] + ".json"))
            if len(existing) > 1:
                raise WorkbookError("Multiple outbox objects use the same message ID")
            destination = existing[0] if existing else self.directory / "outbox" / self._date(message["created_at"]).replace("-", "/") / (message["message_id"] + ".json")
            self._safe(destination.parent)
            publish(destination, message)
            state = self.state()
            if message["message_id"] not in state["outbox"]:
                self._event("dispatched", {"message_id": message["message_id"], "task_id": message["task_id"],
                    "work_item_id": message["work_item_id"], "recipient": message["recipient"], "type": message["type"],
                    "delivery": "awaiting_recipient_scan", "outbox_path": str(destination)})
        return {"message_id": message["message_id"], "delivery": "awaiting_recipient_scan", "chat_wake_sent": False}

    def _published(self):
        rows = []
        by_id = {}
        senders = {sender for sender, recipient in self.registry["allowed_routes"] if recipient == self.actor}
        for sender in sorted(senders):
            directory = self.root / "agents" / sender / "outbox"
            self._safe(directory)
            for path in sorted(directory.rglob("*.json")):
                self._safe(path)
                message = self._message(read(path))
                if message["sender"] != sender or path.stem != message["message_id"]:
                    raise WorkbookError("Outbox envelope/path mismatch")
                if message["recipient"] == self.actor:
                    if message["message_id"] in by_id and by_id[message["message_id"]] != message:
                        raise WorkbookError("Message ID collision across senders")
                    by_id[message["message_id"]] = message
                    rows.append(message)
        return sorted(rows, key=lambda item: (datetime.fromisoformat(item["created_at"]), item["message_id"]))

    def _inbox(self):
        rows = []
        for path in sorted((self.directory / "inbox").rglob("*.json")):
            self._safe(path)
            message = self._message(read(path))
            if message["recipient"] != self.actor or path.relative_to(self.directory / "inbox").parts[0] != message["sender"] or path.stem != message["message_id"]:
                raise WorkbookError("Inbox envelope/path mismatch")
            rows.append(message)
        return sorted(rows, key=lambda item: (datetime.fromisoformat(item["created_at"]), item["message_id"]))

    @projection_outcome
    def scan(self, limit=20):
        with self._lock():
            state = self.state()
            published = self._published()
            unseen = [m for m in published if m["message_id"] not in state["queued"]]
            pending = [message for message in self._inbox() if message["message_id"] not in state["received"]]
            busy = state["active_work_item"] is not None
            if busy:
                return {"actor": self.actor, "gate": "busy_deferred", "active_work_item": state["active_work_item"],
                        "pending_count": len(pending) + len(unseen), "messages": [], "scan_at": now()}
            # Receiver is the ONLY writer of its own inbox, journal and derived views.
            for message in unseen[:limit]:
                existing = list((self.directory / "inbox").rglob(message["message_id"] + ".json"))
                if len(existing) > 1:
                    raise WorkbookError("Multiple inbox objects use the same message ID")
                destination = existing[0] if existing else self.directory / "inbox" / message["sender"] / self._date(now()).replace("-", "/") / (message["message_id"] + ".json")
                self._safe(destination.parent)
                publish(destination, message)
                self._event("queued", {key: message[key] for key in ("message_id", "sender", "task_id", "work_item_id", "type")})
            state = self.state()
            pending = [message for message in self._inbox() if message["message_id"] not in state["received"]]
            self._refresh()
            return {"actor": self.actor, "gate": "ready", "active_work_item": None, "pending_count": len(pending),
                    "messages": pending[:limit], "more_count": max(0, len(pending) - limit),
                    "not_imported_count": max(0, len(unseen) - limit), "scan_at": now()}

    @projection_outcome
    def claim(self, message_id):
        identifier(message_id)
        with self._lock():
            state = self.state()
            if message_id in state["received"]:
                return {"message_id": message_id, "disposition": state["received"][message_id], "duplicate": True}
            if state["active_work_item"] is not None:
                raise WorkbookError("Complete or explicitly block current work before claiming another message")
            message = next((m for m in self._inbox() if m["message_id"] == message_id), None)
            if message is None:
                raise WorkbookError("Message is not in this inbox")
            if message_id not in state["queued"]:
                raise WorkbookError("Owner must scan and record this message before claiming")
            if not message["requires_action"] or message["work_item_id"] in state["items"]:
                disposition = "recorded_no_action" if not message["requires_action"] else "duplicate_work_not_reexecuted"
                self._event("observed", {"message_id": message_id, "disposition": disposition, "work_item_id": message["work_item_id"]})
                return {"message_id": message_id, "disposition": disposition}
            self._event("claimed", {"message_id": message_id, "task_id": message["task_id"], "work_item_id": message["work_item_id"],
                "sender": message["sender"], "authorization_ref": message["authorization_ref"], "claimed_at": now()})
        return {"message_id": message_id, "work_item_id": message["work_item_id"], "status": "claimed", "execution_authorized_by_claim": False}

    @projection_outcome
    def transition(self, work_item_id, status, result):
        allowed = {"claimed": {"in_progress", "completed", "blocked", "rejected", "cancelled"},
                   "in_progress": {"completed", "blocked", "cancelled"},
                   "blocked": {"in_progress", "cancelled"}}
        with self._lock():
            state = self.state()
            if work_item_id not in state["items"]:
                raise WorkbookError("Work item was never claimed")
            previous = state["items"][work_item_id]["status"]
            if previous == status:
                return {"work_item_id": work_item_id, "status": status, "duplicate": True}
            if status not in allowed.get(previous, set()):
                raise WorkbookError("Invalid transition: " + previous + " -> " + status)
            if status == "in_progress" and state["active_work_item"] not in (None, work_item_id):
                raise WorkbookError("Another work item is active")
            if not result.strip():
                raise WorkbookError("Record result, evidence pointer, or explicit blocker")
            if len(result.encode("utf-8")) > 8192:
                raise WorkbookError("Use an evidence pointer; result exceeds 8 KiB")
            self._event("task_state", {"work_item_id": work_item_id, "status": status, "result": result})
        return {"work_item_id": work_item_id, "status": status, "next_scan_required": status in ("completed", "blocked", "cancelled", "rejected")}

    @projection_outcome
    def note(self, category, text, work_item_id=None):
        if category not in {"work", "dispatch", "product", "user", "requirement", "module", "risk"} or not text.strip():
            raise WorkbookError("Use a supported note category and nonempty text")
        if len(text.encode("utf-8")) > 8192:
            raise WorkbookError("Use an evidence pointer; note exceeds 8 KiB")
        with self._lock():
            state = self.state()
            work = work_item_id or state["active_work_item"]
            data = {"category": category, "text": text}
            if work:
                if work not in state["items"]:
                    raise WorkbookError("Note work item was never claimed")
                data.update(work_item_id=work, task_id=state["items"][work]["task_id"])
            self._event("note", data)
        return {"recorded": True, "category": category}

    def receipts(self):
        state = self.state()
        result = []
        for message_id, entry in state["outbox"].items():
            recipient = Workbook(self.root, entry["recipient"])
            if not (recipient.directory / "owner.json").exists():
                status = "recipient_not_enrolled"
            else:
                status = recipient.state()["queued"].get(message_id, {}).get("status", "awaiting_recipient_scan")
            result.append({"message_id": message_id, "recipient": entry["recipient"], "status": status,
                           "source": "recipient_self_written_events"})
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--actor")
    commands = parser.add_subparsers(dest="command", required=True)
    configure_cmd = commands.add_parser("configure")
    configure_cmd.add_argument("--config", required=True)
    commands.add_parser("init")
    commands.add_parser("status")
    commands.add_parser("receipts")
    scan_cmd = commands.add_parser("scan")
    scan_cmd.add_argument("--limit", type=int, default=20)
    send_cmd = commands.add_parser("send")
    send_cmd.add_argument("--message", required=True)
    claim_cmd = commands.add_parser("claim")
    claim_cmd.add_argument("--message-id", required=True)
    state_cmd = commands.add_parser("transition")
    state_cmd.add_argument("--work-item", required=True)
    state_cmd.add_argument("--status", required=True)
    state_cmd.add_argument("--result", required=True)
    note_cmd = commands.add_parser("note")
    note_cmd.add_argument("--category", required=True)
    note_cmd.add_argument("--text", required=True)
    note_cmd.add_argument("--work-item")
    args = parser.parse_args()
    if args.command == "configure":
        result = configure(args.root, read(Path(args.config)), args.actor)
    else:
        book = Workbook(args.root, args.actor)
        if args.command == "receipts":
            result = book.receipts()
        elif args.command == "send":
            result = book.send(read(Path(args.message)))
        elif args.command == "claim":
            result = book.claim(args.message_id)
        elif args.command == "transition":
            result = book.transition(args.work_item, args.status, args.result)
        elif args.command == "note":
            result = book.note(args.category, args.text, args.work_item)
        elif args.command == "scan":
            if not 1 <= args.limit <= 100:
                raise WorkbookError("scan limit must be 1-100")
            result = book.scan(args.limit)
        else:
            result = book.init() if args.command == "init" else book.state()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (WorkbookError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"error": type(exc).__name__, "detail": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
