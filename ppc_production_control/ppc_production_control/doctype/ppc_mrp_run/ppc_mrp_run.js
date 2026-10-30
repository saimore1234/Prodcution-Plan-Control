frappe.ui.form.on("PPC MRP Run", {
	setup(frm) {
		frm.set_query("default_warehouse", () => ({ filters: { company: frm.doc.company, is_group: 0 } }));
		frm.set_query("warehouse", "warehouses", () => ({ filters: { company: frm.doc.company } }));
	},

	refresh(frm) {
		frm.disable_save_on_unchanged = true;
		if (frm.doc.__islocal) return;
		const created = frm.doc.status === "Documents Created";
		if (!created) {
			frm.add_custom_button(__("Calculate"), () => ppc_mrp_call(frm, "mrp_calculate", __("Calculating…")), null).addClass("btn-primary");
		}
		if (frm.doc.status === "Calculated") {
			frm.add_custom_button(__("Create Draft Documents"), () => {
				frappe.confirm(
					__("Create a draft Material Request for the purchase lines and draft Work Orders for the manufacture lines? Stock is checked again first and nothing is submitted."),
					() => ppc_mrp_call(frm, "mrp_create_documents", __("Creating drafts…"))
				);
			});
		}
		if (frm.doc.status === "Draft") {
			frm.dashboard.set_headline(__("Save, then press Calculate."));
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
		}).then((r) => {
			const res = r.message || {};
			if (res.created && res.created.length) {
				frappe.show_alert({ message: __("{0} draft document(s) created", [res.created.length]), indicator: "green" });
			}
			if (res.skipped && res.skipped.length) {
				frappe.msgprint(__("Not created, no longer short: {0}", [res.skipped.join(", ")]));
			}
			return frm.reload_doc();
		});
	return frm.is_dirty() ? frm.save().then(run) : run();
}
