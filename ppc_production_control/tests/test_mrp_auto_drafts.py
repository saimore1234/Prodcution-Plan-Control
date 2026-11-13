"""v1.7 automatic MRP: both triggers, automatic drafts, idempotency, retry, settings, safety stock.
Fixtures live in the test transaction, which is rolled back."""

from unittest import mock

import frappe
from frappe.utils import add_days, nowdate

from ppc_production_control.services import mrp, mrp_auto
from ppc_production_control.tests.test_mrp import MRPTestCase, P, set_type
from ppc_production_control.tests.utils import PREFIX, ensure_stock, make_bom, make_item, make_plan


class AutoCase(MRPTestCase):
	def setUp(self):
		super().setUp()
		frappe.flags.ppc_mrp_auto_in_test = True
		frappe.db.set_single_value("PPC Settings", "mrp_default_warehouse", self.wh)
		frappe.db.set_single_value("PPC Settings", "mrp_enabled", 1)
		frappe.db.set_single_value("PPC Settings", "mrp_auto_create_drafts", 1)

	def tearDown(self):
		frappe.flags.ppc_mrp_auto_in_test = False
		super().tearDown()

	def runs_of(self, doctype, name):
		return frappe.get_all(mrp.RUN_DT, filters={"idempotency_key": mrp_auto.idempotency_key(doctype, name)}, fields=["name", "status", "trigger"])

	def docs_of(self, run):
		mrs = frappe.get_all("Material Request", filters={"ppc_mrp_run": run}, fields=["name", "docstatus", "material_request_type"])
		wos = frappe.get_all("Work Order", filters={"ppc_mrp_run": run}, fields=["name", "docstatus", "production_item", "qty", "ppc_parent_work_order", "ppc_source_documents"])
		return mrs, wos


class TestAutomaticDrafts(AutoCase):
	def test_sales_order_creates_drafts_automatically_multilevel(self):
		so = self.so(delivery_in_days=30)
		(run,) = self.runs_of("Sales Order", so.name)
		self.assertEqual(run.status, "Documents Created")
		mrs, wos = self.docs_of(run.name)
		self.assertTrue(mrs and wos)
		self.assertTrue(all(d.docstatus == 0 for d in mrs + wos), "everything generated must stay a draft")
		self.assertEqual({w.production_item: w.qty for w in wos}, {self.mfg: 10, self.sub: 10})
		self.assertTrue(all(so.name in (w.ppc_source_documents or "") for w in wos))
		mr = frappe.get_doc("Material Request", mrs[0].name)
		self.assertEqual(mr.material_request_type, "Purchase")
		self.assertEqual({i.item_code: i.qty for i in mr.items}, {self.rma: 20, self.rmp: 30})
		self.assertEqual(mr.company, self.company)

	def test_sub_assembly_work_order_points_to_its_parent(self):
		so = self.so()
		(run,) = self.runs_of("Sales Order", so.name)
		_mrs, wos = self.docs_of(run.name)
		by_item = {w.production_item: w for w in wos}
		self.assertFalse(by_item[self.mfg].ppc_parent_work_order)
		self.assertEqual(by_item[self.sub].ppc_parent_work_order, by_item[self.mfg].name)

	def test_purchase_item_partial_stock(self):
		ensure_stock(self.buy, self.wh, 20)
		so = self.so([(self.buy, 100)])
		(run,) = self.runs_of("Sales Order", so.name)
		mrs, wos = self.docs_of(run.name)
		self.assertEqual(wos, [])
		self.assertEqual(frappe.get_doc("Material Request", mrs[0].name).items[0].qty, 80)

	def test_no_documents_when_draft_creation_is_off(self):
		frappe.db.set_single_value("PPC Settings", "mrp_auto_create_drafts", 0)
		so = self.so()
		(run,) = self.runs_of("Sales Order", so.name)
		self.assertEqual(run.status, "Calculated")
		self.assertEqual(self.docs_of(run.name), ([], []))

	def test_enough_stock_creates_nothing(self):
		ensure_stock(self.buy, self.wh, 500)
		so = self.so([(self.buy, 10)])
		(run,) = self.runs_of("Sales Order", so.name)
		self.assertEqual(self.docs_of(run.name), ([], []))


class TestIdempotency(AutoCase):
	def test_repeated_events_give_one_run_and_one_set_of_documents(self):
		so = self.so()
		for _ in range(3):
			mrp_auto.run_for_source("Sales Order", so.name)
		(run,) = self.runs_of("Sales Order", so.name)
		mrs, wos = self.docs_of(run.name)
		self.assertEqual((len(mrs), len(wos)), (1, 2))

	def test_database_refuses_a_second_run_with_the_same_key(self):
		so = self.so()
		key = mrp_auto.idempotency_key("Sales Order", so.name)
		dup = frappe.get_doc({"doctype": mrp.RUN_DT, "company": self.company, "default_warehouse": self.wh, "idempotency_key": key,
			"to_date": add_days(nowdate(), 30), "sources": [{"source_doctype": "Sales Order", "source_name": so.name}]})
		with self.assertRaises(frappe.UniqueValidationError):
			dup.insert()

	def test_concurrent_job_that_loses_the_race_creates_nothing(self):
		so = self.so()
		real = frappe.db.get_value

		def blind(*a, **k):  # the second job's existence check does not see the first job's run yet
			if a and a[0] == mrp.RUN_DT and isinstance(a[1], dict) and "idempotency_key" in a[1]:
				return None
			return real(*a, **k)

		before = frappe.db.count(mrp.RUN_DT)
		with mock.patch.object(frappe.db, "get_value", side_effect=blind), mock.patch.object(frappe.db, "rollback"):
			mrp_auto.run_for_source("Sales Order", so.name)
		self.assertEqual(frappe.db.count(mrp.RUN_DT), before)

	def test_retry_never_duplicates_documents(self):
		so = self.so()
		(run,) = self.runs_of("Sales Order", so.name)
		before = self.docs_of(run.name)
		frappe.db.set_value(mrp.RUN_DT, run.name, "status", "Failed")
		mrp_auto.retry_run(run.name)
		after = self.docs_of(run.name)
		# a recalculation sees the drafts as supply, so nothing more is created
		self.assertEqual(len(after[0]), len(before[0]))
		self.assertEqual(len(after[1]), len(before[1]))

	def test_retry_of_a_calculated_run_does_nothing(self):
		frappe.db.set_single_value("PPC Settings", "mrp_auto_create_drafts", 0)
		so = self.so()
		(run,) = self.runs_of("Sales Order", so.name)
		mrp_auto.retry_run(run.name)
		self.assertEqual(frappe.db.get_value(mrp.RUN_DT, run.name, "status"), "Calculated")


class TestProductionPlanTrigger(AutoCase):
	def test_submitting_a_plan_creates_one_automatic_run(self):
		frappe.flags.ppc_mrp_auto_in_test = False
		so = self.so()
		frappe.flags.ppc_mrp_auto_in_test = True
		plan = make_plan(self.company, [so.name], warehouse=self.wh, submit=True)
		(run,) = self.runs_of("Production Plan", plan.name)
		self.assertEqual(run.trigger, "Automatic")
		doc = frappe.get_doc(mrp.RUN_DT, run.name)
		self.assertEqual(doc.source_production_plan, plan.name)
		self.assertEqual([(s.source_doctype, s.source_name) for s in doc.sources], [("Production Plan", plan.name)])

	def test_plan_rows_of_an_already_planned_sales_order_are_not_counted_twice(self):
		so = self.so()  # automatic run + drafts for the order
		(so_run,) = self.runs_of("Sales Order", so.name)
		plan = make_plan(self.company, [so.name], warehouse=self.wh, submit=True)
		(pp_run,) = self.runs_of("Production Plan", plan.name)
		doc = frappe.get_doc(mrp.RUN_DT, pp_run.name)
		self.assertEqual([d for d in doc.demands if d.source_doctype == "Production Plan"], [])
		self.assertEqual(self.docs_of(pp_run.name), ([], []))

	def test_plan_trigger_can_be_switched_off(self):
		frappe.db.set_single_value("PPC Settings", "mrp_trigger_production_plan", 0)
		frappe.flags.ppc_mrp_auto_in_test = False
		so = self.so()
		frappe.flags.ppc_mrp_auto_in_test = True
		plan = make_plan(self.company, [so.name], warehouse=self.wh, submit=True)
		self.assertEqual(self.runs_of("Production Plan", plan.name), [])

	def test_master_switch_stops_both_triggers(self):
		frappe.db.set_single_value("PPC Settings", "mrp_enabled", 0)
		so = self.so()
		self.assertEqual(self.runs_of("Sales Order", so.name), [])


class TestFailureAndNotifications(AutoCase):
	def test_failure_marks_run_failed_and_notifies(self):
		with mock.patch("ppc_production_control.services.mrp.calculate_run", side_effect=Exception("boom")), \
			mock.patch.object(mrp_auto, "_notify") as notify, mock.patch("frappe.log_error"):
			so = self.so()
		(run,) = self.runs_of("Sales Order", so.name)
		self.assertEqual(run.status, "Failed")
		self.assertIn("boom", frappe.db.get_value(mrp.RUN_DT, run.name, "error_message"))
		self.assertTrue(any(c.kwargs.get("failure") for c in notify.call_args_list))

	def test_failed_run_retry_then_creates_drafts_once(self):
		with mock.patch("ppc_production_control.services.mrp.calculate_run", side_effect=Exception("boom")), mock.patch("frappe.log_error"):
			so = self.so()
		(run,) = self.runs_of("Sales Order", so.name)
		mrp_auto.retry_run(run.name)
		mrp_auto.retry_run(run.name)
		self.assertEqual(frappe.db.get_value(mrp.RUN_DT, run.name, "status"), "Documents Created")
		mrs, wos = self.docs_of(run.name)
		self.assertEqual((len(mrs), len(wos)), (1, 2))


class TestSettingsAndEngineOptions(AutoCase):
	def test_settings_validation(self):
		s = frappe.get_single("PPC Settings")
		s.mrp_planning_horizon_days = 5000
		with self.assertRaises(frappe.ValidationError):
			s.validate_mrp()
		s.mrp_planning_horizon_days = 90
		s.mrp_max_retries = 9
		with self.assertRaises(frappe.ValidationError):
			s.validate_mrp()
		s.mrp_max_retries = 2
		s.mrp_warehouse_policy = "Default Planning Warehouse"
		s.mrp_default_warehouse = None
		with self.assertRaises(frappe.ValidationError):
			s.validate_mrp()

	def test_safety_stock_is_held_back(self):
		frappe.db.set_single_value("PPC Settings", "mrp_auto_create_drafts", 0)
		frappe.db.set_value("Item", self.buy, "safety_stock", 5)
		ensure_stock(self.buy, self.wh, 20)
		(run,) = self.runs_of("Sales Order", self.so([(self.buy, 20)]).name)
		line = self.one(frappe.get_doc(mrp.RUN_DT, run.name), self.buy)
		self.assertEqual(line.net_qty, 5)  # 20 in stock - 5 safety = 15 usable

	def test_safety_stock_can_be_ignored(self):
		frappe.db.set_single_value("PPC Settings", "mrp_auto_create_drafts", 0)
		frappe.db.set_single_value("PPC Settings", "mrp_include_safety_stock", 0)
		frappe.db.set_value("Item", self.buy, "safety_stock", 5)
		ensure_stock(self.buy, self.wh, 20)
		(run,) = self.runs_of("Sales Order", self.so([(self.buy, 20)]).name)
		self.assertEqual(self.lines(frappe.get_doc(mrp.RUN_DT, run.name), self.buy)[0].net_qty, 0)

	def test_missing_procurement_type_warns(self):
		frappe.db.set_single_value("PPC Settings", "mrp_auto_create_drafts", 0)
		set_type(self.buy, "")
		(run,) = self.runs_of("Sales Order", self.so([(self.buy, 1)]).name)
		self.assertIn("no Procurement Type", frappe.db.get_value(mrp.RUN_DT, run.name, "warnings"))

	def test_manual_review_mode_automatic_creates_drafts(self):
		frappe.flags.ppc_mrp_auto_in_test = False
		frappe.db.set_single_value("PPC Settings", "mrp_review_mode", "Automatic")
		run = self.run_for(self.so([(self.buy, 7)]).name)
		run.reload()
		self.assertEqual(run.status, "Documents Created")
		self.assertTrue(self.docs_of(run.name)[0])

	def test_manual_review_first_creates_nothing(self):
		frappe.flags.ppc_mrp_auto_in_test = False
		frappe.db.set_single_value("PPC Settings", "mrp_review_mode", "Review First")
		run = self.run_for(self.so([(self.buy, 7)]).name)
		self.assertEqual(run.status, "Calculated")
		self.assertEqual(self.docs_of(run.name), ([], []))


class TestOverview(AutoCase):
	def test_overview_reports_real_numbers(self):
		so = self.so()
		(run,) = self.runs_of("Sales Order", so.name)
		o = mrp.overview({"company": self.company, "source": so.name})
		self.assertEqual(o["sales_order_demand_qty"], 10)
		self.assertEqual(o["draft_material_requests"], 1)
		self.assertEqual(o["draft_work_orders"], 2)
		self.assertEqual(o["manufacturing_requirement_qty"], 20)  # FG 10 + sub-assembly 10
		self.assertEqual(o["purchase_requirement_qty"], 50)  # 20 + 30 purchased components
		self.assertEqual(o["failed_runs"], 0)

	def test_overview_filters_narrow_the_result(self):
		self.so()
		self.assertEqual(mrp.overview({"company": self.company, "source": "NO-SUCH-ORDER"})["draft_work_orders"], 0)
		self.assertEqual(mrp.overview({"company": self.company, "status": "Failed"})["active_runs"], 0)
