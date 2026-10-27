#!/usr/bin/env python3
"""Local durable delivery and self-owned Agent workbooks. Standard library only.

Identity and authorization are project policy, not account isolation. No chat,
network, worker spawning, task execution, or automatic stale-lock removal.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import uuid


class WorkbookError(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", value):
        raise WorkbookError("IDs must be 1-100 ASCII letters, digits, hyphens or underscores")
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


def configure(root, config):
    root = Path(root).resolve()
    identifier(config["manager"])
    for actor in config["agents"]:
        identifier(actor)
    if config["manager"] not in config["agents"]:
        raise WorkbookError("Manager must be registered")
    for sender, recipient in config["allowed_routes"]:
        if sender not in config["agents"] or recipient not in config["agents"]:
            raise WorkbookError("Route endpoint is not registered")
    root.mkdir(parents=True, exist_ok=True)
    publish(root / "registry.json", config)
    return {"root": str(root), "registered": len(config["agents"]), "enrolled": "each owner must run init"}


class Workbook:
    def __init__(self, root, actor):
        self.root = Path(root).resolve()
        self.actor = identifier(actor)
        self.registry = read(self.root / "registry.json")
        if actor not in self.registry["agents"]:
            raise WorkbookError("Unregistered actor")
        self.directory = self.root / "agents" / actor
        self._safe(self.directory)

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
        for index, path in enumerate(sorted((self.directory / "events").glob("*.json")), 1):
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

    def _refresh(self):
        state = self.state()
        replace(self.directory / "state.json", encoded(state))
        lines = ["# Agent 工作记录本", "", "Owner: " + self.actor,
                 "", "当前工作: " + str(state["active_work_item"] or "none"), "",
                 "此页由本人的不可变事件生成；收件箱内容不等于已认领或已授权。", ""]
        for event in self._events():
            lines += ["## " + str(event["sequence"]) + " · " + event["kind"], "", event["at"], "",
                      "```json", json.dumps(event["data"], ensure_ascii=False, indent=2), "```", ""]
        replace(self.directory / "WORKLOG.md", "\n".join(lines).encode("utf-8"))

    def _event(self, kind, data):
        sequence = len(self._events()) + 1
        event = {"sequence": sequence, "actor": self.actor, "at": now(), "kind": kind, "data": data}
        publish(self.directory / "events" / (f"{sequence:08d}.json"), event)
        self._refresh()

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
        if [message["sender"], message["recipient"]] not in self.registry["allowed_routes"]:
            raise WorkbookError("Route is not authorized in registry")
        if len(encoded(message)) > 65536:
            raise WorkbookError("Use evidence pointers; message exceeds 64 KiB")
        return message

    def send(self, message):
        self._message(message)
        if message["sender"] != self.actor:
            raise WorkbookError("Only sender may publish its outbox")
        with self._lock():
            destination = self.directory / "outbox" / (message["message_id"] + ".json")
            publish(destination, message)
            state = self.state()
            if message["message_id"] not in state["outbox"]:
                self._event("dispatched", {"message_id": message["message_id"], "task_id": message["task_id"],
                    "work_item_id": message["work_item_id"], "recipient": message["recipient"], "type": message["type"],
                    "delivery": "awaiting_recipient_scan", "outbox_path": str(destination)})
        return {"message_id": message["message_id"], "delivery": "awaiting_recipient_scan", "chat_wake_sent": False}

    def _published(self):
        rows = []
        senders = {sender for sender, recipient in self.registry["allowed_routes"] if recipient == self.actor}
        for sender in sorted(senders):
            directory = self.root / "agents" / sender / "outbox"
            self._safe(directory)
            for path in sorted(directory.glob("*.json")):
                self._safe(path)
                message = self._message(read(path))
                if message["sender"] != sender or path.stem != message["message_id"]:
                    raise WorkbookError("Outbox envelope/path mismatch")
                if message["recipient"] == self.actor:
                    rows.append(message)
        return sorted(rows, key=lambda item: (item["created_at"], item["message_id"]))

    def _inbox(self):
        rows = []
        for path in sorted((self.directory / "inbox").glob("*/*.json")):
            self._safe(path)
            message = self._message(read(path))
            if message["recipient"] != self.actor or path.parent.name != message["sender"] or path.stem != message["message_id"]:
                raise WorkbookError("Inbox envelope/path mismatch")
            rows.append(message)
        return sorted(rows, key=lambda item: (item["created_at"], item["message_id"]))

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
                destination = self.directory / "inbox" / message["sender"] / (message["message_id"] + ".json")
                self._safe(destination.parent)
                publish(destination, message)
                self._event("queued", {key: message[key] for key in ("message_id", "sender", "task_id", "work_item_id", "type")})
            state = self.state()
            pending = [message for message in self._inbox() if message["message_id"] not in state["received"]]
            return {"actor": self.actor, "gate": "ready", "active_work_item": None, "pending_count": len(pending),
                    "messages": pending[:limit], "more_count": max(0, len(pending) - limit),
                    "not_imported_count": max(0, len(unseen) - limit), "scan_at": now()}

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
            self._event("task_state", {"work_item_id": work_item_id, "status": status, "result": result})
        return {"work_item_id": work_item_id, "status": status, "next_scan_required": status in ("completed", "blocked", "cancelled", "rejected")}

    def note(self, category, text):
        if category not in {"work", "dispatch", "product", "user", "requirement", "module", "risk"} or not text.strip():
            raise WorkbookError("Use a supported note category and nonempty text")
        with self._lock():
            self._event("note", {"category": category, "text": text})
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
    args = parser.parse_args()
    if args.command == "configure":
        result = configure(args.root, read(Path(args.config)))
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
            result = book.note(args.category, args.text)
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
