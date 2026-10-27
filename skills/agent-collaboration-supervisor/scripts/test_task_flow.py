import copy
import unittest
from task_flow import finding_route, intake_action, closure


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.finding = dict(finding_key="AUDIT-runtime-identity", reporter="auditor", evidence_ref="finding.json",
                            environment="production", expected_baseline_ref="least-privilege.md", confirmed=True,
                            within_approved_baseline=True, authorization_ref="approved-bugfix-scope",
                            allowed_scope="local guard and regression", acceptance_ref="acceptance.md", next_owner="developer",
                            decision_gates=dict.fromkeys(("new_product_scope", "public_contract", "data_model",
                                                         "permission_policy", "irreversible_action"), False))
        self.ready = dict(ready_at="2026-01-01T01:00:00+08:00")

    def test_production_finding_can_prepare_local_fix_while_pm_busy(self):
        result = finding_route(self.finding, [])
        self.assertEqual("ready_for_local_engineering", result["action"])
        self.assertFalse(result["production_authorized"])

    def test_real_security_policy_change_needs_human(self):
        self.finding["decision_gates"]["permission_policy"] = True
        self.assertEqual("human_decision", finding_route(self.finding, [])["action"])

    def test_missing_decision_assertion_is_unknown(self):
        del self.finding["decision_gates"]["permission_policy"]
        self.assertEqual("verify_decision_boundary", finding_route(self.finding, [])["action"])

    def test_suspected_finding_is_verified_first(self):
        self.finding["confirmed"] = False
        self.assertEqual("verify", finding_route(self.finding, [])["action"])

    def test_existing_ticket_not_duplicated(self):
        existing = [dict(finding_key=self.finding["finding_key"], task_id="TASK-original",
                         authority_pointer="dispatch/tasks/TASK-original.json", ledger_owner="dispatcher")]
        self.assertEqual("link_existing", finding_route(self.finding, existing)["action"])
        existing.append(copy.deepcopy(existing[0]))
        self.assertEqual("reconcile_duplicate_tickets", finding_route(self.finding, existing)["action"])

    def test_ready_itself_requires_claim_without_new_assign(self):
        self.assertEqual(dict(action="claim_intake", overdue=True), intake_action(self.ready, "2026-01-01T01:06:00+08:00"))

    def test_dispatch_deadline_not_reset_by_late_claim(self):
        self.ready["claimed_at"] = "2026-01-01T01:09:00+08:00"
        self.assertTrue(intake_action(self.ready, "2026-01-01T01:11:00+08:00")["overdue"])

    def test_publish_and_queue_do_not_prove_claim(self):
        self.ready.update(claimed_at="2026-01-01T01:01:00+08:00", assigned_at="2026-01-01T01:02:00+08:00",
                          developer_queued_at="2026-01-01T01:03:00+08:00")
        self.assertEqual("ensure_recipient_scan_and_claim", intake_action(self.ready, "2026-01-01T01:04:00+08:00")["action"])
        self.ready["developer_claimed_at"] = "2026-01-01T01:03:30+08:00"
        self.assertEqual("verify_actual_developer_action", intake_action(self.ready, "2026-01-01T01:04:00+08:00")["action"])

    def test_passive_wait_not_infinite_exemption(self):
        self.ready["wait"] = dict(owner="developer", condition="busy")
        self.assertEqual("repair_wait_record", intake_action(self.ready, "2026-01-01T01:06:00+08:00")["action"])
        self.ready["wait"].update(evidence_ref="current-task.json", recheck_at="2026-01-01T01:08:00+08:00")
        self.assertEqual("known_wait", intake_action(self.ready, "2026-01-01T01:06:00+08:00")["action"])
        self.assertEqual("recheck_wait", intake_action(self.ready, "2026-01-01T01:09:00+08:00")["action"])

    def test_unknown_or_inconsistent_clock_not_fabricated(self):
        self.assertEqual("verify_readiness_time", intake_action({}, "2026-01-01T00:00:00Z")["action"])
        self.ready["claimed_at"] = "2026-01-01T00:50:00+08:00"
        with self.assertRaises(ValueError):
            intake_action(self.ready, "2026-01-01T01:06:00+08:00")
        with self.assertRaises(ValueError):
            intake_action(dict(ready_at="2026-01-01T01:00:00"), "2026-01-01T01:06:00+08:00")

    def test_main_does_not_close_production_defect(self):
        r=dict(task_id="TASK-1", authority_pointer="task.json", ledger_owner="dispatcher", verification_ref="verify.json",
               regression_ref="test.json", resolution="change", source_commit="source", pre_main_commit="pre",
               review_ref="review", main_commit="main", closure_scope="engineering")
        self.assertTrue(closure(r)["closed"])
        r["closure_scope"] = "production"
        self.assertFalse(closure(r)["closed"])
        for k in closure(r)["missing"]:
            r[k] = "proof/" + k
        self.assertTrue(closure(r)["closed"])

    def test_config_only_does_not_force_fake_product_patch(self):
        r=dict(task_id="TASK-1", authority_pointer="task.json", ledger_owner="dispatcher", verification_ref="verify.json",
               regression_ref="test.json", resolution="configuration_only", resolution_ref="minimal-config.json",
               acceptance_ref="accept.json", closure_scope="engineering")
        self.assertTrue(closure(r)["closed"])
        r["closure_scope"] = "production"
        self.assertFalse(closure(r)["closed"])


if __name__ == "__main__":
    unittest.main()
