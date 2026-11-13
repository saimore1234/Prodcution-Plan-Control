/*
 * MRP review dialog after a Sales Order is submitted - browser end-to-end test (Playwright).
 *
 * A PPC manager submits a draft Sales Order and watches "Calculating…". With no background worker the dialog says it is waiting and
 * offers Calculate Now; with a worker (played here by the fixture) it simply turns into the review. The review is then revised,
 * skipped (with a reason), recalculated and approved - the draft documents appear only then, exactly once - or cancelled. The same
 * review is checked on the MRP Review page, as a viewer (nothing can be changed) and on a phone-sized screen.
 *
 * Run against the PRIVATE STAGING site only (it COMMITS disposable PPCE2E-MRP-* records and removes them again, fixture.py):
 *   PPC_E2E_BASE=http://localhost:8001  PPC_E2E_SITE=ppcbase.local  PPC_E2E_BROWSER=<msedge.exe>  PPC_E2E_RUNNER="wsl.exe -d Ubuntu-22.04 -u sai -e bash <run_fixture.sh>"
 *   (WSLENV=PPC_E2E_SITE so the site name reaches WSL)
 */
const L = require("./e2e_lib");
const { assert, path, SHOTS, BASE, MANAGER, VIEWER, fx, login } = L;

const DIALOG = ".ppc-mrp-dialog";
const state = (page) => page.locator(DIALOG).first().getAttribute("data-state");
const shot = (page, name) => page.screenshot({ path: path.join(SHOTS, name + ".png"), fullPage: false });

/** Submit a draft Sales Order the way a user does: the Submit button, or - on a site with a Sales Order workflow
 *  (Draft -> Pending Approval -> Approved) - the workflow actions in the Actions menu until the order is submitted. */
async function submitSalesOrder(page, so) {
	await page.goto(`${BASE}/app/sales-order/${so}`);
	await page.waitForFunction((n) => window.cur_frm && cur_frm.doc && cur_frm.doc.name === n && !cur_frm.is_dirty(), so, { timeout: 60000 });
	await L.untilIdle(page);
	for (let i = 0; i < 4; i++) {
		const st = await page.evaluate(() => ({ docstatus: cur_frm.doc.docstatus, wf: cur_frm.doc.workflow_state || null }));
		if (st.docstatus === 1) return;
		const direct = page.locator(".page-actions .primary-action:visible").filter({ hasText: /Submit/ });
		if (await direct.count()) {
			await direct.first().click();
			await page.locator(".modal.show .btn-primary").filter({ hasText: /Yes|Submit/ }).first().click(); // "Permanently Submit?"
			return;
		}
		await page.locator(".page-actions .actions-btn-group > button").first().click();
		await page.locator(".actions-btn-group .dropdown-menu a:visible", { hasText: /Submit for L1 Approval|Approve/ }).first().click();
		await page.locator(".modal.show .btn-primary").filter({ hasText: /^\s*(Yes|OK)\s*$/ }).first().click({ timeout: 2500 }).catch(() => {}); // a confirmation, if the site asks
		await page.waitForFunction(([w]) => cur_frm.doc.workflow_state !== w || cur_frm.doc.docstatus === 1, [st.wf], { timeout: 60000 });
	}
	throw new Error("the Sales Order could not be submitted through the UI");
}

const waitState = (page, want, timeout = 60000) =>
	page.waitForFunction(([sel, w]) => { const e = document.querySelector(sel); return !!e && e.getAttribute("data-state") === w; }, [DIALOG, want], { timeout });

async function confirmYes(page) {
	const m = page.locator(".modal.show").last();
	await m.waitFor();
	await page.waitForFunction(L.modalFullyShown);
	await m.locator(".btn-primary").filter({ hasText: /Yes|OK/ }).first().click();
}

async function promptText(page, text) {
	const m = page.locator(".modal.show").last();
	await m.waitFor();
	await page.waitForFunction(L.modalFullyShown);
	await m.locator("textarea").first().fill(text);
	await m.locator(".modal-footer .btn-primary:visible, .btn-modal-primary:visible").last().click(); // Frappe also keeps a hidden copy of Confirm
}

// The row OF this item: its own item cell. (Matching any text would also find the row of a manufactured parent, whose collapsed
// BOM details name its components.)
const row = (page, item) =>
	page.locator(`${DIALOG} .ppc-mrp-table tbody tr`).filter({ has: page.locator("td.item > b", { hasText: new RegExp(`^${item}$`) }) }).first();

(async () => {
	const run = L.makeRunner();
	const { step, openPages } = run;
	const browser = await L.launch();
	const problems = [];
	let page, ctx, setup, orders, runName;
	const db = (so) => fx("mrp_state", { sales_order: so });
	try {
		await step("fixtures: items with a BOM, 4 draft Sales Orders, users", async () => {
			fx("mrp_cleanup");
			setup = fx("mrp_setup", { count: 4 });
			orders = setup.sales_orders;
			assert.equal(orders.length, 4);
			ctx = await browser.newContext({ viewport: { width: 1366, height: 900 } });
			page = await ctx.newPage();
			openPages.push(page);
			L.watch(page, "manager", problems);
			await login(page, MANAGER);
			return orders.join(", ");
		}, { critical: true });

		await step("submit: the dialog says Calculating… and never claims completion; nothing exists yet", async () => {
			await submitSalesOrder(page, orders[0]);
			await page.waitForSelector(DIALOG, { timeout: 30000 });
			assert.equal(await state(page), "calculating");
			const title = (await page.locator(".modal.show .modal-title").last().innerText()).trim();
			assert.equal(title, "Material Requirement Planning");
			assert.ok(!/Completed/.test(await page.locator(".modal.show").last().innerText()), "a queued job must not look finished");
			assert.equal((await db(orders[0])).run, null, "the job is only queued: there is no run yet");
		});

		await step("no worker: after about 20 s it says Waiting for the background worker and offers Calculate Now", async () => {
			await waitState(page, "waiting", 40000);
			const text = await page.locator(DIALOG).innerText();
			assert.match(text, /Waiting for the background worker/);
			assert.ok(await page.locator(`${DIALOG} [data-act=now]`).isVisible());
			assert.ok(!/Completed/.test(text));
			await shot(page, "mrp_1_waiting_desktop");
		});

		await step("Calculate Now: one run, calculated once, nothing created yet", async () => {
			await page.click(`${DIALOG} [data-act=now]`);
			await waitState(page, "calculated", 90000);
			const d = db(orders[0]);
			runName = d.run;
			assert.deepEqual([d.status, d.approval_status, d.documents.length], ["Calculated", "Awaiting Review", 0]);
			assert.equal(d.audit.filter((a) => a === "MRP Auto Calculate").length, 1);
			assert.equal((await page.locator(".modal.show .modal-title").last().innerText()).trim(), "Material Requirement Planning Completed");
		});

		await step("the review: KPI cards, buy and manufacture lines, details, existing supply, warehouses", async () => {
			assert.equal(await page.locator(`${DIALOG} .ppc-mrp-kpi`).count(), 4);
			const kpis = (await page.locator(`${DIALOG} .ppc-mrp-kpis`).innerText()).replace(/\s+/g, " ");
			assert.match(kpis, /Total demand/); assert.match(kpis, /Available stock/); assert.match(kpis, /Existing supply/); assert.match(kpis, /Shortage/);
			assert.match(await row(page, setup.fg).innerText(), /Manufacture/);
			assert.match(await row(page, setup.rm).innerText(), /Buy/);
			assert.match(await row(page, setup.buy).innerText(), /Buy/);
			await row(page, setup.fg).locator("summary").click();
			const detail = await row(page, setup.fg).locator(".ppc-mrp-detail").innerText();
			assert.match(detail, /Stock by warehouse/); assert.match(detail, /Existing Work Orders/); assert.match(detail, new RegExp(setup.rm)); // BOM component
			assert.ok(await page.locator(`${DIALOG} .ppc-mrp-sources`).innerText().then((t) => t.includes(orders[0])));
			await shot(page, "mrp_2_review_desktop");
		});

		await step("revise a quantity: the original stays visible and is audited", async () => {
			await row(page, setup.buy).locator("input.qty").fill("8");
			await row(page, setup.buy).locator("input.qty").blur();
			await page.locator(`${DIALOG} [data-act=revise]`).click();
			await page.waitForFunction(() => !document.querySelector(".ppc-mrp-dialog [data-act=revise]:not([disabled])"));
			assert.match(await row(page, setup.buy).locator(".orig").innerText(), /6/);
			const line = db(orders[0]).lines.find((l) => l.item_code === setup.buy);
			assert.deepEqual([line.approved_qty, line.original_qty], [8, 6]);
			assert.ok(db(orders[0]).audit.includes("MRP Line Revised"));
		});

		await step("skip an item: a reason is required, it is shown and kept", async () => {
			await row(page, setup.rm).locator("input.pick").uncheck();
			const m = page.locator(".modal.show").last();
			await m.waitFor();
			await page.waitForFunction(L.modalFullyShown);
			await m.locator(".modal-footer .btn-primary:visible, .btn-modal-primary:visible").last().click(); // no reason: refused by the form
			assert.ok(await page.locator(".modal.show").count(), "the prompt stays open without a reason");
			// Frappe answers with its "Missing Values Required" message on top of the prompt: read it, then dismiss only that message
			const refusal = page.locator(".modal.show", { hasText: "Missing Values Required" });
			await refusal.waitFor();
			assert.match(await refusal.innerText(), /skipped\?/);
			await refusal.locator(".btn-modal-close:visible, .modal-header .close:visible").first().click();
			await refusal.waitFor({ state: "hidden" });
			await promptText(page, "bought elsewhere");
			await page.waitForFunction((it) => [...document.querySelectorAll(".ppc-mrp-table tbody tr")].some((r) => r.innerText.includes(it) && /bought elsewhere/.test(r.innerText)), setup.rm);
			const line = db(orders[0]).lines.find((l) => l.item_code === setup.rm);
			assert.deepEqual([line.create_document, line.skip_reason], [0, "bought elsewhere"]);
			assert.ok(db(orders[0]).audit.includes("MRP Line Skipped"));
		});

		await step("recalculate discards revisions and skips; still nothing is created", async () => {
			await page.locator(`${DIALOG} [data-act=recalc]`).click();
			await confirmYes(page);
			await page.waitForFunction(([it]) => { const r = [...document.querySelectorAll(".ppc-mrp-table tbody tr")].find((x) => x.innerText.includes(it)); return r && !/bought elsewhere/.test(r.innerText); }, [setup.rm]);
			const d = db(orders[0]);
			assert.equal(d.documents.length, 0);
			assert.deepEqual([d.lines.find((l) => l.item_code === setup.buy).approved_qty, d.lines.find((l) => l.item_code === setup.rm).skip_reason], [6, null]);
			assert.ok(d.audit.includes("MRP Recalculated"));
		});

		await step("approve (double click): exactly one Work Order and one Material Request, as drafts, linked to the run", async () => {
			await page.locator(`${DIALOG} [data-act=approve]`).dblclick();
			await page.waitForFunction(() => document.querySelectorAll(".modal.show").length >= 2); // the confirmation fades in
			await page.waitForTimeout(600); // a second one, if the double click opened it, would have appeared by now
			assert.equal(await page.locator(".modal.show").count(), 2, "one confirmation on top of the review, not two");
			await confirmYes(page);
			await waitState(page, "approved", 90000);
			const d = db(orders[0]);
			assert.deepEqual([d.status, d.approval_status, d.approved_by, d.has_key], ["Documents Created", "Approved", MANAGER, true]);
			const wo = d.documents.filter((x) => x.doctype === "Work Order"), mr = d.documents.filter((x) => x.doctype === "Material Request");
			assert.deepEqual([wo.length, mr.length], [1, 1]);
			assert.ok(d.documents.every((x) => x.docstatus === 0), "drafts only");
			assert.ok(d.audit.includes("MRP Approved"));
			assert.match(await page.locator(DIALOG).innerText(), /Draft documents created/);
			assert.equal(await page.locator(`${DIALOG} [data-act=approve]`).isDisabled(), true, "no second approval");
			await shot(page, "mrp_3_approved_desktop");
		});

		await step("a worker answers in time: Calculating… turns straight into the review, no Calculate Now", async () => {
			await page.keyboard.press("Escape"); await page.keyboard.press("Escape");
			await submitSalesOrder(page, orders[1]);
			await page.waitForSelector(DIALOG, { timeout: 30000 });
			assert.equal(await state(page), "calculating");
			fx("mrp_run_job", { doctype: "Sales Order", name: orders[1] });
			await waitState(page, "calculated", 30000);
			assert.equal(await page.locator(`${DIALOG} [data-act=now]`).count(), 0);
			assert.equal((await db(orders[1])).documents.length, 0);
		});

		await step("cancel needs a reason, creates nothing, and approval is impossible afterwards; recalculate reopens it", async () => {
			await page.locator(`${DIALOG} [data-act=cancel]`).click();
			await promptText(page, "entered twice by mistake");
			await waitState(page, "cancelled", 30000);
			let d = db(orders[1]);
			assert.deepEqual([d.approval_status, d.cancel_reason, d.documents.length], ["Cancelled", "entered twice by mistake", 0]);
			assert.equal(await page.locator(`${DIALOG} [data-act=approve]`).isDisabled(), true);
			assert.ok(d.audit.includes("MRP Review Cancelled"));
			await page.locator(`${DIALOG} [data-act=recalc]`).click();
			await confirmYes(page);
			await waitState(page, "calculated", 60000);
			assert.equal(db(orders[1]).approval_status, "Awaiting Review");
		});

		await step("the existing MRP Review page shows the same review", async () => {
			await page.keyboard.press("Escape"); await page.keyboard.press("Escape");
			await page.goto(`${BASE}/app/ppc-mrp-review/${runName}`);
			await page.waitForSelector(".ppc-mrp[data-run]", { timeout: 60000 });
			assert.equal(await page.locator(".ppc-mrp").first().getAttribute("data-run"), runName);
			assert.equal(await page.locator(".ppc-mrp").first().getAttribute("data-approval"), "Approved");
			assert.match(await page.locator(".ppc-mrp-banner.ok").first().innerText(), /Draft documents created/);
			assert.ok(await page.locator(".ppc-mrp-kpi").count() >= 4);
			await shot(page, "mrp_4_review_page_desktop");
		});

		await step("a viewer can look at the review but every action is disabled", async () => {
			const vctx = await browser.newContext({ viewport: { width: 1366, height: 900 } });
			const vp = await vctx.newPage();
			openPages.push(vp);
			L.watch(vp, "viewer", problems);
			await login(vp, VIEWER);
			const open = db(orders[1]).run;
			await vp.goto(`${BASE}/app/ppc-mrp-review/${open}`);
			await vp.waitForSelector(".ppc-mrp[data-run]", { timeout: 60000 });
			for (const act of ["approve", "cancel", "recalc", "revise"]) assert.equal(await vp.locator(`[data-act=${act}]`).isDisabled(), true, act);
			assert.equal(await vp.locator(".ppc-mrp-table input.pick:not([disabled])").count(), 0);
			await vctx.close();
		});

		await step("phone layout: no sideways scrolling, lines become cards, the actions stay reachable", async () => {
			const mctx = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
			const mp = await mctx.newPage();
			openPages.push(mp);
			L.watch(mp, "mobile", problems);
			await login(mp, MANAGER);
			await submitSalesOrder(mp, orders[2]);
			await mp.waitForSelector(DIALOG, { timeout: 30000 });
			fx("mrp_run_job", { doctype: "Sales Order", name: orders[2] });
			await waitState(mp, "calculated", 60000);
			await mp.waitForSelector(`${DIALOG} .ppc-mrp-table tbody tr`, { timeout: 30000 }); // the state changes before the lines are drawn
			const m = await mp.evaluate(() => {
				const d = document.querySelector(".ppc-mrp-dialog");
				const tr = d.querySelector(".ppc-mrp-table tbody tr");
				return { overflow: d.scrollWidth - d.clientWidth, doc: document.documentElement.scrollWidth - window.innerWidth, trDisplay: getComputedStyle(tr).display, thead: getComputedStyle(d.querySelector(".ppc-mrp-table thead")).display, kpis: d.querySelectorAll(".ppc-mrp-kpi").length };
			});
			assert.ok(m.overflow <= 2 && m.doc <= 2, "no horizontal overflow " + JSON.stringify(m));
			assert.deepEqual([m.trDisplay, m.thead, m.kpis], ["block", "none", 4]);
			assert.ok(await mp.locator(`${DIALOG} [data-act=approve]`).isVisible());
			await shot(mp, "mrp_5_review_mobile");
			await mctx.close();
		});

		await step("no console errors or server errors in any session", async () => { assert.deepEqual(problems, []); });
	} finally {
		await step("cleanup: every run, draft, order and master the test made is gone", async () => {
			const out = fx("mrp_cleanup");
			assert.ok(out.clean, JSON.stringify(out));
			return "clean";
		});
		await browser.close();
	}
	const failed = run.count("FAIL");
	console.log(`\n${run.count("PASS")} passed, ${failed} failed, ${run.count("SKIPPED")} skipped. Screenshots: ${SHOTS}`);
	process.exit(failed ? 1 : 0);
})();
