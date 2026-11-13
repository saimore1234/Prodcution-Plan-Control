"""Item master rules for Procurement Type."""

import frappe

from ppc_production_control.services import item_rules
from ppc_production_control.tests.utils import PREFIX, PPCTestCase, make_bom, make_item


class TestItemRules(PPCTestCase):
	def test_field_exists(self):
		df = frappe.get_meta("Item").get_field("custom_procurement_type")
		self.assertIsNotNone(df)
		self.assertEqual(df.fieldtype, "Select")
		self.assertEqual([o for o in df.options.split("\n") if o], ["Purchase", "Manufacture"])

	def test_blank_is_allowed(self):
		item = frappe.get_doc("Item", make_item(f"{PREFIX}-IR-BLANK"))
		item.custom_procurement_type = ""
		item_rules.validate_item(item)

	def test_purchase_needs_allow_purchase(self):
		item = frappe.get_doc("Item", make_item(f"{PREFIX}-IR-BUY"))
		item.custom_procurement_type = "Purchase"
		item.is_purchase_item = 0
		with self.assertRaises(frappe.ValidationError):
			item_rules.validate_item(item)
		item.is_purchase_item = 1
		item_rules.validate_item(item)

	def test_manufacture_needs_active_bom(self):
		code = make_item(f"{PREFIX}-IR-MAKE", manufacture=True)
		rm = make_item(f"{PREFIX}-IR-RM")
		item = frappe.get_doc("Item", code)
		item.custom_procurement_type = "Manufacture"
		frappe.db.delete("BOM", {"item": code})
		with self.assertRaises(frappe.ValidationError):
			item_rules.validate_item(item)
		make_bom(code, self.company, [(rm, 1)])
		item_rules.validate_item(item)

	def test_invalid_value_rejected(self):
		item = frappe.get_doc("Item", make_item(f"{PREFIX}-IR-BAD"))
		item.custom_procurement_type = "Both"
		with self.assertRaises(frappe.ValidationError):
			item_rules.validate_item(item)
