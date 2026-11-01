import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate


class PPCMRPRun(Document):
	def validate(self):
		if self.to_date and getdate(self.from_date or self.to_date) > getdate(self.to_date):
			frappe.throw(_("Demand From cannot be after Demand Until."))
		self.validate_sources()
		if not self.default_warehouse and self.get("trigger") != "Automatic":
			frappe.throw(_("Default Warehouse is required."))
		seen = set()
		for row in self.warehouses:
			if row.warehouse in seen:
				frappe.throw(_("Warehouse {0} is listed twice.").format(row.warehouse))
			seen.add(row.warehouse)
		if self.default_warehouse:
			company = frappe.db.get_value("Warehouse", self.default_warehouse, "company")
			if company and company != self.company:
				if self.get("trigger") != "Automatic":
					frappe.throw(_("Warehouse {0} belongs to {1}, not {2}.").format(self.default_warehouse, company, self.company))
				# An automatic run must always exist so a failure can be seen and retried. Frappe fills an empty Link
				# with the user's / site's default, which may be another company's warehouse: drop it, the calculation
				# then reports that no default warehouse is set.
				self.default_warehouse = None
			if self.default_warehouse and seen and self.default_warehouse not in seen:
				self.append("warehouses", {"warehouse": self.default_warehouse})
		keys = [d.source_key for d in self.demands]
		if len(keys) != len(set(keys)):
			frappe.throw(_("The same demand line appears twice. Recalculate the run."))
		if not self.is_new():
			self._guard_inputs_after_documents()

	def validate_sources(self):
		"""Listed Sales Orders / Production Plans: each once; new ones must exist, be submitted and belong to this company.
		(A source that was valid when listed and was cancelled since must not make the run unsaveable: the calculation
		then simply finds no demand for it and says so.) Without any, a demand source must be switched on."""
		if not self.sources:
			if not (self.include_sales_orders or self.include_production_plans):
				frappe.throw(_("Select at least one demand source, or list the documents to plan."))
			return
		before = self.get_doc_before_save()
		known = {(r.source_doctype, r.source_name) for r in (before.sources if before else [])}
		seen = set()
		for row in self.sources:
			key = (row.source_doctype, row.source_name)
			if key in seen:
				frappe.throw(_("Row {0}: {1} {2} is listed twice.").format(row.idx, _(row.source_doctype), row.source_name))
			seen.add(key)
			if key in known:
				continue
			info = frappe.db.get_value(row.source_doctype, row.source_name, ["docstatus", "company"], as_dict=True)
			if not info:
				frappe.throw(_("Row {0}: {1} {2} does not exist.").format(row.idx, _(row.source_doctype), row.source_name))
			if info.docstatus != 1:
				frappe.throw(_("Row {0}: {1} {2} must be submitted.").format(row.idx, _(row.source_doctype), row.source_name))
			if info.company != self.company:
				frappe.throw(_("Row {0}: {1} {2} belongs to {3}, not {4}.").format(row.idx, _(row.source_doctype), row.source_name, info.company, self.company))

	def _guard_inputs_after_documents(self):
		if self.status == "Documents Created" and any(self.has_value_changed(f) for f in ("company", "from_date", "to_date", "default_warehouse")):
			frappe.throw(_("Documents were created from this run; its inputs can no longer change."))

	def on_trash(self):
		if self.status == "Documents Created":
			frappe.throw(_("Documents were created from this run; it is kept as the record of why."))
