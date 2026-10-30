"""MRP netting rules - no database."""

import unittest
from datetime import date

from ppc_production_control.services import mrp_engine as e

TODAY = date(2027, 3, 1)


class Ctx:
	"""Dictionary-backed context. boms: name -> (item, quantity, children, fixed_min, scaled_min)."""

	def __init__(self, items, boms=None, stock=None, defaults=None, warehouses=None, default_warehouse="Main"):
		self.today = TODAY
		self.hours_per_day = 8
		self.default_warehouse = default_warehouse
		self.warehouses = warehouses
		self._items, self._boms, self._stock, self._defaults = items, boms or {}, stock or {}, defaults or {}

	def item(self, code):
		return self._items.get(code)

	def default_bom(self, code):
		return self._defaults.get(code)

	def bom(self, name):
		b = self._boms.get(name)
		if not b:
			return None
		item, quantity, children, fixed, scaled = (list(b) + [0, 0])[:5]
		return {"name": name, "item": item, "quantity": quantity, "fixed_minutes": fixed, "scaled_minutes": scaled, "children": children}

	def position(self, item, warehouse):
		return dict({"stock": 0, "incoming": 0}, **self._stock.get((item, warehouse), {}))


def itm(buy=True, lead=0, stock=True):
	return {"item_name": "x", "stock_uom": "Nos", "is_stock_item": stock, "is_purchase_item": buy, "lead_time_days": lead}


def child(code, qty, bom_no=None, wh=None):
	return {"item_code": code, "stock_qty": qty, "bom_no": bom_no, "source_warehouse": wh}


def demand(item, qty, on, warehouse="Main", key=None, bom=None):
	return {"key": key or f"SO:{item}:{on}", "item_code": item, "qty": qty, "required_date": on, "warehouse": warehouse, "bom_no": bom}


def by(lines, item, warehouse="Main"):
	return [r for r in lines if r["item_code"] == item and r["warehouse"] == warehouse]


class TestNetting(unittest.TestCase):
	def test_stock_covers_demand(self):
		ctx = Ctx({"A": itm()}, stock={("A", "Main"): {"stock": 10}})
		out = e.run_mrp([demand("A", 6, date(2027, 3, 10))], ctx)
		line = out["lines"][0]
		self.assertEqual((line["net_qty"], line["from_stock"], line["action"]), (0, 6, "None"))

	def test_shortage_after_stock_and_pending_supply(self):
		ctx = Ctx({"A": itm(lead=5)}, stock={("A", "Main"): {"stock": 4, "incoming": 3}})
		line = e.run_mrp([demand("A", 10, date(2027, 3, 20))], ctx)["lines"][0]
		self.assertEqual((line["from_stock"], line["from_supply"], line["net_qty"]), (4, 3, 3))
		self.assertEqual(line["action"], "Purchase")
		self.assertEqual(line["planned_order_date"], date(2027, 3, 15))

	def test_supply_goes_to_earliest_requirement_first(self):
		ctx = Ctx({"A": itm()}, stock={("A", "Main"): {"stock": 5}})
		out = e.run_mrp([demand("A", 4, date(2027, 4, 1)), demand("A", 4, date(2027, 3, 5))], ctx)
		early, late = sorted(out["lines"], key=lambda r: r["required_date"])
		self.assertEqual((early["net_qty"], late["net_qty"]), (0, 3))

	def test_negative_stock_is_not_supply(self):
		ctx = Ctx({"A": itm()}, stock={("A", "Main"): {"stock": -7}})
		line = e.run_mrp([demand("A", 2, date(2027, 3, 5))], ctx)["lines"][0]
		self.assertEqual(line["net_qty"], 2)

	def test_each_warehouse_is_netted_on_its_own(self):
		ctx = Ctx({"A": itm()}, stock={("A", "Main"): {"stock": 10}, ("A", "Store"): {"stock": 0}})
		out = e.run_mrp([demand("A", 5, date(2027, 3, 5), "Main"), demand("A", 5, date(2027, 3, 5), "Store", key="k2")], ctx)
		self.assertEqual(by(out["lines"], "A", "Main")[0]["net_qty"], 0)
		self.assertEqual(by(out["lines"], "A", "Store")[0]["net_qty"], 5)

	def test_same_day_demands_are_planned_together(self):
		ctx = Ctx({"A": itm()})
		out = e.run_mrp([demand("A", 2, date(2027, 3, 5), key="a"), demand("A", 3, date(2027, 3, 5), key="b")], ctx)
		self.assertEqual(len(out["lines"]), 1)
		self.assertEqual(out["lines"][0]["gross_qty"], 5)

	def test_item_that_cannot_be_bought_or_made_needs_review(self):
		ctx = Ctx({"A": itm(buy=False)})
		line = e.run_mrp([demand("A", 1, date(2027, 3, 5))], ctx)["lines"][0]
		self.assertEqual(line["action"], "Review")

	def test_late_when_planned_date_is_in_the_past(self):
		ctx = Ctx({"A": itm(lead=30)})
		line = e.run_mrp([demand("A", 1, date(2027, 3, 10))], ctx)["lines"][0]
		self.assertTrue(line["is_late"])


class TestExplosion(unittest.TestCase):
	def ctx(self, **kw):
		items = {"FG": itm(buy=False, lead=2), "SUB": itm(buy=False, lead=1), "RM1": itm(lead=7), "RM2": itm(lead=3)}
		boms = {
			"BOM-FG": ("FG", 1, [child("SUB", 2, "BOM-SUB"), child("RM1", 3)]),
			"BOM-SUB": ("SUB", 1, [child("RM1", 1), child("RM2", 4)]),
		}
		return Ctx(items, boms, defaults={"FG": "BOM-FG"}, **kw)

	def test_multilevel_explosion_and_dates(self):
		out = e.run_mrp([demand("FG", 10, date(2027, 3, 20))], self.ctx())
		fg = by(out["lines"], "FG")[0]
		self.assertEqual((fg["action"], fg["net_qty"], fg["planned_order_date"]), ("Manufacture", 10, date(2027, 3, 18)))
		sub = by(out["lines"], "SUB")[0]
		self.assertEqual((sub["gross_qty"], sub["action"], sub["level"]), (20, "Manufacture", 1))
		self.assertEqual(sub["required_date"], date(2027, 3, 18))
		self.assertEqual(sub["planned_order_date"], date(2027, 3, 17))
		rm1 = by(out["lines"], "RM1")[0]
		# 10 x 3 for FG directly + 20 x 1 through SUB, netted once with all the demand
		self.assertEqual(rm1["level"], 2)
		self.assertEqual(sum(r["gross_qty"] for r in by(out["lines"], "RM1")), 50)

	def test_finished_stock_stops_the_explosion(self):
		out = e.run_mrp([demand("FG", 10, date(2027, 3, 20))], self.ctx(stock={("FG", "Main"): {"stock": 10}}))
		self.assertEqual([r["item_code"] for r in out["lines"]], ["FG"])
		self.assertEqual(out["lines"][0]["net_qty"], 0)

	def test_only_the_shortage_is_exploded(self):
		out = e.run_mrp([demand("FG", 10, date(2027, 3, 20))], self.ctx(stock={("FG", "Main"): {"stock": 6}}))
		self.assertEqual(by(out["lines"], "SUB")[0]["gross_qty"], 8)  # (10-6) x 2

	def test_pending_supply_of_a_subassembly_is_used(self):
		out = e.run_mrp([demand("FG", 10, date(2027, 3, 20))], self.ctx(stock={("SUB", "Main"): {"incoming": 20}}))
		sub = by(out["lines"], "SUB")[0]
		self.assertEqual((sub["from_supply"], sub["net_qty"]), (20, 0))
		self.assertEqual([r["item_code"] for r in out["lines"] if r["item_code"] == "RM2"], [])

	def test_operation_time_extends_lead_time(self):
		items = {"FG": itm(buy=False, lead=0), "RM": itm()}
		boms = {"B": ("FG", 1, [child("RM", 1)], 0, 480)}  # 480 min per unit = 1 day each (8 h/day)
		ctx = Ctx(items, boms, defaults={"FG": "B"})
		fg = e.run_mrp([demand("FG", 3, date(2027, 3, 20))], ctx)["lines"][0]
		self.assertEqual((fg["lead_time_days"], fg["planned_order_date"]), (3, date(2027, 3, 17)))

	def test_bom_quantity_scales_components(self):
		items = {"FG": itm(buy=False), "RM": itm()}
		ctx = Ctx(items, {"B": ("FG", 10, [child("RM", 5)])}, defaults={"FG": "B"})
		out = e.run_mrp([demand("FG", 20, date(2027, 3, 20))], ctx)
		self.assertEqual(by(out["lines"], "RM")[0]["gross_qty"], 10)

	def test_component_source_warehouse_and_fallback(self):
		items = {"FG": itm(buy=False), "RM": itm(), "RM2": itm()}
		boms = {"B": ("FG", 1, [child("RM", 1, wh="Store"), child("RM2", 1, wh="Elsewhere")])}
		ctx = Ctx(items, boms, defaults={"FG": "B"}, warehouses={"Main", "Store"})
		out = e.run_mrp([demand("FG", 1, date(2027, 3, 20))], ctx)
		self.assertEqual(len(by(out["lines"], "RM", "Store")), 1)
		self.assertEqual(len(by(out["lines"], "RM2", "Main")), 1)  # not a selected warehouse -> default

	def test_non_stock_component_is_skipped_with_a_warning(self):
		items = {"FG": itm(buy=False), "SVC": itm(stock=False)}
		ctx = Ctx(items, {"B": ("FG", 1, [child("SVC", 1)])}, defaults={"FG": "B"})
		out = e.run_mrp([demand("FG", 1, date(2027, 3, 20))], ctx)
		self.assertEqual([r["item_code"] for r in out["lines"]], ["FG"])
		self.assertTrue(out["warnings"])

	def test_bom_loop_is_an_error(self):
		items = {"A": itm(buy=False), "B": itm(buy=False)}
		boms = {"BA": ("A", 1, [child("B", 1, "BB")]), "BB": ("B", 1, [child("A", 1, "BA")])}
		ctx = Ctx(items, boms, defaults={"A": "BA"})
		with self.assertRaises(e.MRPError):
			e.run_mrp([demand("A", 1, date(2027, 3, 20))], ctx)

	def test_demand_bom_overrides_default(self):
		items = {"FG": itm(buy=False), "X": itm(), "Y": itm()}
		boms = {"B1": ("FG", 1, [child("X", 1)]), "B2": ("FG", 1, [child("Y", 1)])}
		ctx = Ctx(items, boms, defaults={"FG": "B1"})
		out = e.run_mrp([demand("FG", 1, date(2027, 3, 20), bom="B2")], ctx)
		self.assertEqual([r["item_code"] for r in out["lines"]], ["FG", "Y"])

	def test_deactivated_demand_bom_falls_back_to_default(self):
		items = {"FG": itm(buy=False), "X": itm()}
		ctx = Ctx(items, {"B1": ("FG", 1, [child("X", 1)])}, defaults={"FG": "B1"})
		out = e.run_mrp([demand("FG", 1, date(2027, 3, 20), bom="B-GONE")], ctx)
		self.assertEqual([r["item_code"] for r in out["lines"]], ["FG", "X"])

	def test_summary(self):
		out = e.run_mrp([demand("FG", 10, date(2027, 3, 20))], self.ctx())
		s = e.summarize([demand("FG", 10, date(2027, 3, 20))], out["lines"])
		self.assertEqual((s["demand_lines"], s["production_lines"]), (1, 2))
		self.assertGreater(s["purchase_lines"], 0)
		self.assertEqual(s["review_lines"], 0)


if __name__ == "__main__":
	unittest.main()
