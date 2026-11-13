"""Hourly recovery of automatic MRP runs left "Queued" by a lost background job (worker down, Redis restarted, job killed).
Fixtures live in the test transaction, which is rolled back; the settings the tests depend on are set explicitly."""

from unittest import mock

import frappe
from frappe.utils import add_to_date, now_datetime

from ppc_production_control import tasks
from ppc_production_control.services import mrp, mrp_auto
from ppc_production_control.tests.test_mrp import MRPTestCase
from ppc_production_control.tests.utils import use_mrp_settings


class TestStuckRunRecovery(MRPTestCase):
	def setUp(self):
		super().setUp()
		use_mrp_settings(mrp_enabled=1, mrp_auto_create_drafts=0, mrp_max_retries=2)
		patcher = mock.patch.object(mrp_auto, "_notify")
		self.notify = patcher.start()
		self.addCleanup(patcher.stop)

	def queued_run(self, so=None, age_minutes=120, trigger="Automatic"):
		"""What a lost job leaves behind: the run exists (created before the calculation) and is still Queued."""
		so = so or self.so(delivery_in_days=30)
		run = mrp_auto._new_run(frappe.get_doc("Sales Order", so.name), mrp_auto.idempotency_key("Sales Order", so.name))
		run.default_warehouse = self.wh
		run.trigger = trigger
		run.insert(ignore_permissions=True)
		frappe.db.set_value(mrp.RUN_DT, run.name, "modified", add_to_date(now_datetime(), minutes=-age_minutes), update_modified=False)
		return run.name, so

	def status(self, run):
		return frappe.db.get_value(mrp.RUN_DT, run, ["status", "retry_count", "approval_status"], as_dict=True)

	def documents_of(self, run):
		return [frappe.get_all(dt, filters={"ppc_mrp_run": run}) for dt in ("Material Request", "Work Order")]

	def test_a_run_stuck_in_queued_is_calculated_again(self):
		run, so = self.queued_run()
		out = mrp_auto.recover_stuck_runs()
		self.assertEqual(out["requeued"], [run])
		after = self.status(run)
		self.assertEqual((after.status, after.approval_status, after.retry_count), ("Calculated", "Awaiting Review", 1))
		self.assertEqual(self.documents_of(run), [[], []], "recovery only calculates; nothing is created or submitted")
		self.assertTrue(frappe.db.exists("PPC Audit Log", {"action": mrp.A_RETRY, "details": ["like", f"%{run}%"]}))

	def test_a_recent_queued_run_is_left_alone(self):
		run, _so = self.queued_run(age_minutes=5)
		out = mrp_auto.recover_stuck_runs()
		self.assertEqual((out["checked"], self.status(run).status), (0, "Queued"))

	def test_recovery_is_idempotent(self):
		run, _so = self.queued_run()
		mrp_auto.recover_stuck_runs()
		again = mrp_auto.recover_stuck_runs()
		self.assertEqual((again["checked"], again["requeued"]), (0, []))
		self.assertEqual(frappe.db.count(mrp.RUN_DT, {"idempotency_key": mrp_auto.idempotency_key("Sales Order", frappe.db.get_value(mrp.RUN_DT, run, "source_sales_order"))}), 1)
		self.assertEqual(self.status(run).retry_count, 1)

	def test_a_cancelled_review_is_never_recovered(self):
		run, _so = self.queued_run()
		frappe.db.set_value(mrp.RUN_DT, run, "approval_status", "Cancelled", update_modified=False)
		self.assertEqual(mrp_auto.recover_stuck_runs()["checked"], 0)
		self.assertEqual(self.status(run).status, "Queued")

	def test_manual_runs_are_not_touched(self):
		run, _so = self.queued_run(trigger="Manual")
		self.assertEqual(mrp_auto.recover_stuck_runs()["checked"], 0)
		self.assertEqual(self.status(run).status, "Queued")

	def test_when_the_retries_are_used_up_the_run_fails_visibly_instead_of_looping(self):
		run, _so = self.queued_run()
		frappe.db.set_value(mrp.RUN_DT, run, "retry_count", 2, update_modified=False)
		out = mrp_auto.recover_stuck_runs()
		self.assertEqual((out["failed"], out["requeued"]), ([run], []))
		after = frappe.db.get_value(mrp.RUN_DT, run, ["status", "error_message"], as_dict=True)
		self.assertEqual(after.status, "Failed")
		self.assertIn("Stuck in Queued", after.error_message)
		self.notify.assert_called()

	def test_nothing_happens_when_automatic_mrp_is_off(self):
		use_mrp_settings(mrp_enabled=0)
		run, _so = self.queued_run()
		self.assertEqual(mrp_auto.recover_stuck_runs()["checked"], 0)
		self.assertEqual(self.status(run).status, "Queued")

	def test_the_scheduler_task_wraps_the_service_and_is_registered_hourly(self):
		run, _so = self.queued_run()
		tasks.recover_stuck_mrp_runs()
		self.assertEqual(self.status(run).status, "Calculated")
		hooks = frappe.get_hooks("scheduler_events", app_name="ppc_production_control")
		self.assertIn("ppc_production_control.tasks.recover_stuck_mrp_runs", hooks.get("hourly", []))

	def test_a_late_duplicate_job_cannot_recalculate_a_run_a_person_already_reviewed(self):
		run, _so = self.queued_run()
		mrp_auto.recover_stuck_runs()
		line = next(r for r in frappe.get_doc(mrp.RUN_DT, run).lines if r.action in mrp.CREATABLE and r.net_qty > 0)
		from ppc_production_control.services import mrp_approval

		mrp_approval.skip_line(run, line.name, "not needed this month")
		mrp_auto.retry_run(run)  # the worker finally delivers the old job
		doc = frappe.get_doc(mrp.RUN_DT, run)
		self.assertEqual(doc.status, "Calculated")
		self.assertEqual(next(r for r in doc.lines if r.name == line.name).skip_reason, "not needed this month", "the review must survive a late job")

	def test_a_failing_recovery_is_logged_and_does_not_stop_the_others(self):
		first, _so1 = self.queued_run()
		second, _so2 = self.queued_run(self.so(delivery_in_days=40))
		real = mrp_auto.enqueue_once

		def flaky(method, job_id, **kw):
			if first in job_id:
				raise RuntimeError("queue unreachable")
			return real(method, job_id, **kw)

		with mock.patch.object(mrp_auto, "enqueue_once", flaky), mock.patch("frappe.log_error") as log:
			out = mrp_auto.recover_stuck_runs()
		self.assertEqual(out["requeued"], [second])
		self.assertTrue(log.called)
		self.assertEqual(self.status(second).status, "Calculated")
