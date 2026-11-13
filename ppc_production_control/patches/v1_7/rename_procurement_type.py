import frappe


def execute():
	"""Copy Item.ppc_procurement_type (1.6.x) into custom_procurement_type and hide the old field. Idempotent; never overwrites a value."""
	from ppc_production_control.install import ensure_custom_fields

	ensure_custom_fields()  # patches run before after_migrate, so create the new field first
	if not frappe.db.has_column("Item", "ppc_procurement_type") or not frappe.db.has_column("Item", "custom_procurement_type"):
		return
	frappe.db.sql(
		"""update `tabItem` set custom_procurement_type = ppc_procurement_type
		where ifnull(custom_procurement_type, '') = '' and ifnull(ppc_procurement_type, '') != ''"""
	)
	name = frappe.db.get_value("Custom Field", {"dt": "Item", "fieldname": "ppc_procurement_type"})
	if name:
		frappe.db.set_value("Custom Field", name, "hidden", 1)
