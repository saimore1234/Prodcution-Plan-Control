"""MRP review and approval (services/mrp_approval.py): Sales Order -> automatic run -> review -> approval -> draft documents.

Every test sets the PPC Settings it needs (`use_mrp_settings`) and builds its own fixtures inside the test's savepoint, which is
rolled back: no existing document of the site is read as a fixture or changed."""

import json
import uuid
from unittest import mock

import frappe
from frappe.utils import add_days, nowdate

from ppc_production_control import api
from ppc_production_control.services import mrp, mrp_approval, mrp_auto
from ppc_production_control.tests.test_api_security import ERP_MFG, make_user
from ppc_production_control.tests.test_mrp import MRPTestCase
from ppc_production_control.tests.utils import ensure_stock, make_plan, use_mrp_settings

RUN = mrp.RUN_DT


class ApprovalCase(MRPTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.viewer = make_user("apviewer", ["PPC Viewer", *ERP_MFG])
		cls.planner = make_user("applanner", ["Production Planner", *ERP_MFG])
		cls.outsider = make_user("apoutsider", ["Sales User"])

	def setUp(self):
		super().setUp()  # the recommended settings, whatever the site saved
		use_mrp_settings(mrp_default_warehouse=self.wh)

	# ---- helpers
	def auto_on(self):
		frappe.flags.ppc_mrp_auto_in_test = True  # let the Sales Order / Production Plan hook run its job inline, as a worker would

	def tearDown(self):
		frappe.flags.ppc_mrp_auto_in_test = False
		super().tearDown()

	def run_of(self, doctype, name):
		return frappe.db.get_value(RUN, {"idempotency_key": mrp_auto.idempotency_key(doctype, name)}, "name")

	def docs(self, run):
		mrs = frappe.get_all("Material Request", filters={"ppc_mrp_run": run}, fields=["name", "docstatus", "ppc_source_documents"])
		wos = frappe.get_all("Work Order", filters={"ppc_mrp_run": run}, fields=["name", "docstatus", "production_item", "qty", "ppc_source_documents"])
		return mrs, wos

	def token(self, run):
		name = run if isinstance(run, str) else run.name
		return mrp.review_data(name)["token"]

	def key(self):
		return f"key-{uuid.uuid4()}"

	def approve(self, run, *, key=None, token=None, **kw):
		name = run if isinstance(run, str) else run.name
		return mrp_approval.approve_and_create(name, key or self.key(), token or self.token(name), confirm=1, **kw)

	def line(self, run, item):
		run = frappe.get_doc(RUN, run if isinstance(run, str) else run.name)
		return self.one(run, item)

	def so_name_of(self, run):
		return frappe.get_all("PPC MRP Source", filters={"parent": run.name}, pluck="source_name")[0]

	def audit(self, run, action=None):
		rows = frappe.get_all("PPC Audit Log", filters=[["details", "like", f'%"{run}"%']] + ([["action", "=", action]] if action else []),
			fields=["name", "action", "result", "user", "timestamp", "reason", "error_details", "details"], order_by="creation asc")
		for r in rows:
			r["d"] = json.loads(r.details or "{}")
		return rows

	def as_user(self, user):
		frappe.set_user(user)

	def reviewed_run(self, **so_kw):
		"""A manual run for a fresh Sales Order of the manufactured item: Work Orders for mfg + sub, Material Request lines for rma + rmp."""
		so = self.so(delivery_in_days=30, **so_kw)
		return so, self.run_for(so.name)


class TestTrigger(ApprovalCase):
	def test_submitting_a_sales_order_starts_an_automatic_run_that_waits_for_review(self):
		self.auto_on()
		so = self.so(delivery_in_days=30)
		run = self.run_of("Sales Order", so.name)
		doc = frappe.get_doc(RUN, run)
		self.assertEqual((doc.trigger, doc.status, doc.approval_status, doc.source_sales_order), ("Automatic", "Calculated", "Awaiting Review", so.name))
		self.assertEqual(self.docs(run), ([], []), "Review First: nothing is created before an approval")

	def test_the_calculation_tells_the_browser_it_is_done(self):
		self.auto_on()
		with mock.patch("frappe.publish_realtime") as publish:
			so = self.so()
		events = [c for c in publish.call_args_list if c.args and c.args[0] == "ppc_mrp_ready"]
		self.assertEqual(len(events), 1)
		self.assertEqual((events[0].args[1]["source_name"], events[0].args[1]["status"]), (so.name, "Calculated"))


class TestPopupStates(ApprovalCase):
	def test_no_run_yet_is_waiting_and_never_completed(self):
		so = self.so()  # the hook is off in tests unless asked: the job has not run
		s = mrp_approval.run_for_source("Sales Order", so.name)
		self.assertEqual((s["enabled"], s["state"], s["run"]), (True, "waiting", None))

	def test_a_queued_run_is_calculating_not_calculated(self):
		so = self.so()
		run = frappe.get_doc(mrp_auto._new_run(frappe.get_doc("Sales Order", so.name), mrp_auto.idempotency_key("Sales Order", so.name)))
		run.default_warehouse = self.wh
		run.insert(ignore_permissions=True)
		s = mrp_approval.run_for_source("Sales Order", so.name)
		self.assertEqual((s["state"], s["status"]), ("calculating", "Queued"))
		self.assertNotIn(s["state"], ("calculated", "approved"))

	def test_a_calculated_run_is_calculated_and_a_failed_one_says_why(self):
		self.auto_on()
		so = self.so()
		self.assertEqual(mrp_approval.run_for_source("Sales Order", so.name)["state"], "calculated")
		run = self.run_of("Sales Order", so.name)
		mrp.mark_failed(run, "boom")
		frappe.db.set_value(RUN, run, "status", "Failed")
		s = mrp_approval.run_for_source("Sales Order", so.name)
		self.assertEqual((s["state"], s["error"]), ("failed", "boom"))

	def test_the_dialog_stays_closed_when_automatic_mrp_is_off_or_the_order_is_not_submitted(self):
		so = self.so()
		use_mrp_settings(mrp_auto_on_sales_order=0)
		self.assertEqual(mrp_approval.run_for_source("Sales Order", so.name), {"enabled": False, "reason": "automatic_off"})
		use_mrp_settings(mrp_enabled=0)
		self.assertFalse(mrp_approval.run_for_source("Sales Order", so.name)["enabled"])
		use_mrp_settings()
		draft = self.so(submit=False)
		self.assertEqual(mrp_approval.run_for_source("Sales Order", draft.name)["state"], "not_submitted")

	def test_a_site_without_the_approval_fields_answers_quietly(self):
		so = self.so()
		with mock.patch.object(mrp, "approval_model_ready", return_value=False):
			self.assertEqual(mrp_approval.run_for_source("Sales Order", so.name), {"enabled": False, "reason": "not_migrated"})
			with self.assertRaises(frappe.ValidationError):
				mrp_approval.skip_line("x", "y", "because")
			_so, run = self.reviewed_run()
			data = mrp.review_data(run.name)  # the page's data still works (classic selection)
			self.assertFalse(data["approval_ready"])

	def test_only_a_source_document_the_user_may_read_is_answered(self):
		so = self.so()
		self.as_user(self.outsider)
		with self.assertRaises(frappe.PermissionError):
			mrp_approval.run_for_source("Sales Order", so.name)


class TestCalculateNow(ApprovalCase):
	def test_calculate_now_creates_one_run_and_calculates_it_once(self):
		so = self.so()  # no worker: nothing has run
		first = mrp_approval.calculate_now("Sales Order", so.name)
		self.assertEqual((first["state"], first["calculated_here"]), ("calculated", True))
		second = mrp_approval.calculate_now("Sales Order", so.name)
		self.assertEqual((second["state"], second["run"]), ("calculated", first["run"]))
		self.assertEqual(frappe.db.count(RUN, {"idempotency_key": mrp_auto.idempotency_key("Sales Order", so.name)}), 1)
		self.assertEqual(len(self.audit(first["run"], mrp.A_AUTO)), 1, "calculated exactly once")

	def test_the_queued_job_that_comes_later_finds_the_run_and_does_nothing(self):
		so = self.so()
		run = mrp_approval.calculate_now("Sales Order", so.name)["run"]
		mrp_auto.run_for_source("Sales Order", so.name)  # the job a worker finally picks up
		self.assertEqual(frappe.db.count(RUN, {"source_sales_order": so.name}), 1, "no second run")
		rows = self.audit(run, mrp.A_AUTO)
		self.assertEqual([r.result for r in rows], ["Success", "Skipped"], "calculated once; the late job only noted that the run exists")
		self.assertEqual(rows[1].d["reason"], "this document already has an automatic run")

	def test_a_queued_run_is_calculated_by_calculate_now(self):
		so = self.so()
		run = frappe.get_doc(mrp_auto._new_run(frappe.get_doc("Sales Order", so.name), mrp_auto.idempotency_key("Sales Order", so.name)))
		run.default_warehouse = self.wh
		run.insert(ignore_permissions=True)
		out = mrp_approval.calculate_now("Sales Order", so.name)
		self.assertEqual((out["state"], out["run"]), ("calculated", run.name))

	def test_a_user_who_may_only_view_cannot_calculate(self):
		so = self.so()
		self.as_user(self.viewer)
		with self.assertRaises(frappe.PermissionError):
			mrp_approval.calculate_now("Sales Order", so.name)

	def test_calculate_now_is_audited(self):
		so = self.so()
		run = mrp_approval.calculate_now("Sales Order", so.name)["run"]
		(row,) = self.audit(run, "MRP Calculate Now")
		self.assertEqual((row.d["run"], row.d["source"], row.user), (run, [["Sales Order", so.name]], "Administrator"))


class TestApproval(ApprovalCase):
	def test_nothing_is_created_before_the_approval(self):
		so, run = self.reviewed_run()
		self.assertEqual(self.docs(run.name), ([], []))
		mrp_approval.revise_line(run.name, self.line(run, self.rma).name, 25)
		mrp_approval.skip_line(run.name, self.line(run, self.rmp).name, "bought elsewhere")
		self.assertEqual(self.docs(run.name), ([], []), "reviewing, revising and skipping create nothing")

	def test_approval_creates_draft_material_requests_and_work_orders_with_their_links(self):
		so, run = self.reviewed_run()
		out = self.approve(run)
		mrs, wos = self.docs(run.name)
		self.assertTrue(mrs and wos)
		self.assertTrue(all(d.docstatus == 0 for d in mrs + wos), "everything generated stays a draft")
		self.assertEqual({w.production_item: w.qty for w in wos}, {self.mfg: 10, self.sub: 10})  # manufacture -> Work Orders
		mr = frappe.get_doc("Material Request", mrs[0].name)
		self.assertEqual({i.item_code: i.qty for i in mr.items}, {self.rma: 20, self.rmp: 30})  # buy -> Material Request
		self.assertTrue(all(so.name in (d.ppc_source_documents or "") for d in mrs + wos))
		run.reload()
		self.assertEqual((run.status, run.approval_status, run.approved_by, bool(run.approved_on), bool(run.approval_key)), ("Documents Created", "Approved", "Administrator", True, True))
		self.assertEqual(sorted(d["name"] for d in out["documents"]), sorted(r.document for r in run.lines if r.document))

	def test_a_purchase_only_order_creates_only_a_material_request(self):
		so = self.so([(self.buy, 6)])
		run = self.run_for(so.name)
		self.approve(run)
		mrs, wos = self.docs(run.name)
		self.assertEqual((len(mrs), wos), (1, []))

	def test_nothing_is_ever_submitted(self):
		_so, run = self.reviewed_run()
		self.approve(run)
		for doc in sum(self.docs(run.name), []):
			self.assertEqual(doc.docstatus, 0)

	def test_the_documents_are_made_from_what_is_short_now(self):
		so = self.so([(self.buy, 10)])
		run = self.run_for(so.name)
		ensure_stock(self.buy, self.wh, 4)  # stock arrives after the review was opened
		out = self.approve(run)
		mr = frappe.get_doc("Material Request", self.docs(run.name)[0][0].name)
		self.assertEqual(mr.items[0].qty, 6)
		self.assertTrue(out["created"])

	def test_if_nothing_is_short_any_more_nothing_is_created_and_the_review_stays_open(self):
		so = self.so([(self.buy, 10)])
		run = self.run_for(so.name)
		ensure_stock(self.buy, self.wh, 500)
		out = self.approve(run)
		self.assertEqual((out["created"], self.docs(run.name)), ([], ([], [])))
		run.reload()
		self.assertEqual((run.status, run.approval_status, run.approval_key), ("Calculated", "Awaiting Review", None))

	def test_the_old_selection_api_still_works_and_stamps_the_approval(self):
		_so, run = self.reviewed_run()
		out = mrp.create_documents(run.name, None, confirm=1)
		self.assertTrue(out["created"])
		run.reload()
		self.assertEqual((run.approval_status, run.approved_by, run.approval_key), ("Approved", "Administrator", None))

	def test_review_data_keeps_every_1_7_0_key_and_adds_the_new_ones(self):
		_so, run = self.reviewed_run()
		d = mrp.review_data(run.name)
		for key in ("run", "status", "company", "trigger", "source_sales_order", "calculated_on", "warnings", "error_message", "material_request", "can_edit", "can_retry", "max_edit_factor", "lines"):
			self.assertIn(key, d)
		for key in ("approval_status", "token", "approval", "permissions", "sources", "kpis", "expected_completion", "documents", "history", "approval_ready"):
			self.assertIn(key, d)
		row = next(r for r in d["lines"] if r["item_code"] == self.mfg)
		for key in ("name", "net_qty", "approved_qty", "editable", "original_qty", "skip_reason", "stock_by_warehouse", "bom", "supply_documents"):
			self.assertIn(key, row)
		self.assertEqual(d["kpis"]["shortage_qty"], sum(r["net_qty"] for r in d["lines"]))
		self.assertEqual(d["sources"][0]["name"], self.so_name_of(run))

	def test_the_review_shows_existing_supply_and_whose_it_is(self):
		so, run0 = self.reviewed_run()
		other = self.so()
		wo = frappe.get_doc({"doctype": "Work Order", "company": self.company, "production_item": self.mfg, "bom_no": frappe.db.get_value("BOM", {"item": self.mfg, "is_default": 1, "docstatus": 1}),
			"qty": 3, "stock_uom": "Nos", "fg_warehouse": self.wh, "wip_warehouse": self.wip, "use_multi_level_bom": 0, "planned_start_date": f"{nowdate()} 00:00:00", "sales_order": other.name}).insert()
		run = self.run_for(so.name)
		row = next(r for r in mrp.review_data(run.name)["lines"] if r["item_code"] == self.mfg)
		(doc,) = row["supply_documents"]
		self.assertEqual((doc["name"], doc["relation"], doc["qty"], doc["draft"]), (wo.name, "others", 3, True))


class TestRevise(ApprovalCase):
	def test_a_revision_changes_the_quantity_and_keeps_the_original(self):
		_so, run = self.reviewed_run()
		rma = self.line(run, self.rma)
		before = self.token(run)
		out = mrp_approval.revise_line(run.name, rma.name, 25)
		rma = self.line(run, self.rma)
		self.assertEqual((rma.approved_qty, rma.original_qty, rma.net_qty, rma.revised_by), (25, 20, 20, "Administrator"))
		self.assertNotEqual(out["token"], before, "a revision changes what a review is, so an old review can no longer be approved")
		self.assertEqual(out["token"], self.token(run))
		self.approve(run)
		mr = frappe.get_doc("Material Request", self.docs(run.name)[0][0].name)
		self.assertEqual({i.item_code: i.qty for i in mr.items}[self.rma], 25)

	def test_a_revision_follows_the_review_rules(self):
		_so, run = self.reviewed_run()
		rma = self.line(run, self.rma).name
		for qty, fragment in ((0, "greater than zero"), (-3, "greater than zero"), (1.5, "whole number"), (20 * mrp.MAX_EDIT_FACTOR + 1, "may not exceed")):
			with self.assertRaises(frappe.ValidationError) as ctx:
				mrp_approval.revise_line(run.name, rma, qty)
			self.assertIn(fragment, str(ctx.exception))
		self.assertEqual(self.line(run, self.rma).approved_qty, 20, "a refused revision changes nothing")

	def test_revisions_are_audited_with_the_original_and_the_revised_quantity(self):
		_so, run = self.reviewed_run()
		mrp_approval.revise_line(run.name, self.line(run, self.rma).name, 24)
		(row,) = self.audit(run.name, "MRP Line Revised")
		d = row.d
		self.assertEqual((d["item_code"], d["original_qty"], d["revised_qty"], d["previous_qty"], d["run"], d["source"]), (self.rma, 20, 24, 20, run.name, [["Sales Order", self.so_name_of(run)]]))
		self.assertEqual((row.user, bool(row.timestamp)), ("Administrator", True))

	def test_a_revision_of_a_line_with_nothing_to_create_is_refused(self):
		so = self.so([(self.buy, 5)])
		ensure_stock(self.buy, self.wh, 500)
		run = self.run_for(so.name)
		with self.assertRaises(frappe.ValidationError):
			mrp_approval.revise_line(run.name, self.line(run, self.buy).name, 3)

	def test_a_skipped_line_that_is_revised_is_wanted_again(self):
		_so, run = self.reviewed_run()
		rmp = self.line(run, self.rmp).name
		mrp_approval.skip_line(run.name, rmp, "not needed this week")
		mrp_approval.revise_line(run.name, rmp, 30)
		row = self.line(run, self.rmp)
		self.assertEqual((row.create_document, row.skip_reason), (1, None))


class TestSkip(ApprovalCase):
	def test_a_skip_needs_a_reason(self):
		_so, run = self.reviewed_run()
		rma = self.line(run, self.rma).name
		for reason in (None, "", "  ", "ab"):
			with self.assertRaises(frappe.ValidationError):
				mrp_approval.skip_line(run.name, rma, reason)
		self.assertEqual(self.line(run, self.rma).create_document, 1)

	def test_a_skipped_item_is_left_out_with_its_reason_and_audited(self):
		_so, run = self.reviewed_run()
		rma = self.line(run, self.rma)
		mrp_approval.skip_line(run.name, rma.name, "customer supplies it")
		row = self.line(run, self.rma)
		self.assertEqual((row.create_document, row.skip_reason), (0, "customer supplies it"))
		self.approve(run)
		mr = frappe.get_doc("Material Request", self.docs(run.name)[0][0].name)
		self.assertEqual({i.item_code for i in mr.items}, {self.rmp})
		(entry,) = self.audit(run.name, "MRP Line Skipped")
		self.assertEqual((entry.reason, entry.d["item_code"], entry.d["original_qty"], entry.d["reason"]), ("customer supplies it", self.rma, 20, "customer supplies it"))

	def test_an_item_left_unticked_without_a_reason_blocks_the_approval(self):
		_so, run = self.reviewed_run()
		rma = self.line(run, self.rma)
		mrp.save_review(run.name, [{"name": rma.name, "create_document": 0, "approved_qty": rma.approved_qty}])  # the old selection API takes no reason
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.approve(run)
		self.assertIn(self.rma, str(ctx.exception))
		self.assertEqual(self.docs(run.name), ([], []))
		self.assertTrue(next(r for r in mrp.review_data(run.name)["lines"] if r["item_code"] == self.rma)["needs_reason"])

	def test_there_is_nothing_to_skip_on_a_covered_line(self):
		so = self.so([(self.buy, 5)])
		ensure_stock(self.buy, self.wh, 500)
		run = self.run_for(so.name)
		with self.assertRaises(frappe.ValidationError):
			mrp_approval.skip_line(run.name, self.line(run, self.buy).name, "no need")


class TestCancel(ApprovalCase):
	def test_cancel_needs_a_reason_and_creates_nothing(self):
		_so, run = self.reviewed_run()
		with self.assertRaises(frappe.ValidationError):
			mrp_approval.cancel_run(run.name, "")
		mrp_approval.cancel_run(run.name, "order was entered twice")
		run.reload()
		self.assertEqual((run.approval_status, run.cancel_reason, run.status, run.reviewed_by), ("Cancelled", "order was entered twice", "Calculated", "Administrator"))
		self.assertEqual(self.docs(run.name), ([], []))
		(row,) = self.audit(run.name, "MRP Review Cancelled")
		self.assertEqual((row.reason, row.d["approval_status"]), ("order was entered twice", "Cancelled"))

	def test_nothing_can_be_approved_revised_skipped_or_created_after_a_cancellation(self):
		_so, run = self.reviewed_run()
		token = self.token(run)
		line = self.line(run, self.rma).name
		mrp_approval.cancel_run(run.name, "not needed")
		for call in (
			lambda: mrp_approval.approve_and_create(run.name, self.key(), self.token(run), confirm=1),
			lambda: mrp_approval.revise_line(run.name, line, 21),
			lambda: mrp_approval.skip_line(run.name, line, "whatever"),
			lambda: mrp.create_documents(run.name, None, confirm=1),
			lambda: mrp.save_review(run.name, []),
		):
			with self.assertRaises(frappe.ValidationError):
				call()
		self.assertEqual(self.docs(run.name), ([], []))
		refusal = [r for r in self.audit(run.name, "MRP Approved") if r.result == "Skipped"]
		self.assertTrue(refusal and refusal[0].d.get("rejected"), "the refused approval is on record")
		self.assertFalse(mrp.review_data(run.name)["permissions"]["can_approve"])

	def test_cancelling_twice_is_harmless_and_cancelling_after_approval_is_refused(self):
		_so, run = self.reviewed_run()
		mrp_approval.cancel_run(run.name, "first")
		self.assertTrue(mrp_approval.cancel_run(run.name, "second")["already_cancelled"])
		_so2, run2 = self.reviewed_run()
		self.approve(run2)
		with self.assertRaises(frappe.ValidationError):
			mrp_approval.cancel_run(run2.name, "too late")

	def test_a_cancelled_run_is_not_calculated_by_a_late_job_and_the_manual_button_points_to_recalculate(self):
		_so, run = self.reviewed_run()
		mrp_approval.cancel_run(run.name, "not needed")
		mrp_auto.process_run(run.name)  # a retry / late job
		self.assertEqual(frappe.db.get_value(RUN, run.name, ["status", "approval_status"]), ("Calculated", "Cancelled"))
		with self.assertRaises(frappe.ValidationError):
			mrp.calculate(run.name)

	def test_recalculate_reopens_a_cancelled_review(self):
		_so, run = self.reviewed_run()
		mrp_approval.cancel_run(run.name, "mistake")
		out = mrp_approval.recalculate(run.name)
		self.assertTrue(out["reopened"])
		run.reload()
		self.assertEqual((run.approval_status, run.cancel_reason), ("Awaiting Review", None))
		self.approve(run)
		self.assertTrue(self.docs(run.name)[1])


class TestRecalculate(ApprovalCase):
	def test_recalculation_uses_todays_stock_and_discards_revisions_and_skips(self):
		so = self.so([(self.buy, 10)])
		run = self.run_for(so.name)
		mrp_approval.revise_line(run.name, self.line(run, self.buy).name, 12)
		ensure_stock(self.buy, self.wh, 4)
		out = mrp_approval.recalculate(run.name)
		row = self.line(run, self.buy)
		self.assertEqual((row.net_qty, row.approved_qty, row.original_qty, row.revised_by), (6, 6, 6, None))
		self.assertEqual(frappe.db.get_value(RUN, run.name, "approval_status"), "Awaiting Review")
		(entry,) = self.audit(run.name, "MRP Recalculated")
		self.assertEqual((entry.d["discarded_revisions_or_skips"], entry.d["reopened"]), (1, False))

	def test_recalculation_never_creates_documents_twice_or_after_approval(self):
		_so, run = self.reviewed_run()
		mrp_approval.recalculate(run.name)
		mrp_approval.recalculate(run.name)
		self.assertEqual(self.docs(run.name), ([], []))
		self.approve(run)
		count = sum(len(x) for x in self.docs(run.name))
		with self.assertRaises(frappe.ValidationError):
			mrp_approval.recalculate(run.name)
		self.assertEqual(sum(len(x) for x in self.docs(run.name)), count)

	def test_a_recalculation_changes_the_token_so_an_old_review_cannot_be_approved(self):
		_so, run = self.reviewed_run()
		old = self.token(run)
		mrp_approval.recalculate(run.name)
		with self.assertRaises(frappe.ValidationError) as ctx:
			mrp_approval.approve_and_create(run.name, self.key(), old, confirm=1)
		self.assertIn("changed", str(ctx.exception))
		self.assertEqual(self.docs(run.name), ([], []))


class TestDuplicateAndConcurrentApproval(ApprovalCase):
	def test_a_double_click_with_the_same_key_creates_nothing_twice(self):
		_so, run = self.reviewed_run()
		key, token = self.key(), self.token(run)
		first = mrp_approval.approve_and_create(run.name, key, token, confirm=1)
		before = self.docs(run.name)
		again = mrp_approval.approve_and_create(run.name, key, token, confirm=1)
		self.assertEqual((first["replayed"], again["replayed"]), (False, True))
		self.assertEqual(self.docs(run.name), before)
		self.assertEqual(sorted(d["name"] for d in again["documents"]), sorted(d["name"] for d in first["documents"]))

	def test_a_second_approval_with_another_key_is_refused(self):
		_so, run = self.reviewed_run()
		self.approve(run)
		before = self.docs(run.name)
		with self.assertRaises(frappe.ValidationError):
			self.approve(run)
		self.assertEqual(self.docs(run.name), before)

	def test_the_approval_takes_the_row_lock_and_reads_the_run_under_it(self):
		_so, run = self.reviewed_run()
		locked = []
		real = frappe.db.get_value

		def spy(*a, **k):
			if a and a[0] == RUN and k.get("for_update"):
				locked.append(a[1] if len(a) > 1 else None)
			return real(*a, **k)

		with mock.patch.object(frappe.db, "get_value", side_effect=spy):
			self.approve(run)
		self.assertIn(run.name, locked)

	def test_the_database_refuses_the_same_approval_key_on_two_runs(self):
		_a, run_a = self.reviewed_run()
		_b, run_b = self.reviewed_run()
		key = self.key()
		mrp_approval.approve_and_create(run_a.name, key, self.token(run_a), confirm=1)
		with self.assertRaises((frappe.UniqueValidationError, frappe.DuplicateEntryError)):
			mrp_approval.approve_and_create(run_b.name, key, self.token(run_b), confirm=1)
		self.assertEqual(self.docs(run_b.name), ([], []), "the failed approval rolled its documents back")
		self.assertEqual(frappe.db.get_value(RUN, run_b.name, "approval_status"), "Awaiting Review")

	def test_a_missing_key_or_confirmation_is_refused(self):
		_so, run = self.reviewed_run()
		for key, confirm in (("", 1), ("short", 1), (self.key(), 0)):
			with self.assertRaises(frappe.ValidationError):
				mrp_approval.approve_and_create(run.name, key, self.token(run), confirm=confirm)
		self.assertEqual(self.docs(run.name), ([], []))

	def test_stale_review_data_cannot_be_approved(self):
		_so, run = self.reviewed_run()
		stale = self.token(run)
		mrp_approval.revise_line(run.name, self.line(run, self.rma).name, 22)  # somebody else revised meanwhile
		with self.assertRaises(frappe.ValidationError):
			mrp_approval.approve_and_create(run.name, self.key(), stale, confirm=1)
		self.assertEqual(self.docs(run.name), ([], []))
		self.approve(run)  # with the current token it works
		self.assertTrue(self.docs(run.name)[0])


class TestPermissions(ApprovalCase):
	def test_a_viewer_may_look_but_not_change_or_approve(self):
		_so, run = self.reviewed_run()
		line = self.line(run, self.rma).name
		token = self.token(run)
		self.as_user(self.viewer)
		data = mrp.review_data(run.name)
		self.assertEqual((data["permissions"]["can_approve"], data["permissions"]["can_edit"], data["permissions"]["can_cancel"], data["permissions"]["can_recalculate"]), (False, False, False, False))
		for call in (
			lambda: mrp_approval.approve_and_create(run.name, self.key(), token, confirm=1),
			lambda: mrp_approval.revise_line(run.name, line, 21),
			lambda: mrp_approval.skip_line(run.name, line, "no"),
			lambda: mrp_approval.cancel_run(run.name, "no"),
			lambda: mrp_approval.recalculate(run.name),
		):
			with self.assertRaises(frappe.PermissionError):
				call()
		self.as_user("Administrator")
		self.assertEqual(self.docs(run.name), ([], []))
		self.assertEqual(frappe.db.get_value(RUN, run.name, "approval_status"), "Awaiting Review")

	def test_a_planner_may_review_and_approve(self):
		_so, run = self.reviewed_run()
		self.as_user(self.planner)
		data = mrp.review_data(run.name)
		self.assertTrue(data["permissions"]["can_approve"])
		mrp_approval.revise_line(run.name, self.line(run, self.rma).name, 21)
		self.approve(run)
		self.as_user("Administrator")
		self.assertEqual(frappe.db.get_value(RUN, run.name, "approved_by"), self.planner)

	def test_a_user_without_any_ppc_role_sees_and_changes_nothing(self):
		_so, run = self.reviewed_run()
		self.as_user(self.outsider)
		with self.assertRaises(frappe.PermissionError):
			mrp.review_data(run.name)
		with self.assertRaises(frappe.PermissionError):
			mrp_approval.skip_line(run.name, "x", "reason")

	def test_the_api_endpoints_enforce_the_same_rules(self):
		_so, run = self.reviewed_run()
		self.as_user(self.viewer)
		with self.assertRaises(frappe.PermissionError):
			api.mrp_approve_and_create(run.name, self.key(), "token", confirm=1)
		with self.assertRaises(frappe.PermissionError):
			api.mrp_cancel_run(run.name, "no")
		# everything that changes data accepts POST only (so Frappe's CSRF protection applies); the two reads are GET
		for fn in ("mrp_approve_and_create", "mrp_revise_lines", "mrp_revise_line", "mrp_skip_line", "mrp_cancel_run", "mrp_recalculate", "mrp_calculate_now"):
			self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[getattr(api, fn)], ["POST"], fn)
		for fn in ("mrp_run_for_source", "mrp_review_data"):
			self.assertIn("GET", frappe.allowed_http_methods_for_whitelisted_func[getattr(api, fn)], fn)


class TestAuditTrail(ApprovalCase):
	def test_the_whole_review_leaves_a_complete_trail(self):
		so, run = self.reviewed_run()
		mrp_approval.revise_line(run.name, self.line(run, self.rma).name, 24)
		mrp_approval.skip_line(run.name, self.line(run, self.rmp).name, "stock on the way")
		out = self.approve(run)
		actions = [r.action for r in self.audit(run.name)]
		for expected in ("MRP Calculate", "MRP Line Revised", "MRP Line Skipped", "MRP Create Documents", "MRP Approved"):
			self.assertIn(expected, actions)
		(approved,) = self.audit(run.name, "MRP Approved")
		d = approved.d
		self.assertEqual((approved.user, approved.result, bool(approved.timestamp)), ("Administrator", "Success", True))
		self.assertEqual((d["run"], d["source"], d["approval_status"]), (run.name, [["Sales Order", so.name]], "Approved"))
		self.assertEqual(sorted(tuple(x) for x in d["created"]), sorted(tuple(x) for x in out["created"]))
		self.assertTrue(d["approval_key"] and d["approved_by"] == "Administrator" and d["approved_on"])
		rma = next(x for x in d["lines"] if x["item_code"] == self.rma)
		self.assertEqual((rma["original_qty"], rma["revised_qty"]), (20, 24))
		self.assertEqual([(x["item_code"], x["reason"]) for x in d["skipped_by_reviewer"]], [(self.rmp, "stock on the way")])

	def test_a_failed_creation_is_audited_and_leaves_nothing_behind(self):
		_so, run = self.reviewed_run()
		with mock.patch.object(mrp, "_make_work_order", side_effect=frappe.ValidationError("no BOM today")):
			with self.assertRaises(frappe.ValidationError):
				self.approve(run)
		self.assertEqual(self.docs(run.name), ([], []))
		self.assertEqual(frappe.db.get_value(RUN, run.name, ["status", "approval_status"]), ("Calculated", "Awaiting Review"))
		failed = [r for r in self.audit(run.name, "MRP Approved") if r.result == "Failed"]
		self.assertTrue(failed and "no BOM today" in (failed[0].error_details or ""))

	def test_the_review_screen_shows_the_history(self):
		_so, run = self.reviewed_run()
		mrp_approval.skip_line(run.name, self.line(run, self.rma).name, "not now")
		history = mrp.review_data(run.name)["history"]
		self.assertIn("MRP Line Skipped", [h["action"] for h in history])
		self.assertEqual([h["reason"] for h in history if h["action"] == "MRP Line Skipped"], ["not now"])


class TestAutomationPolicy(ApprovalCase):
	def test_review_first_is_the_default_and_creates_nothing_by_itself(self):
		self.auto_on()
		so = self.so()
		run = self.run_of("Sales Order", so.name)
		self.assertEqual((frappe.db.get_value(RUN, run, "approval_status"), self.docs(run)), ("Awaiting Review", ([], [])))
		self.assertEqual(mrp._setting("mrp_auto_create_drafts"), 0)

	def test_switching_the_policy_on_records_who_did_it(self):
		settings = frappe.get_doc("PPC Settings")
		settings.mrp_auto_create_drafts = 1
		settings.save()
		settings.reload()
		self.assertEqual((settings.mrp_policy_set_by, bool(settings.mrp_policy_set_on)), ("Administrator", True))
		(row,) = [r for r in frappe.get_all("PPC Audit Log", filters={"action": "MRP Policy Changed"}, fields=["details", "user"], order_by="creation desc", limit=1)]
		d = json.loads(row.details)
		self.assertEqual((d["now"]["automatic_draft_creation"], d["was"]["automatic_draft_creation"], d["set_by"]), (1, 0, "Administrator"))
		settings.mrp_auto_create_drafts = 0
		settings.save()
		settings.reload()
		self.assertFalse(settings.mrp_policy_set_by)

	def test_when_the_policy_creates_drafts_the_run_says_so_and_it_is_audited(self):
		use_mrp_settings(mrp_default_warehouse=self.wh)
		settings = frappe.get_doc("PPC Settings")
		settings.mrp_auto_create_drafts = 1
		settings.mrp_default_warehouse = self.wh
		settings.save()
		self.auto_on()
		so = self.so(delivery_in_days=30)
		run = frappe.get_doc(RUN, self.run_of("Sales Order", so.name))
		self.assertEqual((run.status, run.approval_status, run.approved_by), ("Documents Created", "Approved", None))
		self.assertIn("Administrator", run.automation_policy)
		self.assertIn("Automatic Draft Creation", run.automation_policy)
		mrs, wos = self.docs(run.name)
		self.assertTrue(mrs and wos and all(d.docstatus == 0 for d in mrs + wos), "drafts only, never submitted")
		(entry,) = self.audit(run.name, "MRP Automation Policy")
		self.assertEqual((entry.d["run"], entry.d["source"]), (run.name, [["Sales Order", so.name]]))
		self.assertIn("Administrator", entry.d["policy"])

	def test_the_policy_never_touches_a_cancelled_or_already_approved_review(self):
		_so, run = self.reviewed_run()
		mrp_approval.cancel_run(run.name, "not needed")
		self.assertIsNone(mrp.create_documents_auto(run.name))
		_so2, run2 = self.reviewed_run()
		self.approve(run2)
		self.assertIsNone(mrp.create_documents_auto(run2.name))
		self.assertEqual(len(self.docs(run2.name)[1]), 2)


class TestProductionPlan(ApprovalCase):
	def test_a_submitted_plan_gets_the_same_review_and_approval(self):
		self.auto_on()
		use_mrp_settings(mrp_trigger_production_plan=1, mrp_auto_on_sales_order=0)  # only the plan starts a run
		so = self.so([(self.mfg, 5)], delivery_in_days=30)
		plan = make_plan(self.company, [so.name], submit=True)
		run = self.run_of("Production Plan", plan.name)
		self.assertTrue(run)
		state = mrp_approval.run_for_source("Production Plan", plan.name)
		self.assertEqual(state["state"], "calculated")
		self.assertEqual(self.docs(run), ([], []))
		self.approve(run)
		mrs, wos = self.docs(run)
		self.assertTrue(wos and all(d.docstatus == 0 for d in mrs + wos))
		self.assertTrue(all(f"Production Plan {plan.name}" in (d.ppc_source_documents or "") for d in mrs + wos))
		self.assertEqual(frappe.db.get_value(RUN, run, "source_production_plan"), plan.name)

	def test_the_plan_dialog_stays_closed_when_the_plan_trigger_is_off(self):
		so = self.so([(self.mfg, 5)])
		plan = make_plan(self.company, [so.name], submit=True)
		use_mrp_settings(mrp_trigger_production_plan=0)
		self.assertFalse(mrp_approval.run_for_source("Production Plan", plan.name)["enabled"])

	def test_approving_a_plan_run_does_not_duplicate_what_a_sales_order_run_made(self):
		self.auto_on()
		use_mrp_settings(mrp_trigger_production_plan=1, mrp_auto_on_sales_order=1)
		so = self.so([(self.mfg, 5)], delivery_in_days=30)
		so_run = self.run_of("Sales Order", so.name)
		self.approve(so_run)
		made = len(self.docs(so_run)[1])
		plan = make_plan(self.company, [so.name], submit=True)  # its rows belong to the order that already has a run
		plan_run = self.run_of("Production Plan", plan.name)
		if plan_run:
			self.assertEqual([r for r in frappe.get_doc(RUN, plan_run).lines if r.action in ("Manufacture", "Purchase") and r.net_qty > 0], [])
		self.assertEqual(len(self.docs(so_run)[1]), made)
