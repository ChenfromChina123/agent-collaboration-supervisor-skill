"""Behavioral checks for independent mail, directed discovery and owner intake."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mail_transport import AgentMail, configure_mail
from workbook import Workbook, WorkbookError, ProjectionWriteError, configure, now, read


class MailTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-mail-test-")
        self.base = Path(self.temp.name).resolve()
        self.books_root, self.store = self.base / "workbooks", self.base / "mail"
        configure(self.books_root, {"manager": "manager", "agents": {a: {"role": a} for a in ["manager", "worker", "other"]},
            "allowed_routes": [["manager", "worker"], ["worker", "manager"], ["other", "worker"]], "calendar_utc_offset_minutes": 480})
        self.books = {a: Workbook(self.books_root, a) for a in ["manager", "worker", "other"]}
        for b in self.books.values(): b.init()
        configure_mail(self.store, self.books_root, "manager")
        self.mail = {a: AgentMail(self.store, self.books_root, a) for a in self.books}

    def tearDown(self):
        self.assertEqual(self.base.parent, Path(tempfile.gettempdir()).resolve())
        self.assertTrue(self.base.name.startswith("agent-mail-test-"))
        self.temp.cleanup()

    def message(self, number=1, sender="manager"):
        return {"message_id": f"MAIL-{number}", "sender": sender, "recipient": "worker", "work_item_id": f"WORK-{number}",
            "task_id": "TASK-LOCAL-TRIAL", "type": "ASSIGN", "subject": "Inspect the fixed local fixture", "summary": "Scope and next step",
            "context": "FULL-CONTEXT-ONLY-ON-OPEN\nApproved scope, inputs, acceptance and constraints.", "next_action": "Inspect local fixture",
            "requires_action": True, "authorization_ref": "explicit local test", "created_at": now()}

    def receiver_bytes(self):
        d=self.books["worker"].directory
        return {p.relative_to(d).as_posix():p.read_bytes() for p in d.rglob("*") if p.is_file()}

    def deliver(self, message):
        self.mail[message["sender"]].send(message)
        self.mail["worker"].open(message["sender"], message["message_id"])
        return self.mail["worker"].accept(message["sender"], message["message_id"])

    def test_sender_writes_independent_store_not_either_todo(self):
        before = self.receiver_bytes()
        result = self.mail["manager"].send(self.message())
        self.assertEqual(before, self.receiver_bytes())
        self.assertFalse(list((self.books["manager"].directory / "outbox").rglob("*.json")))
        self.assertEqual(self.books["manager"].state()["items"], {})
        self.assertFalse(result["chat_wake_sent"])
        self.assertTrue((self.store / "messages/manager/MAIL-1.json").exists())

    def test_headers_are_directed_and_do_not_read_context_or_peer_workbooks(self):
        self.mail["manager"].send(self.message())
        original = Path.rglob
        def guarded(path, pattern):
            self.assertTrue(path.is_relative_to(self.store / "inboxes/worker"))
            return original(path, pattern)
        with patch.object(Path, "rglob", guarded):
            headers = self.mail["worker"].headers()
        self.assertEqual(len(headers["headers"]), 1)
        self.assertNotIn("context", headers["headers"][0])
        self.assertNotIn("FULL-CONTEXT", str(headers))
        self.assertEqual(self.mail["other"].headers()["headers"], [])

    def test_open_is_separate_from_personal_intake_and_claim(self):
        message = self.message(); self.mail["manager"].send(message)
        before = self.receiver_bytes()
        opened = self.mail["worker"].open("manager", message["message_id"])
        self.assertEqual(opened["message"], message)
        self.assertEqual(before, self.receiver_bytes())
        self.assertTrue(self.mail["manager"].status(message["message_id"])["opened"])
        self.assertFalse(self.mail["manager"].status(message["message_id"])["owner_queued"])
        accepted = self.mail["worker"].accept("manager", message["message_id"])
        self.assertEqual(accepted["status"], "queued")
        self.assertIsNone(self.books["worker"].state()["active_work_item"])
        self.books["worker"].claim(message["message_id"])
        self.assertEqual(self.books["worker"].state()["active_work_item"], message["work_item_id"])

    def test_accept_requires_open_and_busy_defers_without_touching_todo(self):
        first=self.message(); second=self.message(2)
        self.deliver(first); self.books["worker"].claim(first["message_id"])
        self.mail["manager"].send(second)
        with self.assertRaises(WorkbookError): self.mail["worker"].accept("manager", second["message_id"])
        self.mail["worker"].open("manager", second["message_id"])
        before=self.receiver_bytes()
        self.assertEqual(self.mail["worker"].accept("manager", second["message_id"])["gate"], "busy_deferred")
        self.assertEqual(before, self.receiver_bytes())
        self.books["worker"].transition(first["work_item_id"], "completed", "local fixture done")
        self.assertEqual(self.mail["worker"].accept("manager", second["message_id"])["status"], "queued")

    def test_replay_never_creates_second_dispatch_or_intake_event(self):
        m=self.message();self.deliver(m)
        for _ in range(2):
            self.mail["manager"].send(m);self.mail["worker"].open("manager", m["message_id"]);self.mail["worker"].accept("manager", m["message_id"])
        self.assertEqual(sum(e["kind"]=="dispatched" for e in self.books["manager"]._events()), 1)
        self.assertEqual(sum(e["kind"]=="queued" for e in self.books["worker"]._events()), 1)
        self.assertEqual(self.mail["worker"].headers()["headers"], [])
        with self.assertRaises(WorkbookError): self.mail["manager"].send({**m,"context":"different"})

    def test_incomplete_submission_same_id_retry_repairs_header(self):
        m=self.message(); original=self.mail["manager"]._delivery
        def fail(_): raise OSError("interrupted after complete mail object")
        with patch.object(self.mail["manager"], "_delivery", fail):
            with self.assertRaises(OSError): self.mail["manager"].send(m)
        self.assertEqual(self.mail["worker"].headers()["headers"], [])
        self.mail["manager"].send(m)
        self.assertEqual(read(original(m))["message_id"], m["message_id"])

    def test_recipient_id_collision_rejected_and_not_delivered_twice(self):
        self.mail["manager"].send(self.message())
        with self.assertRaises(WorkbookError): self.mail["other"].send(self.message(sender="other"))
        self.assertEqual(len(self.mail["worker"].headers()["headers"]), 1)

    def test_routes_recipient_and_paths_remain_enforced(self):
        self.mail["manager"].send(self.message())
        with self.assertRaises(WorkbookError): self.mail["other"].open("manager", "MAIL-1")
        with self.assertRaises(WorkbookError): self.mail["worker"].open("manager", "../escape")
        with self.assertRaises(WorkbookError): self.mail["manager"].send({**self.message(2),"recipient":"other"})
        with self.assertRaises(WorkbookError): self.mail["worker"].send(self.message(2))

    def test_mail_root_cannot_be_nested_in_or_contain_workbooks(self):
        for p in [self.books_root, self.books_root/"mail", self.base]:
            with self.assertRaises(WorkbookError): configure_mail(p,self.books_root,"manager")
        with self.assertRaises(WorkbookError): configure_mail(self.base/"other-mail",self.books_root,"worker")

    def test_unknown_storage_and_existing_owner_locks_are_preserved(self):
        unknown=self.base/"unknown";unknown.mkdir();(unknown/"owner.json").write_text("unknown",encoding="utf-8")
        with self.assertRaises(WorkbookError): configure_mail(unknown,self.books_root,"manager")
        lock=self.books["manager"].directory/".writer.lock";lock.write_text("unknown",encoding="utf-8")
        with self.assertRaises(WorkbookError): self.mail["manager"].send(self.message())
        self.assertEqual(lock.read_text(encoding="utf-8"),"unknown")

    def test_notice_reservation_prevents_duplicates_and_only_one_known_failed_retry(self):
        self.mail["manager"].send(self.message());m=self.mail["manager"]
        self.assertFalse(m.reserve_notice("MAIL-1")["reserved"])
        notice=m.reserve_notice("MAIL-1",safe_idle=True)
        self.assertNotIn("context", notice["notice"]);self.assertTrue(notice["reserved"])
        self.assertFalse(m.reserve_notice("MAIL-1",safe_idle=True,retry=True)["reserved"])
        m.notice_result("MAIL-1",1,"failed")
        self.assertTrue(m.reserve_notice("MAIL-1",safe_idle=True,retry=True)["reserved"])
        m.notice_result("MAIL-1",2,"sent")
        self.assertFalse(m.reserve_notice("MAIL-1",safe_idle=True,retry=True)["reserved"])
        with self.assertRaises(WorkbookError):m.notice_result("MAIL-1",2,"failed")

    def test_no_action_mail_does_not_wake_and_legacy_ids_are_not_redelivered(self):
        m=self.message();self.books["manager"].send(m)
        with self.assertRaises(WorkbookError):self.mail["manager"].send(m)
        m={**self.message(2),"requires_action":False};self.mail["manager"].send(m)
        with self.assertRaises(WorkbookError):self.mail["manager"].reserve_notice(m["message_id"],True)

    def test_projection_failure_keeps_mail_and_exact_current_operation_events(self):
        book=self.mail["manager"].book
        error=ProjectionWriteError(book.directory/"state.json", PermissionError(13,"fixture sharing denial"))
        with patch.object(book,"_project",side_effect=error):
            first=self.mail["manager"].send(self.message())
            second=self.mail["manager"].send(self.message(2))
        self.assertEqual(first["operation_status"],"committed_projection_pending")
        self.assertEqual(len(first["committed_event_sequences"]),1)
        self.assertEqual(len(second["committed_event_sequences"]),1)
        self.assertNotEqual(first["committed_event_sequences"],second["committed_event_sequences"])
        self.assertEqual(len(self.mail["worker"].headers()["headers"]),2)
        self.assertEqual(len(book.state()["outbox"]),2)


if __name__ == "__main__": unittest.main()
