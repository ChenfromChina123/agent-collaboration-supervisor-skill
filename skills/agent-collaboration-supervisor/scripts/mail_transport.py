#!/usr/bin/env python3
"""Independent Agent mail storage; short notices, explicit open, owner-only intake.

No network/chat calls, task execution, background worker or account isolation.
Use with the workbook.py from the same reviewed, frozen release.
"""
import argparse
from functools import wraps
import json
from pathlib import Path

from workbook import Workbook, WorkbookError, identifier, now, publish, read


PROTOCOL = "agent-mail/v1"
CATEGORIES = {"tasks", "results", "control", "information"}


def mail_operation(method):
    @wraps(method)
    def operation(self, *args, **kwargs):
        self.book._projection_warnings = []
        self.book._committed_sequences = []
        return method(self, *args, **kwargs)
    return operation


def independent_root(store, workbooks):
    store, workbooks = Path(store).resolve(), Path(workbooks).resolve()
    if store.is_relative_to(workbooks) or workbooks.is_relative_to(store):
        raise WorkbookError("Mail storage and personal workbooks must be separate roots")
    return store, workbooks


def configure_mail(store, workbooks, actor):
    store, workbooks = independent_root(store, workbooks)
    book = Workbook(workbooks, actor)
    if actor != book.registry["manager"]:
        raise WorkbookError("Only the existing registry manager may initialize mail storage")
    config = {"protocol": PROTOCOL, "root": str(store), "workbooks": str(workbooks), "manager": actor}
    with book._lock():
        target = store / "config.json"
        if store.exists() and not target.exists() and any(store.iterdir()):
            raise WorkbookError("Unknown nonempty mail storage; preserve its owner/content")
        publish(target, config)
    return {"configured": True, "store": str(store), "protocol": PROTOCOL}


class AgentMail:
    def __init__(self, store, workbooks, actor):
        self.root, workbooks = independent_root(store, workbooks)
        self.book = Workbook(workbooks, actor)
        self.actor = self.book.actor
        self.book._owned()
        config = read(self.root / "config.json")
        if config != {"protocol": PROTOCOL, "root": str(self.root), "workbooks": str(workbooks),
                      "manager": self.book.registry["manager"]}:
            raise WorkbookError("Mail configuration/authority mismatch; preserve and investigate")

    def _safe(self, path):
        if not path.resolve().is_relative_to(self.root):
            raise WorkbookError("Mail path escaped the configured storage root")
        return path

    def _object(self, sender, message_id):
        return self._safe(self.root / "messages" / identifier(sender) / (identifier(message_id) + ".json"))

    def _category(self, message):
        category = message.get("mail_category", "tasks" if message["requires_action"] else "information")
        if category not in CATEGORIES:
            raise WorkbookError("Unknown mail category")
        return category

    def _header(self, message):
        subject = message.get("subject", message["summary"])
        if not isinstance(subject, str) or not subject.strip():
            raise WorkbookError("Mail subject must be nonempty text")
        return {"protocol": PROTOCOL, "message_id": message["message_id"], "sender": message["sender"],
                "recipient": message["recipient"], "task_id": message["task_id"], "type": message["type"],
                "subject": " ".join(subject.split())[:120], "requires_action": message["requires_action"],
                "created_at": message["created_at"], "category": self._category(message),
                "mail_ref": self._object(message["sender"], message["message_id"]).relative_to(self.root).as_posix()}

    def _delivery(self, message):
        date = self.book._date(message["created_at"]).replace("-", "/")
        return self._safe(self.root / "inboxes" / message["recipient"] / date / self._category(message)
                          / (message["sender"] + "__" + message["message_id"] + ".json"))

    def _receipt(self, sender, message_id, kind):
        return self._safe(self.root / "receipts" / self.actor / identifier(sender) / identifier(message_id) / (kind + ".json"))

    def _load(self, sender, message_id):
        message = self.book._message(read(self._object(sender, message_id)))
        if message["sender"] != sender or message["message_id"] != message_id:
            raise WorkbookError("Mail identity/path mismatch")
        if message["recipient"] != self.actor and sender != self.actor:
            raise WorkbookError("Only the sender or addressed recipient may access this mail")
        header = self._header(message)
        if read(self._delivery(message)) != header:
            raise WorkbookError("Mail delivery/header mismatch or incomplete submission")
        return message

    @mail_operation
    def send(self, message):
        self.book._message(message)
        if message["sender"] != self.actor:
            raise WorkbookError("Only the sender may submit mail")
        header = self._header(message)
        with self.book._lock():
            previous = self.book.state()["outbox"].get(message["message_id"])
            if previous and previous.get("transport") != PROTOCOL:
                raise WorkbookError("Already published by the legacy transport; do not redeliver it as new mail")
            self.book._safe(self.book.directory / "outbox")
            if list((self.book.directory / "outbox").rglob(message["message_id"] + ".json")):
                raise WorkbookError("Legacy outbox already contains this ID; drain it without redispatch")
            # Complete immutable context first, then the recipient-only discovery header.
            # A same-ID retry repairs an interrupted submission, never creates another task.
            publish(self._object(self.actor, message["message_id"]), message)
            key = self._safe(self.root / "delivery-keys" / message["recipient"] / (message["message_id"] + ".json"))
            publish(key, {"sender": self.actor, "mail_ref": header["mail_ref"]})
            publish(self._delivery(message), header)
            if not previous:
                self.book._event("dispatched", {"message_id": message["message_id"], "task_id": message["task_id"],
                    "work_item_id": message["work_item_id"], "recipient": message["recipient"], "type": message["type"],
                    "transport": PROTOCOL, "mail_ref": header["mail_ref"], "delivery": "stored_not_notified"})
        return self.book._operation_result({"message_id": message["message_id"], "delivery": "stored_not_notified",
            "notification": header, "chat_wake_sent": False, "recipient_todo_written": False})

    def headers(self, limit=20):
        if not 1 <= limit <= 100:
            raise WorkbookError("Header limit must be 1-100")
        directory = self._safe(self.root / "inboxes" / self.actor)
        pending = []
        # This is the ONLY discovery directory read. No peer outboxes or workbooks.
        for path in sorted(directory.rglob("*.json")):
            self._safe(path)
            header = read(path)
            for field in ("sender", "recipient", "message_id"):
                identifier(header[field])
            if header["protocol"] != PROTOCOL or header["recipient"] != self.actor:
                raise WorkbookError("Recipient header identity mismatch")
            expected = self._object(header["sender"], header["message_id"]).relative_to(self.root).as_posix()
            if header["mail_ref"] != expected or header["category"] not in CATEGORIES:
                raise WorkbookError("Recipient header reference mismatch")
            if not self._receipt(header["sender"], header["message_id"], "accepted").exists():
                pending.append({**header, "opened": self._receipt(header["sender"], header["message_id"], "opened").exists()})
        pending.sort(key=lambda h: (h["created_at"], h["message_id"]))
        return {"actor": self.actor, "headers": pending[:limit], "more_count": max(0, len(pending) - limit),
                "context_loaded": False, "peer_workbooks_scanned": False}

    def open(self, sender, message_id):
        with self.book._lock():
            message = self._load(sender, message_id)
            if message["recipient"] != self.actor:
                raise WorkbookError("Only the recipient records opening a mail")
            receipt = self._receipt(sender, message_id, "opened")
            if not receipt.exists():
                publish(receipt, {"actor": self.actor, "sender": sender, "message_id": message_id,
                                  "opened_at": now(), "meaning": "mail opened; no task claim or authorization"})
        return {"message": message, "mail_state": "opened", "recipient_todo_written": False}

    @mail_operation
    def accept(self, sender, message_id):
        with self.book._lock():
            message = self._load(sender, message_id)
            if message["recipient"] != self.actor:
                raise WorkbookError("Only the recipient may import its own mail")
            if not self._receipt(sender, message_id, "opened").exists():
                raise WorkbookError("Open the complete mail before deciding its personal intake")
            state = self.book.state()
            if state["active_work_item"] is not None:
                return {"gate": "busy_deferred", "active_work_item": state["active_work_item"], "todo_created": False}
            existing = list((self.book.directory / "inbox").rglob(message_id + ".json"))
            if len(existing) > 1:
                raise WorkbookError("Multiple personal inbox objects use this ID")
            destination = existing[0] if existing else self.book.directory / "inbox" / sender / self.book._date(now()).replace("-", "/") / (message_id + ".json")
            self.book._safe(destination)
            publish(destination, message)
            if message_id not in state["queued"]:
                self.book._event("queued", {key: message[key] for key in ("message_id", "sender", "task_id", "work_item_id", "type")})
            receipt = self._receipt(sender, message_id, "accepted")
            if not receipt.exists():
                publish(receipt, {"actor": self.actor, "sender": sender, "message_id": message_id,
                                  "accepted_at": now(), "meaning": "owner queued; claim and actual action are separate"})
        current = self.book.state()
        return self.book._operation_result({"gate": "ready", "message_id": message_id,
            "status": current["queued"][message_id]["status"], "claim_required": message_id not in current["received"]})

    def status(self, message_id):
        message = self._load(self.actor, identifier(message_id))
        recipient = message["recipient"]
        base = self._safe(self.root / "receipts" / recipient / self.actor / message_id)
        return {"message_id": message_id, "recipient": recipient, "stored": True,
                "opened": (base / "opened.json").exists(), "owner_queued": (base / "accepted.json").exists(),
                "business_execution": "not_proven_by_transport_receipts"}

    def reserve_notice(self, message_id, safe_idle=False, retry=False):
        message = self._load(self.actor, identifier(message_id))
        if not message["requires_action"]:
            raise WorkbookError("No-action mail does not need a chat wakeup")
        if not safe_idle:
            return {"reserved": False, "state": "deferred_until_safe_checkpoint", "chat_wake_sent": False}
        with self.book._lock():
            directory = self._safe(self.root / "notices" / self.actor / message_id)
            attempt = 1
            first = directory / "attempt-1.json"
            if first.exists():
                result = directory / "result-1.json"
                if not result.exists() or read(result)["outcome"] != "failed" or not retry:
                    return {"reserved": False, "state": read(result)["outcome"] if result.exists() else "delivery_unknown"}
                attempt = 2
                if (directory / "attempt-2.json").exists():
                    return {"reserved": False, "state": "retry_already_reserved"}
            publish(directory / (f"attempt-{attempt}.json"), {"sender": self.actor, "message_id": message_id,
                "recipient": message["recipient"], "attempt": attempt, "reserved_at": now()})
        header = self._header(message)
        return {"reserved": True, "attempt": attempt, "notice": {key: header[key] for key in
                ("message_id", "sender", "recipient", "task_id", "subject", "mail_ref")},
                "context_included": False, "chat_wake_sent": False,
                "limitation": "Caller must use an authorized native tool; no atomic idle-and-send guarantee"}

    def notice_result(self, message_id, attempt, outcome):
        if attempt not in (1, 2) or outcome not in {"sent", "failed", "delivery_unknown"}:
            raise WorkbookError("Invalid notice attempt/outcome")
        self._load(self.actor, identifier(message_id))
        with self.book._lock():
            directory = self._safe(self.root / "notices" / self.actor / message_id)
            reservation = read(directory / (f"attempt-{attempt}.json"))
            if reservation["sender"] != self.actor:
                raise WorkbookError("Notice reservation owner mismatch")
            result = directory / (f"result-{attempt}.json")
            if result.exists():
                if read(result)["outcome"] != outcome:
                    raise WorkbookError("Do not overwrite an uncertain or previously recorded notice outcome")
            else:
                publish(result, {"sender": self.actor, "message_id": message_id, "attempt": attempt,
                                 "outcome": outcome, "recorded_at": now(), "meaning": "native notice only; not read or claimed"})
        return {"message_id": message_id, "attempt": attempt, "outcome": outcome}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True)
    parser.add_argument("--workbooks", required=True)
    parser.add_argument("--actor", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("configure")
    command = sub.add_parser("send"); command.add_argument("--message", required=True)
    command = sub.add_parser("headers"); command.add_argument("--limit", type=int, default=20)
    for name in ("open", "accept"):
        command = sub.add_parser(name); command.add_argument("--sender", required=True); command.add_argument("--message-id", required=True)
    command = sub.add_parser("status"); command.add_argument("--message-id", required=True)
    command = sub.add_parser("reserve-notice"); command.add_argument("--message-id", required=True); command.add_argument("--safe-idle", action="store_true"); command.add_argument("--retry", action="store_true")
    command = sub.add_parser("notice-result"); command.add_argument("--message-id", required=True); command.add_argument("--attempt", type=int, required=True); command.add_argument("--outcome", required=True)
    args = parser.parse_args()
    if args.command == "configure":
        result = configure_mail(args.store, args.workbooks, args.actor)
    else:
        mail = AgentMail(args.store, args.workbooks, args.actor)
        if args.command == "send": result = mail.send(read(Path(args.message)))
        elif args.command == "headers": result = mail.headers(args.limit)
        elif args.command in ("open", "accept"): result = getattr(mail, args.command)(args.sender, args.message_id)
        elif args.command == "status": result = mail.status(args.message_id)
        elif args.command == "reserve-notice": result = mail.reserve_notice(args.message_id, args.safe_idle, args.retry)
        else: result = mail.notice_result(args.message_id, args.attempt, args.outcome)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (WorkbookError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"error": type(exc).__name__, "detail": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
