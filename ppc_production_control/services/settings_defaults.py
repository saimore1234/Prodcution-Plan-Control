"""Recommended PPC Settings (MRP) for whatever is *missing* - never an overwrite of anything an administrator saved.

Why this exists: a Single's new fields have no stored row on a site that was upgraded. Frappe then loads the document with
those fields empty, the settings form shows every unsaved checkbox unticked, and the next Save writes a 0 for each of them -
that is how "Enable Automatic MRP" and "Include Incoming ..." ended up off and the safety stock off on a site that never chose it.
`PPCSettings.load_from_db` therefore fills the missing ones with the recommended value before the form (or a save) sees them, and
`fill_missing_defaults` (patch + after_migrate) stores them once, idempotently.

"Missing" means *no stored row, or a stored empty value (NULL / "")*. A stored 0 is a saved choice and is left exactly as it is.
"""

import frappe

from ppc_production_control.services.constants import MRP_SETTING_DEFAULTS, SETTINGS_DT


def stored_values() -> dict:
	"""{field: stored value} of the MRP rows of PPC Settings, read straight from `tabSingles` (a Single's loader hides "nothing stored")."""
	rows = frappe.db.sql("select field, value from `tabSingles` where doctype = %s and field like %s", (SETTINGS_DT, "mrp\\_%"))
	return {field: value for field, value in rows}


def missing_defaults(stored: dict | None = None) -> dict:
	"""{field: recommended value} for every MRP setting that has nothing usable stored."""
	stored = stored_values() if stored is None else stored
	return {field: default for field, default in MRP_SETTING_DEFAULTS.items() if stored.get(field) in (None, "")}


def fill_missing_defaults() -> dict:
	"""Store the recommended value for every missing MRP setting; returns what was written ({} when nothing was missing).
	Safe to run any number of times: a value that is present is never touched, so a second run writes nothing."""
	written = missing_defaults()
	for field, value in written.items():
		frappe.db.set_single_value(SETTINGS_DT, field, value, update_modified=False)
	return written
