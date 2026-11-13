"""MRP on a real site: manual and automatic runs, Procurement Type, review rules, confirmation, revalidation and
duplicate protection. Fixtures live in the test transaction, which is rolled back."""

from unittest import mock

import frappe
from frappe.utils import add_days, nowdate

from ppc_production_control.overrides import sales_order_dashboard
from ppc_production_control.services import mrp, mrp_auto
from ppc_production_control.tests.utils import (
	PREFIX,
	PPCTestCase,
	ensure_stock,
	make_bom,
	make_item,
	make_plan,
	make_sales_order,
	make_warehouse,
	use_mrp_settings,
)

P = f"{PREFIX}-MRP"


def set_type(item, ptype):
	frappe.db.set_value("Item", item, "custom_procurement_type", ptype)


class MRPTestCase(PPCTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.wh = make_warehouse(f"{PREFIX} MRP A", cls.company)
		cls.wh_b = make_warehouse(f"{PREFIX} MRP B", cls.company)
		frappe.db.set_value("UOM", "Nos", "must_be_whole_number", 1)
		cls.mfg = make_item(f"{P}-FG", manufacture=True)  # manufactured, with a manufactured sub-assembly and two purchased parts
		cls.sub = make_item(f"{P}-SUB", manufacture=True)
		cls.rma = make_item(f"{P}-RMA")
		cls.rmp = make_item(f"{P}-RMP")
		cls.buy = make_item(f"{P}-BUY")  # a plain purchased finished item
		for item, ptype in ((cls.mfg, "Manufacture"), (cls.sub, "Manufacture"), (cls.rma, "Purchase"), (cls.rmp, "Purchase"), (cls.buy, "Purchase")):
			set_type(item, ptype)
		make_bom(cls.sub, cls.company, [(cls.rmp, 3)])
		make_bom(cls.mfg, cls.company, [(cls.sub, 1), (cls.rma, 2)])
		for item in (cls.mfg, cls.sub, cls.rma, cls.rmp, cls.buy):
			frappe.db.delete("Bin", {"item_code": item})
		frappe.flags.ppc_mrp_auto_in_test = False

	def setUp(self):
		super().setUp()
		use_mrp_settings()  # the recommended settings, whatever the site saved (rolled back with the test's savepoint)

	def so(self, lines=None, **kw):
		return make_sales_order(self.company, self.customer, lines or [(self.mfg, 10)], warehouse=self.wh, **kw)

	def run_for(self, *orders, calculate=True, to_days=60, **extra):
		doc = frappe.get_doc({
			"doctype": mrp.RUN_DT, "company": self.company, "default_warehouse": self.wh, "to_date": add_days(nowdate(), to_days),
			"sources": [{"source_doctype": "Sales Order", "source_name": o} for o in orders], **extra,
		}).insert()
		if calculate:
			mrp.calculate(doc.name)
			doc.reload()
		return doc

	def lines(self, run, item=None):
		return [r for r in run.lines if not item or r.item_code == item]

	def one(self, run, item):
		rows = self.lines(run, item)
		self.assertEqual(len(rows), 1, f"{item}: {[(r.warehouse, r.required_date) for r in rows]}")
		return rows[0]


class TestProcurementAndExplosion(MRPTestCase):
	def test_item_has_a_procurement_type_field_with_both_options(self):
		df = frappe.get_meta("Item").get_field("custom_procurement_type")
		self.assertIsNotNone(df)
		self.assertEqual([o for o in df.options.split("\n") if o], ["Purchase", "Manufacture"])

	def test_manufactured_item_explodes_to_work_orders_and_purchase_requests(self):
		run = self.run_for(self.so(delivery_in_days=30).name)
		self.assertEqual(run.status, "Calculated")
		actions = {r.item_code: (r.action, r.net_qty) for r in run.lines}
		self.assertEqual(actions[self.mfg], ("Manufacture", 10))
		self.assertEqual(actions[self.sub], ("Manufacture", 10))
		self.assertEqual(actions[self.rma], ("Purchase", 20))  # purchased component inside the manufacturing BOM
		self.assertEqual(actions[self.rmp], ("Purchase", 30))  # purchased component of the sub-assembly (multilevel)

	def test_purchase_type_item_gets_a_material_request_recommendation(self):
		run = self.run_for(self.so([(self.buy, 4)]).name)
		line = self.one(run, self.buy)
		self.assertEqual((line.action, line.net_qty, line.procurement_type), ("Purchase", 4, "Purchase"))

	def test_procurement_type_wins_over_an_existing_bom(self):
		set_type(self.mfg, "Purchase")
		run = self.run_for(self.so().name)
		self.assertEqual([(r.item_code, r.action) for r in run.lines], [(self.mfg, "Purchase")])

	def test_manufacture_type_without_a_bom_is_flagged_for_review(self):
		item = make_item(f"{P}-NOBOM")
		set_type(item, "Manufacture")
		line = self.one(self.run_for(self.so([(item, 1)]).name), item)
		self.assertEqual(line.action, "Review")
		self.assertIn("no active BOM", line.note)

	def test_blank_procurement_type_is_inferred_and_reported(self):
		set_type(self.buy, "")
		run = self.run_for(self.so([(self.buy, 1)]).name)
		self.assertEqual(self.one(run, self.buy).action, "Purchase")
		self.assertIn("no Procurement Type", run.warnings)

	def test_backfill_is_a_dry_run_by_default(self):
		set_type(self.buy, "")
		out = mrp.backfill_procurement_type(only=[self.buy])
		self.assertTrue(out["dry_run"])
		self.assertEqual(out["Purchase"], 1)
		self.assertEqual(frappe.db.get_value("Item", self.buy, "custom_procurement_type") or "", "")
		mrp.backfill_procurement_type(dry_run=0, only=[self.buy])
		self.assertEqual(frappe.db.get_value("Item", self.buy, "custom_procurement_type"), "Purchase")

	def test_every_line_names_the_sales_order_it_serves(self):
		so = self.so()
		run = self.run_for(so.name)
		for r in run.lines:
			self.assertIn(f"Sales Order {so.name}", r.source_documents, r.item_code)

	def test_warehouse_wise_stock_of_the_demand_warehouse_only(self):
		ensure_stock(self.buy, self.wh_b, 1000)  # plenty in the OTHER warehouse
		ensure_stock(self.buy, self.wh, 3)
		line = self.one(self.run_for(self.so([(self.buy, 10)]).name), self.buy)
		self.assertEqual((line.from_stock, line.net_qty, line.warehouse), (3, 7, self.wh))

	def test_stock_reserved_for_other_open_orders_is_not_spent(self):
		ensure_stock(self.buy, self.wh, 10)
		first = self.so([(self.buy, 6)])
		self.so([(self.buy, 6)])  # a second order is waiting for the same stock
		line = self.one(self.run_for(first.name), self.buy)
		self.assertEqual((line.from_stock, line.net_qty), (4, 2))

	def test_a_plan_row_that_belongs_to_a_sales_order_is_not_counted_again(self):
		so = self.so([(self.mfg, 5)])  # Production Plans only pick up items that have a BOM
		make_plan(self.company, [so.name], submit=True)
		run = frappe.get_doc({"doctype": mrp.RUN_DT, "company": self.company, "default_warehouse": self.wh, "to_date": add_days(nowdate(), 60)}).insert()
		mrp.calculate(run.name)
		run.reload()
		keys = [d.source_key for d in run.demands]
		self.assertEqual(len([k for k in keys if k.startswith("Sales Order Item")]), 1)
		self.assertFalse([d for d in run.demands if d.source_doctype == "Production Plan" and d.item_code == self.mfg and d.source_name in
			frappe.get_all("Production Plan Item", filters={"sales_order": so.name}, pluck="parent")])
		self.assertEqual(len(keys), len(set(keys)))
		self.assertIn("not counted a second time", run.warnings)

	def test_production_plan_can_be_planned_directly(self):
		so = self.so([(self.mfg, 5)])
		plan = make_plan(self.company, [so.name], submit=True)
		run = frappe.get_doc({
			"doctype": mrp.RUN_DT, "company": self.company, "default_warehouse": self.wh, "to_date": add_days(nowdate(), 60),
			"include_sales_orders": 0, "sources": [{"source_doctype": "Production Plan", "source_name": plan.name}],
		}).insert()
		mrp.calculate(run.name)
		run.reload()
		self.assertTrue(any(d.source_doctype == "Production Plan" for d in run.demands))
		self.assertIn(f"Production Plan {plan.name}", self.one(run, self.mfg).source_documents)


class TestRunValidation(MRPTestCase):
	def run_doc(self, **kw):
		return frappe.get_doc({"doctype": mrp.RUN_DT, "company": self.company, "default_warehouse": self.wh, "to_date": add_days(nowdate(), 30), **kw})

	def test_listed_documents_must_exist_be_submitted_and_unique(self):
		so = self.so(submit=False)
		with self.assertRaises(frappe.ValidationError):
			self.run_doc(sources=[{"source_doctype": "Sales Order", "source_name": so.name}]).insert()  # draft
		so.submit()
		with self.assertRaises(frappe.ValidationError):
			self.run_doc(sources=[{"source_doctype": "Sales Order", "source_name": so.name}] * 2).insert()  # twice
		with self.assertRaises(frappe.ValidationError):
			self.run_doc(sources=[{"source_doctype": "Sales Order", "source_name": "SO-DOES-NOT-EXIST"}]).insert()
		self.run_doc(sources=[{"source_doctype": "Sales Order", "source_name": so.name}]).insert()

	def test_a_run_needs_some_demand_source(self):
		with self.assertRaises(frappe.ValidationError):
			self.run_doc(include_sales_orders=0, include_production_plans=0).insert()

	def test_a_manual_run_needs_a_default_warehouse(self):
		with self.assertRaises(frappe.ValidationError):
			self.run_doc(default_warehouse=None).insert()

	def test_duplicate_demand_lines_are_rejected(self):
		run = self.run_for(self.so().name)
		run.append("demands", {"source_key": run.demands[0].source_key, "item_code": self.mfg, "qty": 1})
		with self.assertRaises(frappe.ValidationError):
			run.save()


class TestReview(MRPTestCase):
	def setUp(self):
		super().setUp()
		self.run = self.run_for(self.so(delivery_in_days=30).name)
		self.rma_row = self.one(self.run, self.rma)

	def sel(self, row, on=1, qty=None):
		return {"name": row.name, "create_document": on, "approved_qty": row.net_qty if qty is None else qty}

	def test_lines_start_selected_with_the_shortage_as_quantity(self):
		self.assertTrue(all(r.create_document and r.approved_qty == r.net_qty for r in self.run.lines if r.action in ("Purchase", "Manufacture")))

	def test_invalid_quantities_are_refused_with_the_row_named(self):
		for qty, fragment in ((0, "greater than zero"), (-1, "greater than zero"), (1.5, "whole number"), (20 * mrp.MAX_EDIT_FACTOR + 1, "may not exceed")):
			with self.assertRaises(frappe.ValidationError) as ctx:
				mrp.save_review(self.run.name, [self.sel(self.rma_row, qty=qty)])
			self.assertIn(fragment, str(ctx.exception), qty)
			self.assertIn(self.rma, str(ctx.exception))

	def test_valid_edits_and_deselection_are_stored(self):
		sub_row = self.one(self.run, self.sub)
		mrp.save_review(self.run.name, [self.sel(self.rma_row, qty=25), self.sel(sub_row, on=0)])
		self.run.reload()
		self.assertEqual(self.one(self.run, self.rma).approved_qty, 25)
		self.assertEqual(self.one(self.run, self.sub).create_document, 0)

	def test_a_line_with_nothing_to_create_cannot_be_selected(self):
		ensure_stock(self.rma, self.wh, 1000)
		run = self.run_for(self.so().name)
		covered = self.one(run, self.rma)
		self.assertEqual(covered.net_qty, 0)
		with self.assertRaises(frappe.ValidationError):
			mrp.save_review(run.name, [{"name": covered.name, "create_document": 1, "approved_qty": 1}])

	def test_unknown_rows_are_refused(self):
		with self.assertRaises(frappe.ValidationError):
			mrp.save_review(self.run.name, [{"name": "does-not-exist", "create_document": 1}])

	def test_review_data_marks_what_can_be_edited(self):
		data = mrp.review_data(self.run.name)
		self.assertTrue(data["can_edit"])
		self.assertTrue(all(r["editable"] for r in data["lines"] if r["action"] in ("Purchase", "Manufacture")))


class TestDocumentCreation(MRPTestCase):
	def create(self, run, selections=None):
		return mrp.create_documents(run.name, selections, confirm=1)

	def test_explicit_confirmation_is_required(self):
		run = self.run_for(self.so().name)
		for confirm in (0, None, ""):
			with self.assertRaises(frappe.ValidationError):
				mrp.create_documents(run.name, None, confirm)
		self.assertEqual(frappe.db.get_value(mrp.RUN_DT, run.name, "status"), "Calculated")
		self.assertFalse(frappe.db.exists("Material Request", {"ppc_mrp_run": run.name}))

	def test_nothing_selected_creates_nothing(self):
		run = self.run_for(self.so().name)
		off = [{"name": r.name, "create_document": 0} for r in run.lines]
		with self.assertRaises(frappe.ValidationError):
			self.create(run, off)

	def test_drafts_are_created_with_the_reviewed_quantities_and_references(self):
		so = self.so(delivery_in_days=30)
		run = self.run_for(so.name)
		rma = self.one(run, self.rma)
		out = self.create(run, [{"name": r.name, "create_document": 1, "approved_qty": 25 if r.name == rma.name else r.net_qty}
			for r in run.lines if r.action in ("Purchase", "Manufacture")])
		self.assertEqual(out["status"], "Documents Created")
		mr = frappe.get_doc("Material Request", frappe.db.get_value(mrp.RUN_DT, run.name, "material_request"))
		self.assertEqual((mr.docstatus, mr.material_request_type, mr.ppc_mrp_run), (0, "Purchase", run.name))
		self.assertEqual({i.item_code: i.qty for i in mr.items}, {self.rma: 25, self.rmp: 30})
		self.assertIn(f"Sales Order {so.name}", mr.ppc_source_documents)
		self.assertIn(f"Sales Order {so.name}", mr.items[0].ppc_source_documents)
		orders = frappe.get_all("Work Order", filters={"ppc_mrp_run": run.name}, fields=["name", "docstatus", "production_item", "qty", "ppc_source_documents", "use_multi_level_bom"])
		self.assertEqual({o.production_item: o.qty for o in orders}, {self.mfg: 10, self.sub: 10})
		self.assertTrue(all(o.docstatus == 0 and not o.use_multi_level_bom and f"Sales Order {so.name}" in o.ppc_source_documents for o in orders))

	def test_nothing_generated_is_ever_submitted(self):
		run = self.run_for(self.so().name)
		self.create(run)
		for dt in ("Material Request", "Work Order"):
			self.assertFalse(frappe.db.exists(dt, {"ppc_mrp_run": run.name, "docstatus": ["!=", 0]}))

	def test_a_run_creates_documents_only_once(self):
		run = self.run_for(self.so().name)
		self.create(run)
		with self.assertRaises(frappe.ValidationError):
			self.create(run)
		with self.assertRaises(frappe.ValidationError):
			mrp.calculate(run.name)  # and cannot be recalculated either
		self.assertEqual(frappe.db.count("Work Order", {"ppc_mrp_run": run.name}), 2)

	def test_the_drafts_count_as_supply_so_a_second_run_recommends_nothing_twice(self):
		so = self.so()
		self.create(self.run_for(so.name))
		again = self.run_for(so.name)
		self.assertEqual([(r.item_code, r.net_qty) for r in again.lines if r.net_qty > 0], [])
		self.assertIn("already planned", again.warnings)

	def test_stock_that_arrives_before_creation_removes_the_line(self):
		run = self.run_for(self.so().name)
		ensure_stock(self.rma, self.wh, 1000)  # received after the calculation
		out = self.create(run)
		self.assertIn(self.rma, out["skipped"])
		mr = frappe.get_doc("Material Request", frappe.db.get_value(mrp.RUN_DT, run.name, "material_request"))
		self.assertNotIn(self.rma, [i.item_code for i in mr.items])
		self.assertIn(self.rmp, [i.item_code for i in mr.items])
		self.assertEqual(frappe.db.get_value("PPC MRP Line", {"parent": run.name, "item_code": self.rma}, "status"), "Skipped: no longer short")

	def test_stock_that_arrives_reduces_a_line_instead_of_overbuying(self):
		run = self.run_for(self.so([(self.buy, 10)]).name)
		ensure_stock(self.buy, self.wh, 4)
		self.create(run)
		mr = frappe.get_doc("Material Request", frappe.db.get_value(mrp.RUN_DT, run.name, "material_request"))
		self.assertEqual(mr.items[0].qty, 6)

	def test_a_cancelled_order_creates_nothing(self):
		so = self.so()
		run = self.run_for(so.name)
		so.cancel()  # the demand disappears between the calculation and the confirmation
		out = self.create(run)
		self.assertEqual(out["created"], [])
		self.assertTrue(out["skipped"])
		self.assertTrue(out["demand_gone"])
		self.assertEqual(frappe.db.get_value(mrp.RUN_DT, run.name, "status"), "Calculated")  # still open: nothing was created
		self.assertFalse(frappe.db.exists("Work Order", {"ppc_mrp_run": run.name}))
		self.assertFalse(frappe.db.exists("Material Request", {"ppc_mrp_run": run.name}))

	def test_creation_is_all_or_nothing(self):
		run = self.run_for(self.so().name)
		with mock.patch.object(mrp, "_make_work_order", side_effect=frappe.ValidationError("boom")):
			with self.assertRaises(frappe.ValidationError):
				self.create(run)
		self.assertFalse(frappe.db.exists("Material Request", {"ppc_mrp_run": run.name}))
		self.assertEqual(frappe.db.get_value(mrp.RUN_DT, run.name, "status"), "Calculated")
		self.assertFalse(frappe.db.get_value("PPC MRP Line", {"parent": run.name, "item_code": self.rma}, "document"))

	def test_permission_is_checked_on_every_step(self):
		run = self.run_for(self.so().name)
		frappe.set_user("Guest")
		try:
			for call in (lambda: mrp.calculate(run.name), lambda: mrp.save_review(run.name, []), lambda: mrp.create_documents(run.name, None, 1), lambda: mrp.retry(run.name)):
				with self.assertRaises(frappe.PermissionError):
					call()
		finally:
			frappe.set_user("Administrator")


class TestAutomaticMRP(MRPTestCase):
	def setUp(self):
		super().setUp()
		frappe.flags.ppc_mrp_auto_in_test = True

	def tearDown(self):
		frappe.flags.ppc_mrp_auto_in_test = False
		super().tearDown()

	def auto_runs(self, so):
		return frappe.get_all(mrp.RUN_DT, filters={"source_sales_order": so.name}, fields=["name", "status", "trigger", "error_message", "retry_count"])

	def test_submitting_a_sales_order_calculates_an_automatic_run(self):
		so = self.so(delivery_in_days=20)
		runs = self.auto_runs(so)
		self.assertEqual(len(runs), 1)
		self.assertEqual((runs[0].status, runs[0].trigger), ("Calculated", "Automatic"))
		run = frappe.get_doc(mrp.RUN_DT, runs[0].name)
		self.assertEqual([(s.source_doctype, s.source_name) for s in run.sources], [("Sales Order", so.name)])
		self.assertEqual({r.item_code: r.action for r in run.lines if r.net_qty > 0}[self.mfg], "Manufacture")
		self.assertFalse(frappe.db.exists("Material Request", {"ppc_mrp_run": run.name}))  # recommendations only: documents wait for the review

	def test_a_second_run_for_the_same_order_is_not_created(self):
		so = self.so()
		mrp_auto.run_for_sales_order(so.name)
		self.assertEqual(len(self.auto_runs(so)), 1)

	def test_automatic_and_manual_calculation_use_one_engine(self):
		so = self.so(delivery_in_days=20)
		auto = frappe.get_doc(mrp.RUN_DT, self.auto_runs(so)[0].name)
		frappe.flags.ppc_mrp_auto_in_test = False
		manual = self.run_for(so.name, to_days=60)
		view = lambda run: sorted((r.item_code, r.warehouse, str(r.required_date), r.action, r.gross_qty, r.net_qty, str(r.planned_order_date)) for r in run.lines)
		self.assertEqual(view(auto), view(manual))

	def test_a_site_that_never_saved_the_setting_runs_automatically(self):
		# a Single's new Check has no stored row after an upgrade; "nothing stored" must mean on, not off
		frappe.db.delete("Singles", {"doctype": "PPC Settings", "field": "mrp_auto_on_sales_order"})
		self.assertTrue(mrp_auto.enabled())
		self.assertEqual(len(self.auto_runs(self.so())), 1)

	def test_switching_the_automatic_run_off_in_settings(self):
		frappe.db.set_single_value("PPC Settings", "mrp_auto_on_sales_order", 0)
		self.assertEqual(self.auto_runs(self.so()), [])

	def test_a_failed_calculation_is_logged_and_does_not_block_the_order(self):
		with mock.patch.object(mrp, "compute", side_effect=frappe.ValidationError("deliberate failure")):
			so = self.so()
		self.assertEqual(frappe.db.get_value("Sales Order", so.name, "docstatus"), 1)  # the submit went through
		(run,) = self.auto_runs(so)
		self.assertEqual((run.status, run.retry_count), ("Failed", 1))
		self.assertIn("deliberate failure", run.error_message)
		self.assertTrue(frappe.db.exists("PPC Audit Log", {"action": "MRP Calculation Failed"}))
		self.assertGreaterEqual(mrp.dashboard()["attention"]["failed"], 1)

	def test_a_failed_run_can_be_retried_by_hand(self):
		with mock.patch.object(mrp, "compute", side_effect=frappe.ValidationError("deliberate failure")):
			so = self.so()
		(run,) = self.auto_runs(so)
		out = mrp.retry(run.name)
		self.assertEqual(out["status"], "Calculated")
		after = frappe.get_doc(mrp.RUN_DT, run.name)
		self.assertTrue(after.lines)
		self.assertFalse(after.error_message)
		with self.assertRaises(frappe.ValidationError):
			mrp.retry(run.name)  # only failed / queued runs

	def test_a_failing_retry_stays_failed_and_counts_the_attempt(self):
		with mock.patch.object(mrp, "compute", side_effect=frappe.ValidationError("first")):
			so = self.so()
		(run,) = self.auto_runs(so)
		with mock.patch.object(mrp, "compute", side_effect=frappe.ValidationError("second")):
			with self.assertRaises(frappe.ValidationError):
				mrp.retry(run.name)
		row = frappe.db.get_value(mrp.RUN_DT, run.name, ["status", "retry_count", "error_message"], as_dict=True)
		self.assertEqual((row.status, row.retry_count), ("Failed", 2))
		self.assertIn("second", row.error_message)

	def test_no_warehouse_available_fails_the_run_not_the_order(self):
		with mock.patch.object(mrp_auto, "default_warehouse_for", return_value=None):
			so = self.so()
		self.assertEqual(frappe.db.get_value("Sales Order", so.name, "docstatus"), 1)
		(run,) = self.auto_runs(so)
		self.assertEqual(run.status, "Failed")
		self.assertIn("default warehouse", run.error_message.lower())

	def test_even_a_broken_queue_cannot_block_the_submit(self):
		with mock.patch.object(mrp_auto, "enqueue_once", side_effect=RuntimeError("redis is down")):
			so = self.so()
		self.assertEqual(frappe.db.get_value("Sales Order", so.name, "docstatus"), 1)

	def test_sales_order_connections_show_the_run(self):
		data = sales_order_dashboard.get_data({"transactions": []})
		self.assertEqual(data["non_standard_fieldnames"]["PPC MRP Run"], "source_sales_order")
		self.assertIn("PPC MRP Run", [i for g in data["transactions"] for i in g["items"]])


class TestAuditAndDashboard(MRPTestCase):
	def test_calculation_review_and_creation_leave_audit_entries(self):
		run = self.run_for(self.so().name)
		mrp.save_review(run.name, [])
		mrp.create_documents(run.name, None, 1)
		actions = set(frappe.get_all("PPC Audit Log", filters={"timestamp": [">=", add_days(nowdate(), -1)]}, pluck="action"))
		self.assertTrue({"MRP Calculate", "MRP Review Saved", "MRP Create Documents"} <= actions)

	def test_dashboard_and_reports_read_the_run(self):
		run = self.run_for(self.so().name)
		dash = mrp.dashboard(self.company, run.name)
		self.assertEqual(dash["run"], run.name)
		self.assertGreater(dash["summary"]["production_lines"], 0)
		self.assertGreater(dash["summary"]["purchase_lines"], 0)
		for group in ("item", "warehouse", "date"):
			rows = mrp.report_rows(group, {"run": run.name})
			self.assertTrue(rows, group)
		wh_rows = mrp.report_rows("warehouse", {"run": run.name})
		self.assertEqual({r.warehouse for r in wh_rows}, {self.wh})

	def test_lead_time_planning_dates(self):
		frappe.db.set_value("Item", self.rma, "lead_time_days", 7)
		so = self.so(delivery_in_days=30)
		run = self.run_for(so.name)
		fg = self.one(run, self.mfg)
		rma = self.one(run, self.rma)
		self.assertEqual(str(fg.required_date), str(add_days(nowdate(), 30)))
		self.assertEqual(rma.required_date, fg.planned_order_date)  # components are needed when production has to start
		self.assertEqual(rma.planned_order_date, add_days(fg.planned_order_date, -7))
