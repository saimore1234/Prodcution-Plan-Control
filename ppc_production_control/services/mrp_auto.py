"""Automatic MRP after a Sales Order or a Production Plan is submitted.

Submitting only *queues* a background job; the job creates one PPC MRP Run for that document (trigger
"Automatic") and calculates it with the same `mrp.calculate_run` a person uses. If "Automatic Draft Creation" is on in
PPC Settings the job then creates the draft Material Requests / Work Orders too; otherwise the result waits in the
MRP Review. Nothing is ever submitted.

A failure anywhere must never block or undo the source document, so:

* the submit hook swallows every error (it is only an enqueue, and is logged if even that fails);
* the job records a failure on the run (status Failed, reason, audit entry, Error Log), notifies the PPC Managers and
  retries by itself up to the configured number of times; a planner can also retry from the run, the dashboard or the review.

Idempotency: a run's `idempotency_key` (unique in the database) is `auto::<doctype>::<name>`, so repeated events or
concurrent jobs for the same document can never create two runs; the second one finds the first.
"""

import frappe
from frappe import _
from frappe.utils import add_days, add_to_date, cint, getdate, now_datetime, nowdate

from ppc_production_control.services import audit, mrp
from ppc_production_control.services.constants import (
	AP_CANCELLED,
	MRP_RECOVERY_BATCH,
	MRP_SETTING_DEFAULTS,
	MRP_STUCK_AFTER_MINUTES,
	R_SKIPPED,
	R_SUCCESS,
	SETTINGS_DT,
)
from ppc_production_control.services.utils import commit_if_allowed, enqueue_once, in_test

JOB = "ppc_production_control.services.mrp_auto.run_for_sales_order"
JOB_SOURCE = "ppc_production_control.services.mrp_auto.run_for_source"
JOB_RETRY = "ppc_production_control.services.mrp_auto.retry_run"
SOURCE_DOCTYPES = ("Sales Order", "Production Plan")
A_SKIP = mrp.A_AUTO


def setting(field: str, default=None):
	"""A PPC Settings value, or `default` (else the recommended value of `MRP_SETTING_DEFAULTS`) when nothing is stored. A Single's
	new field has no stored value on a site that has not saved the settings since the upgrade, and `get_single_value` turns
	"nothing stored" into 0 - so read the stored row. A stored 0 is a saved choice and is returned as such."""
	if default is None:
		default = MRP_SETTING_DEFAULTS.get(field)
	rows = frappe.db.sql("select value from `tabSingles` where doctype = %s and field = %s", (SETTINGS_DT, field))
	if not rows or rows[0][0] in (None, ""):
		return default
	return type(default)(cint(rows[0][0])) if isinstance(default, (bool, int)) else rows[0][0]


def enabled(doctype: str = "Sales Order") -> bool:
	"""Automatic MRP master switch AND the trigger of that document type (both recommended on)."""
	if not setting("mrp_enabled"):
		return False
	if doctype == "Production Plan":
		return bool(setting("mrp_trigger_production_plan"))
	return bool(setting("mrp_auto_on_sales_order"))


def auto_drafts() -> bool:
	return bool(setting("mrp_auto_create_drafts"))


def idempotency_key(doctype: str, name: str) -> str:
	return f"auto::{doctype}::{name}"


def on_sales_order_submit(doc, method=None):
	_on_submit(doc)


def on_production_plan_submit(doc, method=None):
	_on_submit(doc)


def _on_submit(doc):
	"""`doc_events` hook. Never raises."""
	try:
		if not enabled(doc.doctype):
			return
		if in_test() and not frappe.flags.get("ppc_mrp_auto_in_test"):
			return  # other test suites submit these documents; only the MRP tests exercise this on purpose
		enqueue_once(JOB_SOURCE, job_id=f"ppc_mrp::{doc.doctype}::{doc.name}", queue="long", timeout=1800, doctype=doc.doctype, name=doc.name)
	except Exception:
		frappe.log_error(title=f"PPC MRP: could not queue the automatic calculation for {doc.name}", message=frappe.get_traceback())


def default_warehouse_for(doc) -> str | None:
	"""Warehouse used for demand / components that name none: PPC Settings, then the document's own, then Stock Settings."""
	own = [doc.get("set_warehouse"), *[row.get("warehouse") for row in doc.get("items") or []], *[row.get("warehouse") for row in doc.get("po_items") or []]]
	default = frappe.db.get_single_value(SETTINGS_DT, "mrp_default_warehouse")
	policy_default_only = setting("mrp_warehouse_policy") == "Default Planning Warehouse"
	candidates = [default] if policy_default_only else [default, *own]
	candidates.append(frappe.db.get_single_value("Stock Settings", "default_warehouse"))
	for wh in candidates:
		if wh and frappe.db.get_value("Warehouse", wh, "company") == doc.company:
			return wh
	return None


def run_for_sales_order(sales_order: str):
	"""Kept for queued jobs of 1.6.x."""
	run_for_source("Sales Order", sales_order)


def run_for_source(doctype: str, name: str):
	"""Background job: one automatic run per source document. Never raises - failures are recorded on the run."""
	run_name = None
	try:
		if doctype not in SOURCE_DOCTYPES:
			return
		doc = frappe.get_doc(doctype, name)
		if doc.docstatus != 1:
			return
		key = idempotency_key(doctype, name)
		existing = frappe.db.get_value(mrp.RUN_DT, {"idempotency_key": key}, ["name", "status"], as_dict=True)
		if existing:
			audit.log(mrp.A_AUTO, R_SKIPPED, details={"source": name, "run": existing.name, "reason": "this document already has an automatic run"})
			return
		run = _new_run(doc, key)
		try:
			run.insert(ignore_permissions=True)
		except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
			frappe.db.rollback()  # a concurrent job won the race: its run is the one
			audit.log(mrp.A_AUTO, R_SKIPPED, details={"source": name, "reason": "concurrent job created the run"})
			return
		run_name = run.name
		commit_if_allowed()  # the run now exists independently of what the calculation does
		process_run(run_name)
	except Exception:
		frappe.log_error(title=f"PPC MRP: automatic run for {name} failed", message=frappe.get_traceback())
		if run_name:
			fail(run_name, frappe.get_traceback(with_context=False))


def _new_run(doc, key: str):
	if doc.doctype == "Sales Order":
		dates = [getdate(r.delivery_date) for r in doc.items if r.delivery_date] or [getdate(doc.delivery_date or nowdate())]
		to_date, extra = max(max(dates), getdate(nowdate())), {"source_sales_order": doc.name, "include_sales_orders": 1, "include_production_plans": 0}
	else:
		dates = [getdate(r.planned_start_date) for r in doc.get("po_items") or [] if r.get("planned_start_date")] or [getdate(doc.posting_date or nowdate())]
		to_date, extra = max(max(dates), getdate(nowdate())), {"source_production_plan": doc.name, "include_sales_orders": 0, "include_production_plans": 1}
	return frappe.get_doc({
		"doctype": mrp.RUN_DT,
		"company": doc.company,
		"trigger": "Automatic",
		"idempotency_key": key,
		"status": mrp.S_QUEUED,
		"to_date": to_date,  # the document's own dates decide what is planned
		"default_warehouse": default_warehouse_for(doc),
		"sources": [{"source_doctype": doc.doctype, "source_name": doc.name}],
		**extra,
	})


def process_run(run_name: str):
	"""Calculate a run and, when Automatic Draft Creation is on, create its drafts. Failures are recorded, never raised."""
	if mrp.is_cancelled(run_name):
		return  # a reviewer cancelled it: no retry or late job may calculate (or create) anything for it
	savepoint = "ppc_mrp_auto"
	frappe.db.savepoint(savepoint)
	try:
		out = mrp.calculate_run(run_name, automatic=True)
		if auto_drafts():
			mrp.create_documents_auto(run_name)
		notify_done(run_name, out)
	except Exception as e:
		frappe.db.rollback(save_point=savepoint)
		frappe.log_error(title=f"PPC MRP: automatic calculation of {run_name} failed", message=frappe.get_traceback())
		fail(run_name, str(e) or frappe.get_traceback(with_context=False))
	commit_if_allowed()


def fail(run_name: str, error: str):
	"""Record the failure, tell the managers and queue the configured number of automatic retries."""
	mrp.mark_failed(run_name, error)
	retries = cint(frappe.db.get_value(mrp.RUN_DT, run_name, "retry_count"))
	_notify(_("MRP run {0} failed").format(run_name), (error or "")[:300], run_name, failure=True)
	if retries <= cint(setting("mrp_max_retries")) and not in_test():
		try:
			frappe.enqueue(JOB_RETRY, queue="long", timeout=1800, enqueue_after_commit=True, run_name=run_name, at_front=False)
		except Exception:
			frappe.log_error(title=f"PPC MRP: could not queue the retry of {run_name}", message=frappe.get_traceback())
	commit_if_allowed()


def retry_run(run_name: str):
	"""Automatic retry / recovery job: only a run that is still Failed / Queued, decided under the run's row lock (so a late or duplicate
	job can never calculate a run that a person already reviewed); documents already created are never created again."""
	from ppc_production_control.services import mrp_approval

	mrp_approval._process_once(run_name)


def recover_stuck_runs(minutes: int | None = None, limit: int = MRP_RECOVERY_BATCH) -> dict:
	"""Hourly safety net for automatic runs whose background job was lost. A run is created *before* it is calculated, so a worker that
	dies, a Redis that restarts or a job that is killed leaves it "Queued" for good - and the run's idempotency key makes every later
	job for the same document stop at "already has a run". This finds such runs (Automatic, Queued, untouched for `minutes`), and for
	each one either queues one recovery job (same idempotent `retry_run`; the run's own retry count is raised, which also refreshes
	`modified`, so the next hourly pass leaves it alone until the job had its time) or, once the configured number of retries is used up,
	marks it Failed with the reason so a person sees it. Never raises, never touches a cancelled review, does nothing when automatic MRP
	is off or the site has not been migrated, never creates a document itself."""
	out = {"checked": 0, "requeued": [], "failed": []}
	try:
		if not mrp.approval_model_ready() or not setting("mrp_enabled"):
			return out
		cutoff = add_to_date(now_datetime(), minutes=-cint(minutes or MRP_STUCK_AFTER_MINUTES))
		rows = frappe.get_all(
			mrp.RUN_DT, filters={"status": mrp.S_QUEUED, "trigger": "Automatic", "approval_status": ["!=", AP_CANCELLED], "modified": ["<", cutoff]},
			fields=["name", "retry_count", "modified"], order_by="modified asc", limit_page_length=cint(limit) or MRP_RECOVERY_BATCH,
		)
		out["checked"] = len(rows)
		max_retries = cint(setting("mrp_max_retries"))
		for r in rows:
			savepoint = "ppc_mrp_recover"
			frappe.db.savepoint(savepoint)
			try:
				retries = cint(r.retry_count)
				if retries >= max_retries:
					reason = _("Stuck in Queued: its background job never finished and the {0} automatic retries are used up. Use Retry Calculation.").format(max_retries)
					mrp.mark_failed(r.name, reason)
					_notify(_("MRP run {0} is stuck").format(r.name), reason, r.name, failure=True)
					out["failed"].append(r.name)
				else:
					frappe.db.set_value(mrp.RUN_DT, r.name, "retry_count", retries + 1)
					audit.log(mrp.A_RETRY, R_SUCCESS, details={"run": r.name, "recovered": True, "reason": "stuck in Queued", "queued_since": str(r.modified), "attempt": retries + 1})
					enqueue_once(JOB_RETRY, job_id=f"ppc_mrp_recover::{r.name}::{retries + 1}", queue="long", timeout=1800, run_name=r.name)
					out["requeued"].append(r.name)
				commit_if_allowed()
			except Exception:
				frappe.db.rollback(save_point=savepoint)  # only this run's changes; the others carry on
				frappe.log_error(title=f"PPC MRP: could not recover run {r.name}", message=frappe.get_traceback())
	except Exception:
		frappe.log_error(title="PPC MRP: recovery of stuck runs failed", message=frappe.get_traceback())
	return out


def notify_done(run_name: str, out: dict):
	run = frappe.db.get_value(mrp.RUN_DT, run_name, ["status", "material_request"], as_dict=True)
	counts = frappe.db.sql(
		"select sum(document_type='Material Request'), sum(document_type='Work Order') from `tabPPC MRP Line` where parent=%s", run_name
	)[0]
	s = out.get("summary") or {}
	msg = _("{0}: {1} shortage line(s), {2} purchase, {3} manufacture, {4} need review.").format(
		run_name, s.get("shortage_items", 0), s.get("purchase_lines", 0), s.get("production_lines", 0), s.get("review_lines", 0)
	)
	if run and run.status == mrp.S_CREATED:
		msg += " " + _("Draft documents created ({0} Material Request line(s), {1} Work Order(s)).").format(int(counts[0] or 0), int(counts[1] or 0))
	_notify(_("MRP calculated: {0}").format(run_name), msg, run_name)
	_publish_ready(run_name, run.status if run else None)


def _publish_ready(run_name: str, status: str | None):
	"""Tell the browser of whoever submitted the document that the calculation finished, so its dialog can refresh at once (it also polls,
	so a missing socket server only makes it a few seconds slower). Sent after the commit; never raises."""
	try:
		src = frappe.db.get_value(mrp.RUN_DT, run_name, ["source_sales_order", "source_production_plan"], as_dict=True) or {}
		frappe.publish_realtime(
			"ppc_mrp_ready",
			{"run": run_name, "status": status, "source_doctype": "Sales Order" if src.get("source_sales_order") else "Production Plan", "source_name": src.get("source_sales_order") or src.get("source_production_plan")},
			user=frappe.session.user, after_commit=True,
		)
	except Exception:
		frappe.log_error(title="PPC MRP: realtime notification failed", message=frappe.get_traceback())


def _notify(subject: str, message: str, run_name: str, failure: bool = False):
	"""Notification Log for PPC Managers (failures always, when switched on); never raises."""
	try:
		if failure and not setting("mrp_notify_on_failure"):
			return
		users = frappe.get_all("Has Role", filters={"role": "PPC Manager", "parenttype": "User"}, pluck="parent")
		users = [u for u in set(users) if u not in ("Administrator", "Guest") and frappe.db.get_value("User", u, "enabled")]
		if not users:
			return
		from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification

		enqueue_create_notification(users, {
			"type": "Alert", "subject": subject, "email_content": message,
			"document_type": mrp.RUN_DT, "document_name": run_name,
		})
	except Exception:
		frappe.log_error(title="PPC MRP: notification failed", message=frappe.get_traceback())
