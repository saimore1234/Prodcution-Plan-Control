frappe.pages["ppc-mrp-dashboard"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("Material Planning (MRP)"), single_column: true });
	const $body = $('<div class="ppc-mrp"></div>').appendTo(page.main);
	const esc = frappe.utils.escape_html;
	const fmt = (v) => format_number(v || 0, null, 2);

	const run = page.add_field({ fieldname: "run", label: __("MRP Run"), fieldtype: "Link", options: "PPC MRP Run", change: () => load() });
	page.set_primary_action(__("New MRP Run"), () => frappe.new_doc("PPC MRP Run"));
	page.add_inner_button(__("Refresh"), () => load());
	wrapper.ppc_run_field = run;

	function card(label, count, qty, color, hint) {
		return (
			'<div class="ppc-mrp-card" style="border-left:4px solid ' + color + '">' +
			'<div class="lbl">' + esc(label) + "</div>" +
			'<div class="val">' + esc(String(count)) + "</div>" +
			(qty != null ? '<div class="sub">' + esc(fmt(qty)) + " " + __("units") + "</div>" : "") +
			(hint ? '<div class="sub">' + esc(hint) + "</div>" : "") + "</div>"
		);
	}

	function attention(d) {
		const a = d.attention || {};
		let h = '<div class="ppc-mrp-cards">' +
			card(__("Awaiting Review"), a.awaiting_review || 0, null, "#d97706", __("calculated runs")) +
			card(__("Failed Runs"), a.failed || 0, null, "#dc2626", __("retry from the run or review")) +
			card(__("Queued Runs"), a.queued || 0, null, "#64748b", __("automatic, not finished")) + "</div>";
		(d.failed || []).forEach((f) => {
			h += '<div class="alert alert-danger"><a href="/app/ppc-mrp-run/' + encodeURIComponent(f.name) + '">' + esc(f.name) + "</a>" +
				(f.source_sales_order ? " · " + esc(f.source_sales_order) : "") + ": " + esc((f.error_message || "").split("\n")[0].slice(0, 200)) + "</div>";
		});
		return h;
	}

	function render(d) {
		if (!d.run) {
			$body.html(attention(d) + '<p class="text-muted">' + __("No calculated MRP run yet. Create one with “New MRP Run”, then press Calculate.") + "</p>");
			return;
		}
		const s = d.summary || {};
		let h = attention(d) + '<p class="text-muted">' + __("Run") + ' <a href="/app/ppc-mrp-run/' + encodeURIComponent(d.run) + '">' + esc(d.run) + "</a> · " +
			esc(d.status) + (d.calculated_on ? " · " + esc(frappe.datetime.str_to_user(d.calculated_on)) : "") + "</p>";
		h += '<div class="ppc-mrp-cards">' +
			card(__("Total Demand"), s.demand_lines, s.demand_qty, "#2563eb", __("open demand lines")) +
			card(__("Stock Shortages"), s.shortage_items, s.shortage_qty, "#dc2626", __("items short in a warehouse")) +
			card(__("Purchase Requirements"), s.purchase_lines, s.purchase_qty, "#d97706") +
			card(__("Production Requirements"), s.production_lines, s.production_qty, "#16a34a") +
			card(__("Late Orders"), s.late_lines, null, "#9333ea", __("order date already passed")) +
			card(__("Need Review"), s.review_lines, null, "#64748b", __("no BOM and not purchasable")) + "</div>";
		(d.warnings || []).forEach((w) => (h += '<div class="alert alert-warning">' + esc(w) + "</div>"));
		h += "<h5>" + __("Largest open shortages") + "</h5>";
		if (!d.shortages.length) {
			h += '<p class="text-muted">' + __("No shortages.") + "</p>";
		} else {
			h += '<table class="table table-bordered"><thead><tr><th>' + [__("Item"), __("Warehouse"), __("Shortage"), __("Action"), __("Required On"), __("Order / Start By")]
				.join("</th><th>") + "</th></tr></thead><tbody>";
			d.shortages.forEach((r) => {
				h += "<tr" + (r.is_late ? ' class="text-danger"' : "") + "><td>" + esc(r.item_code) + ' <span class="text-muted">' + esc(r.item_name || "") + "</span></td><td>" +
					esc(r.warehouse || "") + "</td><td>" + esc(fmt(r.net_qty)) + " " + esc(r.uom || "") + "</td><td>" + esc(r.action) + "</td><td>" +
					esc(frappe.datetime.str_to_user(r.required_date)) + "</td><td>" + esc(r.planned_order_date ? frappe.datetime.str_to_user(r.planned_order_date) : "") + "</td></tr>";
			});
			h += "</tbody></table>";
		}
		h += '<p><a class="btn btn-sm btn-primary" href="/app/ppc-mrp-review/' + encodeURIComponent(d.run) + '">' + __("Open MRP Review") + "</a></p>";
		h += '<p><a href="/app/query-report/PPC%20MRP%20Item%20Wise?run=' + encodeURIComponent(d.run) + '">' + __("Item-wise report") + "</a> · " +
			'<a href="/app/query-report/PPC%20MRP%20Warehouse%20Wise?run=' + encodeURIComponent(d.run) + '">' + __("Warehouse-wise report") + "</a> · " +
			'<a href="/app/query-report/PPC%20MRP%20Date%20Wise?run=' + encodeURIComponent(d.run) + '">' + __("Date-wise report") + "</a></p>";
		$body.html(h);
	}

	function load() {
		frappe.call({ method: "ppc_production_control.api.mrp_dashboard", args: { run: run.get_value() || null } }).then((r) => render(r.message || {}));
	}
	wrapper.ppc_reload = load;
	load();
};

frappe.pages["ppc-mrp-dashboard"].on_page_show = function (wrapper) {
	const route = frappe.get_route();
	if (route[1] && wrapper.ppc_run_field) wrapper.ppc_run_field.set_value(decodeURIComponent(route[1]));
	else if (wrapper.ppc_reload) wrapper.ppc_reload();
};
