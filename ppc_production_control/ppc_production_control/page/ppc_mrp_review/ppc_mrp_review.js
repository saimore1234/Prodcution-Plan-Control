frappe.pages["ppc-mrp-review"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("MRP Review"), single_column: true });
	const esc = frappe.utils.escape_html;
	const fmt = (v) => format_number(v || 0, null, 3);
	const API = "ppc_production_control.api.";
	let data = null;
	const edits = {}; // row name -> {create_document, approved_qty}

	const runField = page.add_field({ fieldname: "run", label: __("MRP Run"), fieldtype: "Link", options: "PPC MRP Run", change: () => load() });
	const actionField = page.add_field({ fieldname: "action", label: __("Action"), fieldtype: "Select", options: "\nPurchase\nManufacture\nReview", change: () => render() });
	const pickedField = page.add_field({ fieldname: "picked_only", label: __("Only selected"), fieldtype: "Check", change: () => render() });
	const $body = $('<div class="ppc-review"></div>').appendTo(page.main);
	page.set_primary_action(__("Create Draft Documents"), () => createDocuments());
	page.add_inner_button(__("Save Selection"), () => save());
	page.add_inner_button(__("Retry Calculation"), () => retry());
	page.add_inner_button(__("Open Run"), () => data && frappe.set_route("Form", "PPC MRP Run", data.run));
	wrapper.ppc_run_field = runField;

	// ------------------------------------------------------------------ state of one row
	function state(r) {
		const e = edits[r.name] || {};
		return {
			on: e.create_document != null ? !!e.create_document : !!r.create_document,
			qty: e.approved_qty != null ? e.approved_qty : r.approved_qty || r.net_qty,
		};
	}

	// the same rules the server enforces (it re-checks everything)
	function problem(r, st) {
		if (!r.editable || !st.on) return "";
		const q = flt(st.qty);
		if (!(q > 0)) return __("Quantity must be greater than zero");
		if (cint(r.whole_number) && Math.abs(q - Math.round(q)) > 1e-9) return __("{0} must be a whole number", [r.uom]);
		if (q > flt(r.net_qty) * data.max_edit_factor + 1e-9) return __("At most {0} × the shortage ({1})", [data.max_edit_factor, fmt(r.net_qty)]);
		return "";
	}

	function selections() {
		return data.lines.filter((r) => r.editable).map((r) => {
			const st = state(r);
			return { name: r.name, create_document: st.on ? 1 : 0, approved_qty: flt(st.qty) };
		});
	}

	function firstProblem() {
		for (const r of data.lines) {
			const m = problem(r, state(r));
			if (m) return __("Row {0} ({1}): {2}", [r.idx, r.item_code, m]);
		}
		return "";
	}

	// ------------------------------------------------------------------ render
	function badge(action) {
		const c = { Purchase: "orange", Manufacture: "green", Review: "red", None: "gray" }[action] || "gray";
		return '<span class="indicator-pill ' + c + '">' + esc(__(action)) + "</span>";
	}

	function render() {
		if (!data) return;
		const d = data;
		let h = '<div class="ppc-review-head">' +
			"<b>" + esc(d.run) + "</b> · " + esc(__(d.status)) + " · " + esc(__(d.trigger || "Manual")) +
			(d.source_sales_order ? " · " + __("Sales Order") + " " + esc(d.source_sales_order) : "") +
			(d.calculated_on ? " · " + esc(frappe.datetime.str_to_user(d.calculated_on)) : "") + "</div>";
		if (d.error_message) h += '<div class="alert alert-danger"><b>' + __("Calculation failed") + ":</b> " + esc(d.error_message) + "</div>";
		if (d.status === "Queued") h += '<div class="alert alert-info">' + __("This run is queued or did not finish. Use Retry Calculation if it stays here.") + "</div>";
		(d.warnings || []).forEach((w) => (h += '<div class="alert alert-warning">' + esc(w) + "</div>"));
		if (d.status === "Documents Created") {
			h += '<div class="alert alert-success">' + __("Draft documents were created from this run.") +
				(d.material_request ? ' <a href="/app/material-request/' + encodeURIComponent(d.material_request) + '">' + esc(d.material_request) + "</a>" : "") + "</div>";
		} else if (d.can_edit) {
			h += '<p class="text-muted">' + __("Tick the recommendations to create and adjust quantities. Nothing is created until you press “Create Draft Documents” and confirm. Documents are always created as drafts. Unticking a Work Order does not remove the component lines planned for it.") + "</p>";
		}
		const rows = d.lines.filter((r) => (!actionField.get_value() || r.action === actionField.get_value()) && (!pickedField.get_value() || state(r).on));
		h += '<div class="table-responsive"><table class="table table-bordered ppc-review-table"><thead><tr>' +
			'<th style="width:32px"><input type="checkbox" class="ppc-all" ' + (d.can_edit ? "" : "disabled") + "></th>" +
			[__("Item"), __("Warehouse"), __("Required On"), __("Order / Start By"), __("Action"), __("Shortage"), __("Quantity to Create"), __("UOM"), __("Source Documents"), __("Result / Note")]
				.map((t) => "<th>" + esc(t) + "</th>").join("") + "</tr></thead><tbody>";
		rows.forEach((r) => {
			const st = state(r), msg = problem(r, st);
			h += '<tr data-name="' + esc(r.name) + '"' + (r.net_qty > 0 ? "" : ' class="text-muted"') + ">" +
				'<td><input type="checkbox" class="ppc-pick" ' + (r.editable ? "" : "disabled") + (st.on && r.editable ? " checked" : "") + "></td>" +
				"<td>" + esc(r.item_code) + ' <span class="text-muted">' + esc(r.item_name || "") + "</span>" +
				(r.procurement_type ? "" : ' <span class="indicator-pill gray" title="' + esc(__("Procurement Type not set on the Item")) + '">' + __("type?") + "</span>") + "</td>" +
				"<td>" + esc(r.warehouse || "") + "</td><td>" + esc(frappe.datetime.str_to_user(r.required_date)) + "</td>" +
				'<td class="' + (r.is_late ? "text-danger" : "") + '">' + esc(r.planned_order_date ? frappe.datetime.str_to_user(r.planned_order_date) : "") + (r.is_late ? " (" + __("late") + ")" : "") + "</td>" +
				"<td>" + badge(r.action) + "</td><td>" + esc(fmt(r.net_qty)) + "</td>" +
				"<td>" + (r.editable ? '<input type="text" class="form-control input-xs ppc-qty' + (msg ? " has-error" : "") + '" style="width:110px" value="' + esc(String(st.qty)) + '">' +
					(msg ? '<div class="text-danger small">' + esc(msg) + "</div>" : "") : esc(r.document ? fmt(r.approved_qty) : "")) + "</td>" +
				"<td>" + esc(r.uom || "") + "</td><td>" + esc(r.source_documents || "") + "</td><td>" +
				(r.document ? '<a href="/app/' + frappe.router.slug(r.document_type) + "/" + encodeURIComponent(r.document) + '">' + esc(r.document) + "</a> " : "") +
				esc(r.status || r.note || "") + "</td></tr>";
		});
		if (!rows.length) h += '<tr><td colspan="11" class="text-muted text-center">' + __("No lines.") + "</td></tr>";
		h += "</tbody></table></div>";
		const picked = d.lines.filter((r) => r.editable && state(r).on);
		h += '<p class="text-muted">' + __("{0} selected: {1} purchase, {2} manufacture", [picked.length, picked.filter((r) => r.action === "Purchase").length, picked.filter((r) => r.action === "Manufacture").length]) + "</p>";
		$body.html(h);
		bind();
	}

	function bind() {
		$body.find(".ppc-all").on("change", function () {
			const on = this.checked;
			data.lines.forEach((r) => { if (r.editable) edits[r.name] = Object.assign({}, edits[r.name], { create_document: on ? 1 : 0, approved_qty: state(r).qty }); });
			render();
		});
		$body.find(".ppc-pick").on("change", function () {
			const r = rowOf(this);
			edits[r.name] = Object.assign({}, edits[r.name], { create_document: this.checked ? 1 : 0, approved_qty: state(r).qty });
			render();
		});
		$body.find(".ppc-qty").on("change", function () {
			const r = rowOf(this);
			edits[r.name] = Object.assign({}, edits[r.name], { approved_qty: this.value, create_document: state(r).on ? 1 : 0 });
			render();
		});
	}

	function rowOf(el) {
		const name = $(el).closest("tr").data("name");
		return data.lines.find((r) => r.name === name);
	}

	// ------------------------------------------------------------------ actions
	function load() {
		const run = runField.get_value();
		if (!run) { $body.html('<p class="text-muted">' + __("Choose an MRP Run.") + "</p>"); data = null; return; }
		frappe.call({ method: API + "mrp_review_data", args: { run } }).then((r) => {
			data = r.message;
			Object.keys(edits).forEach((k) => delete edits[k]);
			render();
		});
	}

	function guard() {
		if (!data) return false;
		if (!data.can_edit) { frappe.msgprint(__("This run cannot be edited (status {0}).", [__(data.status)])); return false; }
		const bad = firstProblem();
		if (bad) { frappe.msgprint({ title: __("Check the quantities"), message: esc(bad), indicator: "red" }); return false; }
		return true;
	}

	function save() {
		if (!guard()) return;
		return frappe.call({ method: API + "mrp_save_review", type: "POST", args: { run: data.run, lines: JSON.stringify(selections()) }, freeze: true })
			.then((r) => { frappe.show_alert({ message: __("Saved: {0} selected", [r.message.selected]), indicator: "green" }); load(); });
	}

	function createDocuments() {
		if (!guard()) return;
		const picked = data.lines.filter((r) => r.editable && state(r).on);
		if (!picked.length) return frappe.msgprint(__("Select at least one recommendation."));
		const buy = picked.filter((r) => r.action === "Purchase").length, make = picked.length - buy;
		frappe.confirm(
			__("Create {0} draft Material Request line(s) and {1} draft Work Order(s)? Stock, supply and demand are checked again first. Nothing is submitted.", [buy, make]),
			() => frappe.call({
				method: API + "mrp_create_documents", type: "POST", freeze: true, freeze_message: __("Creating drafts…"),
				args: { run: data.run, lines: JSON.stringify(selections()), confirm: 1 },
			}).then((r) => {
				const res = r.message || {};
				frappe.show_alert({ message: __("{0} draft document(s) created", [(res.created || []).length]), indicator: "green" });
				if ((res.skipped || []).length) frappe.msgprint(__("Not created, no longer short: {0}", [res.skipped.join(", ")]));
				load();
			})
		);
	}

	function retry() {
		if (!data || !data.can_retry) return frappe.msgprint(__("Only a failed or queued run can be retried."));
		frappe.call({ method: API + "mrp_retry", type: "POST", args: { run: data.run }, freeze: true, freeze_message: __("Calculating…") }).then(() => load());
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
