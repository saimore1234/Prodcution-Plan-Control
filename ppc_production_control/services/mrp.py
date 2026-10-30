"""Material Requirements Planning on top of ERPNext.

Flow of one PPC MRP Run: fetch demand (Sales Orders, Production Plans) -> multilevel BOM explosion and
warehouse-wise netting (services/mrp_engine.py) -> recommendations -> draft Material Request / Work
Orders for the user to review. Nothing is ever submitted, and nothing is created without checking the
stock again first.

What counts as what (so nothing is counted twice):

* Demand: open Sales Order lines (ordered - delivered). A Production Plan row that belongs to a Sales
  Order line is *not* extra demand - the Sales Order line already is; only plan rows without a Sales
  Order (make-to-stock) are added, for planned - produced.
* Usable stock: Bin actual qty, minus stock reserved for existing Work Orders / subcontracting and minus
  Stock Reservation Entries that do not belong to a demand line of this run (those that do are that
  demand's own stock). Sales Order reservations in the Bin are not subtracted - the Sales Order is
  already counted as demand.
* Pending supply: open Purchase Orders (ordered), open Material Requests (indented), open Work Orders
  (planned) and every *draft* Purchase Material Request / Work Order, which ERPNext does not count yet.
"""

import json
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate, now_datetime, nowdate

from ppc_production_control.services import audit
from ppc_production_control.services.constants import CAP_MANAGE_PLANS, R_FAILED, R_SUCCESS, SETTINGS_DT
from ppc_production_control.services.mrp_engine import (
	ACTION_MANUFACTURE,
	ACTION_PURCHASE,
	MRPError,
	run_mrp,
	summarize,
)
from ppc_production_control.services.permissions import check_doc_permission, require_capability

RUN_DT = "PPC MRP Run"
CLOSED_SO_STATUSES = ("Closed", "On Hold", "Completed", "Cancelled")
CLOSED_PLAN_STATUSES = ("Completed", "Closed", "Cancelled")
S_DRAFT, S_CALCULATED, S_CREATED = "Draft", "Calculated", "Documents Created"
A_MRP_CALCULATE, A_MRP_CREATE = "MRP Calculate", "MRP Create Documents"


# ============================================================================================ parameters
def params_of(run) -> dict:
	"""The inputs of a run as a plain dict (also what the revalidation recalculates from)."""
	return {
		"name": run.name,
		"company": run.company,
		"from_date": getdate(run.from_date) if run.from_date else None,
		"to_date": getdate(run.to_date),
		"include_sales_orders": cint(run.include_sales_orders),
		"include_production_plans": cint(run.include_production_plans),
		"default_warehouse": run.default_warehouse,
		"warehouses": [r.warehouse for r in run.get("warehouses") or [] if r.warehouse],
		"hours_per_day": flt(run.working_hours_per_day) or 8.0,
	}


# ================================================================================================ demand
def fetch_demands(p: dict) -> tuple[list[dict], list[str]]:
	"""Open demand of the run as [{key, source_doctype, source_name, row_name, item_code, qty, uom,
	required_date, warehouse, bom_no, source}], plus warnings. Duplicate keys are impossible by
	construction (one dict entry per source row)."""
	demands: dict[str, dict] = {}
	warnings: list[str] = []
	allowed = set(p["warehouses"])
	skipped_wh = skipped_bundle = skipped_unplanned = 0
	so_rows = set()

	if p["include_sales_orders"]:
		rows = frappe.db.sql(
			"""
			select soi.name as row_name, so.name as sales_order, soi.item_code, soi.qty, soi.delivered_qty,
				soi.conversion_factor, soi.stock_uom, soi.warehouse, soi.bom_no,
				coalesce(soi.delivery_date, so.delivery_date, so.transaction_date) as required_date
			from `tabSales Order Item` soi join `tabSales Order` so on so.name = soi.parent
			where so.docstatus = 1 and so.company = %(company)s and so.status not in %(closed)s
				and soi.delivered_qty < soi.qty and ifnull(soi.delivered_by_supplier, 0) = 0
				and coalesce(soi.delivery_date, so.delivery_date, so.transaction_date) <= %(to_date)s
				and (%(from_date)s is null or coalesce(soi.delivery_date, so.delivery_date, so.transaction_date) >= %(from_date)s)
			""",
			{"company": p["company"], "closed": CLOSED_SO_STATUSES, "to_date": p["to_date"], "from_date": p["from_date"]},
			as_dict=True,
		)
		bundles = _product_bundles([r.item_code for r in rows])
		for r in rows:
			if r.item_code in bundles:
				skipped_bundle += 1
				continue
			wh = r.warehouse or p["default_warehouse"]
			if allowed and wh not in allowed:
				skipped_wh += 1
				continue
			qty = (flt(r.qty) - flt(r.delivered_qty)) * (flt(r.conversion_factor) or 1)
			key = f"Sales Order Item:{r.row_name}"
			so_rows.add(r.row_name)
			demands[key] = _demand(key, "Sales Order", r.sales_order, r.row_name, r.item_code, qty, r.stock_uom, r.required_date, wh, r.bom_no)

	if p["include_production_plans"]:
		rows = frappe.db.sql(
			"""
			select ppi.name as row_name, pp.name as plan, ppi.item_code, ppi.planned_qty, ppi.produced_qty,
				ppi.stock_uom, ppi.warehouse, ppi.bom_no, ppi.sales_order_item,
				coalesce(date(ppi.planned_start_date), pp.posting_date) as required_date
			from `tabProduction Plan Item` ppi join `tabProduction Plan` pp on pp.name = ppi.parent
			where pp.docstatus = 1 and pp.company = %(company)s and pp.status not in %(closed)s
				and coalesce(date(ppi.planned_start_date), pp.posting_date) <= %(to_date)s
				and (%(from_date)s is null or coalesce(date(ppi.planned_start_date), pp.posting_date) >= %(from_date)s)
			""",
			{"company": p["company"], "closed": CLOSED_PLAN_STATUSES, "to_date": p["to_date"], "from_date": p["from_date"]},
			as_dict=True,
		)
		for r in rows:
			if p["include_sales_orders"] and r.sales_order_item:
				skipped_unplanned += 1  # the Sales Order line is the demand; counting the plan row too would double it
				continue
			wh = r.warehouse or p["default_warehouse"]
			if allowed and wh not in allowed:
				skipped_wh += 1
				continue
			qty = flt(r.planned_qty) - flt(r.produced_qty)
			if qty <= 0:
				continue
			key = f"Production Plan Item:{r.row_name}"
			demands[key] = _demand(key, "Production Plan", r.plan, r.row_name, r.item_code, qty, r.stock_uom, r.required_date, wh, r.bom_no)

	if skipped_unplanned:
		warnings.append(_("{0} Production Plan row(s) belong to a Sales Order line and are not counted a second time.").format(skipped_unplanned))
	if skipped_wh:
		warnings.append(_("{0} demand line(s) skipped: their warehouse is not one of the selected warehouses.").format(skipped_wh))
	if skipped_bundle:
		warnings.append(_("{0} Sales Order line(s) skipped: Product Bundles are planned through their items.").format(skipped_bundle))
	return list(demands.values()), warnings


def _demand(key, doctype, name, row_name, item, qty, uom, required, warehouse, bom_no) -> dict:
	return {
		"key": key,
		"source_doctype": doctype,
		"source_name": name,
		"row_name": row_name,
		"item_code": item,
		"qty": flt(qty),
		"uom": uom,
		"required_date": getdate(required),
		"warehouse": warehouse,
		"bom_no": bom_no or None,
		"source": f"{name}",
	}


def _product_bundles(items: list[str]) -> set[str]:
	if not items:
		return set()
	return set(
		frappe.get_all("Product Bundle", filters={"new_item_code": ["in", list(set(items))], "disabled": 0}, pluck="new_item_code")
	)


# ======================================================================================== ERPNext context
class SiteContext:
	"""What the engine asks of ERPNext: items, BOMs and warehouse-wise stock position. Cached per run."""

	def __init__(self, p: dict, demands: list[dict]):
		self.p = p
		self.today = getdate(nowdate())
		self.hours_per_day = p["hours_per_day"]
		self.default_warehouse = p["default_warehouse"]
		self.warehouses = set(p["warehouses"]) | {p["default_warehouse"]} if p["warehouses"] else None
		self._items: dict[str, dict | None] = {}
		self._default_boms: dict[str, str | None] = {}
		self._boms: dict[str, dict | None] = {}
		self._positions: dict[tuple, dict] = {}
		self._wh_leaves: dict[str, list[str]] = {}
		# stock reserved for the Sales Order lines that are demand in this run is that demand's own stock
		self._own_so_rows = [d["row_name"] for d in demands if d["source_doctype"] == "Sales Order"]
		self.notes: set[str] = set()

	# ---- items
	def item(self, code):
		if code not in self._items:
			row = frappe.db.get_value(
				"Item", code, ["item_name", "stock_uom", "is_stock_item", "is_purchase_item", "lead_time_days", "disabled"], as_dict=True
			)
			self._items[code] = None if not row or row.disabled else {
				"item_name": row.item_name,
				"stock_uom": row.stock_uom,
				"is_stock_item": cint(row.is_stock_item),
				"is_purchase_item": cint(row.is_purchase_item),
				"lead_time_days": cint(row.lead_time_days),
			}
		return self._items[code]

	def default_bom(self, code):
		if code not in self._default_boms:
			self._default_boms[code] = frappe.db.get_value(
				"BOM", {"item": code, "is_default": 1, "is_active": 1, "docstatus": 1, "company": ["in", [self.p["company"], ""]]}, "name"
			) or frappe.db.get_value("BOM", {"item": code, "is_default": 1, "is_active": 1, "docstatus": 1}, "name")
		return self._default_boms[code]

	def bom(self, name):
		if name not in self._boms:
			head = frappe.db.get_value("BOM", name, ["item", "quantity", "is_active", "docstatus"], as_dict=True)
			if not head or not head.is_active or head.docstatus != 1:
				self.notes.add(_("BOM {0} is missing, inactive or not submitted; the item's current default BOM is used instead, if it has one.").format(name))
				self._boms[name] = None
			else:
				children = frappe.get_all(
					"BOM Item",
					filters={"parent": name, "parenttype": "BOM"},
					fields=["item_code", "stock_qty", "source_warehouse", "bom_no", "sourced_by_supplier", "include_item_in_manufacturing"],
					order_by="idx",
				)
				ops = frappe.get_all("BOM Operation", filters={"parent": name}, fields=["time_in_mins", "fixed_time", "batch_size"])
				fixed = sum(flt(o.time_in_mins) for o in ops if cint(o.fixed_time))
				scaled = sum(flt(o.time_in_mins) / (flt(o.batch_size) or 1) for o in ops if not cint(o.fixed_time))
				self._boms[name] = {
					"name": name,
					"item": head.item,
					"quantity": flt(head.quantity) or 1,
					"fixed_minutes": fixed,
					"scaled_minutes": scaled,
					"children": [
						{"item_code": c.item_code, "stock_qty": flt(c.stock_qty), "source_warehouse": c.source_warehouse, "bom_no": c.bom_no or None}
						for c in children
						if not cint(c.sourced_by_supplier) and cint(c.include_item_in_manufacturing) != 0
					],
				}
		return self._boms[name]

	# ---- stock position
	def _leaves(self, warehouse) -> list[str]:
		if warehouse not in self._wh_leaves:
			wh = frappe.db.get_value("Warehouse", warehouse, ["is_group", "lft", "rgt"], as_dict=True)
			if wh and wh.is_group:
				self._wh_leaves[warehouse] = frappe.get_all("Warehouse", filters={"lft": [">=", wh.lft], "rgt": ["<=", wh.rgt], "is_group": 0}, pluck="name") or [warehouse]
			else:
				self._wh_leaves[warehouse] = [warehouse]
		return self._wh_leaves[warehouse]

	def position(self, item, warehouse):
		key = (item, warehouse)
		if key not in self._positions:
			self._positions[key] = stock_position(item, self._leaves(warehouse), self._own_so_rows)
		return self._positions[key]


def stock_position(item: str, warehouses: list[str], own_so_rows: list[str] | None = None) -> dict:
	"""Usable stock and pending supply of one item in the given (leaf) warehouses, read live."""
	bins = frappe.db.sql(
		"""select coalesce(sum(actual_qty), 0) actual, coalesce(sum(reserved_qty_for_production), 0) res_prod,
			coalesce(sum(reserved_qty_for_sub_contract), 0) res_sub, coalesce(sum(ordered_qty), 0) ordered,
			coalesce(sum(indented_qty), 0) indented, coalesce(sum(planned_qty), 0) planned
		from `tabBin` where item_code = %s and warehouse in %s""",
		(item, tuple(warehouses)),
		as_dict=True,
	)[0]
	reserved_other = _reserved_by_others(item, warehouses, own_so_rows or [])
	draft_mr = flt(
		frappe.db.sql(
			"""select coalesce(sum(mri.stock_qty), 0) from `tabMaterial Request Item` mri
			join `tabMaterial Request` mr on mr.name = mri.parent
			where mr.docstatus = 0 and mr.material_request_type = 'Purchase' and mri.item_code = %s and mri.warehouse in %s""",
			(item, tuple(warehouses)),
		)[0][0]
	)
	draft_wo = flt(
		frappe.db.sql(
			"""select coalesce(sum(qty), 0) from `tabWork Order`
			where docstatus = 0 and production_item = %s and fg_warehouse in %s""",
			(item, tuple(warehouses)),
		)[0][0]
	)
	reserved = flt(bins.res_prod) + flt(bins.res_sub) + reserved_other
	incoming = flt(bins.ordered) + flt(bins.indented) + flt(bins.planned) + draft_mr + draft_wo
	return {
		"stock": flt(bins.actual) - reserved,
		"incoming": incoming,
		"actual_qty": flt(bins.actual),
		"reserved_qty": reserved,
		"incoming_qty": incoming,
	}


def _reserved_by_others(item, warehouses, own_so_rows) -> float:
	try:
		return flt(
			frappe.db.sql(
				"""select coalesce(sum(reserved_qty - delivered_qty), 0) from `tabStock Reservation Entry`
				where docstatus = 1 and item_code = %(item)s and warehouse in %(wh)s
					and status in ('Reserved', 'Partially Reserved', 'Partially Delivered')
					and not (voucher_type = 'Sales Order' and voucher_detail_no in %(own)s)""",
				{"item": item, "wh": tuple(warehouses), "own": tuple(own_so_rows) or ("",)},
			)[0][0]
		)
	except Exception:
		# Stock Reservation Entry not available on this site: no reservations to respect.
		return 0.0


# ================================================================================================ calculation
def compute(p: dict) -> dict:
	"""Demand -> netted plan. Pure read of ERPNext; raises MRPError for impossible data (BOM loops)."""
	demands, warnings = fetch_demands(p)
	ctx = SiteContext(p, demands)
	try:
		result = run_mrp(demands, ctx)
	except MRPError as e:
		frappe.throw(_(str(e)), frappe.ValidationError)
	warnings += result["warnings"] + sorted(ctx.notes)
	return {"demands": demands, "lines": result["lines"], "warnings": warnings, "summary": summarize(demands, result["lines"])}


def calculate(run_name: str) -> dict:
	"""(Re)calculate a run and store demand lines and recommendations on it."""
	require_capability(CAP_MANAGE_PLANS)
	check_doc_permission(RUN_DT, run_name, "write")
	frappe.db.get_value(RUN_DT, run_name, "name", for_update=True)  # row lock: two clicks cannot calculate or create at once
	run = frappe.get_doc(RUN_DT, run_name)
	if run.status == S_CREATED:
		frappe.throw(_("Documents were already created from this run. Start a new run instead of recalculating it."))
	p = params_of(run)
	if not p["default_warehouse"]:
		frappe.throw(_("Select a default warehouse."))
	result = compute(p)

	result["warnings"] += _already_converted_warnings(run.name, result["demands"])
	run.set("demands", [])
	run.set("lines", [])
	for d in result["demands"]:
		run.append("demands", {
			"source_key": d["key"], "source_doctype": d["source_doctype"], "source_name": d["source_name"], "row_name": d["row_name"],
			"item_code": d["item_code"], "qty": d["qty"], "uom": d["uom"], "required_date": d["required_date"], "warehouse": d["warehouse"],
		})
	for r in result["lines"]:
		run.append("lines", dict(
			{k: r[k] for k in ("item_code", "item_name", "uom", "warehouse", "required_date", "gross_qty", "from_stock", "from_supply", "net_qty",
				"action", "bom_no", "lead_time_days", "planned_order_date", "is_late", "level", "sources", "note", "actual_qty", "reserved_qty", "incoming_qty")},
			create_document=1 if r["net_qty"] > 0 and r["action"] in (ACTION_PURCHASE, ACTION_MANUFACTURE) else 0,
		))
	for k, v in result["summary"].items():
		run.set(k, v)
	run.status = S_CALCULATED
	run.calculated_on = now_datetime()
	run.calculated_by = frappe.session.user
	run.warnings = "\n".join(result["warnings"])
	run.save()
	audit.log(A_MRP_CALCULATE, R_SUCCESS, details={"run": run.name, **result["summary"]})
	return {"run": run.name, "status": run.status, "summary": result["summary"], "warnings": result["warnings"]}


def _already_converted_warnings(run_name: str, demands: list[dict]) -> list[str]:
	"""Demand that another run already turned into documents: those drafts count as supply, so this run
	will not recommend it again - say so, so the planner understands why a shortage is missing."""
	keys = [d["key"] for d in demands]
	if not keys:
		return []
	rows = frappe.db.sql(
		"""select distinct r.name from `tabPPC MRP Demand` d join `tabPPC MRP Run` r on r.name = d.parent
		where r.status = %s and r.name != %s and d.source_key in %s""",
		(S_CREATED, run_name, tuple(keys)),
	)
	return [_("Part of this demand was already planned in {0}; its draft documents are counted as supply.").format(r[0]) for r in rows]


# ========================================================================================== draft documents
def create_documents(run_name: str) -> dict:
	"""Create the draft Material Request (purchase lines) and draft Work Orders (manufacture lines).

	The stock position is read again first: a line is created with the smaller of the stored and the
	fresh shortage, and skipped when it is no longer short. Everything is created in one transaction
	(all or nothing) and stays in draft."""
	require_capability(CAP_MANAGE_PLANS)
	check_doc_permission(RUN_DT, run_name, "write")
	frappe.db.get_value(RUN_DT, run_name, "name", for_update=True)  # row lock: two clicks cannot calculate or create at once
	run = frappe.get_doc(RUN_DT, run_name)
	if run.status == S_CREATED:
		frappe.throw(_("Documents were already created from this run."))
	if run.status != S_CALCULATED:
		frappe.throw(_("Calculate the run before creating documents."))
	if frappe.has_permission("Material Request", "create") is False or frappe.has_permission("Work Order", "create") is False:
		frappe.throw(_("You need permission to create Material Requests and Work Orders."), frappe.PermissionError)

	fresh = compute(params_of(run))
	fresh_net: dict[tuple, float] = defaultdict(float)
	for r in fresh["lines"]:
		fresh_net[(r["item_code"], r["warehouse"], str(r["required_date"]), r["action"])] += r["net_qty"]

	buy, make, skipped = [], [], []
	for row in run.lines:
		if not cint(row.create_document) or row.action not in (ACTION_PURCHASE, ACTION_MANUFACTURE) or flt(row.net_qty) <= 0 or row.document:
			continue
		key = (row.item_code, row.warehouse, str(getdate(row.required_date)), row.action)
		qty = min(flt(row.net_qty), fresh_net.get(key, 0))
		if qty <= 0:
			row.status = _("Skipped: no longer short")
			skipped.append(row.item_code)
			continue
		(buy if row.action == ACTION_PURCHASE else make).append((row, qty))
		if qty < flt(row.net_qty):
			row.status = _("Reduced to {0}: stock changed").format(qty)

	savepoint = "ppc_mrp_create"
	frappe.db.savepoint(savepoint)
	try:
		created = []
		if buy:
			mr = _make_material_request(run, buy)
			created.append(("Material Request", mr.name))
			for row, _qty in buy:
				row.document_type, row.document = "Material Request", mr.name
				row.status = row.status or _("Draft created")
			run.material_request = mr.name
		for row, qty in make:
			wo = _make_work_order(run, row, qty)
			created.append(("Work Order", wo.name))
			row.document_type, row.document = "Work Order", wo.name
			row.status = row.status or _("Draft created")
		if created:
			run.status = S_CREATED
		run.save()
	except Exception as e:
		frappe.db.rollback(save_point=savepoint)
		audit.log(A_MRP_CREATE, R_FAILED, error=str(e)[:2000], details={"run": run_name})
		raise
	audit.log(A_MRP_CREATE, R_SUCCESS, details={"run": run_name, "created": created, "skipped": skipped})
	return {"run": run_name, "created": created, "skipped": skipped, "status": run.status}


def _make_material_request(run, buy: list[tuple]):
	mr = frappe.new_doc("Material Request")
	mr.material_request_type = "Purchase"
	mr.company = run.company
	mr.transaction_date = nowdate()
	mr.schedule_date = max(getdate(nowdate()), min(getdate(r.planned_order_date or r.required_date) for r, _q in buy))  # ERPNext refuses past dates; late lines stay flagged on the run
	mr.ppc_mrp_run = run.name
	for row, qty in buy:
		mr.append("items", {
			"item_code": row.item_code, "qty": qty, "uom": row.uom, "stock_uom": row.uom, "conversion_factor": 1,
			"warehouse": row.warehouse, "schedule_date": max(getdate(nowdate()), getdate(row.planned_order_date or row.required_date)),
		})
	mr.insert()
	return mr


def _make_work_order(run, row, qty):
	settings = frappe.get_cached_doc("Manufacturing Settings")
	wo = frappe.new_doc("Work Order")
	wo.company = run.company
	wo.production_item = row.item_code
	wo.bom_no = row.bom_no
	wo.qty = qty
	wo.stock_uom = row.uom
	wo.fg_warehouse = row.warehouse
	wo.wip_warehouse = settings.get("default_wip_warehouse")
	wo.use_multi_level_bom = 0  # sub-assemblies have their own planned Work Order
	wo.planned_start_date = f"{getdate(row.planned_order_date or row.required_date)} 00:00:00"
	wo.ppc_mrp_run = run.name
	wo.insert()
	return wo


# ================================================================================================ dashboard
def dashboard(company: str | None = None, run: str | None = None) -> dict:
	"""Totals of one run (the latest calculated one by default) and the most short items."""
	require_capability("View")
	if not run:
		filters = {"status": ["in", [S_CALCULATED, S_CREATED]]}
		if company:
			filters["company"] = company
		run = frappe.db.get_value(RUN_DT, filters, "name", order_by="calculated_on desc")
	runs = frappe.get_all(RUN_DT, fields=["name", "company", "status", "calculated_on", "to_date"], order_by="creation desc", limit=10)
	if not run:
		return {"run": None, "runs": runs, "summary": None, "shortages": []}
	check_doc_permission(RUN_DT, run, "read")
	doc = frappe.get_doc(RUN_DT, run)
	shortages = frappe.get_all(
		"PPC MRP Line",
		filters={"parent": run, "parenttype": RUN_DT, "net_qty": [">", 0]},
		fields=["item_code", "item_name", "warehouse", "net_qty", "uom", "action", "required_date", "planned_order_date", "is_late"],
		order_by="is_late desc, required_date asc, net_qty desc",
		limit=15,
	)
	keys = ["demand_lines", "demand_qty", "shortage_items", "shortage_qty", "purchase_lines", "purchase_qty", "production_lines", "production_qty", "review_lines", "late_lines"]
	return {
		"run": run, "status": doc.status, "company": doc.company, "calculated_on": doc.calculated_on, "runs": runs,
		"summary": {k: doc.get(k) for k in keys}, "shortages": shortages,
		"warnings": [w for w in (doc.warnings or "").split("\n") if w],
	}


# ================================================================================================= reports
REPORT_GROUPS = {
	"item": ["item_code", "item_name", "uom"],
	"warehouse": ["warehouse", "item_code", "item_name", "uom"],
	"date": ["required_date", "item_code", "item_name", "uom"],
}


def report_rows(group: str, filters: dict) -> list[dict]:
	"""Item-wise / warehouse-wise / date-wise material requirement, summed over the lines of runs."""
	filters = frappe._dict(filters or {})
	run = filters.run or frappe.db.get_value(RUN_DT, {"status": ["in", [S_CALCULATED, S_CREATED]], **({"company": filters.company} if filters.company else {})}, "name", order_by="calculated_on desc")
	if not run:
		return []
	check_doc_permission(RUN_DT, run, "read")
	cond, args = ["l.parent = %(run)s", "l.parenttype = 'PPC MRP Run'"], {"run": run}
	for field in ("item_code", "warehouse", "action"):
		if filters.get(field):
			cond.append(f"l.{field} = %({field})s")
			args[field] = filters[field]
	if filters.from_date:
		cond.append("l.required_date >= %(from_date)s")
		args["from_date"] = filters.from_date
	if filters.to_date:
		cond.append("l.required_date <= %(to_date)s")
		args["to_date"] = filters.to_date
	if filters.only_shortages:
		cond.append("l.net_qty > 0")
	cols = REPORT_GROUPS[group]
	select = ", ".join(f"l.{c}" for c in cols)
	return frappe.db.sql(
		f"""select {select}, sum(l.gross_qty) gross_qty, sum(l.from_stock) from_stock, sum(l.from_supply) from_supply,
			sum(l.net_qty) net_qty, min(l.planned_order_date) planned_order_date,
			sum(case when l.action = 'Purchase' then l.net_qty else 0 end) purchase_qty,
			sum(case when l.action = 'Manufacture' then l.net_qty else 0 end) production_qty,
			max(l.is_late) is_late
		from `tabPPC MRP Line` l where {' and '.join(cond)}
		group by {select} order by {select}""",
		args,
		as_dict=True,
	)
