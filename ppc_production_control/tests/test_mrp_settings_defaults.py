"""Recommended MRP settings: fresh sites get them, upgraded sites receive only what is missing, a saved value is never overwritten.

Pure settings tests: each sets (or removes) the PPC Settings rows it needs inside its own savepoint and nothing is read from, or
left in, the site's real settings."""

import frappe
from frappe.tests.utils import FrappeTestCase

from ppc_production_control.patches.v1_7 import fill_missing_mrp_settings
from ppc_production_control.services import mrp, mrp_auto
from ppc_production_control.services.constants import MRP_SETTING_DEFAULTS, SETTINGS_DT
from ppc_production_control.services.settings_defaults import fill_missing_defaults, missing_defaults, stored_values
from ppc_production_control.tests.utils import use_mrp_settings

RECOMMENDED = {
	"mrp_enabled": 1,
	"mrp_auto_on_sales_order": 1,
	"mrp_auto_create_drafts": 0,
	"mrp_review_mode": "Review First",
	"mrp_include_existing_wo": 1,
	"mrp_include_incoming_po": 1,
	"mrp_include_safety_stock": 1,
}


class SettingsCase(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint("ppc_settings_case")

	def tearDown(self):
		frappe.db.rollback(save_point="ppc_settings_case")
		frappe.clear_document_cache(SETTINGS_DT, SETTINGS_DT)

	def remove(self, *fields):
		"""A site where these settings were never stored (an upgrade added them)."""
		frappe.db.delete("Singles", {"doctype": SETTINGS_DT, "field": ("in", list(fields))})

	def remove_all_mrp(self):
		frappe.db.delete("Singles", {"doctype": SETTINGS_DT, "field": ("like", "mrp\\_%")})

	def stored(self):
		return dict(stored_values())


class TestRecommendedDefaults(SettingsCase):
	def test_the_recommended_values_are_the_ones_that_were_asked_for(self):
		for field, value in RECOMMENDED.items():
			self.assertEqual(MRP_SETTING_DEFAULTS[field], value, field)

	def test_doctype_field_defaults_agree_with_the_recommended_table(self):
		meta = frappe.get_meta(SETTINGS_DT)
		for field, value in MRP_SETTING_DEFAULTS.items():
			self.assertEqual(str(meta.get_field(field).default), str(value), field)

	def test_fresh_installation_receives_the_recommended_defaults(self):
		self.remove_all_mrp()  # nothing stored, as on a new site
		written = fill_missing_defaults()
		self.assertEqual(set(written), set(MRP_SETTING_DEFAULTS))
		stored = self.stored()
		for field, value in MRP_SETTING_DEFAULTS.items():
			self.assertEqual(str(stored[field]), str(value), field)

	def test_fallbacks_agree_with_the_recommendation_when_nothing_is_stored(self):
		self.remove_all_mrp()
		self.assertTrue(mrp_auto.enabled("Sales Order"))
		self.assertTrue(mrp_auto.enabled("Production Plan"))
		self.assertFalse(mrp_auto.auto_drafts())
		self.assertEqual(mrp._setting("mrp_review_mode"), "Review First")
		for field in ("mrp_include_existing_wo", "mrp_include_incoming_po", "mrp_include_safety_stock"):
			self.assertEqual(mrp._setting(field), 1, field)
		self.assertEqual((mrp._setting("mrp_planning_horizon_days"), mrp._setting("mrp_max_retries")), (365, 2))

	def test_automatic_draft_creation_is_off_and_review_first_is_on_by_default(self):
		self.remove_all_mrp()
		fill_missing_defaults()
		self.assertEqual(frappe.db.get_single_value(SETTINGS_DT, "mrp_auto_create_drafts"), 0)
		self.assertEqual(frappe.db.get_single_value(SETTINGS_DT, "mrp_review_mode"), "Review First")


class TestUpgradeNeverOverwrites(SettingsCase):
	def test_existing_saved_values_remain_unchanged(self):
		saved = {"mrp_enabled": 0, "mrp_auto_create_drafts": 1, "mrp_review_mode": "Automatic", "mrp_include_incoming_po": 0,
			"mrp_include_existing_wo": 0, "mrp_include_safety_stock": 0, "mrp_max_retries": 0, "mrp_auto_on_sales_order": 0}
		use_mrp_settings(**saved)  # an administrator's choices (a saved 0 is a choice, not "missing")
		before = self.stored()
		self.assertEqual(missing_defaults(), {})
		self.assertEqual(fill_missing_defaults(), {})
		self.assertEqual(self.stored(), before)
		for field, value in saved.items():
			self.assertEqual(str(self.stored()[field]), str(value), field)

	def test_missing_settings_are_populated_and_nothing_else_changes(self):
		use_mrp_settings(mrp_enabled=0, mrp_auto_create_drafts=1, mrp_review_mode="Automatic", mrp_max_retries=5)
		self.remove("mrp_include_existing_wo", "mrp_include_incoming_po", "mrp_include_safety_stock")
		before = self.stored()
		written = fill_missing_defaults()
		self.assertEqual(written, {"mrp_include_existing_wo": 1, "mrp_include_incoming_po": 1, "mrp_include_safety_stock": 1})
		after = self.stored()
		for field in set(before) - set(written):
			self.assertEqual(after[field], before[field], field)  # the saved ones, untouched
		self.assertEqual(str(after["mrp_enabled"]), "0")  # a saved "off" stays off

	def test_an_empty_stored_value_counts_as_missing_but_a_zero_does_not(self):
		use_mrp_settings(mrp_include_existing_wo=0)
		frappe.db.set_single_value(SETTINGS_DT, "mrp_warehouse_policy", None)  # saved as NULL: nothing chosen
		self.assertEqual(missing_defaults(), {"mrp_warehouse_policy": "Demand Warehouse"})

	def test_running_the_patch_and_the_fill_again_changes_nothing(self):
		self.remove("mrp_enabled", "mrp_review_mode")
		fill_missing_mrp_settings.execute()
		snapshot = self.stored()
		self.assertEqual(fill_missing_defaults(), {})
		fill_missing_mrp_settings.execute()
		fill_missing_mrp_settings.execute()
		self.assertEqual(self.stored(), snapshot)

	def test_the_settings_form_and_a_save_do_not_turn_never_chosen_into_off(self):
		"""The cause of the bad site: a missing row loaded as 0, and the next Save stored that 0."""
		use_mrp_settings(mrp_auto_create_drafts=1)
		self.remove("mrp_enabled", "mrp_include_incoming_po", "mrp_include_existing_wo")
		doc = frappe.get_doc(SETTINGS_DT)
		self.assertEqual((doc.mrp_enabled, doc.mrp_include_incoming_po, doc.mrp_include_existing_wo), (1, 1, 1))
		doc.save()
		stored = self.stored()
		self.assertEqual([str(stored[f]) for f in ("mrp_enabled", "mrp_include_incoming_po", "mrp_include_existing_wo")], ["1", "1", "1"])
		self.assertEqual(str(stored["mrp_auto_create_drafts"]), "1")  # a saved value survives the save

	def test_a_saved_zero_is_loaded_as_zero(self):
		use_mrp_settings(mrp_enabled=0, mrp_include_incoming_po=0)
		frappe.clear_document_cache(SETTINGS_DT, SETTINGS_DT)
		doc = frappe.get_doc(SETTINGS_DT)
		self.assertEqual((doc.mrp_enabled, doc.mrp_include_incoming_po), (0, 0))
