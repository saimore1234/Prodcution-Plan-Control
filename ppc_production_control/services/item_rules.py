"""Item master rules for MRP: a valid Procurement Type and a matching setup."""

import frappe
from frappe import _


def validate_item(doc, method=None):
	"""`doc_events` hook (Item, validate). Blank stays allowed (MRP infers it and warns); a set value must be consistent."""
	ptype = (doc.get("custom_procurement_type") or "").strip()
	if not ptype:
		return
	if ptype not in ("Purchase", "Manufacture"):
		frappe.throw(_("Procurement Type must be Purchase or Manufacture."))
	if ptype == "Purchase" and not doc.get("is_purchase_item"):
		frappe.throw(_("Procurement Type is Purchase, so 'Allow Purchase' must be ticked on item {0}.").format(doc.name))
	if ptype == "Manufacture" and not doc.is_new() and not frappe.db.exists(
		"BOM", {"item": doc.name, "is_active": 1, "docstatus": 1}
	):
		frappe.throw(_("Procurement Type is Manufacture, but item {0} has no active submitted BOM. Create the BOM first.").format(doc.name))
