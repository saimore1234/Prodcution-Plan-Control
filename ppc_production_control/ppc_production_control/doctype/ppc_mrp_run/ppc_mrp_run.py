import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate


class PPCMRPRun(Document):
	def validate(self):
		if getdate(self.from_date or self.to_date) > getdate(self.to_date):
			frappe.throw(_("Demand From cannot be after Demand Until."))
		if not (self.include_sales_orders or self.include_production_plans):
			frappe.throw(_("Select at least one demand source."))
		seen = set()
		for row in self.warehouses:
			if row.warehouse in seen:
				frappe.throw(_("Warehouse {0} is listed twice.").format(row.warehouse))
			seen.add(row.warehouse)
		if self.default_warehouse:
			company = frappe.db.get_value("Warehouse", self.default_warehouse, "company")
			if company and company != self.company:
				frappe.throw(_("Warehouse {0} belongs to {1}, not {2}.").format(self.default_warehouse, company, self.company))
			if seen and self.default_warehouse not in seen:
				self.append("warehouses", {"warehouse": self.default_warehouse})
		keys = [d.source_key for d in self.demands]
		if len(keys) != len(set(keys)):
			frappe.throw(_("The same demand line appears twice. Recalculate the run."))
		if not self.is_new():
			self._guard_inputs_after_documents()

	def _guard_inputs_after_documents(self):
		if self.status == "Documents Created" and any(self.has_value_changed(f) for f in ("company", "from_date", "to_date", "default_warehouse")):
			frappe.throw(_("Documents were created from this run; its inputs can no longer change."))

	def on_trash(self):
		if self.status == "Documents Created":
			frappe.throw(_("Documents were created from this run; it is kept as the record of why."))
