"""Read-only smoke run of the MRP against the site's real data (rolls everything back).

    bench --site stage.local execute ppc_production_control.tests.mrp_smoke.run
"""

import frappe
from frappe.utils import add_days, nowdate

from ppc_production_control.services import mrp


def run(horizon_days=365, warehouses=None, create=0):
	frappe.set_user("Administrator")
	company = frappe.defaults.get_global_default("company") or frappe.get_all("Company", pluck="name", limit=1)[0]
	wh = frappe.get_all("Warehouse", filters={"company": company, "is_group": 0, "disabled": 0}, pluck="name", limit=1)[0]
	doc = frappe.get_doc({
		"doctype": mrp.RUN_DT, "company": company, "to_date": add_days(nowdate(), int(horizon_days)),
		"default_warehouse": wh, "include_sales_orders": 1, "include_production_plans": 1,
		"warehouses": [{"warehouse": w} for w in (warehouses or [])],
	}).insert()
	try:
		res = mrp.calculate(doc.name)
		doc.reload()
		print("company:", company, "| default warehouse:", wh)
		print("summary:", res["summary"])
		print("warnings:", res["warnings"])
		print("demand lines:", len(doc.demands), "| result lines:", len(doc.lines))
		for r in doc.lines[:12]:
			print(f"  L{r.level} {r.item_code[:28]:28} {str(r.warehouse)[:22]:22} need {r.gross_qty:>9.2f} stock {r.from_stock:>9.2f} "
				f"supply {r.from_supply:>9.2f} short {r.net_qty:>9.2f} {r.action:11} order by {r.planned_order_date}")
		if int(create):
			out = mrp.create_documents(doc.name)
			print("created:", out["created"], "| skipped:", out["skipped"], "| status:", out["status"])
			for dt, name in out["created"]:
				print("  ", dt, name, "docstatus", frappe.db.get_value(dt, name, "docstatus"), "run", frappe.db.get_value(dt, name, "ppc_mrp_run"))
			try:
				mrp.create_documents(doc.name)
				print("ERROR: second creation was allowed")
			except frappe.ValidationError as e:
				print("second creation refused:", str(e)[:70])
			again = frappe.get_doc({
				"doctype": mrp.RUN_DT, "company": company, "to_date": add_days(nowdate(), int(horizon_days)),
				"default_warehouse": wh, "warehouses": [{"warehouse": w} for w in (warehouses or [])],
			}).insert()
			res2 = mrp.calculate(again.name)
			print("second run after drafts exist ->", {k: res2["summary"][k] for k in ("shortage_items", "purchase_lines", "production_lines")})
		d = mrp.dashboard(company, doc.name)
		print("dashboard keys:", sorted(d), "| shortages shown:", len(d["shortages"]))
		for g in ("item", "warehouse", "date"):
			print("report", g, "rows:", len(mrp.report_rows(g, {"run": doc.name})))
	finally:
		frappe.db.rollback()


def work_order_check():
	"""Create one draft Work Order from a real active BOM through the MRP code path, then roll back."""
	frappe.set_user("Administrator")
	bom = frappe.get_all("BOM", filters={"is_active": 1, "is_default": 1, "docstatus": 1}, fields=["name", "item", "company", "uom"], limit=1)[0]
	wh = frappe.get_all("Warehouse", filters={"company": bom.company, "is_group": 0, "disabled": 0}, pluck="name", limit=1)[0]
	run_doc = frappe.get_doc({"doctype": mrp.RUN_DT, "company": bom.company, "to_date": nowdate(), "default_warehouse": wh}).insert()
	try:
		row = frappe._dict(item_code=bom.item, bom_no=bom.name, uom=frappe.db.get_value("Item", bom.item, "stock_uom"), warehouse=wh,
			planned_order_date=add_days(nowdate(), 3), required_date=add_days(nowdate(), 5))
		wo = mrp._make_work_order(run_doc, row, 2)
		print("work order", wo.name, "| docstatus", wo.docstatus, "| qty", wo.qty, "| bom", wo.bom_no, "| required items", len(wo.required_items),
			"| operations", len(wo.operations), "| multi-level", wo.use_multi_level_bom, "| run", wo.ppc_mrp_run, "| start", wo.planned_start_date)
	finally:
		frappe.db.rollback()
