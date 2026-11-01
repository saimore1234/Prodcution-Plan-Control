"""Adds the automatic MRP run to the Connections panel of the standard Sales Order."""

from frappe import _


def get_data(data):
	"""`override_doctype_dashboards` hook: receives ERPNext's dashboard dict and returns it extended."""
	data = data or {}
	data.setdefault("non_standard_fieldnames", {})["PPC MRP Run"] = "source_sales_order"
	data.setdefault("transactions", []).append({"label": _("Material Planning"), "items": ["PPC MRP Run"]})
	return data
