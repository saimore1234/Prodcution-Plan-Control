/* MRP Review page: a host for the shared review (public/js/ppc_mrp_review_ui.js), the same one the dialog after a Sales Order submit
 * shows. All behaviour - selecting, revising, skipping with a reason, approving, cancelling, recalculating, the history - lives there,
 * so the page and the dialog can never disagree. On a site that has not been migrated the shared review shows the classic selection.
 */
frappe.pages["ppc-mrp-review"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("MRP Review"), single_column: true });
	const runField = page.add_field({ fieldname: "run", label: __("MRP Run"), fieldtype: "Link", options: "PPC MRP Run", change: () => load() });
	const $body = $('<div class="ppc-review"></div>').appendTo(page.main);
	let seq = 0;
	wrapper.ppc_run_field = runField;

	function load() {
		const run = runField.get_value();
		if (!run) {
			$body.html('<p class="text-muted">' + __("Choose an MRP Run.") + "</p>");
			return;
		}
		const mine = ++seq; // a slower answer for a run that is no longer chosen is ignored
		frappe.require("/assets/ppc_production_control/js/ppc_mrp_review_ui.js", () => {
			if (mine === seq && window.ppc_mrp) window.ppc_mrp.mount($body, run);
		});
	}

	wrapper.ppc_reload = load;
};

frappe.pages["ppc-mrp-review"].on_page_show = function (wrapper) {
	const route = frappe.get_route();
	if (route[1] && wrapper.ppc_run_field) {
		const run = decodeURIComponent(route[1]);
		if (wrapper.ppc_run_field.get_value() !== run) wrapper.ppc_run_field.set_value(run);
		else wrapper.ppc_reload && wrapper.ppc_reload();
	} else if (wrapper.ppc_run_field && !wrapper.ppc_run_field.get_value()) {
		frappe.db.get_value("PPC MRP Run", { status: "Calculated" }, "name").then((r) => r.message && r.message.name && wrapper.ppc_run_field.set_value(r.message.name));
	}
};
