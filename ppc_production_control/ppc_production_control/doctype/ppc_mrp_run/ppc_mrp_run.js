frappe.ui.form.on("PPC MRP Run", {
	setup(frm) {
		frm.set_query("default_warehouse", () => ({ filters: { company: frm.doc.company, is_group: 0 } }));
		frm.set_query("warehouse", "warehouses", () => ({ filters: { company: frm.doc.company } }));
		// Sales Orders and Production Plans both carry a company; only submitted ones can be planned
		frm.set_query("source_name", "sources", () => ({ filters: { company: frm.doc.company, docstatus: 1 } }));
	},

	refresh(frm) {
		if (frm.doc.__islocal) return;
		const created = frm.doc.status === "Documents Created";
		if (frm.doc.status === "Failed" || frm.doc.status === "Queued") {
			frm.add_custom_button(__("Retry Calculation"), () => ppc_mrp_call(frm, "mrp_retry", __("Calculating…"))).addClass("btn-primary");
		} else if (!created) {
			frm.add_custom_button(__("Calculate"), () => ppc_mrp_call(frm, "mrp_calculate", __("Calculating…"))).addClass(frm.doc.status === "Draft" ? "btn-primary" : "");
		}
		if (frm.doc.status === "Calculated" || created) {
			frm.add_custom_button(__("Open MRP Review"), () => frappe.set_route("ppc-mrp-review", frm.doc.name)).addClass(frm.doc.status === "Calculated" ? "btn-primary" : "");
		}
		if (frm.doc.status === "Draft") {
			frm.dashboard.set_headline(__("Save, then press Calculate. You will review the recommendations before anything is created."));
		} else if (frm.doc.status === "Failed") {
			frm.dashboard.set_headline_alert(frappe.utils.escape_html(frm.doc.error_message || __("The calculation failed.")), "red");
		} else if (frm.doc.warnings) {
			frm.dashboard.set_headline_alert(frappe.utils.escape_html(frm.doc.warnings).replace(/\n/g, "<br>"), "orange");
		}
		if (frm.doc.material_request) {
			frm.add_custom_button(__("Material Request"), () => frappe.set_route("Form", "Material Request", frm.doc.material_request), __("View"));
		}
		frm.add_custom_button(__("Dashboard"), () => frappe.set_route("ppc-mrp-dashboard", frm.doc.name), __("View"));
	},
});

function ppc_mrp_call(frm, method, message) {
	const run = () =>
		frappe.call({
			method: "ppc_production_control.api." + method,
			type: "POST",
			args: { run: frm.doc.name },
			freeze: true,
			freeze_message: message,
		}).then(() => frm.reload_doc());
	return frm.is_dirty() ? frm.save().then(run) : run();
}
