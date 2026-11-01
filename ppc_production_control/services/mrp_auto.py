"""Automatic MRP after a Sales Order is submitted.

Submitting a Sales Order only *queues* a background job; the job creates a PPC MRP Run for that order
(trigger "Automatic") and calculates it with the same `mrp.calculate_run` a person uses. The result is a
set of recommendations waiting in the MRP Review - nothing is created from them automatically.

A failure anywhere must never block or undo the Sales Order, so:

* the submit hook swallows every error (it is only an enqueue, and is logged if even that fails);
* the job records a failure on the run (status Failed, reason, audit entry, Error Log) and the planner can
  retry it from the run, the dashboard or the review screen.
"""

import frappe
from frappe import _
from frappe.utils import add_days, cint, getdate, nowdate

from ppc_production_control.services import audit, mrp
from ppc_production_control.services.constants import R_SKIPPED, SETTINGS_DT
from ppc_production_control.services.utils import commit_if_allowed, enqueue_once, in_test

JOB = "ppc_production_control.services.mrp_auto.run_for_sales_order"
OPEN_STATES = (mrp.S_QUEUED, mrp.S_CALCULATED, mrp.S_FAILED)


def enabled() -> bool:
	"""On unless switched off in PPC Settings. A Single's new field has no stored value on a site that has not saved the
	settings since the upgrade, and `get_single_value` turns "nothing stored" into 0 for a Check - so read the stored
	row itself: no row means the default (on), not off."""
	rows = frappe.db.sql("select value from `tabSingles` where doctype = %s and field = %s", (SETTINGS_DT, "mrp_auto_on_sales_order"))
	if not rows or rows[0][0] in (None, ""):
		return True
	return bool(cint(rows[0][0]))


def on_sales_order_submit(doc, method=None):
	"""`doc_events` hook (Sales Order, on_submit). Never raises."""
	try:
		if not enabled():
			return
		if in_test() and not frappe.flags.get("ppc_mrp_auto_in_test"):
			return  # other test suites submit Sales Orders; only the MRP tests exercise this on purpose
		enqueue_once(JOB, job_id=f"ppc_mrp_so::{doc.name}", queue="long", timeout=1800, sales_order=doc.name)
	except Exception:
		frappe.log_error(title=f"PPC MRP: could not queue the automatic calculation for {doc.name}", message=frappe.get_traceback())


def default_warehouse_for(so) -> str | None:
	"""Warehouse used for demand / components that name none: PPC Settings, then the order's own, then Stock Settings."""
	candidates = [
		frappe.db.get_single_value(SETTINGS_DT, "mrp_default_warehouse"),
		so.get("set_warehouse"),
		*[row.warehouse for row in so.get("items") or []],
		frappe.db.get_single_value("Stock Settings", "default_warehouse"),
	]
	for wh in candidates:
		if wh and frappe.db.get_value("Warehouse", wh, "company") == so.company:
			return wh
	return None


def run_for_sales_order(sales_order: str):
	"""Background job: one automatic run per Sales Order. Never raises - failures are recorded on the run."""
	run_name = None
	try:
		so = frappe.get_doc("Sales Order", sales_order)
		if so.docstatus != 1:
			return
		if frappe.db.exists(mrp.RUN_DT, {"source_sales_order": so.name, "trigger": "Automatic", "status": ["in", list(OPEN_STATES)]}):
			audit.log(mrp.A_AUTO, R_SKIPPED, details={"sales_order": so.name, "reason": "an automatic run for this order is already open"})
			return
		deliveries = [getdate(r.delivery_date) for r in so.items if r.delivery_date] or [getdate(so.delivery_date or nowdate())]
		run = frappe.get_doc({
			"doctype": mrp.RUN_DT,
			"company": so.company,
			"trigger": "Automatic",
			"source_sales_order": so.name,
			"status": mrp.S_QUEUED,
			"to_date": max(max(deliveries), getdate(add_days(nowdate(), 0))),
			"default_warehouse": default_warehouse_for(so),
			"include_sales_orders": 1,
			"include_production_plans": 0,
			"sources": [{"source_doctype": "Sales Order", "source_name": so.name}],
		})
		run.flags.ignore_permissions = True
		run.insert()
		run_name = run.name
		commit_if_allowed()  # the run now exists independently of what the calculation does
		_calculate(run_name)
	except Exception:
		frappe.log_error(title=f"PPC MRP: automatic run for {sales_order} failed", message=frappe.get_traceback())
		if run_name:
			mrp.mark_failed(run_name, frappe.get_traceback(with_context=False))
			commit_if_allowed()


def _calculate(run_name: str):
	savepoint = "ppc_mrp_auto"
	frappe.db.savepoint(savepoint)
	try:
		mrp.calculate_run(run_name, automatic=True)
	except Exception as e:
		frappe.db.rollback(save_point=savepoint)
		frappe.log_error(title=f"PPC MRP: automatic calculation of {run_name} failed", message=frappe.get_traceback())
		mrp.mark_failed(run_name, str(e) or frappe.get_traceback(with_context=False))
	commit_if_allowed()
