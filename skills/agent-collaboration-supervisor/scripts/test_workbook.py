"""Behavioral tests: local delivery, owner-only import, replay and failure recovery."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from workbook import Workbook, WorkbookError, configure, now, publish


class WorkbookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-workbook-test-")
        self.root = Path(self.temp.name).resolve()
        self.assertEqual(self.root.parent, Path(tempfile.gettempdir()).resolve())
        self.assertTrue(self.root.name.startswith("agent-workbook-test-"))
        actors = ["manager", "worker", "sender2", "unrouted"]
        configure(self.root, {"manager": "manager", "agents": {x: {"role": x} for x in actors},
                              "allowed_routes": [["manager", "worker"], ["worker", "manager"], ["sender2", "worker"]]})
        self.books = {actor: Workbook(self.root, actor) for actor in actors}
        for book in self.books.values():
            book.init()

    def tearDown(self):
        self.assertEqual(self.root.parent, Path(tempfile.gettempdir()).resolve())
        self.assertTrue(self.root.name.startswith("agent-workbook-test-"))
        self.temp.cleanup()

    def message(self, number=1, sender="manager", action=True, work=None):
        return {"message_id": f"MSG-{sender}-{number}", "sender": sender, "recipient": "worker",
                "work_item_id": work or f"WORK-{sender}-{number}", "task_id": "TASK-PROTOCOL-TRIAL",
                "type": "TASK", "summary": "local protocol fixture", "next_action": "record this controlled local fixture",
                "requires_action": action, "authorization_ref": "explicit local test scope", "created_at": now()}

    def ingest_claim(self, message):
        self.books[message["sender"]].send(message)
        self.books["worker"].scan()
        self.books["worker"].claim(message["message_id"])

    def test_sender_never_changes_or_creates_receiver_files(self):
        book = self.books["worker"]
        before = {str(p.relative_to(book.directory)): p.read_bytes() for p in book.directory.rglob("*") if p.is_file()}
        self.books["manager"].send(self.message())
        after = {str(p.relative_to(book.directory)): p.read_bytes() for p in book.directory.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(self.books["manager"].receipts()[0]["status"], "awaiting_recipient_scan")
        book.scan()
        self.assertEqual(self.books["manager"].receipts()[0]["status"], "queued")

    def test_busy_then_finish_then_self_import_and_claim(self):
        first, second = self.message(), self.message(2)
        self.ingest_claim(first)
        self.books["worker"].transition(first["work_item_id"], "in_progress", "controlled work")
        self.books["manager"].send(second)
        scan = self.books["worker"].scan()
        self.assertEqual(scan["gate"], "busy_deferred")
        self.assertEqual(scan["messages"], [])
        self.assertNotIn(second["message_id"], self.books["worker"].state()["queued"])
        with self.assertRaises(WorkbookError):
            self.books["worker"].claim(second["message_id"])
        self.books["worker"].transition(first["work_item_id"], "completed", "first done")
        self.books["worker"].scan()
        self.books["worker"].claim(second["message_id"])
        self.assertEqual(self.books["worker"].state()["active_work_item"], second["work_item_id"])

    def test_replay_deduplicates_and_conflicting_payload_fails(self):
        message = self.message()
        self.books["manager"].send(message)
        self.books["manager"].send(message)
        self.assertEqual(len(self.books["manager"].state()["outbox"]), 1)
        message = {**message, "summary": "different content under same ID"}
        with self.assertRaises(WorkbookError):
            self.books["manager"].send(message)

    def test_crash_after_claim_event_replays_without_second_claim(self):
        message = self.message()
        self.books["manager"].send(message)
        book = self.books["worker"]
        book.scan()
        def fail():
            raise OSError("simulated crash before derived view replacement")
        book._refresh = fail
        with self.assertRaises(OSError):
            book.claim(message["message_id"])
        recovered = Workbook(self.root, "worker")
        self.assertEqual(recovered.state()["active_work_item"], message["work_item_id"])
        self.assertTrue(recovered.claim(message["message_id"])["duplicate"])
        recovered.init()
        self.assertEqual(json.loads((recovered.directory / "state.json").read_text())["active_work_item"], message["work_item_id"])

    def test_concurrent_claim_has_one_claim_event(self):
        message = self.message()
        self.books["manager"].send(message)
        self.books["worker"].scan()
        def attempt(_):
            try:
                return Workbook(self.root, "worker").claim(message["message_id"])
            except WorkbookError:
                return {"blocked_by_writer_lock": True}
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(attempt, range(2)))
        self.assertEqual(sum(x["kind"] == "claimed" for x in self.books["worker"]._events()), 1)

    def test_multiple_senders_do_not_write_receiver_and_no_delivery_lost(self):
        first, second = self.message(), self.message(1, "sender2")
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.books[m["sender"]].send, m) for m in (first, second)]
            for future in futures:
                future.result()
        self.assertEqual(self.books["worker"].state()["queued"], {})
        self.assertEqual(self.books["worker"].scan()["pending_count"], 2)

    def test_blocker_is_persisted_and_does_not_start_external_feedback(self):
        message = self.message()
        self.ingest_claim(message)
        self.books["worker"].transition(message["work_item_id"], "blocked", "missing approved input; next_owner=manager")
        self.assertIsNone(self.books["worker"].state()["active_work_item"])
        self.assertEqual(self.books["worker"].state()["items"][message["work_item_id"]]["status"], "blocked")
        self.assertEqual(self.books["worker"].state()["outbox"], {})

    def test_information_is_recorded_without_active_task(self):
        message = self.message(action=False)
        self.ingest_claim(message)
        self.assertIsNone(self.books["worker"].state()["active_work_item"])
        self.assertEqual(self.books["worker"].state()["received"][message["message_id"]], "recorded_no_action")

    def test_different_message_same_work_is_not_reexecuted(self):
        first = self.message()
        self.ingest_claim(first)
        self.books["worker"].transition(first["work_item_id"], "completed", "done")
        second = self.message(2, work=first["work_item_id"])
        self.books["manager"].send(second)
        self.books["worker"].scan()
        self.assertEqual(self.books["worker"].claim(second["message_id"])["disposition"], "duplicate_work_not_reexecuted")

    def test_unknown_owner_and_unsafe_ids_preserved(self):
        owner = self.books["worker"].directory / "owner.json"
        owner.write_text('{"actor":"somebody-else"}', encoding="utf-8")
        with self.assertRaises(WorkbookError):
            self.books["worker"].init()
        self.assertEqual(json.loads(owner.read_text())["actor"], "somebody-else")
        for bad in ("../worker", "a:b", "CON"):
            with self.assertRaises(WorkbookError):
                Workbook(self.root, bad)

    def test_unregistered_route_rejected(self):
        message = self.message(sender="unrouted")
        with self.assertRaises(WorkbookError):
            self.books["unrouted"].send(message)

    def test_scan_bounds_and_next_start_sees_late_arrival(self):
        self.assertEqual(self.books["worker"].scan()["pending_count"], 0)
        for number in range(3):
            self.books["manager"].send(self.message(number))
        scan = self.books["worker"].scan(limit=1)
        self.assertEqual(len(scan["messages"]), 1)
        self.assertEqual(scan["not_imported_count"], 2)
        self.assertEqual(len(self.books["worker"].state()["queued"]), 1)

    def test_partial_temporary_file_is_not_visible_and_bad_published_json_fails(self):
        outbox = self.books["manager"].directory / "outbox"
        outbox.mkdir(exist_ok=True)
        (outbox / ".pending-interrupted").write_text("{", encoding="utf-8")
        self.assertEqual(self.books["worker"].scan()["pending_count"], 0)
        (outbox / "MSG-bad.json").write_text("{", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.books["worker"].scan()

    def test_cli_owner_initialization_and_readback(self):
        command = [sys.executable, str(Path(__file__).with_name("workbook.py")), "--root", str(self.root), "--actor", "worker", "status"]
        result = subprocess.run(command, text=True, encoding="utf-8", capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout)["actor"], "worker")

    def test_timezone_order_uses_real_instants(self):
        later = {**self.message(1), "created_at": "2026-01-01T01:00:00+00:00"}
        earlier = {**self.message(2), "created_at": "2026-01-01T08:00:00+08:00"}
        self.books["manager"].send(later)
        self.books["manager"].send(earlier)
        self.assertEqual(self.books["worker"].scan()["messages"][0]["message_id"], earlier["message_id"])

    def test_publish_before_dispatch_event_is_repaired_on_same_message_retry(self):
        message = self.message()
        book = self.books["manager"]
        original = book._event
        def fail(kind, data):
            raise OSError("simulated crash after outbox publication")
        book._event = fail
        with self.assertRaises(OSError):
            book.send(message)
        book._event = original
        self.assertEqual(book.state()["outbox"], {})
        book.send(message)
        self.assertEqual(len(book.state()["outbox"]), 1)
        self.assertEqual(self.books["worker"].scan()["pending_count"], 1)

    def test_notes_partition_by_date_category_and_fixed_size_pages(self):
        book = self.books["worker"]
        with patch("workbook.now", return_value="2026-01-01T23:00:00+00:00"):
            for index in range(51):
                book.note("module", "module fact " + str(index))
        with patch("workbook.now", return_value="2026-01-02T00:00:00+00:00"):
            book.note("requirement", "separate date and category")
        first = book.directory / "days/2026/01/01/module/part-001.md"
        second = book.directory / "days/2026/01/01/module/part-002.md"
        self.assertEqual(first.read_text(encoding="utf-8").count("\n## "), 50)
        self.assertEqual(second.read_text(encoding="utf-8").count("\n## "), 1)
        self.assertTrue((book.directory / "days/2026/01/02/requirement/part-001.md").exists())
        self.assertNotIn("module fact", (book.directory / "WORKLOG.md").read_text(encoding="utf-8"))

    def test_manager_updates_calendar_with_preserved_registry_and_no_other_owner_writes(self):
        config=json.loads((self.root / "registry.json").read_text(encoding="utf-8"))
        config["calendar_utc_offset_minutes"] = 480
        with self.assertRaises(WorkbookError):
            configure(self.root, config, actor="worker")
        configure(self.root, config, actor="manager")
        self.assertEqual(len(list((self.root / "registry-history").glob("*.json"))), 1)
        book=Workbook(self.root,"worker")
        with patch("workbook.now", return_value="2026-01-01T17:00:00+00:00"):
            book.note("requirement", "local calendar day")
        self.assertTrue((book.directory / "days/2026/01/02/requirement/part-001.md").exists())


if __name__ == "__main__":
    unittest.main()
