import frappe
from frappe import _

from ppc_production_control.services import mrp


def execute(filters=None):
	return get_columns(), mrp.report_rows("warehouse", filters or {})


def get_columns():
	qty = lambda f, l: {"fieldname": f, "label": _(l), "fieldtype": "Float", "width": 110}
	return [
		{"fieldname": "warehouse", "label": _("Warehouse"), "fieldtype": "Link", "options": "Warehouse", "width": 170},
		{"fieldname": "item_code", "label": _("Item"), "fieldtype": "Link", "options": "Item", "width": 160},
		{"fieldname": "item_name", "label": _("Item Name"), "fieldtype": "Data", "width": 180},
		{"fieldname": "uom", "label": _("UOM"), "fieldtype": "Link", "options": "UOM", "width": 70},
		qty("gross_qty", "Required"),
		qty("from_stock", "From Stock"),
		qty("from_supply", "From Pending Supply"),
		qty("net_qty", "Shortage"),
		qty("purchase_qty", "To Purchase"),
		qty("production_qty", "To Manufacture"),
		{"fieldname": "planned_order_date", "label": _("Order / Start By"), "fieldtype": "Date", "width": 110},
		{"fieldname": "is_late", "label": _("Late"), "fieldtype": "Check", "width": 60},
	]
