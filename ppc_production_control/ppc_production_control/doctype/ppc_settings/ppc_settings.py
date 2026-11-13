import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, now_datetime

from ppc_production_control.services import audit

from ppc_production_control.services.constants import A_MRP_POLICY_CHANGE, DEFAULT_CAPABILITY_ROLES
from ppc_production_control.services.settings_defaults import missing_defaults, stored_values


class PPCSettings(Document):
	def load_from_db(self):
		super().load_from_db()
		# A setting added by an upgrade has no stored value yet: load the recommended one, so neither the settings form nor a save
		# turns "never chosen" into an unticked box. Anything already stored - a saved 0 included - is not touched.
		for field, value in missing_defaults().items():
			self.set(field, value)

	def track_automation_policy(self):
		"""Remember who switched an automation policy (Automatic Draft Creation, or Review Mode "Automatic") on, and when. The run
		that such a policy creates documents for records it, so a document made without a reviewer is never anonymous."""
		if not self.meta.has_field("mrp_policy_set_by"):
			return  # a site that has not been migrated to 1.7.x yet
		stored = stored_values()  # what is saved now (validate runs before the new values are written); "never stored" = recommended = off
		was = (cint(stored.get("mrp_auto_create_drafts") or 0), (stored.get("mrp_review_mode") or "Review First") == "Automatic")
		now = (cint(self.get("mrp_auto_create_drafts")), self.get("mrp_review_mode") == "Automatic")
		self.flags.ppc_policy_change = None
		if now == was:
			return
		if any(now):
			self.mrp_policy_set_by, self.mrp_policy_set_on = frappe.session.user, now_datetime()
		else:
			self.mrp_policy_set_by = self.mrp_policy_set_on = None
		self.flags.ppc_policy_change = {"was": {"automatic_draft_creation": was[0], "review_mode_automatic": was[1]}, "now": {"automatic_draft_creation": now[0], "review_mode_automatic": now[1]}}

	def on_update(self):
		change = self.flags.get("ppc_policy_change")
		if change:
			audit.log(A_MRP_POLICY_CHANGE, details=dict(change, set_by=self.get("mrp_policy_set_by"), user=frappe.session.user))

	def validate(self):
		self.track_automation_policy()
		self.validate_capabilities()
		for field, minimum in (("bulk_batch_size", 1), ("max_gantt_tasks", 50), ("default_operation_minutes", 1)):
			if (self.get(field) or 0) < minimum:
				frappe.throw(_("{0} must be at least {1}.").format(self.meta.get_label(field), minimum))
		self.validate_recommendation()
		self.validate_mrp()
		self.bulk_batch_size = min(self.bulk_batch_size, 200)
		self.max_gantt_tasks = min(self.max_gantt_tasks, 5000)

	def validate_recommendation(self):
		weights = ("rec_weight_completion", "rec_weight_load", "rec_weight_delivery", "rec_weight_cost", "rec_weight_efficiency")
		# A site upgraded from before 1.5.0 has nothing stored for these fields yet (a Single's defaults are not written by a migrate):
		# take the defaults instead of refusing to save the settings.
		if not any(self.get(f) for f in weights) and not self.get("rec_horizon_days") and not self.get("rec_load_window_days"):
			for field in (*weights, "rec_horizon_days", "rec_load_window_days", "rec_unavailable_tolerance_hours"):
				self.set(field, cint(self.meta.get_field(field).default))
		for field in weights:
			if (self.get(field) or 0) < 0:
				frappe.throw(_("{0} cannot be negative.").format(self.meta.get_label(field)))
		if sum((self.get(f) or 0) for f in weights) <= 0:
			frappe.throw(_("At least one recommendation weight must be above zero."))
		for field, low, high in (("rec_horizon_days", 7, 730), ("rec_load_window_days", 1, 60), ("rec_unavailable_tolerance_hours", 0, 24 * 90)):
			value = self.get(field)
			if value is None or value < low or value > high:
				frappe.throw(_("{0} must be between {1} and {2}.").format(self.meta.get_label(field), low, high))

	def validate_capabilities(self):
		seen = set()
		for row in self.capability_roles:
			if row.capability not in DEFAULT_CAPABILITY_ROLES:
				frappe.throw(_("Row {0}: unknown capability {1}").format(row.idx, row.capability))
			key = (row.capability, row.role)
			if key in seen:
				frappe.throw(_("Row {0}: {1} is already assigned to {2}.").format(row.idx, row.role, row.capability))
			seen.add(key)
		# A site that can no longer view or manage anything would lock every non-admin out.
		configured = {row.capability for row in self.capability_roles}
		if self.capability_roles and "View" not in configured:
			frappe.throw(_("At least one role must keep the 'View' capability."))

	def validate_mrp(self):
		# A site upgraded from before 1.7.0 has nothing stored for these fields (migrate does not store a Single's defaults).
		if self.get("mrp_planning_horizon_days") is None or (not self.get("mrp_planning_horizon_days") and not self.get("mrp_max_retries")):
			self.mrp_planning_horizon_days = 365
			self.mrp_max_retries = cint(self.get("mrp_max_retries")) or 2
		for field, low, high in (("mrp_planning_horizon_days", 1, 1825), ("mrp_default_lead_time_days", 0, 365), ("mrp_max_retries", 0, 5)):
			value = cint(self.get(field))
			if value < low or value > high:
				frappe.throw(_("{0} must be between {1} and {2}.").format(self.meta.get_label(field), low, high))
		if self.get("mrp_warehouse_policy") == "Default Planning Warehouse" and not self.get("mrp_default_warehouse"):
			frappe.throw(_("Select a Default Warehouse for Automatic MRP, or use the 'Demand Warehouse' policy."))
