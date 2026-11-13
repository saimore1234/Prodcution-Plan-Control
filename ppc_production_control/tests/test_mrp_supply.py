"""MRP supply netting at document level (services/mrp_supply.py + mrp.stock_position).

Every test sets the PPC Settings it needs (`use_mrp_settings`) and builds its own Sales Orders, Work Orders and Material Requests
inside the test's savepoint, which is rolled back: no existing document of the site is read as fixture or changed."""

import frappe
from frappe.utils import add_days, nowdate

from ppc_production_control.services import mrp
from ppc_production_control.tests.test_mrp import MRPTestCase
from ppc_production_control.tests.utils import ensure_stock, use_mrp_settings


class SupplyCase(MRPTestCase):
	# ---- fixtures: documents that stand for "supply that already exists"
	def bom_of(self, item):
		return frappe.db.get_value("BOM", {"item": item, "is_active": 1, "is_default": 1, "docstatus": 1})

	def make_wo(self, qty=10, *, item=None, sales_order=None, run=None, submit=False, cancel=False):
		item = item or self.mfg
		wo = frappe.get_doc({
			"doctype": "Work Order", "company": self.company, "production_item": item, "bom_no": self.bom_of(item), "qty": qty,
			"stock_uom": "Nos", "fg_warehouse": self.wh, "wip_warehouse": self.wip, "use_multi_level_bom": 0,
			"planned_start_date": f"{nowdate()} 00:00:00", "sales_order": sales_order, "ppc_mrp_run": run,
		})
		wo.insert()
		if submit or cancel:
			wo.submit()
		if cancel:
			wo.cancel()
		return wo

	def make_mr(self, rows, *, run=None, submit=False, cancel=False):
		"""rows: [(item, qty, sales_order or None)] - one purchase Material Request."""
		mr = frappe.get_doc({
			"doctype": "Material Request", "material_request_type": "Purchase", "company": self.company, "transaction_date": nowdate(),
			"schedule_date": add_days(nowdate(), 5), "ppc_mrp_run": run,
			"items": [
				{"item_code": item, "qty": qty, "uom": "Nos", "stock_uom": "Nos", "conversion_factor": 1, "warehouse": self.wh,
				 "schedule_date": add_days(nowdate(), 5), "sales_order": so}
				for item, qty, so in rows
			],
		})
		mr.insert()
		if submit or cancel:
			mr.submit()
		if cancel:
			mr.cancel()
		return mr

	def make_run_for(self, *orders):
		"""A finished PPC run whose structured demand rows are these Sales Orders (what PPC-generated supply points to)."""
		run = frappe.get_doc({
			"doctype": mrp.RUN_DT, "company": self.company, "default_warehouse": self.wh, "to_date": add_days(nowdate(), 60),
			"sources": [{"source_doctype": "Sales Order", "source_name": o} for o in orders],
		})
		for o in orders:
			row = frappe.db.get_value("Sales Order Item", {"parent": o}, "name")
			run.append("demands", {
				"source_key": f"Sales Order Item:{row}", "source_doctype": "Sales Order", "source_name": o, "row_name": row,
				"item_code": self.mfg, "qty": 10, "uom": "Nos", "required_date": nowdate(), "warehouse": self.wh,
			})
		run.insert()
		return run.name

	def mfg_line(self, order):
		return self.one(self.run_for(order.name), self.mfg)

	def buy_line(self, order):
		return self.one(self.run_for(order.name), self.buy)

	def close(self, order, status="Cancelled", docstatus=2):
		"""Stand-in for an order that is no longer open demand (set on a fixture of this test only)."""
		frappe.db.set_value("Sales Order", order.name, {"docstatus": docstatus, "status": status})


class TestDraftsAlwaysCount(SupplyCase):
	def test_draft_work_order_counts_even_when_existing_work_orders_are_excluded(self):
		use_mrp_settings(mrp_include_existing_wo=0)
		so = self.so()
		self.make_wo(10)  # unallocated draft
		line = self.mfg_line(so)
		self.assertEqual((line.from_supply, line.net_qty, line.action), (10, 0, "None"))

	def test_draft_material_request_counts_even_when_incoming_is_excluded(self):
		use_mrp_settings(mrp_include_incoming_po=0)
		so = self.so([(self.buy, 10)])
		self.make_mr([(self.buy, 10, None)])
		line = self.buy_line(so)
		self.assertEqual((line.from_supply, line.net_qty), (10, 0))

	def test_the_include_settings_still_decide_about_submitted_documents(self):
		so = self.so([(self.buy, 10)])
		self.make_mr([(self.buy, 10, None)], submit=True)  # in ERPNext's Bin (indented), not a draft
		use_mrp_settings(mrp_include_incoming_po=0)
		self.assertEqual(self.buy_line(so).from_supply, 0)
		use_mrp_settings(mrp_include_incoming_po=1)
		self.assertEqual(self.buy_line(so).from_supply, 10)

	def test_a_draft_is_counted_once_not_twice(self):
		so = self.so([(self.buy, 10)])
		self.make_mr([(self.buy, 4, None)])
		self.make_mr([(self.buy, 3, None)])
		line = self.buy_line(so)
		self.assertEqual((line.from_supply, line.net_qty), (7, 3))


class TestAllocationBetweenOrders(SupplyCase):
	def test_supply_linked_to_this_order_counts_for_it(self):
		so = self.so()
		self.make_wo(10, sales_order=so.name)
		line = self.mfg_line(so)
		self.assertEqual((line.from_supply, line.net_qty), (10, 0))

	def test_supply_linked_to_another_open_order_is_not_free(self):
		a, b = self.so(), self.so()
		self.make_wo(10, sales_order=a.name)
		line_b = self.mfg_line(b)
		self.assertEqual((line_b.from_supply, line_b.net_qty, line_b.action), (0, 10, "Manufacture"))
		self.assertIn("belongs to other open orders", frappe.get_doc(mrp.RUN_DT, line_b.parent).warnings)
		self.assertEqual(self.mfg_line(a).from_supply, 10)  # ...and it is still A's own

	def test_the_same_supply_cannot_cover_two_orders(self):
		a, b = self.so(), self.so()
		self.make_wo(10, sales_order=a.name)
		self.assertEqual((self.mfg_line(a).net_qty, self.mfg_line(b).net_qty), (0, 10))

	def test_unallocated_supply_is_free_for_any_order(self):
		a = self.so()
		self.make_wo(10)
		self.assertEqual(self.mfg_line(a).from_supply, 10)

	def test_supply_of_a_cancelled_work_order_does_not_count(self):
		so = self.so()
		self.make_wo(10, cancel=True)
		self.assertEqual(self.mfg_line(so).from_supply, 0)

	def test_supply_of_a_cancelled_material_request_does_not_count(self):
		so = self.so([(self.buy, 10)])
		self.make_mr([(self.buy, 10, None)], cancel=True)
		self.assertEqual(self.buy_line(so).from_supply, 0)

	def test_a_cancelled_order_reserves_nothing(self):
		a, b = self.so(), self.so()
		self.make_wo(10, sales_order=a.name)
		self.close(a)  # cancelled
		self.assertEqual(self.mfg_line(b).from_supply, 10)

	def test_closed_completed_and_on_hold_orders_reserve_nothing_either(self):
		for status in ("Closed", "Completed", "On Hold"):
			a, b = self.so([(self.buy, 10)]), self.so([(self.buy, 10)])
			self.make_mr([(self.buy, 10, a.name)])
			self.close(a, status=status, docstatus=1)
			self.assertEqual(self.buy_line(b).from_supply, 10, status)

	def test_a_draft_order_reserves_nothing(self):
		draft = self.so(submit=False)
		b = self.so([(self.buy, 10)])
		self.make_mr([(self.buy, 10, draft.name)])
		self.assertEqual(self.buy_line(b).from_supply, 10)

	def test_one_material_request_with_two_explicit_allocations(self):
		a, b, c = (self.so([(self.buy, 10)]) for _ in range(3))
		self.make_mr([(self.buy, 6, a.name), (self.buy, 4, b.name)])
		self.assertEqual((self.buy_line(a).from_supply, self.buy_line(a).net_qty), (6, 4))
		self.assertEqual((self.buy_line(b).from_supply, self.buy_line(b).net_qty), (4, 6))
		self.assertEqual((self.buy_line(c).from_supply, self.buy_line(c).net_qty), (0, 10))  # all of it belongs to A and B

	def test_submitted_supply_of_another_order_is_subtracted_exactly_once(self):
		a, b = self.so([(self.buy, 10)]), self.so([(self.buy, 10)])
		self.make_mr([(self.buy, 10, a.name)], submit=True)  # in the Bin and reserved for A: 10 - 10
		self.make_mr([(self.buy, 5, None)])  # a free draft
		line_b, line_a = self.buy_line(b), self.buy_line(a)
		self.assertEqual((line_b.incoming_qty, line_b.from_supply), (5, 5))  # A's 10 are not B's; only the free 5
		self.assertEqual(line_a.incoming_qty, 15)  # A's own 10 plus the free 5, each counted once


class TestPpcGeneratedSupply(SupplyCase):
	def test_owner_comes_from_the_structured_demand_rows_of_the_run(self):
		a, b = self.so(), self.so()
		run = self.make_run_for(a.name)
		wo = self.make_wo(10, run=run)  # no Sales Order field at all: only ppc_mrp_run
		self.assertFalse(frappe.db.get_value("Work Order", wo.name, "sales_order"))
		self.assertEqual(self.mfg_line(a).from_supply, 10)
		self.assertEqual(self.mfg_line(b).from_supply, 0)

	def test_the_free_text_source_field_is_never_used(self):
		a, b = self.so(), self.so()
		wo = self.make_wo(10)
		frappe.db.set_value("Work Order", wo.name, "ppc_source_documents", f"Sales Order {a.name}")
		self.assertEqual(self.mfg_line(b).from_supply, 10)  # text says "A", but nothing structured says so

	def test_shared_supply_without_a_quantity_split_is_not_invented(self):
		a, b, c = self.so(), self.so(), self.so()
		run = self.make_run_for(a.name, b.name)
		self.make_wo(10, run=run)
		line_b = self.mfg_line(b)  # serves A and B: not free for B, and not B's alone either
		self.assertEqual((line_b.from_supply, line_b.net_qty), (0, 10))
		self.assertIn("no quantity split", frappe.get_doc(mrp.RUN_DT, line_b.parent).warnings)
		self.assertIn(self.mfg, frappe.get_doc(mrp.RUN_DT, line_b.parent).warnings)
		self.assertEqual(self.mfg_line(c).from_supply, 0)  # for an outsider it is simply reserved for A and B

	def test_shared_supply_becomes_unambiguous_when_the_other_order_is_gone(self):
		a, b = self.so(), self.so()
		run = self.make_run_for(a.name, b.name)
		self.make_wo(10, run=run)
		self.close(a)
		self.assertEqual(self.mfg_line(b).from_supply, 10)

	def test_the_run_that_made_the_supply_sees_it_as_its_own(self):
		a = self.so()
		run = self.make_run_for(a.name)
		self.make_wo(10, run=run)
		self.make_mr([(self.buy, 7, None)], run=run)
		self.assertEqual(self.mfg_line(a).net_qty, 0)


class TestStockIsNotTouched(SupplyCase):
	def test_allocation_never_changes_warehouse_stock(self):
		a, b = self.so([(self.buy, 10)]), self.so([(self.buy, 10)])
		ensure_stock(self.buy, self.wh, 5)
		self.make_mr([(self.buy, 10, a.name)])  # reserved for A: supply only
		ensure_stock(self.buy, self.wh_b, 100)  # stock of another warehouse
		line = self.buy_line(b)
		# 5 in stock but A's open order waits for it (reserved), so B gets 0 stock - and the supply is A's as well
		self.assertEqual((line.from_stock, line.from_supply, line.net_qty), (0, 0, 10))

	def test_supply_in_another_warehouse_is_not_supply_here(self):
		so = self.so([(self.buy, 10)])
		mr = self.make_mr([(self.buy, 10, None)])
		frappe.db.set_value("Material Request Item", {"parent": mr.name}, "warehouse", self.wh_b)
		line = self.buy_line(so)
		self.assertEqual((line.from_supply, line.net_qty, line.warehouse), (0, 10, self.wh))

	def test_stock_and_free_supply_add_up_once(self):
		so = self.so([(self.buy, 10)])
		ensure_stock(self.buy, self.wh, 4)
		self.make_mr([(self.buy, 3, None)])
		line = self.buy_line(so)
		self.assertEqual((line.from_stock, line.from_supply, line.net_qty), (4, 3, 3))
