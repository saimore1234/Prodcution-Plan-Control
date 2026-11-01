"""Material Requirements Planning on top of ERPNext.

One PPC MRP Run is calculated by exactly one function (`calculate_run`), whether a person pressed
Calculate (manual, `calculate`) or a Sales Order was submitted (automatic, services/mrp_auto.py).
Flow: demand (Sales Orders, Production Plans) -> Procurement Type decides purchase vs manufacture ->
multilevel BOM explosion and warehouse-wise netting (services/mrp_engine.py) -> recommendations ->
**MRP Review** (select / deselect / edit quantities) -> after an explicit confirmation, draft
Material Request / Work Orders. Nothing is ever submitted, and nothing is created without reading
stock, supply and demand again first.

What counts as what (so nothing is counted twice):

* Demand: open Sales Order lines (ordered - delivered). A Production Plan row that belongs to a Sales
  Order line is *not* extra demand - the Sales Order line already is; only plan rows without a Sales
  Order (make-to-stock) are added, for planned - produced. A run can instead be limited to listed
  Sales Orders / Production Plans.
* Usable stock: Bin actual qty, minus stock reserved for existing Work Orders / subcontracting, minus
  Stock Reservation Entries that do not belong to a demand line of this run, minus Sales Order demand
  that is *not* part of this run (the Bin's reserved qty beyond this run's own Sales Order lines) - so a
  run for one Sales Order does not spend stock that other open orders are waiting for.
* Production coverage / pending supply: open Purchase Orders (ordered), open Material Requests
  (indented), open Work Orders (planned) and every *draft* Purchase Material Request / Work Order,
  which ERPNext does not count yet.
"""

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate, now_datetime, nowdate

from ppc_production_control.services import audit
from ppc_production_control.services.constants import CAP_MANAGE_PLANS, CAP_VIEW, R_FAILED, R_SUCCESS
from ppc_production_control.services.mrp_engine import (
	ACTION_MANUFACTURE,
	ACTION_PURCHASE,
	MRPError,
	run_mrp,
	summarize,
)
from ppc_production_control.services.permissions import check_doc_permission, require_capability
from ppc_production_control.services.utils import commit_if_allowed

RUN_DT = "PPC MRP Run"
LINE_DT = "PPC MRP Line"
CLOSED_SO_STATUSES = ("Closed", "On Hold", "Completed", "Cancelled")
CLOSED_PLAN_STATUSES = ("Completed", "Closed", "Cancelled")
S_DRAFT, S_QUEUED, S_CALCULATED, S_CREATED, S_FAILED = "Draft", "Queued", "Calculated", "Documents Created", "Failed"
A_CALCULATE, A_AUTO, A_REVIEW, A_CREATE, A_RETRY, A_FAILED = (
	"MRP Calculate", "MRP Auto Calculate", "MRP Review Saved", "MRP Create Documents", "MRP Retry", "MRP Calculation Failed",
)
MAX_EDIT_FACTOR = 10  # an edited quantity may not exceed this multiple of the recommended shortage
CREATABLE = (ACTION_PURCHASE, ACTION_MANUFACTURE)


# ============================================================================================ parameters
def params_of(run) -> dict:
	"""The inputs of a run as a plain dict (also what the revalidation recalculates from)."""
	sources = [(r.source_doctype, r.source_name) for r in run.get("sources") or [] if r.source_doctype and r.source_name]
	return {
		"name": run.name,
		"company": run.company,
		"from_date": getdate(run.from_date) if run.from_date else None,
		"to_date": getdate(run.to_date) if run.to_date else getdate(add_days(nowdate(), 365)),
		"include_sales_orders": cint(run.include_sales_orders),
		"include_production_plans": cint(run.include_production_plans),
		"default_warehouse": run.default_warehouse,
		"warehouses": [r.warehouse for r in run.get("warehouses") or [] if r.warehouse],
		"hours_per_day": flt(run.working_hours_per_day) or 8.0,
		"sales_orders": sorted({n for d, n in sources if d == "Sales Order"}),
		"production_plans": sorted({n for d, n in sources if d == "Production Plan"}),
	}


# ================================================================================================ demand
def fetch_demands(p: dict) -> tuple[list[dict], list[str]]:
	"""Open demand of the run as [{key, source_doctype, source_name, row_name, item_code, qty, uom,
	required_date, warehouse, bom_no, source, root}], plus warnings. Duplicate keys are impossible by
	construction (one dict entry per source row).

	Listed Sales Orders / Production Plans replace the date window for their own kind: the person named
	them, so their demand must not silently drop out because of a delivery date."""
	demands: dict[str, dict] = {}
	warnings: list[str] = []
	allowed = set(p["warehouses"])
	skipped_wh = skipped_bundle = skipped_covered = 0
	explicit_so, explicit_pp = p.get("sales_orders") or [], p.get("production_plans") or []
	explicit = bool(explicit_so or explicit_pp)
	do_so = bool(explicit_so) if explicit else bool(p["include_sales_orders"])
	do_pp = bool(explicit_pp) if explicit else bool(p["include_production_plans"])
	window = {"to_date": p["to_date"], "from_date": p["from_date"]}

	if do_so:
		rows = frappe.db.sql(
			"""
			select soi.name as row_name, so.name as sales_order, soi.item_code, soi.qty, soi.delivered_qty,
				soi.conversion_factor, soi.stock_uom, soi.warehouse, soi.bom_no,
				coalesce(soi.delivery_date, so.delivery_date, so.transaction_date) as required_date
			from `tabSales Order Item` soi join `tabSales Order` so on so.name = soi.parent
			where so.docstatus = 1 and so.company = %(company)s and so.status not in %(closed)s
				and soi.delivered_qty < soi.qty and ifnull(soi.delivered_by_supplier, 0) = 0
				and ((%(explicit)s = 1 and so.name in %(names)s) or (%(explicit)s = 0
					and coalesce(soi.delivery_date, so.delivery_date, so.transaction_date) <= %(to_date)s
					and (%(from_date)s is null or coalesce(soi.delivery_date, so.delivery_date, so.transaction_date) >= %(from_date)s)))
			""",
			{"company": p["company"], "closed": CLOSED_SO_STATUSES, "explicit": 1 if explicit_so else 0, "names": tuple(explicit_so) or ("",), **window},
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
			demands[key] = _demand(key, "Sales Order", r.sales_order, r.row_name, r.item_code, qty, r.stock_uom, r.required_date, wh, r.bom_no)

	if do_pp:
		rows = frappe.db.sql(
			"""
			select ppi.name as row_name, pp.name as plan, ppi.item_code, ppi.planned_qty, ppi.produced_qty,
				ppi.stock_uom, ppi.warehouse, ppi.bom_no, ppi.sales_order_item,
				coalesce(date(ppi.planned_start_date), pp.posting_date) as required_date
			from `tabProduction Plan Item` ppi join `tabProduction Plan` pp on pp.name = ppi.parent
			where pp.docstatus = 1 and pp.company = %(company)s and pp.status not in %(closed)s
				and ((%(explicit)s = 1 and pp.name in %(names)s) or (%(explicit)s = 0
					and coalesce(date(ppi.planned_start_date), pp.posting_date) <= %(to_date)s
					and (%(from_date)s is null or coalesce(date(ppi.planned_start_date), pp.posting_date) >= %(from_date)s)))
			""",
			{"company": p["company"], "closed": CLOSED_PLAN_STATUSES, "explicit": 1 if explicit_pp else 0, "names": tuple(explicit_pp) or ("",), **window},
			as_dict=True,
		)
		for r in rows:
			if do_so and r.sales_order_item:
				skipped_covered += 1  # the Sales Order line is the demand; counting the plan row too would double it
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

	if skipped_covered:
		warnings.append(_("{0} Production Plan row(s) belong to a Sales Order line and are not counted a second time.").format(skipped_covered))
	if skipped_wh:
		warnings.append(_("{0} demand line(s) skipped: their warehouse is not one of the selected warehouses.").format(skipped_wh))
	if skipped_bundle:
		warnings.append(_("{0} Sales Order line(s) skipped: Product Bundles are planned through their items.").format(skipped_bundle))
	if explicit and not demands:
		warnings.append(_("The listed documents have no open demand (not submitted, closed, fully delivered or in other warehouses)."))
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
		"root": f"{doctype} {name}",
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
		self.warehouses = (set(p["warehouses"]) | {p["default_warehouse"]}) if p["warehouses"] else None
		self._items: dict[str, dict | None] = {}
		self._default_boms: dict[str, str | None] = {}
		self._boms: dict[str, dict | None] = {}
		self._positions: dict[tuple, dict] = {}
		self._wh_leaves: dict[str, list[str]] = {}
		self._whole: dict[str, int] = {}
		# stock reserved for the Sales Order lines that are demand in this run is that demand's own stock
		self._own_so_rows = [d["row_name"] for d in demands if d["source_doctype"] == "Sales Order"]
		self._own_so_qty: dict[tuple, float] = defaultdict(float)
		for d in demands:
			if d["source_doctype"] == "Sales Order":
				self._own_so_qty[(d["item_code"], d["warehouse"] or self.default_warehouse)] += flt(d["qty"])
		self.notes: set[str] = set()

	# ---- items
	def _whole_number(self, uom) -> int:
		if uom not in self._whole:
			self._whole[uom] = cint(frappe.db.get_value("UOM", uom, "must_be_whole_number")) if uom else 0
		return self._whole[uom]

	def item(self, code):
		if code not in self._items:
			row = frappe.db.get_value(
				"Item", code,
				["item_name", "stock_uom", "is_stock_item", "is_purchase_item", "lead_time_days", "disabled", "ppc_procurement_type"],
				as_dict=True,
			)
			self._items[code] = None if not row or row.disabled else {
				"item_name": row.item_name,
				"stock_uom": row.stock_uom,
				"is_stock_item": cint(row.is_stock_item),
				"is_purchase_item": cint(row.is_purchase_item),
				"lead_time_days": cint(row.lead_time_days),
				"procurement_type": row.ppc_procurement_type or "",
				"whole_number": self._whole_number(row.stock_uom),
			}
		return self._items[code]

	def default_bom(self, code):
		if code not in self._default_boms:
			self._default_boms[code] = frappe.db.get_value(
				"BOM", {"item": code, "is_default": 1, "is_active": 1, "docstatus": 1, "company": self.p["company"]}, "name"
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
			self._positions[key] = stock_position(item, self._leaves(warehouse), self._own_so_rows, self._own_so_qty.get(key, 0.0))
		return self._positions[key]


def stock_position(item: str, warehouses: list[str], own_so_rows: list[str] | None = None, own_so_qty: float = 0.0) -> dict:
	"""Usable stock and pending supply of one item in the given (leaf) warehouses, read live."""
	bins = frappe.db.sql(
		"""select coalesce(sum(actual_qty), 0) actual, coalesce(sum(reserved_qty), 0) res_so,
			coalesce(sum(reserved_qty_for_production), 0) res_prod,
			coalesce(sum(reserved_qty_for_sub_contract), 0) res_sub, coalesce(sum(ordered_qty), 0) ordered,
			coalesce(sum(indented_qty), 0) indented, coalesce(sum(planned_qty), 0) planned
		from `tabBin` where item_code = %s and warehouse in %s""",
		(item, tuple(warehouses)),
		as_dict=True,
	)[0]
	reserved_other = _reserved_by_others(item, warehouses, own_so_rows or [])
	# Sales Order demand beyond the lines this run already counts as demand is somebody else's stock
	other_orders = max(0.0, flt(bins.res_so) - flt(own_so_qty))
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
	reserved = flt(bins.res_prod) + flt(bins.res_sub) + reserved_other + other_orders
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
	"""Demand -> netted plan. Pure read of ERPNext; raises a validation error for impossible data (BOM loops)."""
	demands, warnings = fetch_demands(p)
	ctx = SiteContext(p, demands)
	try:
		result = run_mrp(demands, ctx)
	except MRPError as e:
		frappe.throw(_(str(e)), frappe.ValidationError)
	warnings += result["warnings"] + sorted(ctx.notes)
	return {"demands": demands, "lines": result["lines"], "warnings": warnings, "summary": summarize(demands, result["lines"])}


def calculate(run_name: str) -> dict:
	"""Manual calculation: permission-checked, then the shared engine."""
	require_capability(CAP_MANAGE_PLANS)
	check_doc_permission(RUN_DT, run_name, "write")
	return calculate_run(run_name, automatic=False)


def calculate_run(run_name: str, *, automatic: bool = False) -> dict:
	"""THE calculation: (re)calculate a run and store demand lines and recommendations on it.

	Called by the manual button / API and by the automatic Sales Order job, with identical results; only
	the permission check (done by the caller) and the audit action differ."""
	frappe.db.get_value(RUN_DT, run_name, "name", for_update=True)  # row lock: two clicks / jobs cannot calculate at once
	run = frappe.get_doc(RUN_DT, run_name)
	if run.status == S_CREATED:
		frappe.throw(_("Documents were already created from this run. Start a new run instead of recalculating it."))
	p = params_of(run)
	if not p["default_warehouse"]:
		frappe.throw(_("Select a default warehouse (or set one in PPC Settings for automatic runs)."))
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
		creatable = r["net_qty"] > 0 and r["action"] in CREATABLE
		run.append("lines", dict(
			{k: r[k] for k in ("item_code", "item_name", "uom", "whole_number", "procurement_type", "warehouse", "required_date", "gross_qty",
				"from_stock", "from_supply", "net_qty", "action", "bom_no", "lead_time_days", "planned_order_date", "is_late", "level",
				"sources", "source_documents", "note", "actual_qty", "reserved_qty", "incoming_qty")},
			create_document=1 if creatable else 0,
			approved_qty=r["net_qty"] if creatable else 0,
		))
	for k, v in result["summary"].items():
		run.set(k, v)
	run.status = S_CALCULATED
	run.calculated_on = now_datetime()
	run.calculated_by = frappe.session.user
	run.warnings = "\n".join(result["warnings"])
	run.error_message = None
	run.flags.ignore_permissions = automatic
	run.save()
	audit.log(A_AUTO if automatic else A_CALCULATE, R_SUCCESS, details={"run": run.name, "trigger": run.get("trigger"), **result["summary"]})
	return {"run": run.name, "status": run.status, "summary": result["summary"], "warnings": result["warnings"]}


def mark_failed(run_name: str, error: str):
	"""Record that a calculation failed. The run stays visible with the reason and can be retried by hand;
	this never raises (it runs inside a failure handler)."""
	try:
		retries = cint(frappe.db.get_value(RUN_DT, run_name, "retry_count"))
		frappe.db.set_value(RUN_DT, run_name, {"status": S_FAILED, "error_message": (error or "")[:2000], "retry_count": retries + 1})
		audit.log(A_FAILED, R_FAILED, error=(error or "")[:2000], details={"run": run_name})
	except Exception:
		frappe.log_error(title="PPC MRP: could not record a failed run", message=frappe.get_traceback())


def retry(run_name: str) -> dict:
	"""Manual retry of a failed (or stuck queued) calculation, with the same engine and the same checks."""
	require_capability(CAP_MANAGE_PLANS)
	check_doc_permission(RUN_DT, run_name, "write")
	status = frappe.db.get_value(RUN_DT, run_name, "status")
	if status not in (S_FAILED, S_QUEUED):
		frappe.throw(_("Only a failed or queued run can be retried (this one is {0}).").format(_(status)))
	savepoint = "ppc_mrp_retry"
	frappe.db.savepoint(savepoint)
	try:
		out = calculate_run(run_name, automatic=False)
	except Exception as e:
		frappe.db.rollback(save_point=savepoint)
		mark_failed(run_name, str(e) or frappe.get_traceback(with_context=False))
		raise
	audit.log(A_RETRY, R_SUCCESS, details={"run": run_name})
	return out


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


# ============================================================================================ MRP Review
def review_data(run_name: str) -> dict:
	"""Everything the MRP Review screen shows for one run."""
	require_capability(CAP_VIEW)
	check_doc_permission(RUN_DT, run_name, "read")
	run = frappe.get_doc(RUN_DT, run_name)
	can_edit = run.status == S_CALCULATED and bool(frappe.has_permission(RUN_DT, "write", doc=run))
	fields = ("name", "idx", "item_code", "item_name", "warehouse", "required_date", "planned_order_date", "is_late", "action", "procurement_type",
		"gross_qty", "from_stock", "from_supply", "net_qty", "approved_qty", "uom", "whole_number", "create_document", "source_documents",
		"note", "document_type", "document", "status", "bom_no", "level")
	lines = []
	for r in run.lines:
		row = {f: r.get(f) for f in fields}
		row["editable"] = bool(can_edit and r.action in CREATABLE and flt(r.net_qty) > 0 and not r.document)
		lines.append(row)
	return {
		"run": run.name, "status": run.status, "company": run.company, "trigger": run.get("trigger"), "source_sales_order": run.get("source_sales_order"),
		"calculated_on": run.calculated_on, "warnings": [w for w in (run.warnings or "").split("\n") if w], "error_message": run.error_message,
		"material_request": run.material_request, "can_edit": can_edit, "can_retry": run.status in (S_FAILED, S_QUEUED),
		"max_edit_factor": MAX_EDIT_FACTOR, "lines": lines,
	}


def _validate_selection(run, selections: list[dict]):
	"""Apply the user's tick boxes / quantities to the run's lines, or raise one error naming every bad row."""
	by_name = {r.name: r for r in run.lines}
	errors = []
	for s in selections or []:
		row = by_name.get(s.get("name"))
		if row is None:
			errors.append(_("A line in your selection no longer exists. Reload the review."))
			continue
		label = _("Row {0} ({1})").format(row.idx, row.item_code)
		wanted = cint(s.get("create_document"))
		editable = row.action in CREATABLE and flt(row.net_qty) > 0 and not row.document
		if wanted and not editable:
			errors.append(_("{0}: nothing to create for this line.").format(label))
			continue
		if not editable:
			continue
		qty = flt(s.get("approved_qty")) if s.get("approved_qty") not in (None, "") else flt(row.approved_qty) or flt(row.net_qty)
		if wanted:
			if qty <= 0:
				errors.append(_("{0}: the quantity must be greater than zero.").format(label))
				continue
			if cint(row.whole_number) and abs(qty - round(qty)) > 1e-9:
				errors.append(_("{0}: {1} must be a whole number.").format(label, row.uom))
				continue
			if qty > flt(row.net_qty) * MAX_EDIT_FACTOR + 1e-9:
				errors.append(_("{0}: the quantity may not exceed {1} times the shortage ({2}).").format(label, MAX_EDIT_FACTOR, flt(row.net_qty)))
				continue
			row.approved_qty = flt(qty, 6)
		row.create_document = wanted
	if errors:
		frappe.throw("<br>".join(errors), frappe.ValidationError)


def save_review(run_name: str, selections) -> dict:
	"""Store the review (ticks and quantities) without creating anything."""
	require_capability(CAP_MANAGE_PLANS)
	check_doc_permission(RUN_DT, run_name, "write")
	frappe.db.get_value(RUN_DT, run_name, "name", for_update=True)
	run = frappe.get_doc(RUN_DT, run_name)
	if run.status != S_CALCULATED:
		frappe.throw(_("Only a calculated run can be reviewed (this one is {0}).").format(_(run.status)))
	_validate_selection(run, selections)
	run.save()
	chosen = [r for r in run.lines if cint(r.create_document)]
	audit.log(A_REVIEW, R_SUCCESS, details={"run": run.name, "selected": len(chosen)})
	return {"run": run.name, "selected": len(chosen)}


# ========================================================================================== draft documents
def create_documents(run_name: str, selections=None, confirm=0) -> dict:
	"""Create the draft Material Request (purchase lines) and draft Work Orders (manufacture lines).

	Only on explicit confirmation, only for the lines left ticked in the review, with the reviewed
	quantities. Stock, supply and demand are read again first: a line whose shortage has disappeared is
	skipped, and a line is never created above what is still short unless the reviewer deliberately raised
	it. Everything is created in one transaction (all or nothing) and stays in draft."""
	if not cint(confirm):
		frappe.throw(_("Creating documents needs your explicit confirmation."))
	require_capability(CAP_MANAGE_PLANS)
	check_doc_permission(RUN_DT, run_name, "write")
	frappe.db.get_value(RUN_DT, run_name, "name", for_update=True)  # row lock: one creation at a time
	run = frappe.get_doc(RUN_DT, run_name)
	if run.status == S_CREATED:
		frappe.throw(_("Documents were already created from this run."))
	if run.status != S_CALCULATED:
		frappe.throw(_("Calculate the run before creating documents."))
	if not frappe.has_permission("Material Request", "create") or not frappe.has_permission("Work Order", "create"):
		frappe.throw(_("You need permission to create Material Requests and Work Orders."), frappe.PermissionError)
	if selections is not None:
		_validate_selection(run, selections)
	chosen = [r for r in run.lines if cint(r.create_document) and r.action in CREATABLE and flt(r.net_qty) > 0 and not r.document]
	if not chosen:
		frappe.throw(_("Select at least one recommendation to create."))

	fresh = compute(params_of(run))
	fresh_net: dict[tuple, float] = defaultdict(float)
	for r in fresh["lines"]:
		fresh_net[(r["item_code"], r["warehouse"], str(r["required_date"]), r["action"])] += r["net_qty"]
	vanished = sorted({d.source_key for d in run.demands} - {d["key"] for d in fresh["demands"]})

	buy, make, skipped = [], [], []
	for row in chosen:
		key = (row.item_code, row.warehouse, str(getdate(row.required_date)), row.action)
		approved = flt(row.approved_qty) or flt(row.net_qty)
		still_short = fresh_net.get(key, 0)
		if still_short <= 0:
			row.status = _("Skipped: no longer short")
			row.create_document = 0
			skipped.append(row.item_code)
			continue
		qty = approved if approved > flt(row.net_qty) else min(approved, still_short)  # raising above the shortage was the reviewer's choice
		if qty < approved:
			row.status = _("Reduced to {0}: stock or supply changed").format(qty)
		(buy if row.action == ACTION_PURCHASE else make).append((row, qty))

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
		audit.log(A_CREATE, R_FAILED, error=str(e)[:2000], details={"run": run_name})
		raise
	audit.log(A_CREATE, R_SUCCESS, details={"run": run_name, "created": created, "skipped": skipped, "demand_gone": vanished})
	return {"run": run_name, "created": created, "skipped": skipped, "status": run.status, "demand_gone": vanished}


def _source_text(rows) -> str:
	seen = list(dict.fromkeys(s.strip() for r in rows for s in (r.source_documents or "").split(",") if s.strip()))
	return ", ".join(seen[:12]) + (f" (+{len(seen) - 12} more)" if len(seen) > 12 else "")


def _make_material_request(run, buy: list[tuple]):
	mr = frappe.new_doc("Material Request")
	mr.material_request_type = "Purchase"
	mr.company = run.company
	mr.transaction_date = nowdate()
	mr.schedule_date = max(getdate(nowdate()), min(getdate(r.planned_order_date or r.required_date) for r, _q in buy))  # ERPNext refuses past dates; late lines stay flagged on the run
	mr.ppc_mrp_run = run.name
	mr.ppc_source_documents = _source_text(r for r, _q in buy)
	for row, qty in buy:
		mr.append("items", {
			"item_code": row.item_code, "qty": qty, "uom": row.uom, "stock_uom": row.uom, "conversion_factor": 1,
			"warehouse": row.warehouse, "schedule_date": max(getdate(nowdate()), getdate(row.planned_order_date or row.required_date)),
			"ppc_source_documents": row.source_documents,
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
	wo.ppc_source_documents = row.source_documents
	wo.insert()
	return wo


# ================================================================================================ dashboard
def dashboard(company: str | None = None, run: str | None = None) -> dict:
	"""Totals of one run (the latest calculated one by default), the most short items and the runs needing attention."""
	require_capability(CAP_VIEW)
	if not run:
		filters = {"status": ["in", [S_CALCULATED, S_CREATED]]}
		if company:
			filters["company"] = company
		run = frappe.db.get_value(RUN_DT, filters, "name", order_by="calculated_on desc")
	runs = frappe.get_all(RUN_DT, fields=["name", "company", "status", "trigger", "calculated_on", "to_date"], order_by="creation desc", limit=10)
	counts = {s: frappe.db.count(RUN_DT, {"status": s}) for s in (S_CALCULATED, S_FAILED, S_QUEUED)}
	attention = {"awaiting_review": counts[S_CALCULATED], "failed": counts[S_FAILED], "queued": counts[S_QUEUED]}
	failed = frappe.get_all(RUN_DT, filters={"status": S_FAILED}, fields=["name", "source_sales_order", "error_message", "modified"], order_by="modified desc", limit=5)
	if not run:
		return {"run": None, "runs": runs, "summary": None, "shortages": [], "attention": attention, "failed": failed}
	check_doc_permission(RUN_DT, run, "read")
	doc = frappe.get_doc(RUN_DT, run)
	shortages = frappe.get_all(
		LINE_DT,
		filters={"parent": run, "parenttype": RUN_DT, "net_qty": [">", 0]},
		fields=["item_code", "item_name", "warehouse", "net_qty", "uom", "action", "required_date", "planned_order_date", "is_late", "source_documents"],
		order_by="is_late desc, required_date asc, net_qty desc",
		limit=15,
	)
	keys = ["demand_lines", "demand_qty", "shortage_items", "shortage_qty", "purchase_lines", "purchase_qty", "production_lines", "production_qty", "review_lines", "late_lines"]
	return {
		"run": run, "status": doc.status, "company": doc.company, "calculated_on": doc.calculated_on, "runs": runs,
		"summary": {k: doc.get(k) for k in keys}, "shortages": shortages, "attention": attention, "failed": failed,
		"warnings": [w for w in (doc.warnings or "").split("\n") if w],
	}


# ================================================================================================= reports
REPORT_GROUPS = {
	"item": ["item_code", "item_name", "uom"],
	"warehouse": ["warehouse", "item_code", "item_name", "uom"],
	"date": ["required_date", "item_code", "item_name", "uom"],
}


def report_rows(group: str, filters: dict) -> list[dict]:
	"""Item-wise / warehouse-wise / date-wise material requirement, summed over the lines of a run."""
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


# ================================================================================ master data helper
def backfill_procurement_type(dry_run: int = 1, only: list[str] | None = None) -> dict:
	"""Fill a *blank* Procurement Type on Items from what they already are: an active default BOM ->
	Manufacture, otherwise 'Allow Purchase' -> Purchase. Items that already have a value, and items that
	are neither, are left alone. Dry run by default:

	    bench --site <site> execute ppc_production_control.services.mrp.backfill_procurement_type --kwargs "{'dry_run': 0}"
	"""
	boms = set(frappe.get_all("BOM", filters={"is_default": 1, "is_active": 1, "docstatus": 1}, pluck="item"))
	filters = {"disabled": 0, "ppc_procurement_type": ["in", ["", None]]}
	if only:
		filters["name"] = ["in", only]
	items = frappe.get_all("Item", filters=filters, fields=["name", "is_purchase_item"])
	plan = {"Manufacture": [], "Purchase": [], "skipped": []}
	for it in items:
		kind = "Manufacture" if it.name in boms else "Purchase" if cint(it.is_purchase_item) else None
		(plan[kind] if kind else plan["skipped"]).append(it.name)
	if not cint(dry_run):
		for kind in ("Manufacture", "Purchase"):
			for name in plan[kind]:
				frappe.db.set_value("Item", name, "ppc_procurement_type", kind, update_modified=False)
		commit_if_allowed()
	return {"dry_run": bool(cint(dry_run)), **{k: len(v) for k, v in plan.items()}, "sample": {k: v[:5] for k, v in plan.items()}}
