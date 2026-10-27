"""Derived-view failures cannot turn committed journal actions into replay requests."""
import copy
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import workbook as wb


class ProjectionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workbook-projection-test-")
        self.root = Path(self.temp.name).resolve()
        self.config = {"manager": "owner", "agents": {"owner": {"role": "owner"}},
                       "allowed_routes": [["owner", "owner"]]}
        wb.configure(self.root, self.config)
        self.book = wb.Workbook(self.root, "owner")
        self.book.init()
        self.book.note("work", "before")
        self.page = next((self.book.directory / "days").rglob("work/part-001.md"))

    def tearDown(self):
        self.assertEqual(self.root.parent, Path(tempfile.gettempdir()).resolve())
        self.assertTrue(self.root.name.startswith("workbook-projection-test-"))
        self.temp.cleanup()

    def test_identical_views_are_not_replaced_or_rejournaled(self):
        before = self.book._events()
        with patch.object(wb.os, "replace", side_effect=AssertionError("unchanged view must not be rewritten")):
            result = self.book.init()
        self.assertTrue(result["initialized"])
        self.assertEqual(self.book._events(), before)

    def test_transient_projection_denial_retries_only_view_without_duplicate_event(self):
        original = wb.os.replace
        attempts = []
        def replace(source, target):
            if Path(target) == self.page:
                attempts.append(target)
                if len(attempts) < 3:
                    raise PermissionError(errno.EACCES, "fixture sharing denial")
            return original(source, target)
        count = len(self.book._events())
        with patch.object(wb.os, "replace", side_effect=replace), patch.object(wb.time, "sleep") as sleep:
            result = self.book.note("work", "after")
        self.assertTrue(result["recorded"])
        self.assertNotIn("projection_status", result)
        self.assertEqual(len(attempts), 3)
        self.assertEqual([x.args[0] for x in sleep.call_args_list], [0.05, 0.15])
        self.assertEqual(len(self.book._events()), count + 1)
        self.assertIn("after", self.page.read_text(encoding="utf-8"))

    def test_persistent_projection_failure_reports_commit_and_rebuild_does_not_replay(self):
        original = wb.os.replace
        before_page = self.page.read_bytes()
        count = len(self.book._events())
        def denied(source, target):
            if Path(target) == self.page:
                raise PermissionError(errno.EACCES, "fixture sharing denial")
            return original(source, target)
        with patch.object(wb.os, "replace", side_effect=denied), patch.object(wb.time, "sleep"):
            result = self.book.note("work", "committed-once")
            self.assertEqual(result["operation_status"], "committed_projection_pending")
            self.assertTrue(result["canonical_event_committed"])
            self.assertEqual(result["committed_event_sequences"], [count + 1])
            self.assertEqual(self.page.read_bytes(), before_page)
            rebuild = self.book.init()
            self.assertEqual(rebuild["operation_status"], "projection_pending_no_new_event")
            self.assertFalse(rebuild["canonical_event_committed"])
        self.book.init()
        self.assertEqual(len(self.book._events()), count + 1)
        self.assertEqual(sum(e["data"].get("text") == "committed-once" for e in self.book._events()), 1)
        self.assertIn("committed-once", self.page.read_text(encoding="utf-8"))

    def test_registry_replace_is_not_retried_or_downgraded_to_projection_warning(self):
        before = (self.root / "registry.json").read_bytes()
        changed = copy.deepcopy(self.config); changed["calendar_utc_offset_minutes"] = 480
        with patch.object(wb.os, "replace", side_effect=PermissionError(errno.EACCES, "critical registry denied")) as replace, patch.object(wb.time, "sleep") as sleep:
            with self.assertRaises(PermissionError):
                wb.configure(self.root, changed, actor="owner")
        self.assertEqual(replace.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual((self.root / "registry.json").read_bytes(), before)

    def test_scan_final_refresh_clears_repaired_warning_without_requeue(self):
        def message(key):
            return {"message_id": "MSG-" + key, "work_item_id": "WORK-" + key, "task_id": "TASK-" + key,
                    "sender": "owner", "recipient": "owner", "type": "TASK", "requires_action": True,
                    "created_at": wb.now(), "summary": "fixture", "next_action": "local fixture only",
                    "authorization_ref": "isolated fixture"}
        self.book.send(message("FIRST")); self.book.scan()
        page = next((self.book.directory / "days").rglob("inbox/part-001.md"))
        self.book.send(message("SECOND"))
        original = wb.os.replace; failures = []
        def replace(source, target):
            if Path(target) == page and len(failures) < 3:
                failures.append(target)
                raise PermissionError(errno.EACCES, "fixture reader released before final refresh")
            return original(source, target)
        with patch.object(wb.os, "replace", side_effect=replace), patch.object(wb.time, "sleep"):
            result = self.book.scan()
        self.assertEqual(result["pending_count"], 2)
        self.assertNotIn("projection_status", result)
        self.assertEqual(sum(e["kind"] == "queued" for e in self.book._events()), 2)
        self.assertIn("MSG-SECOND", page.read_text(encoding="utf-8"))

    def test_canonical_read_failure_is_not_treated_as_optional_projection(self):
        original = wb.read
        def denied(path):
            if "events" in path.parts:
                raise PermissionError(errno.EACCES, "canonical journal unreadable")
            return original(path)
        count = len(self.book._events())
        with patch.object(wb, "read", side_effect=denied):
            with self.assertRaises(PermissionError):
                self.book.note("work", "must-not-be-committed")
        self.assertEqual(len(self.book._events()), count)

    @unittest.skipUnless(os.name == "nt", "requires actual Windows file sharing semantics")
    def test_real_windows_reader_handle_cli_claim_commit_status_and_owner_repair(self):
        import ctypes
        from ctypes import wintypes
        message = {"message_id": "MSG-LOCKED", "work_item_id": "WORK-LOCKED", "task_id": "TASK-LOCKED",
                   "sender": "owner", "recipient": "owner", "type": "TASK", "requires_action": True,
                   "created_at": wb.now(), "summary": "controlled fixture", "next_action": "local fixture only",
                   "authorization_ref": "isolated fixture, no external work"}
        self.book.send(message); self.book.scan()
        count = len(self.book._events())
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]; kernel.CloseHandle.restype = wintypes.BOOL
        # This test owns the reader handle; no user application/process is closed.
        handle = kernel.CreateFileW(str(self.page), 0x80000000, 0x1 | 0x2, None, 3, 0x80, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        def cli(*args):
            cmd = [sys.executable, "-B", str(Path(wb.__file__).resolve()), "--root", str(self.root), "--actor", "owner", *args]
            result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)
        try:
            result = cli("claim", "--message-id", message["message_id"])
            self.assertEqual(result["status"], "claimed")
            self.assertTrue(result["canonical_event_committed"])
            self.assertEqual(result["projection_status"], "pending")
            self.assertEqual(cli("status")["active_work_item"], message["work_item_id"])
            self.assertEqual(cli("init")["operation_status"], "projection_pending_no_new_event")
        finally:
            self.assertTrue(kernel.CloseHandle(handle))
        repaired = cli("init")
        self.assertNotIn("projection_status", repaired)
        self.assertEqual(len(self.book._events()), count + 1)
        self.assertEqual(sum(e["kind"] == "claimed" for e in self.book._events()), 1)
        self.assertIn(message["work_item_id"], self.page.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
