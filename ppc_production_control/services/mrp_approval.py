"""MRP review and approval: Sales Order -> calculated run -> a person reviews -> approves -> draft documents.

This module is the server side of the review dialog and of the MRP Review page (one implementation for both). The state of a run
is two independent things, so every existing status keeps its meaning:

* `status`           Draft / Queued / Calculated / Documents Created / Failed - what the calculation and the document creation did.
* `approval_status`  Awaiting Review / Approved / Cancelled - what the people did with the recommendations.

Rules the code enforces (each has a test):

* Nothing is created before an approval. The one exception is an automation policy an administrator switched on (Automatic Draft
  Creation, or Review Mode "Automatic"): the run then records that policy and who enabled it, the execution is audited, and nothing
  is submitted either way.
* An approval locks the run row, carries a client-made `approval_key` (unique in the database) and the `token` of the review the
  person looked at. A repeated request with the same key only replays the result (a double click creates nothing twice); another
  key, a cancelled run, a run changed since the review was opened, or a run that was already approved is refused.
* Stock, supply and demand are read again inside the approval (`mrp._create_from`): a line that is no longer short is skipped, a
  quantity is never created above what is still short unless the reviewer deliberately raised it, and everything is created in one
  transaction.
* A skipped recommendation needs a reason; a cancellation needs a reason. Revisions keep the calculated quantity in `original_qty`.
* Every action leaves a PPC Audit Log row naming the run, the source document(s), the user, the quantities, the reason and the
  documents. Refusals are logged too (result "Skipped").
* The same permissions as the rest of MRP: the *Manage Plans* capability plus write permission on the run for every change,
  the *View* capability plus read permission for looking.
* Everything here refuses politely on a site that has not been migrated yet (`mrp.approval_model_ready`).
"""

import json

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate, now_datetime, nowdate

from ppc_production_control.services import audit, mrp, mrp_auto, mrp_supply
from ppc_production_control.services.mrp_engine import ACTION_REVIEW
from ppc_production_control.services.constants import (
	A_MRP_APPROVE,
	A_MRP_CALC_NOW,
	A_MRP_CANCEL,
	A_MRP_RECALC,
	A_MRP_REVISE,
	A_MRP_SKIP,
	AP_APPROVED,
	AP_AWAITING,
	AP_CANCELLED,
	AUDIT_DT,
	CAP_MANAGE_PLANS,
	CAP_VIEW,
	MIN_REASON_LENGTH,
	R_FAILED,
	R_SKIPPED,
	R_SUCCESS,
)
from ppc_production_control.services.permissions import check_doc_permission, has_capability, require_capability
from ppc_production_control.services.utils import commit_if_allowed

RUN_DT = mrp.RUN_DT
SOURCE_DOCTYPES = mrp_auto.SOURCE_DOCTYPES  # ("Sales Order", "Production Plan")
MAX_BINS_PER_ITEM = 10
MAX_SUPPLY_PER_ITEM = 12
MAX_BOM_ITEMS = 60


# ============================================================================================ helpers
def _require_model():
	if not mrp.approval_model_ready():
		frappe.throw(_("The review and approval fields are not installed on this site yet. Run bench migrate first."))


def _sources(run) -> list[list[str]]:
	rows = [[s.source_doctype, s.source_name] for s in run.get("sources") or [] if s.source_doctype and s.source_name]
	if not rows:
		rows = sorted({(d.source_doctype, d.source_name) for d in run.get("demands") or []})
		rows = [list(r) for r in rows]
	return rows


def _log(action: str, run, result: str = R_SUCCESS, *, reason: str | None = None, error: str | None = None, **details):
	"""One audit row: run, source documents, approval status and whatever the action adds. Never raises."""
	audit.log(
		action, result, reason=reason, error=error,
		details={"run": run.name, "source": _sources(run), "approval_status": mrp.approval_state(run), "reason": reason, **details},
	)


def _reject(run, action: str, message: str, **details):
	"""Refuse an action and keep a record of the refusal: the audit row is committed before the error rolls the request back."""
	_log(action, run, R_SKIPPED, error=message, rejected=True, **details)
	commit_if_allowed()
	frappe.throw(message)


def _prepare(run_name: str):
	"""Capability + document permission, the migration check and the row lock; returns the run. Used by every change."""
	require_capability(CAP_MANAGE_PLANS)
	check_doc_permission(RUN_DT, run_name, "write")
	_require_model()
	# One change at a time per run: the second caller waits here and then sees the first one's result. The run is read FOR UPDATE too -
	# a plain read would use the snapshot this request took before it waited (REPEATABLE READ) and could still show the old state.
	frappe.db.get_value(RUN_DT, run_name, "name", for_update=True)
	return frappe.get_doc(RUN_DT, run_name, for_update=True)


def _check_reviewable(run, action: str):
	state = mrp.approval_state(run)
	if state == AP_CANCELLED:
		_reject(run, action, _("This review was cancelled. Recalculate it to reopen it."))
	if run.status == mrp.S_CREATED or state == AP_APPROVED:
		_reject(run, action, _("Documents were already created from this run; its review is closed."))
	if run.status != mrp.S_CALCULATED:
		_reject(run, action, _("Only a calculated run can be reviewed (this one is {0}).").format(_(run.status)))


def _clean_reason(reason, what: str) -> str:
	reason = (reason or "").strip()
	if len(reason) < MIN_REASON_LENGTH:
		frappe.throw(_("A reason is required to {0}.").format(what))
	return reason[:500]


def _line(run, line_name: str):
	row = next((r for r in run.lines if r.name == line_name), None)
	if row is None:
		frappe.throw(_("That line no longer exists. Reload the review."))
	return row


def _is_creatable(row) -> bool:
	return row.action in mrp.CREATABLE and flt(row.net_qty) > 0 and not row.document


def _created(run) -> list[dict]:
	return [
		{"doctype": r.document_type, "name": r.document, "item_code": r.item_code, "qty": flt(r.approved_qty), "uom": r.uom}
		for r in run.lines if r.document
	]


# ============================================================================================ the dialog's questions
def run_for_source(doctype: str, name: str) -> dict:
	"""What the Sales Order / Production Plan dialog asks every couple of seconds. `state`:

	waiting      no run yet (the background job has not started)       calculating  the run exists and is being calculated
	calculated   recommendations are ready for review                   approved     the draft documents exist
	cancelled    the review was cancelled                               failed       the calculation failed (see `error`)

	`enabled` is false (and the dialog never opens) when automatic MRP is off for this document type, the user has no PPC view
	capability, or the site has not been migrated. Only `calculated` / `approved` / `cancelled` are ever shown as a result."""
	require_capability(CAP_VIEW)
	if doctype not in SOURCE_DOCTYPES:
		frappe.throw(_("{0} is not a source of MRP.").format(_(doctype)))
	check_doc_permission(doctype, name, "read")
	if not mrp.approval_model_ready():
		return {"enabled": False, "reason": "not_migrated"}
	if not mrp_auto.enabled(doctype):
		return {"enabled": False, "reason": "automatic_off"}
	if cint(frappe.db.get_value(doctype, name, "docstatus")) != 1:
		return {"enabled": True, "state": "not_submitted", "run": None}
	return {"enabled": True, **_state(doctype, name)}


def _state(doctype: str, name: str) -> dict:
	run = frappe.db.get_value(
		RUN_DT, {"idempotency_key": mrp_auto.idempotency_key(doctype, name)},
		["name", "status", "approval_status", "error_message", "retry_count", "calculated_on"], as_dict=True,
	)
	base = {"run": run.name if run else None, "can_calculate": has_capability(CAP_MANAGE_PLANS), "server_time": str(now_datetime())}
	if not run:
		return {**base, "state": "waiting", "status": None}
	state = {mrp.S_DRAFT: "calculating", mrp.S_QUEUED: "calculating", mrp.S_FAILED: "failed", mrp.S_CREATED: "approved"}.get(run.status)
	if run.status == mrp.S_CALCULATED:
		state = "cancelled" if run.approval_status == AP_CANCELLED else "calculated"
	return {**base, "state": state, "status": run.status, "approval_status": run.approval_status, "error": run.error_message, "calculated_on": run.calculated_on}


def calculate_now(doctype: str, name: str) -> dict:
	"""The dialog's "Calculate Now": when no background worker answers, do the same calculation right here. It cannot create a second
	run (the run's idempotency key is unique) and cannot calculate twice (the second caller waits on the row lock, then finds the run
	already calculated), and the queued job that may still come later finds the run and does nothing."""
	require_capability(CAP_MANAGE_PLANS)
	if doctype not in SOURCE_DOCTYPES:
		frappe.throw(_("{0} is not a source of MRP.").format(_(doctype)))
	check_doc_permission(doctype, name, "read")
	_require_model()
	if not mrp_auto.enabled(doctype):
		frappe.throw(_("Automatic MRP is switched off in PPC Settings."))
	if cint(frappe.db.get_value(doctype, name, "docstatus")) != 1:
		frappe.throw(_("Submit the {0} first.").format(_(doctype)))
	key = mrp_auto.idempotency_key(doctype, name)
	before = frappe.db.get_value(RUN_DT, {"idempotency_key": key}, "status")
	if not before:
		mrp_auto.run_for_source(doctype, name)  # creates the run once (unique key) and calculates it; failures are recorded on the run
	run_name = frappe.db.get_value(RUN_DT, {"idempotency_key": key}, "name")
	processed = bool(run_name) and _process_once(run_name)
	run = frappe.get_doc(RUN_DT, run_name) if run_name else None
	if run:
		_log(A_MRP_CALC_NOW, run, R_SUCCESS if run.status != mrp.S_FAILED else R_FAILED, error=run.error_message, previous_status=before, calculated_here=processed or not before)
	return {**_state(doctype, name), "calculated_here": bool(run) and (processed or not before)}


def _process_once(run_name: str) -> bool:
	"""Calculate a queued / failed run exactly once, however many callers arrive together: re-read the status under the row lock."""
	fields = ["status", "approval_status"] if mrp.approval_model_ready() else ["status"]
	row = frappe.db.get_value(RUN_DT, run_name, fields, as_dict=True, for_update=True)
	if not row or row.status not in (mrp.S_DRAFT, mrp.S_QUEUED, mrp.S_FAILED) or row.get("approval_status") == AP_CANCELLED:
		return False
	mrp_auto.process_run(run_name)
	return True


# ============================================================================================ the review's payload
def review_extras(run, base: dict) -> dict:
	"""What `mrp.review_data` adds for the dialog and the page: approval state, permissions, the sources, KPIs and, per line, the stock by
	warehouse, the BOM, the existing supply (and whose it is) and the revisions. Read-only; the numbers are the ones stored on the run."""
	can_manage = has_capability(CAP_MANAGE_PLANS)
	state = base["approval_status"]
	open_review = mrp.reviewable(run)
	writable = bool(frappe.has_permission(RUN_DT, "write", doc=run))
	creatable = bool(frappe.has_permission("Material Request", "create") and frappe.has_permission("Work Order", "create"))
	ready = mrp.approval_model_ready()
	closed = run.status == mrp.S_CREATED or state in (AP_APPROVED, AP_CANCELLED)
	own = {(s[0], s[1]) for s in _sources(run)} | {(d.source_doctype, d.source_name) for d in run.get("demands") or []}
	lines = base["lines"]
	by_name = {r.name: r for r in run.lines}

	extra = _line_details(run, lines, own)
	for row in lines:
		row.update(extra.get(row["name"], {}))
		src = by_name[row["name"]]
		row["skipped"] = bool(src.get("skip_reason"))
		# unticked without a reason (the old selection API allowed it): the approval will refuse until a reason is given or it is ticked again
		row["needs_reason"] = bool(_is_creatable(src) and not cint(src.create_document) and not src.get("skip_reason"))
		row["revised"] = bool(src.get("revised_by")) and abs(flt(src.approved_qty) - flt(src.get("original_qty") or src.net_qty)) > 1e-9

	creatable_lines = [r for r in run.lines if r.action in mrp.CREATABLE and flt(r.net_qty) > 0]
	kpis = {
		"demand_qty": flt(run.get("demand_qty")),
		"requirement_qty": sum(flt(r.gross_qty) for r in run.lines),
		"stock_qty": sum(flt(r.from_stock) for r in run.lines),
		"supply_qty": sum(flt(r.from_supply) for r in run.lines),
		"shortage_qty": sum(flt(r.net_qty) for r in run.lines),
		"purchase_qty": sum(flt(r.net_qty) for r in creatable_lines if r.action == mrp.ACTION_PURCHASE),
		"production_qty": sum(flt(r.net_qty) for r in creatable_lines if r.action == mrp.ACTION_MANUFACTURE),
		"purchase_lines": sum(1 for r in creatable_lines if r.action == mrp.ACTION_PURCHASE),
		"production_lines": sum(1 for r in creatable_lines if r.action == mrp.ACTION_MANUFACTURE),
		"review_lines": sum(1 for r in run.lines if r.action == ACTION_REVIEW),
		"late_lines": sum(1 for r in creatable_lines if r.is_late),
		"selected_lines": sum(1 for r in creatable_lines if cint(r.create_document) and not r.document),
	}
	today = getdate(nowdate())
	completions = [add_days(max(getdate(r.planned_order_date or r.required_date), today), cint(r.lead_time_days)) for r in creatable_lines]
	return {
		"approval_ready": ready,
		"approval": {
			"status": state, "reviewed_by": run.get("reviewed_by"), "reviewed_on": run.get("reviewed_on"), "approved_by": run.get("approved_by"),
			"approved_on": run.get("approved_on"), "cancel_reason": run.get("cancel_reason"), "automation_policy": run.get("automation_policy"),
		},
		"permissions": {
			"can_manage": can_manage,
			"can_edit": bool(base["can_edit"] and can_manage),
			"can_approve": bool(open_review and can_manage and writable and creatable and ready),
			"can_cancel": bool(can_manage and writable and ready and not closed),
			# a calculated run can be calculated again until its documents exist; a cancelled one too (that reopens it)
			"can_recalculate": bool(can_manage and writable and ready and run.status == mrp.S_CALCULATED and state in (AP_AWAITING, AP_CANCELLED)),
		},
		"sources": _source_cards(run),
		"kpis": kpis,
		"expected_completion": str(max(completions)) if completions else None,
		"documents": _created(run),
		"history": _history(run.name),
	}


def _source_cards(run) -> list[dict]:
	"""The orders / plans the run is for: customer, dates, status and the demanded items with their BOM."""
	level0_bom = {r.item_code: r.bom_no for r in run.lines if cint(r.level) == 0 and r.bom_no}
	cards = []
	for doctype, name in _sources(run):
		head = frappe.db.get_value(doctype, name, ["docstatus", "status", "company"], as_dict=True) or {}
		card = {"doctype": doctype, "name": name, "status": head.get("status"), "docstatus": head.get("docstatus")}
		if doctype == "Sales Order":
			so = frappe.db.get_value("Sales Order", name, ["customer", "customer_name", "transaction_date", "delivery_date"], as_dict=True) or {}
			card.update({"customer": so.get("customer"), "customer_name": so.get("customer_name"), "date": so.get("transaction_date"), "delivery_date": so.get("delivery_date")})
		elif doctype == "Production Plan":
			pp = frappe.db.get_value("Production Plan", name, ["posting_date"], as_dict=True) or {}
			card.update({"date": pp.get("posting_date")})
		items = []
		for d in run.get("demands") or []:
			if (d.source_doctype, d.source_name) == (doctype, name):
				items.append({
					"item_code": d.item_code, "item_name": frappe.db.get_value("Item", d.item_code, "item_name"), "qty": flt(d.qty), "uom": d.uom,
					"warehouse": d.warehouse, "required_date": d.required_date, "bom_no": level0_bom.get(d.item_code),
				})
		card["items"] = items
		cards.append(card)
	return cards


def _line_details(run, lines: list[dict], own: set) -> dict:
	"""Per line: stock by warehouse, BOM and the existing supply with its owner relation - three batched look-ups, not one per line."""
	items = sorted({r["item_code"] for r in lines})
	if not items:
		return {}
	bins: dict[str, list] = {}
	for b in frappe.db.sql(
		"""select item_code, warehouse, actual_qty, reserved_qty, ordered_qty, indented_qty, planned_qty from `tabBin`
		where item_code in %s and (actual_qty != 0 or reserved_qty != 0 or ordered_qty != 0 or indented_qty != 0 or planned_qty != 0)""",
		(tuple(items),), as_dict=True,
	):
		bins.setdefault(b.item_code, []).append(b)

	boms: dict[str, dict] = {}
	for bom in {r["bom_no"] for r in lines if r.get("bom_no")}:
		head = frappe.db.get_value("BOM", bom, ["item", "quantity", "uom"], as_dict=True)
		if head:
			boms[bom] = {
				"name": bom, "quantity": flt(head.quantity) or 1, "uom": head.uom,
				"items": frappe.get_all("BOM Item", filters={"parent": bom, "parenttype": "BOM"}, fields=["item_code", "item_name", "stock_qty", "stock_uom", "bom_no"], order_by="idx", limit=MAX_BOM_ITEMS),
			}

	use_incoming, use_wo = bool(mrp._setting("mrp_include_incoming_po")), bool(mrp._setting("mrp_include_existing_wo"))
	cache: dict = {}
	supply_cache: dict[tuple, list] = {}
	out = {}
	for r in lines:
		leaves = mrp.leaf_warehouses(r["warehouse"]) if r.get("warehouse") else []
		key = (r["item_code"], r.get("warehouse"))
		if key not in supply_cache:
			docs = mrp_supply.describe(r["item_code"], leaves, own, include_incoming=use_incoming, include_wo=use_wo, cache=cache) if leaves else []
			supply_cache[key] = [
				{"doctype": d["doctype"], "name": d["name"], "qty": flt(d["qty"]), "draft": bool(d["draft"]), "relation": d["relation"], "owners": d["owners"], "counted": d["counted"]}
				for d in docs[:MAX_SUPPLY_PER_ITEM]
			]
		stock = sorted(bins.get(r["item_code"], []), key=lambda b: (b.warehouse not in leaves, -flt(b.actual_qty)))[:MAX_BINS_PER_ITEM]
		bom = boms.get(r.get("bom_no"))
		bom_view = None
		if bom:
			qty = flt(r.get("net_qty")) or flt(r.get("gross_qty"))
			bom_view = {
				"name": bom["name"], "quantity": bom["quantity"], "uom": bom["uom"],
				"items": [dict(i, needed=flt(i.stock_qty) / bom["quantity"] * qty) for i in bom["items"]],
			}
		out[r["name"]] = {
			"stock_by_warehouse": [
				{"warehouse": b.warehouse, "actual": flt(b.actual_qty), "reserved": flt(b.reserved_qty), "ordered": flt(b.ordered_qty),
				 "indented": flt(b.indented_qty), "planned": flt(b.planned_qty), "in_run": b.warehouse in leaves}
				for b in stock
			],
			"bom": bom_view,
			"supply_documents": supply_cache[key],
		}
	return out


def _history(run_name: str) -> list[dict]:
	"""The audit trail of this run, newest first (what was calculated, revised, skipped, approved, created, cancelled)."""
	rows = frappe.get_all(
		AUDIT_DT, filters=[["details", "like", f'%"{run_name}"%'], ["action", "like", "MRP%"]],
		fields=["name", "action", "result", "user", "timestamp", "reason", "error_details", "details"], order_by="timestamp desc, creation desc", limit=40,
	)
	out = []
	for r in rows:
		try:
			details = json.loads(r.details or "{}")
		except ValueError:
			details = {}
		out.append({
			"action": r.action, "result": r.result, "user": r.user, "timestamp": r.timestamp, "reason": r.reason, "error": r.error_details,
			"item_code": details.get("item_code"), "original_qty": details.get("original_qty"), "revised_qty": details.get("revised_qty"),
			"created": details.get("created"),
		})
	return out


# ============================================================================================ the five actions
def revise_lines(run_name: str, lines) -> dict:
	"""Change the quantity to create of one or more lines (a skipped line that is revised is wanted again). Validated like the review
	always was (> 0, whole numbers for whole-number UOMs, at most 10 x the shortage); the calculated quantity stays in Original Quantity."""
	run = _prepare(run_name)
	_check_reviewable(run, A_MRP_REVISE)
	selections = []
	before = {}
	for s in lines or []:
		row = _line(run, s.get("name"))
		if not _is_creatable(row):
			_reject(run, A_MRP_REVISE, _("Row {0} ({1}): nothing to revise on this line.").format(row.idx, row.item_code))
		before[row.name] = (flt(row.approved_qty), cint(row.create_document))
		selections.append({"name": row.name, "create_document": 1, "approved_qty": s.get("qty")})
	if not selections:
		frappe.throw(_("Nothing to revise."))
	try:
		mrp._validate_selection(run, selections)
	except frappe.ValidationError as e:
		_log(A_MRP_REVISE, run, R_SKIPPED, error=str(e)[:500], rejected=True)
		commit_if_allowed()
		raise
	changed = []
	for s in selections:
		row = _line(run, s["name"])
		old_qty, old_on = before[row.name]
		if abs(old_qty - flt(row.approved_qty)) > 1e-9 or not old_on:
			changed.append(row)
	run.set("reviewed_by", frappe.session.user)
	run.set("reviewed_on", now_datetime())
	run.save()
	for row in changed:
		_log(A_MRP_REVISE, run, item_code=row.item_code, line=row.name, original_qty=flt(row.original_qty) or flt(row.net_qty),
			previous_qty=before[row.name][0], revised_qty=flt(row.approved_qty), included_again=not before[row.name][1])
	return {"run": run.name, "token": str(run.modified), "changed": len(changed)}


def revise_line(run_name: str, line: str, qty) -> dict:
	return revise_lines(run_name, [{"name": line, "qty": qty}])


def skip_line(run_name: str, line: str, reason: str) -> dict:
	"""Leave one recommendation out of the documents. A reason is mandatory and is kept on the line and in the audit log."""
	run = _prepare(run_name)
	reason = _clean_reason(reason, _("skip an item"))
	_check_reviewable(run, A_MRP_SKIP)
	row = _line(run, line)
	if not _is_creatable(row):
		_reject(run, A_MRP_SKIP, _("Row {0} ({1}): there is nothing to skip on this line.").format(row.idx, row.item_code))
	row.create_document = 0
	row.skip_reason = reason
	run.set("reviewed_by", frappe.session.user)
	run.set("reviewed_on", now_datetime())
	run.save()
	_log(A_MRP_SKIP, run, reason=reason, item_code=row.item_code, line=row.name, original_qty=flt(row.original_qty) or flt(row.net_qty), revised_qty=flt(row.approved_qty))
	return {"run": run.name, "token": str(run.modified)}


def cancel_run(run_name: str, reason: str) -> dict:
	"""Close the review without creating anything. Final for these recommendations; Recalculate reopens it (and is audited). Cancelling
	twice is harmless; cancelling after the documents exist is refused."""
	run = _prepare(run_name)
	reason = _clean_reason(reason, _("cancel the review"))
	if mrp.approval_state(run) == AP_CANCELLED:
		return {"run": run.name, "token": str(run.modified), "already_cancelled": True}
	if run.status == mrp.S_CREATED or mrp.approval_state(run) == AP_APPROVED:
		_reject(run, A_MRP_CANCEL, _("Documents were already created from this run; it cannot be cancelled."))
	run.set("approval_status", AP_CANCELLED)
	run.set("cancel_reason", reason)
	run.set("reviewed_by", frappe.session.user)
	run.set("reviewed_on", now_datetime())
	run.save()
	_log(A_MRP_CANCEL, run, reason=reason)
	return {"run": run.name, "token": str(run.modified), "already_cancelled": False}


def recalculate(run_name: str) -> dict:
	"""Calculate again with today's stock and supply. Earlier revisions and skips are discarded (they belonged to the old numbers) and a
	cancelled review is reopened. Refused once documents were created."""
	run = _prepare(run_name)
	state = mrp.approval_state(run)
	if run.status == mrp.S_CREATED or state == AP_APPROVED:
		_reject(run, A_MRP_RECALC, _("Documents were already created from this run. Start a new run instead."))
	if run.status in (mrp.S_DRAFT, mrp.S_QUEUED, mrp.S_FAILED):
		frappe.throw(_("This run has not been calculated yet (it is {0}). Use Retry Calculation.").format(_(run.status)))
	lost = sum(1 for r in run.lines if r.get("skip_reason") or r.get("revised_by"))
	before = {k: run.get(k) for k in ("shortage_qty", "purchase_qty", "production_qty")}
	out = mrp.calculate_run(run_name, automatic=False, reopen=True)
	run = frappe.get_doc(RUN_DT, run_name)
	_log(A_MRP_RECALC, run, reopened=state == AP_CANCELLED, discarded_revisions_or_skips=lost, before=before, after=out["summary"])
	return {"run": run.name, "token": str(run.modified), "status": run.status, "reopened": state == AP_CANCELLED}


def approve_and_create(run_name: str, approval_key: str, token: str, lines=None, confirm=0) -> dict:
	"""Approve the review and create the draft Material Request / Work Orders - exactly once.

	`approval_key`: made by the client for this review (the same key is re-sent when a request is repeated); `token`: what the client saw
	(`review_data.token`). Under the run's row lock: a repeated key replays the result; any other approval, a cancelled run, a changed
	run or a missing reason for a skipped line is refused; then the shortages are re-read and everything is created in one transaction,
	together with the approval stamp. Nothing is submitted."""
	run = _prepare(run_name)
	if not cint(confirm):
		frappe.throw(_("Creating documents needs your explicit confirmation."))
	approval_key = (approval_key or "").strip()
	if len(approval_key) < 8:
		frappe.throw(_("The approval needs its key. Reload the review and try again."))
	if run.get("approval_key") == approval_key and run.status == mrp.S_CREATED:
		_log(A_MRP_APPROVE, run, R_SKIPPED, replayed=True, approval_key=approval_key, created=_created(run))
		commit_if_allowed()
		return {"run": run.name, "replayed": True, "created": [[d["doctype"], d["name"]] for d in _created(run)], "documents": _created(run),
			"skipped": [], "status": run.status, "approval_status": mrp.approval_state(run)}
	_check_reviewable(run, A_MRP_APPROVE)
	if str(token or "") != str(run.modified):
		_reject(run, A_MRP_APPROVE, _("The recommendations changed since you opened this review (revised, skipped or recalculated). Reload to see the current data."), approval_key=approval_key)
	if not frappe.has_permission("Material Request", "create") or not frappe.has_permission("Work Order", "create"):
		_reject(run, A_MRP_APPROVE, _("You need permission to create Material Requests and Work Orders."))
	if lines is not None:
		mrp._validate_selection(run, lines)
	missing = [r for r in run.lines if _is_creatable(r) and not cint(r.create_document) and not (r.get("skip_reason") or "").strip()]
	if missing:
		_reject(run, A_MRP_APPROVE, _("Give a reason for each skipped item, or tick it again: {0}").format(", ".join(f"{r.idx} ({r.item_code})" for r in missing)))
	chosen = [r for r in run.lines if cint(r.create_document) and _is_creatable(r)]
	if not chosen:
		_reject(run, A_MRP_APPROVE, _("Select at least one recommendation to create."))
	plan = [{"item_code": r.item_code, "action": r.action, "original_qty": flt(r.original_qty) or flt(r.net_qty), "revised_qty": flt(r.approved_qty)} for r in chosen]
	skipped_lines = [{"item_code": r.item_code, "original_qty": flt(r.original_qty) or flt(r.net_qty), "reason": r.get("skip_reason")} for r in run.lines if _is_creatable(r) and not cint(r.create_document)]
	try:
		out = mrp._create_from(run, run.name, chosen, automatic=False, approval=mrp.approval_stamp(approval_key=approval_key))
	except Exception as e:
		_log(A_MRP_APPROVE, run, R_FAILED, error=str(e)[:2000], approval_key=approval_key, lines=plan)
		commit_if_allowed()
		raise
	if not out["created"]:
		_log(A_MRP_APPROVE, run, R_SKIPPED, error="nothing left to create: no longer short", approval_key=approval_key, lines=plan, skipped=out["skipped"])
		return {**out, "run": run.name, "replayed": False, "documents": [], "approval_status": mrp.approval_state(run), "token": str(run.modified),
			"message": _("Nothing was created: the recommended items are no longer short.")}
	run.reload()
	_log(A_MRP_APPROVE, run, approval_key=approval_key, created=out["created"], skipped=out["skipped"], lines=plan, skipped_by_reviewer=skipped_lines,
		approved_by=run.get("approved_by"), approved_on=str(run.get("approved_on")))
	return {**out, "run": run.name, "replayed": False, "documents": _created(run), "approval_status": mrp.approval_state(run), "token": str(run.modified)}
