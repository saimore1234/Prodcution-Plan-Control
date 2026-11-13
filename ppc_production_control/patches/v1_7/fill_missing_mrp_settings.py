"""Store the recommended value for every MRP setting that has none (never changes a saved value; running it again writes nothing)."""

from ppc_production_control.services.settings_defaults import fill_missing_defaults


def execute():
	fill_missing_defaults()
