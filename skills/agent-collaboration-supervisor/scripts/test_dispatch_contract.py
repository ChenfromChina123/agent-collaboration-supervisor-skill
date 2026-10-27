"""Execution-state and feedback routing through distinct source/executor/ledger fixtures."""
import json
from pathlib import Path
import tempfile
import unittest

from dispatch_contract import ContractError, feedback_recipient, resolve_task_view


class DispatchContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dispatch-contract-test-")
        self.root = Path(self.tmp.name).resolve()
        self.legacy = self.root / "legacy"
        self.card = self.write("product/card.json", {"task_id": "TASK-A", "status": "ready"})
        self.view = self.write("dispatch/tasks/TASK-A.json", {
            "task_id": "TASK-A", "execution_owner": "developer", "ledger_owner": "dispatcher",
            "authority_pointer": str(self.root / "dispatch/tasks/TASK-A.json"),
            "source_card": str(self.card), "authorization_ref": str(self.card), "status": "blocked"})

    def tearDown(self):
        self.assertEqual(self.root.parent, Path(tempfile.gettempdir()).resolve())
        self.assertTrue(self.root.name.startswith("dispatch-contract-test-"))
        self.tmp.cleanup()

    def write(self, name, record):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(record), encoding="utf-8")
        return p

    def test_distinct_executor_and_writer_feedback_uses_current_execution_state(self):
        record = resolve_task_view(self.view, "TASK-A")
        new_with_legacy_context = resolve_task_view(self.view, "TASK-A", legacy_root=self.legacy, legacy_owner="product")
        self.assertEqual(feedback_recipient(new_with_legacy_context, "PROGRESS"), "dispatcher")
        self.assertEqual(record["status"], "blocked")
        self.assertEqual(json.loads(self.card.read_text())["status"], "ready")
        for kind in ("PROGRESS", "BLOCKED", "TEST_REQUEST", "TEST_RESULT"):
            self.assertEqual(feedback_recipient(record, kind), "dispatcher")
        self.assertNotEqual(record["execution_owner"], feedback_recipient(record, "PROGRESS"))
        with self.assertRaises(ContractError):
            feedback_recipient(record, "PRODUCT_QUERY")

    def test_source_card_cannot_be_used_as_execution_authority(self):
        record = json.loads(self.view.read_text())
        record["authority_pointer"] = str(self.card)
        self.write("dispatch/tasks/TASK-A.json", record)
        with self.assertRaises(ContractError):
            resolve_task_view(self.view, "TASK-A")
        with self.assertRaises(ContractError):
            resolve_task_view(self.card, "TASK-A")

    def test_existing_legacy_task_requires_handoff_when_reference_is_omitted(self):
        old = self.write("legacy/tasks/TASK-A.json", {"task_id": "TASK-A", "owner_thread": "developer", "status": "in_progress"})
        before = old.read_bytes()
        self.assertNotIn("handoff_ref", json.loads(self.view.read_text()))
        with self.assertRaises(ContractError):
            resolve_task_view(self.view, "TASK-A", legacy_root=self.legacy, legacy_owner="product")
        self.assertEqual(old.read_bytes(), before)

    def test_legacy_fallback_only_resolves_registered_root_without_rewriting_it(self):
        old = self.write("legacy/tasks/TASK-OLD.json", {"task_id": "TASK-OLD", "owner_thread": "old-worker", "status": "in_progress"})
        before = old.read_bytes()
        result = resolve_task_view(old, "TASK-OLD", legacy_root=self.legacy, legacy_owner="product")
        self.assertEqual(feedback_recipient(result, "PROGRESS"), "product")
        self.assertEqual(result["execution_owner"], "old-worker")
        self.assertEqual(old.read_bytes(), before)
        outside = self.write("unregistered/tasks/TASK-OLD.json", json.loads(before))
        with self.assertRaises(ContractError):
            resolve_task_view(outside, "TASK-OLD", legacy_root=self.legacy, legacy_owner="product")
        with self.assertRaises(ContractError):
            resolve_task_view(old, "../TASK-OLD", legacy_root=self.legacy, legacy_owner="product")

    def test_explicit_handoff_requires_stop_and_accept_and_keeps_other_legacy_owner(self):
        old = self.write("legacy/tasks/TASK-A.json", {"task_id": "TASK-A", "owner_thread": "developer", "status": "paused_for_handoff"})
        untouched = self.write("legacy/tasks/TASK-B.json", {"task_id": "TASK-B", "owner_thread": "other-worker", "status": "in_progress"})
        before = {p: p.read_bytes() for p in (old, untouched)}
        handoff = {"task_id": "TASK-A", "from_ledger_owner": "product", "to_ledger_owner": "dispatcher",
                   "old_authority_pointer": str(old), "new_authority_pointer": str(self.view),
                   "prior_writer_stopped": True, "accepted_by_new_owner": True}
        proof = self.write("product/handoff.json", handoff)
        record = json.loads(self.view.read_text()); record["handoff_ref"] = str(proof)
        self.write("dispatch/tasks/TASK-A.json", record)
        moved = resolve_task_view(self.view, "TASK-A", legacy_root=self.legacy, legacy_owner="product")
        remaining = resolve_task_view(untouched, "TASK-B", legacy_root=self.legacy, legacy_owner="product")
        self.assertEqual(feedback_recipient(moved, "PROGRESS"), "dispatcher")
        self.assertEqual(feedback_recipient(remaining, "PROGRESS"), "product")
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        invalid_values = {"prior_writer_stopped": False, "accepted_by_new_owner": False,
                          "from_ledger_owner": "wrong-prior-writer", "to_ledger_owner": "developer",
                          "task_id": "TASK-B", "old_authority_pointer": str(self.card)}
        for field, value in invalid_values.items():
            invalid = dict(handoff); invalid[field] = value; self.write("product/handoff.json", invalid)
            with self.assertRaises(ContractError):
                resolve_task_view(self.view, "TASK-A", legacy_root=self.legacy, legacy_owner="product")


if __name__ == "__main__":
    unittest.main()
