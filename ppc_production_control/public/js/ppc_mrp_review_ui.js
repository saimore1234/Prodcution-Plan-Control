/* PPC MRP review - ONE implementation of the review, used by the dialog that opens after a Sales Order / Production Plan is submitted
 * and by the MRP Review page.
 *
 * The browser only asks and displays. Every rule - what may be revised or skipped, who may approve, what is created, whether a request
 * is a repeat - is enforced by the server (ppc_production_control.services.mrp_approval); the checks here only save a round trip.
 *
 *   ppc_mrp.watch(doctype, name)            open the dialog for a submitted Sales Order / Production Plan (Calculating -> review)
 *   ppc_mrp.mount($container, run, opts)    show the review of one run in a container (the MRP Review page)
 *
 * Loaded with frappe.require("/assets/ppc_production_control/js/ppc_mrp_review_ui.js"); it styles itself (no build needed) and uses
 * Frappe's CSS variables, so it follows the light / dark theme.
 */
(function () {
	"use strict";
	if (window.ppc_mrp) return;

	const API = "ppc_production_control.api.";
	const WAIT_MS = 20000; // no result after this long: say the worker is not answering and offer Calculate Now
	const esc = (v) => frappe.utils.escape_html(v == null ? "" : String(v));
	const num = (v, d) => (v == null || v === "" ? "—" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d == null ? 3 : d }));
	const day = (v) => (v ? frappe.datetime.str_to_user(String(v).slice(0, 10)) : "—");
	const stamp = (v) => (v ? frappe.datetime.str_to_user(String(v).slice(0, 19)) : "—");
	const link = (doctype, name) => '<a href="/app/' + frappe.router.slug(doctype) + "/" + encodeURIComponent(name) + '" target="_blank">' + esc(name) + "</a>";
	const newKey = () => (window.crypto && crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2) + Date.now().toString(36) + Math.random().toString(36).slice(2));

	const ACTION = {
		Purchase: { label: __("Buy"), css: "orange" },
		Manufacture: { label: __("Manufacture"), css: "green" },
		Review: { label: __("Needs review"), css: "red" },
		None: { label: __("Covered"), css: "gray" },
	};
	const RELATION = {
		own: { label: __("For this order"), css: "green" },
		others: { label: __("Reserved for another order"), css: "orange" },
		ambiguous: { label: __("Shared - not counted"), css: "red" },
		free: { label: __("Free"), css: "blue" },
	};
	const pill = (text, css, title) => '<span class="indicator-pill ' + (css || "gray") + '"' + (title ? ' title="' + esc(title) + '"' : "") + ">" + esc(text) + "</span>";

	/* ------------------------------------------------------------------------------------------------ style */
	function ensureStyle() {
		if (document.getElementById("ppc-mrp-style")) return;
		const css = `
.ppc-mrp{--ppc-gap:12px;color:var(--text-color);font-size:var(--text-md,13px)}
.ppc-mrp *{box-sizing:border-box}
.ppc-mrp-head{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;margin-bottom:var(--ppc-gap)}
.ppc-mrp-head b{font-size:1.1em}
.ppc-mrp-muted{color:var(--text-muted)}
.ppc-mrp-banner{border:1px solid var(--border-color);border-left-width:4px;border-radius:var(--border-radius,6px);padding:8px 12px;margin:0 0 8px;background:var(--card-bg,transparent)}
.ppc-mrp-banner.warn{border-left-color:var(--orange-500,#e9973f)}.ppc-mrp-banner.err{border-left-color:var(--red-500,#e24c4c)}
.ppc-mrp-banner.ok{border-left-color:var(--green-500,#38a169)}.ppc-mrp-banner.info{border-left-color:var(--blue-500,#318ad8)}
.ppc-mrp-sources{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:var(--ppc-gap);margin-bottom:var(--ppc-gap)}
.ppc-mrp-card{border:1px solid var(--border-color);border-radius:var(--border-radius-md,8px);background:var(--card-bg,transparent);padding:10px 12px;min-width:0}
.ppc-mrp-card h5{margin:0 0 6px;font-size:1em}
.ppc-mrp-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:var(--ppc-gap);margin-bottom:8px}
.ppc-mrp-kpi{border:1px solid var(--border-color);border-radius:var(--border-radius-md,8px);background:var(--card-bg,transparent);padding:10px 12px}
.ppc-mrp-kpi .t{color:var(--text-muted);font-size:.85em}.ppc-mrp-kpi .v{font-size:1.6em;font-weight:600;line-height:1.25}
.ppc-mrp-kpi .h{color:var(--text-muted);font-size:.8em}.ppc-mrp-kpi.short .v{color:var(--red-600,#c0392b)}
.ppc-mrp-chips{display:flex;flex-wrap:wrap;gap:6px 14px;margin-bottom:var(--ppc-gap);color:var(--text-muted)}
.ppc-mrp-toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:var(--ppc-gap) 0}
.ppc-mrp-toolbar .sp{flex:1 1 auto}
.ppc-mrp-filters{display:flex;flex-wrap:wrap;gap:6px}
.ppc-mrp-filters button{border:1px solid var(--border-color);background:transparent;color:var(--text-color);border-radius:16px;padding:2px 12px;font-size:.9em}
.ppc-mrp-filters button.on{background:var(--primary,#2490ef);border-color:var(--primary,#2490ef);color:#fff}
.ppc-mrp-scroll{overflow-x:auto;border:1px solid var(--border-color);border-radius:var(--border-radius-md,8px)}
.ppc-mrp-table{width:100%;border-collapse:collapse;min-width:900px}
.ppc-mrp-table th{background:var(--subtle-fg,rgba(127,127,127,.08));font-weight:500;text-align:left;padding:8px;border-bottom:1px solid var(--border-color);white-space:nowrap;font-size:.9em}
.ppc-mrp-table td{padding:8px;border-bottom:1px solid var(--border-color);vertical-align:top}
.ppc-mrp-table tr.skipped td,.ppc-mrp-table tr.none td{opacity:.65}
.ppc-mrp-table tr.changed td{background:rgba(233,151,63,.12)}
.ppc-mrp-table .r{text-align:right;white-space:nowrap}
.ppc-mrp-table input.qty{width:96px;text-align:right}
.ppc-mrp-table .orig{text-decoration:line-through;color:var(--text-muted);margin-right:4px}
.ppc-mrp-table details{margin-top:6px}.ppc-mrp-table summary{cursor:pointer;color:var(--text-muted);font-size:.9em}
.ppc-mrp-detail{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:var(--ppc-gap);margin-top:8px;font-size:.92em}
.ppc-mrp-detail table{width:100%;border-collapse:collapse}.ppc-mrp-detail th,.ppc-mrp-detail td{padding:3px 6px;border-bottom:1px solid var(--border-color);text-align:left}
.ppc-mrp-detail .r{text-align:right}
.ppc-mrp-err{color:var(--red-600,#c0392b);font-size:.85em}
.ppc-mrp-history{margin-top:var(--ppc-gap)}.ppc-mrp-history li{margin-bottom:2px}
.ppc-mrp-spin{display:inline-block;width:18px;height:18px;border:3px solid var(--border-color);border-top-color:var(--primary,#2490ef);border-radius:50%;animation:ppcspin .9s linear infinite;vertical-align:-3px;margin-right:8px}
@keyframes ppcspin{to{transform:rotate(360deg)}}
.ppc-mrp-status{padding:28px 8px;text-align:center}.ppc-mrp-status h4{margin:8px 0}
@media (max-width:720px){
 .ppc-mrp-table{min-width:0}.ppc-mrp-table thead{display:none}
 .ppc-mrp-table tr{display:block;border-bottom:6px solid var(--bg-color,transparent);padding:4px 0}
 .ppc-mrp-table td{display:flex;justify-content:space-between;gap:12px;border-bottom:0;padding:4px 10px;text-align:right}
 .ppc-mrp-table td::before{content:attr(data-label);color:var(--text-muted);text-align:left;flex:0 0 40%}
 .ppc-mrp-table td.item{display:block;text-align:left}.ppc-mrp-table td.item::before{display:none}
 .ppc-mrp-toolbar .btn{flex:1 1 45%}.ppc-mrp-kpi .v{font-size:1.3em}
}`;
		const el = document.createElement("style");
		el.id = "ppc-mrp-style";
		el.textContent = css;
		document.head.appendChild(el);
	}

	/* ------------------------------------------------------------------------------------------------ server */
	function get(method, args, silent) {
		return frappe.call({ method: API + method, args: args || {}, type: "GET", silent: !!silent }).then((r) => r.message);
	}
	function post(method, args, freeze) {
		return frappe.call({ method: API + method, type: "POST", args: args || {}, freeze: !!freeze, freeze_message: typeof freeze === "string" ? freeze : undefined }).then((r) => r.message);
	}

	/* ------------------------------------------------------------------------------------------------ the review */
	class Review {
		constructor(opts) {
			this.$el = opts.$container;
			this.run = opts.run;
			this.standalone = !!opts.standalone; // on the page: offers "Open Run"
			this.onChange = opts.onChange || (() => {});
			this.data = null;
			this.filter = "all";
			this.pending = {}; // line -> quantity typed but not sent yet
			this.edits = {}; // legacy (not migrated) mode: line -> {on, qty}
			this.busy = false;
			this.approvalKey = null; // made when the first approval is attempted, re-sent when a request is repeated
		}

		load() {
			return get("mrp_review_data", { run: this.run }).then((d) => {
				this.data = d;
				this.pending = {};
				this.edits = {};
				this.render();
				this.onChange(d);
				return d;
			});
		}

		get legacy() { return !this.data.approval_ready; }

		/* ---- state helpers */
		qtyOf(r) {
			if (this.legacy) { const e = this.edits[r.name] || {}; return e.qty != null ? e.qty : r.approved_qty || r.net_qty; }
			return this.pending[r.name] != null ? this.pending[r.name] : r.approved_qty || r.net_qty;
		}
		onOf(r) {
			if (this.legacy) { const e = this.edits[r.name] || {}; return e.on != null ? !!e.on : !!r.create_document; }
			return !!cint(r.create_document);
		}
		problem(r) {
			if (!r.editable || !this.onOf(r)) return "";
			const q = flt(this.qtyOf(r));
			if (!(q > 0)) return __("Quantity must be greater than zero");
			if (cint(r.whole_number) && Math.abs(q - Math.round(q)) > 1e-9) return __("{0} must be a whole number", [r.uom]);
			if (q > flt(r.net_qty) * this.data.max_edit_factor + 1e-9) return __("At most {0} × the shortage ({1})", [this.data.max_edit_factor, num(r.net_qty)]);
			return "";
		}
		firstProblem() {
			for (const r of this.data.lines) { const m = this.problem(r); if (m) return __("Row {0} ({1}): {2}", [r.idx, r.item_code, m]); }
			return "";
		}
		changedLines() { return this.data.lines.filter((r) => this.pending[r.name] != null && flt(this.pending[r.name]) !== flt(r.approved_qty)); }
		picked() { return this.data.lines.filter((r) => r.editable && this.onOf(r)); }

		/* ---- render */
		statusPills() {
			const d = this.data, run = { Draft: ["Calculating", "blue"], Queued: ["Calculating", "blue"], Calculated: ["Calculated", "green"], "Documents Created": ["Drafts Created", "green"], Failed: ["Failed", "red"] }[d.status] || [d.status, "gray"];
			let h = pill(__(run[0]), run[1]);
			const ap = d.approval_status;
			if (ap) h += " " + pill(__(ap), { "Awaiting Review": "orange", Approved: "green", Cancelled: "gray" }[ap] || "gray");
			return h;
		}

		sourcesHtml() {
			return '<div class="ppc-mrp-sources">' + (this.data.sources || []).map((s) => {
				const items = (s.items || []).map((i) => "<tr><td>" + esc(i.item_code) + ' <span class="ppc-mrp-muted">' + esc(i.item_name || "") + "</span></td><td class=\"r\">" + num(i.qty) + " " + esc(i.uom || "") + "</td><td>" +
					(i.bom_no ? link("BOM", i.bom_no) : '<span class="ppc-mrp-muted">—</span>') + "</td></tr>").join("");
				return '<div class="ppc-mrp-card"><h5>' + esc(__(s.doctype)) + " " + link(s.doctype, s.name) + "</h5>" +
					(s.customer ? "<div>" + esc(s.customer_name || s.customer) + "</div>" : "") +
					'<div class="ppc-mrp-muted">' + (s.date ? esc(__("Date")) + " " + day(s.date) : "") + (s.delivery_date ? " · " + esc(__("Delivery")) + " " + day(s.delivery_date) : "") + (s.status ? " · " + esc(__(s.status)) : "") + "</div>" +
					(items ? '<table class="ppc-mrp-detail" style="display:table;margin-top:6px"><thead><tr><th>' + esc(__("Item")) + '</th><th class="r">' + esc(__("Ordered")) + "</th><th>" + esc(__("BOM")) + "</th></tr></thead><tbody>" + items + "</tbody></table>" : "") +
					"</div>";
			}).join("") + "</div>";
		}

		kpisHtml() {
			const k = this.data.kpis || {};
			const card = (t, v, h, cls) => '<div class="ppc-mrp-kpi ' + (cls || "") + '"><div class="t">' + esc(t) + '</div><div class="v">' + num(v) + '</div><div class="h">' + esc(h || "") + "</div></div>";
			return '<div class="ppc-mrp-kpis">' +
				card(__("Total demand"), k.demand_qty, __("ordered quantity of this run")) +
				card(__("Available stock"), k.stock_qty, __("used from stock, all lines")) +
				card(__("Existing supply"), k.supply_qty, __("open Work Orders / Material Requests counted")) +
				card(__("Shortage"), k.shortage_qty, __("still to plan"), k.shortage_qty > 0 ? "short" : "") + "</div>" +
				'<div class="ppc-mrp-chips"><span>' + esc(__("Buy")) + ": <b>" + num(k.purchase_lines, 0) + "</b> " + esc(__("line(s)")) + " · " + num(k.purchase_qty) + "</span>" +
				"<span>" + esc(__("Manufacture")) + ": <b>" + num(k.production_lines, 0) + "</b> " + esc(__("line(s)")) + " · " + num(k.production_qty) + "</span>" +
				(k.review_lines ? "<span>" + esc(__("Need review")) + ": <b>" + k.review_lines + "</b></span>" : "") +
				(k.late_lines ? '<span class="text-danger">' + esc(__("Late")) + ": <b>" + k.late_lines + "</b></span>" : "") +
				(this.data.expected_completion ? "<span>" + esc(__("Expected completion")) + ": <b>" + day(this.data.expected_completion) + "</b></span>" : "") + "</div>";
		}

		bannersHtml() {
			const d = this.data, ap = d.approval || {};
			let h = "";
			if (d.error_message) h += '<div class="ppc-mrp-banner err"><b>' + esc(__("Calculation failed")) + ":</b> " + esc(d.error_message) + "</div>";
			if (d.status === "Queued" || d.status === "Draft") h += '<div class="ppc-mrp-banner info">' + esc(__("This run is queued or did not finish. Use Retry Calculation if it stays here.")) + "</div>";
			if (ap.status === "Cancelled") h += '<div class="ppc-mrp-banner warn"><b>' + esc(__("Review cancelled")) + ".</b> " + esc(ap.cancel_reason || "") + " " + esc(__("Recalculate to reopen it.")) + "</div>";
			if (d.status === "Documents Created") {
				const docs = (d.documents || []).map((x) => link(x.doctype, x.name)).filter((v, i, a) => a.indexOf(v) === i).join(", ");
				h += '<div class="ppc-mrp-banner ok"><b>' + esc(__("Draft documents created")) + ".</b> " + docs + " " + esc(__("They are drafts: review and submit them yourself.")) +
					(ap.approved_by ? "<br>" + esc(__("Approved by {0} on {1}", [ap.approved_by, stamp(ap.approved_on)])) : "") +
					(ap.automation_policy ? "<br>" + esc(__("Created by automation policy: {0}", [ap.automation_policy])) : "") + "</div>";
			}
			(d.warnings || []).forEach((w) => (h += '<div class="ppc-mrp-banner warn">' + esc(w) + "</div>"));
			if (!d.approval_ready) h += '<div class="ppc-mrp-banner info">' + esc(__("The review and approval fields are not installed on this site (run bench migrate); the classic selection is shown.")) + "</div>";
			return h;
		}

		toolbarHtml() {
			const d = this.data, p = d.permissions || {}, b = (id, label, cls, on, title) =>
				'<button type="button" class="btn btn-sm ' + cls + '" data-act="' + id + '"' + (on && !this.busy ? "" : " disabled") + (title ? ' title="' + esc(title) + '"' : "") + ">" + esc(label) + "</button>";
			const chips = [["all", __("All")], ["Purchase", __("Buy")], ["Manufacture", __("Manufacture")], ["Review", __("Needs review")], ["skipped", __("Skipped")]]
				.map((c) => '<button type="button" data-filter="' + c[0] + '" class="' + (this.filter === c[0] ? "on" : "") + '">' + esc(c[1]) + "</button>").join("");
			let h = '<div class="ppc-mrp-toolbar"><div class="ppc-mrp-filters">' + chips + '</div><span class="sp"></span>';
			if (this.legacy) {
				h += b("save", __("Save Selection"), "btn-default", d.can_edit) + b("create", __("Create Draft Documents"), "btn-primary", d.can_edit);
			} else {
				h += b("revise", __("Revise"), "btn-default", p.can_edit && this.changedLines().length, __("Apply the quantities you changed"));
				h += b("recalc", __("Recalculate"), "btn-default", p.can_recalculate);
				h += b("cancel", __("Cancel"), "btn-default", p.can_cancel, __("Close the review without creating anything"));
				h += b("approve", __("Approve & Create Drafts"), "btn-primary", p.can_approve && this.picked().length);
			}
			if (d.can_retry) h += b("retry", __("Retry Calculation"), "btn-default", d.permissions ? d.permissions.can_manage : true);
			if (this.standalone) h += b("open", __("Open Run"), "btn-default", true);
			return h + "</div>";
		}

		rowHtml(r) {
			const edit = r.editable, on = this.onOf(r), msg = this.problem(r), changed = this.pending[r.name] != null && flt(this.pending[r.name]) !== flt(r.approved_qty);
			const cls = [r.skipped ? "skipped" : "", r.action === "None" ? "none" : "", changed ? "changed" : ""].join(" ");
			const act = ACTION[r.action] || { label: r.action, css: "gray" };
			const orig = flt(r.original_qty) || flt(r.net_qty);
			const qtyCell = edit
				? (r.revised && !changed ? '<span class="orig">' + num(orig) + "</span>" : "") +
				  '<input type="text" class="form-control input-xs qty' + (msg ? " has-error" : "") + '" value="' + esc(String(this.qtyOf(r))) + '"' + (on ? "" : " disabled") + ">" + (msg ? '<div class="ppc-mrp-err">' + esc(msg) + "</div>" : "")
				: (r.document ? num(r.approved_qty) : "");
			let result = "";
			if (r.document) result += link(r.document_type, r.document) + " ";
			if (r.skipped) result += pill(__("Skipped"), "gray") + " " + esc(r.skip_reason || "");
			else if (r.needs_reason) result += pill(__("Needs a reason"), "red", __("Skip it with a reason or tick it again"));
			else result += esc(r.status || r.note || "");
			if (r.revised) result += " " + pill(__("Revised"), "orange", __("Revised by {0}", [r.revised_by]));
			return '<tr data-name="' + esc(r.name) + '" class="' + cls + '">' +
				'<td data-label="' + esc(__("Include")) + '"><input type="checkbox" class="pick"' + (edit ? "" : " disabled") + (edit && on ? " checked" : "") + "></td>" +
				'<td class="item" data-label="' + esc(__("Item")) + '"><b>' + esc(r.item_code) + '</b> <span class="ppc-mrp-muted">' + esc(r.item_name || "") + "</span> " +
				(r.procurement_type ? "" : pill(__("type?"), "gray", __("Procurement Type not set on the Item; it was inferred"))) + (r.is_late ? " " + pill(__("Late"), "red") : "") +
				this.detailsHtml(r) + "</td>" +
				'<td data-label="' + esc(__("Type")) + '">' + pill(act.label, act.css) + "</td>" +
				'<td data-label="' + esc(__("Required / order by")) + '">' + day(r.required_date) + (r.planned_order_date ? '<div class="ppc-mrp-muted ' + (r.is_late ? "text-danger" : "") + '">' + esc(__("order by")) + " " + day(r.planned_order_date) + "</div>" : "") + "</td>" +
				'<td data-label="' + esc(__("Warehouse")) + '">' + esc(r.warehouse || "") + "</td>" +
				'<td class="r" data-label="' + esc(__("Demand")) + '">' + num(r.gross_qty) + "</td>" +
				'<td class="r" data-label="' + esc(__("Stock")) + '">' + num(r.from_stock) + "</td>" +
				'<td class="r" data-label="' + esc(__("Supply")) + '">' + num(r.from_supply) + "</td>" +
				'<td class="r" data-label="' + esc(__("Shortage")) + '"><b>' + num(r.net_qty) + "</b></td>" +
				'<td class="r" data-label="' + esc(__("Quantity to create")) + '">' + qtyCell + ' <span class="ppc-mrp-muted">' + esc(r.uom || "") + "</span></td>" +
				'<td data-label="' + esc(__("Result")) + '">' + result + "</td></tr>";
		}

		detailsHtml(r) {
			const stock = (r.stock_by_warehouse || []).map((b) => "<tr" + (b.in_run ? ' style="font-weight:600"' : "") + "><td>" + esc(b.warehouse) + '</td><td class="r">' + num(b.actual) + '</td><td class="r">' + num(b.reserved) + '</td><td class="r">' + num(b.ordered + b.indented + b.planned) + "</td></tr>").join("");
			const supply = (r.supply_documents || []).map((s) => {
				const rel = RELATION[s.relation] || RELATION.free;
				return "<tr><td>" + link(s.doctype, s.name) + (s.draft ? " " + esc(__("(draft)")) : "") + '</td><td class="r">' + num(s.qty) + "</td><td>" + pill(rel.label, rel.css, (s.owners || []).join(", ")) + (s.counted ? "" : " " + pill(__("excluded by setting"), "gray")) + "</td></tr>";
			}).join("");
			const bom = r.bom ? "<tr><td colspan=\"3\">" + link("BOM", r.bom.name) + " · " + num(r.bom.quantity) + " " + esc(r.bom.uom || "") + "</td></tr>" +
				r.bom.items.map((i) => "<tr><td>" + esc(i.item_code) + '</td><td class="r">' + num(i.stock_qty) + " " + esc(i.stock_uom || "") + '</td><td class="r">' + num(i.needed) + "</td></tr>").join("") : "";
			return '<details><summary>' + esc(__("Details: stock, supply, BOM")) + "</summary><div class=\"ppc-mrp-detail\">" +
				"<div><b>" + esc(__("Stock by warehouse")) + "</b><table><thead><tr><th>" + esc(__("Warehouse")) + '</th><th class="r">' + esc(__("On hand")) + '</th><th class="r">' + esc(__("Reserved")) + '</th><th class="r">' + esc(__("Incoming")) + "</th></tr></thead><tbody>" +
				(stock || '<tr><td colspan="4" class="ppc-mrp-muted">' + esc(__("No stock")) + "</td></tr>") + "</tbody></table></div>" +
				"<div><b>" + esc(__("Existing Work Orders / Material Requests / Purchase Orders")) + "</b><table><thead><tr><th>" + esc(__("Document")) + '</th><th class="r">' + esc(__("Open qty")) + "</th><th>" + esc(__("Whose supply")) + "</th></tr></thead><tbody>" +
				(supply || '<tr><td colspan="3" class="ppc-mrp-muted">' + esc(__("None")) + "</td></tr>") + "</tbody></table></div>" +
				"<div><b>" + esc(__("BOM")) + "</b><table><thead><tr><th>" + esc(__("Component")) + '</th><th class="r">' + esc(__("Per BOM")) + '</th><th class="r">' + esc(__("Needed")) + "</th></tr></thead><tbody>" +
				(bom || '<tr><td colspan="3" class="ppc-mrp-muted">' + esc(__("No BOM for this line")) + "</td></tr>") + "</tbody></table></div></div>" +
				(r.source_documents ? '<div class="ppc-mrp-muted" style="margin-top:6px">' + esc(__("For")) + ": " + esc(r.source_documents) + "</div>" : "") + "</details>";
		}

		historyHtml() {
			const h = this.data.history || [];
			if (!h.length) return "";
			return '<details class="ppc-mrp-history"><summary>' + esc(__("Review history ({0})", [h.length])) + "</summary><ul>" + h.map((e) =>
				"<li>" + esc(stamp(e.timestamp)) + " · " + esc(e.user || "") + " · <b>" + esc(e.action) + "</b>" + (e.result && e.result !== "Success" ? " (" + esc(e.result) + ")" : "") +
				(e.item_code ? " · " + esc(e.item_code) : "") + (e.original_qty != null || e.revised_qty != null ? " · " + esc(__("original {0} → {1}", [num(e.original_qty), num(e.revised_qty)])) : "") +
				(e.reason ? " · " + esc(e.reason) : "") + (e.error ? ' · <span class="ppc-mrp-err">' + esc(e.error) + "</span>" : "") + "</li>").join("") + "</ul></details>";
		}

		render() {
			if (!this.data) return;
			const d = this.data;
			let rows = d.lines;
			if (this.filter === "skipped") rows = rows.filter((r) => r.skipped || r.needs_reason);
			else if (this.filter !== "all") rows = rows.filter((r) => r.action === this.filter);
			const sel = this.picked();
			this.$el.html(
				'<div class="ppc-mrp" data-run="' + esc(d.run) + '" data-status="' + esc(d.status) + '" data-approval="' + esc(d.approval_status || "") + '">' +
				'<div class="ppc-mrp-head"><b>' + link("PPC MRP Run", d.run) + "</b>" + this.statusPills() + '<span class="ppc-mrp-muted">' + esc(__(d.trigger || "Manual")) +
				(d.calculated_on ? " · " + esc(stamp(d.calculated_on)) : "") + "</span></div>" +
				this.bannersHtml() + this.sourcesHtml() + this.kpisHtml() + this.toolbarHtml() +
				'<div class="ppc-mrp-scroll"><table class="ppc-mrp-table"><thead><tr><th style="width:36px"></th><th>' + [__("Item"), __("Type"), __("Required / order by"), __("Warehouse"), __("Demand"), __("Stock"), __("Supply"), __("Shortage"), __("Quantity to create"), __("Result")].map(esc).join("</th><th>") +
				"</th></tr></thead><tbody>" + rows.map((r) => this.rowHtml(r)).join("") + (rows.length ? "" : '<tr><td colspan="11" class="ppc-mrp-muted" style="text-align:center">' + esc(__("No lines.")) + "</td></tr>") + "</tbody></table></div>" +
				'<p class="ppc-mrp-muted" style="margin-top:8px">' + esc(__("{0} selected: {1} buy, {2} manufacture", [sel.length, sel.filter((r) => r.action === "Purchase").length, sel.filter((r) => r.action === "Manufacture").length])) +
				" · " + esc(__("Nothing is created until you approve, and everything is created as a draft.")) + "</p>" + this.historyHtml() + "</div>"
			);
			this.bind();
		}

		/* ---- events */
		line(el) { const name = $(el).closest("tr").data("name"); return this.data.lines.find((r) => r.name === name); }

		bind() {
			const $e = this.$el;
			$e.find("[data-filter]").on("click", (ev) => { this.filter = ev.currentTarget.dataset.filter; this.render(); });
			$e.find("[data-act]").on("click", (ev) => this.act(ev.currentTarget.dataset.act));
			$e.find(".pick").on("change", (ev) => this.toggle(this.line(ev.currentTarget), ev.currentTarget.checked));
			$e.find(".qty").on("input change", (ev) => {
				const r = this.line(ev.currentTarget);
				if (this.legacy) this.edits[r.name] = Object.assign({}, this.edits[r.name], { qty: ev.currentTarget.value, on: this.onOf(r) ? 1 : 0 });
				else this.pending[r.name] = ev.currentTarget.value;
				if (ev.type === "change") this.render();
			});
		}

		toggle(r, on) {
			if (this.legacy) { this.edits[r.name] = Object.assign({}, this.edits[r.name], { on: on ? 1 : 0, qty: this.qtyOf(r) }); return this.render(); }
			if (on) return this.run_(post("mrp_revise_lines", { run: this.run, lines: JSON.stringify([{ name: r.name, qty: this.qtyOf(r) }]) }));
			// skipping needs a reason: ask, and put the tick back if the person walks away
			let sent = false;
			const d = frappe.prompt([{ fieldname: "reason", fieldtype: "Small Text", label: __("Why is {0} skipped?", [r.item_code]), reqd: 1 }], (v) => {
				sent = true;
				this.run_(post("mrp_skip_line", { run: this.run, line: r.name, reason: v.reason }));
			}, __("Skip {0}", [r.item_code]), __("Skip"));
			d.onhide = () => { if (!sent) this.render(); };
		}

		/* every change goes through here: one request at a time (double clicks cannot start a second), then the review is reloaded */
		run_(promise, done) {
			if (this.busy) return Promise.resolve();
			this.busy = true;
			this.render();
			return promise.then((res) => { this.busy = false; return this.load().then(() => { if (done) done(res); return res; }); })
				.catch((e) => { this.busy = false; return this.load().then(() => Promise.reject(e)); });
		}

		act(name) {
			const d = this.data;
			if (name === "open") return frappe.set_route("Form", "PPC MRP Run", d.run);
			if (name === "retry") return this.run_(post("mrp_retry", { run: this.run }, __("Calculating…")));
			if (name === "revise") {
				const bad = this.firstProblem();
				if (bad) return frappe.msgprint({ title: __("Check the quantities"), message: esc(bad), indicator: "red" });
				return this.run_(post("mrp_revise_lines", { run: this.run, lines: JSON.stringify(this.changedLines().map((r) => ({ name: r.name, qty: this.pending[r.name] }))) }), (res) => frappe.show_alert({ message: __("{0} quantity(ies) revised", [res.changed]), indicator: "green" }));
			}
			if (name === "recalc") {
				return frappe.confirm(__("Calculate again with today's stock and supply? Quantities you revised and items you skipped will be discarded."), () =>
					this.run_(post("mrp_recalculate", { run: this.run }, __("Calculating…")), () => frappe.show_alert({ message: __("Recalculated"), indicator: "green" })));
			}
			if (name === "cancel") {
				let sent = false;
				const p = frappe.prompt([{ fieldname: "reason", fieldtype: "Small Text", label: __("Why is this review cancelled?"), reqd: 1 }], (v) => {
					sent = true;
					this.run_(post("mrp_cancel_run", { run: this.run, reason: v.reason }), () => frappe.show_alert({ message: __("Review cancelled; nothing was created"), indicator: "orange" }));
				}, __("Cancel the review"), __("Cancel the review"));
				p.onhide = () => { if (!sent) this.render(); };
				return;
			}
			if (this.legacy) return this.legacyAct(name);
			if (name === "approve") return this.approve();
		}

		approve() {
			if (this.approving) return; // a double click opens one confirmation, not two (the server also refuses a second approval)
			const bad = this.firstProblem();
			if (bad) return frappe.msgprint({ title: __("Check the quantities"), message: esc(bad), indicator: "red" });
			this.approving = true;
			const release = () => { this.approving = false; };
			// quantities typed but not applied yet are applied first (audited), so what is approved is what is on screen
			const go = () => {
				const sel = this.picked(), buy = sel.filter((r) => r.action === "Purchase").length, make = sel.length - buy;
				if (!sel.length) { release(); return frappe.msgprint(__("Select at least one recommendation.")); }
				const confirmation = frappe.confirm(
					__("Create {0} draft Material Request line(s) and {1} draft Work Order(s)? Stock, supply and demand are checked again first. Nothing is submitted.", [buy, make]),
					() => {
						this.approvalKey = this.approvalKey || newKey();
						this.run_(post("mrp_approve_and_create", { run: this.run, approval_key: this.approvalKey, token: this.data.token, confirm: 1 }, __("Creating drafts…")), (res) => {
							this.approvalKey = null;
							frappe.show_alert({ message: res.replayed ? __("Already approved: nothing was created twice") : (res.message || __("{0} draft document(s) created", [(res.created || []).length])), indicator: "green" });
							if ((res.skipped || []).length) frappe.msgprint(__("Not created, no longer short: {0}", [res.skipped.join(", ")]));
						});
					}
				);
				if (confirmation && confirmation.$wrapper) confirmation.$wrapper.on("hidden.bs.modal", release);
				else release();
			};
			if (this.changedLines().length) return this.run_(post("mrp_revise_lines", { run: this.run, lines: JSON.stringify(this.changedLines().map((r) => ({ name: r.name, qty: this.pending[r.name] }))) })).then(go, release);
			go();
		}

		/* the classic selection + create, for a site whose review fields are not installed yet (unchanged server API) */
		legacyAct(name) {
			const bad = this.firstProblem();
			if (bad) return frappe.msgprint({ title: __("Check the quantities"), message: esc(bad), indicator: "red" });
			const selections = JSON.stringify(this.data.lines.filter((r) => r.editable).map((r) => ({ name: r.name, create_document: this.onOf(r) ? 1 : 0, approved_qty: flt(this.qtyOf(r)) })));
			if (name === "save") return this.run_(post("mrp_save_review", { run: this.run, lines: selections }), (r) => frappe.show_alert({ message: __("Saved: {0} selected", [r.selected]), indicator: "green" }));
			const sel = this.picked();
			if (!sel.length) return frappe.msgprint(__("Select at least one recommendation."));
			const buy = sel.filter((r) => r.action === "Purchase").length;
			frappe.confirm(__("Create {0} draft Material Request line(s) and {1} draft Work Order(s)? Stock, supply and demand are checked again first. Nothing is submitted.", [buy, sel.length - buy]),
				() => this.run_(post("mrp_create_documents", { run: this.run, lines: selections, confirm: 1 }, __("Creating drafts…")), (r) => frappe.show_alert({ message: __("{0} draft document(s) created", [(r.created || []).length]), indicator: "green" })));
		}
	}

	/* ------------------------------------------------------------------------------------------------ the dialog after a submit */
	const watching = {}; // doctype + name -> true while its dialog is open: a submit and a workflow action for the same document open one dialog

	function watch(doctype, name) {
		ensureStyle();
		const key = doctype + "\u0000" + name;
		if (watching[key]) return Promise.resolve(null);
		watching[key] = true;
		const release = () => { delete watching[key]; };
		return get("mrp_run_for_source", { doctype, name }, true).then((s) => {
			if (!s || !s.enabled || s.state === "not_submitted") { release(); return null; } // automatic MRP is off / not available: no dialog at all
			const h = openDialog(doctype, name, s);
			h.dialog.$wrapper.on("hidden.bs.modal", release);
			return h;
		}).catch(() => { release(); return null; }); // no PPC role, not migrated ...: the submit is never disturbed
	}

	function openDialog(doctype, name, first) {
		const started = Date.now();
		let timer = null, closed = false, review = null, calculating = false, shown = null;
		const d = new frappe.ui.Dialog({ title: __("Material Requirement Planning"), size: "extra-large", fields: [{ fieldtype: "HTML", fieldname: "body" }] });
		const $body = d.fields_dict.body.$wrapper;
		$body.html('<div class="ppc-mrp-dialog" data-state="calculating"><div class="ppc-mrp-status"></div><div class="ppc-mrp-review"></div></div>');
		const $root = $body.find(".ppc-mrp-dialog"), $status = $root.find(".ppc-mrp-status"), $review = $root.find(".ppc-mrp-review");
		const setState = (s) => $root.attr("data-state", s);

		const stopTimer = () => { if (timer) { clearTimeout(timer); timer = null; } };
		const onReady = (msg) => { if (msg && msg.source_name === name) poll(); };
		frappe.realtime.on("ppc_mrp_ready", onReady);
		d.onhide = () => { closed = true; stopTimer(); frappe.realtime.off("ppc_mrp_ready", onReady); };

		const showCalculating = () => {
			setState("calculating");
			d.set_title(__("Material Requirement Planning"));
			$status.html('<h4><span class="ppc-mrp-spin"></span>' + esc(__("Calculating…")) + "</h4><p class=\"ppc-mrp-muted\">" + esc(__("Working out the material requirements of {0}. This usually takes a few seconds.", [name])) + "</p>");
		};
		const showWaiting = (canCalc) => {
			setState("waiting");
			d.set_title(__("Material Requirement Planning"));
			$status.html('<h4><span class="ppc-mrp-spin"></span>' + esc(__("Waiting for the background worker")) + "</h4>" +
				'<p class="ppc-mrp-muted">' + esc(__("The calculation is queued, but no background worker has picked it up yet. It starts by itself as soon as one does.")) + "</p>" +
				(canCalc ? '<button type="button" class="btn btn-primary btn-sm" data-act="now">' + esc(__("Calculate Now")) + "</button>" : '<p class="ppc-mrp-muted">' + esc(__("Ask someone with the Manage Plans capability to calculate it.")) + "</p>"));
			$status.find("[data-act=now]").on("click", calculateNow);
		};
		const showFailed = (s) => {
			setState("failed");
			d.set_title(__("Material Requirement Planning"));
			$status.html('<div class="ppc-mrp-banner err" style="text-align:left"><b>' + esc(__("The calculation failed")) + ".</b> " + esc(s.error || "") + "</div>" +
				(s.can_calculate ? '<button type="button" class="btn btn-primary btn-sm" data-act="now">' + esc(__("Calculate Now")) + "</button>" : ""));
			$status.find("[data-act=now]").on("click", calculateNow);
		};
		const showReview = (s) => {
			stopTimer();
			setState(s.state);
			$status.empty();
			d.set_title(s.state === "approved" ? __("Draft documents created") : s.state === "cancelled" ? __("Review cancelled") : __("Material Requirement Planning Completed"));
			if (!review || review.run !== s.run) {
				review = new Review({ $container: $review, run: s.run, onChange: (data) => setState(data.status === "Documents Created" ? "approved" : data.approval_status === "Cancelled" ? "cancelled" : "calculated") });
			}
			review.load();
		};

		const handle = (s) => {
			if (closed || !s || !s.enabled) return;
			const key = s.state;
			if (["calculated", "approved", "cancelled"].includes(key)) { if (shown !== s.run + key) { shown = s.run + key; showReview(s); } return; }
			if (key === "failed") { showFailed(s); stopTimer(); return; }
			// waiting / calculating: only a real Calculated run ever shows the completed title
			if (Date.now() - started > WAIT_MS && !calculating) showWaiting(s.can_calculate); else if (!calculating) showCalculating();
			schedule();
		};
		const schedule = () => { stopTimer(); if (!closed) timer = setTimeout(poll, Date.now() - started > WAIT_MS ? 4000 : 1500); };
		const poll = () => { if (closed) return; stopTimer(); get("mrp_run_for_source", { doctype, name }, true).then(handle).catch(() => schedule()); };
		const calculateNow = () => {
			calculating = true;
			stopTimer();
			showCalculating();
			post("mrp_calculate_now", { doctype, name }).then((s) => { calculating = false; handle(Object.assign({ enabled: true }, s)); }).catch(() => { calculating = false; poll(); });
		};

		d.show();
		showCalculating();
		handle(first);
		return { dialog: d, poll, calculateNow };
	}

	/* ------------------------------------------------------------------------------------------------ the page */
	function mount($container, run, opts) {
		ensureStyle();
		const r = new Review(Object.assign({ $container, run, standalone: true }, opts || {}));
		return r.load().then(() => r);
	}

	window.ppc_mrp = { watch, mount, Review, ensureStyle };
})();
