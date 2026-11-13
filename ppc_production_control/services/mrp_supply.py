"""Document-level pending supply for the MRP netting: whose Work Order / Material Request / Purchase Order is it?

ERPNext's Bin only holds totals per item and warehouse (planned, indented and ordered quantity), so a Work Order that was
created for one Sales Order looked like free supply to the next one. This module lists the open supply documents behind those
totals and decides, document by document, who they are for:

* **Explicit link** - Work Order.sales_order, Material Request Item.sales_order, Purchase Order Item.sales_order. A Purchase Order
  row without its own link inherits it from the Material Request row it was made from. The whole open quantity of the row belongs
  to that Sales Order (a Material Request with two rows for two orders therefore gives each its own quantity).
* **PPC-generated** - documents made by an MRP run carry `ppc_mrp_run`. Their owners are the *structured* demand rows of that run
  (PPC MRP Demand), never the free-text source field. One owner: the document is that owner's. Several owners: there is no
  quantity split to rely on.
* **No link** - free supply, usable by any order.

For the order(s) of the run being calculated ("own"):

| the document is for | counts as supply of this run |
|---|---|
| this run's own demand source(s) only | yes |
| another *open* Sales Order / Production Plan (and not this run's) | no - it is reserved for that order |
| several orders including this run's and another open one | no, and the run says so (ambiguous: nothing is invented) |
| nobody, or only orders that are cancelled / closed / completed / on hold / draft | yes (free) |

Cancelled supply documents never count; neither does a document that is not open any more (Completed / Stopped / Closed Work
Order, Stopped Material Request, fully received Purchase Order). A document row is looked at once.
"""

from collections import defaultdict

import frappe
from frappe.utils import flt

from ppc_production_control.services.constants import CLOSED_PLAN_STATUSES, CLOSED_SO_STATUSES, WO_LOCKED_STATUSES

WO, MR, PO = "Work Order", "Material Request", "Purchase Order"


def pending_supply(item: str, warehouses) -> list[dict]:
	"""Open supply rows of one item in the (leaf) warehouses, one dict per document row: {doctype, name, row, draft, qty, sales_order,
	run, mr_item}. Draft Work Orders and draft purchase Material Requests are included (ERPNext's Bin does not count them yet);
	submitted ones only while they still have an open quantity. Cancelled documents (docstatus 2) are never selected."""
	wh = tuple(warehouses) or ("",)
	out: list[dict] = []

	for r in frappe.db.sql(
		"""select name, docstatus, qty, produced_qty, sales_order, ppc_mrp_run from `tabWork Order`
		where production_item = %(item)s and fg_warehouse in %(wh)s
			and (docstatus = 0 or (docstatus = 1 and status not in %(done)s and qty > produced_qty))""",
		{"item": item, "wh": wh, "done": WO_LOCKED_STATUSES},
		as_dict=True,
	):
		qty = flt(r.qty) if r.docstatus == 0 else flt(r.qty) - flt(r.produced_qty)
		out.append({"doctype": WO, "name": r.name, "row": r.name, "draft": r.docstatus == 0, "qty": qty, "sales_order": r.sales_order, "run": r.ppc_mrp_run, "mr_item": None})

	for r in frappe.db.sql(
		"""select mr.name, mr.docstatus, mr.ppc_mrp_run, mri.name as row_name, mri.sales_order, mri.stock_qty, mri.ordered_qty
		from `tabMaterial Request Item` mri join `tabMaterial Request` mr on mr.name = mri.parent
		where mri.item_code = %(item)s and mri.warehouse in %(wh)s and mr.material_request_type = 'Purchase'
			and (mr.docstatus = 0 or (mr.docstatus = 1 and mr.status not in ('Stopped', 'Cancelled')))""",
		{"item": item, "wh": wh},
		as_dict=True,
	):
		qty = flt(r.stock_qty) if r.docstatus == 0 else max(0.0, flt(r.stock_qty) - flt(r.ordered_qty))
		out.append({"doctype": MR, "name": r.name, "row": r.row_name, "draft": r.docstatus == 0, "qty": qty, "sales_order": r.sales_order, "run": r.ppc_mrp_run, "mr_item": None})

	for r in frappe.db.sql(
		"""select po.name, poi.name as row_name, poi.sales_order, poi.material_request_item, poi.qty, poi.received_qty, poi.conversion_factor
		from `tabPurchase Order Item` poi join `tabPurchase Order` po on po.name = poi.parent
		where poi.item_code = %(item)s and poi.warehouse in %(wh)s and po.docstatus = 1
			and po.status not in ('Closed', 'Delivered', 'Cancelled') and ifnull(poi.delivered_by_supplier, 0) = 0 and poi.qty > poi.received_qty""",
		{"item": item, "wh": wh},
		as_dict=True,
	):
		qty = (flt(r.qty) - flt(r.received_qty)) * (flt(r.conversion_factor) or 1)
		out.append({"doctype": PO, "name": r.name, "row": r.row_name, "draft": False, "qty": qty, "sales_order": r.sales_order, "run": None, "mr_item": r.material_request_item})

	seen, unique = set(), []
	for r in out:  # a document row is counted once, whatever the queries above returned
		key = (r["doctype"], r["name"], r["row"])
		if key not in seen and r["qty"] > 0:
			seen.add(key)
			unique.append(r)
	return unique


def resolve_owners(rows: list[dict]) -> None:
	"""Set `owners` (a set of (doctype, name) of Sales Orders / Production Plans) on every row. An explicit Sales Order link wins; a PPC run's
	structured demand rows come next; a Purchase Order row without a link inherits those of the Material Request row it came from."""
	inherited: dict[str, dict] = {}
	po_rows = {r["mr_item"] for r in rows if r["doctype"] == PO and r["mr_item"] and not r["sales_order"]}
	if po_rows:
		for x in frappe.db.sql(
			"""select mri.name, mri.sales_order, mr.ppc_mrp_run from `tabMaterial Request Item` mri
			join `tabMaterial Request` mr on mr.name = mri.parent where mri.name in %s""",
			(tuple(po_rows),),
			as_dict=True,
		):
			inherited[x.name] = x

	runs = {r["run"] for r in rows if r["run"] and not r["sales_order"]} | {x.ppc_mrp_run for x in inherited.values() if x.ppc_mrp_run and not x.sales_order}
	run_owners: dict[str, set] = defaultdict(set)
	if runs:
		for x in frappe.db.sql(
			"select distinct parent, source_doctype, source_name from `tabPPC MRP Demand` where parent in %s", (tuple(runs),), as_dict=True
		):
			run_owners[x.parent].add((x.source_doctype, x.source_name))

	for r in rows:
		so, run = r["sales_order"], r["run"]
		if not so and r["mr_item"] in inherited:
			so, run = inherited[r["mr_item"]].sales_order, inherited[r["mr_item"]].ppc_mrp_run
		if so:
			r["owners"] = {("Sales Order", so)}
		elif run:
			r["owners"] = set(run_owners.get(run, ()))
		else:
			r["owners"] = set()


def open_owners(owners: set, cache: dict) -> set:
	"""The owners that are still open demand: a submitted Sales Order / Production Plan that is not closed, on hold, completed or
	cancelled. Anything else (a draft, a cancelled order ...) reserves nothing. `cache` lives for one calculation."""
	need = [o for o in owners if o not in cache]
	for doctype, closed in (("Sales Order", CLOSED_SO_STATUSES), ("Production Plan", CLOSED_PLAN_STATUSES)):
		names = [n for d, n in need if d == doctype]
		if names:
			live = set(frappe.db.sql_list(f"select name from `tab{doctype}` where name in %s and docstatus = 1 and status not in %s", (tuple(names), closed)))
			for n in names:
				cache[(doctype, n)] = n in live
	for o in need:
		cache.setdefault(o, False)
	return {o for o in owners if cache.get(o)}


def allocation(item: str, warehouses, own_sources: set | None, *, include_incoming: bool, include_wo: bool, cache: dict | None = None) -> dict:
	"""How much of the pending supply counted for this item is not free for the run whose demand sources are `own_sources`.

	Returns {reserved, ambiguous, reserved_docs, ambiguous_docs}: `reserved` is supply of other open orders, `ambiguous` supply that
	serves this run's order and another open one without a quantity split. Both are subtracted from the supply the run may use. Only
	what is actually counted as supply is looked at: drafts always, submitted Work Orders when `include_wo`, submitted Material Requests
	and Purchase Orders when `include_incoming` - so nothing is subtracted that was never added."""
	out = {"reserved": 0.0, "ambiguous": 0.0, "reserved_docs": [], "ambiguous_docs": []}
	if own_sources is None:
		return out
	for r in describe(item, warehouses, own_sources, include_incoming=include_incoming, include_wo=include_wo, cache=cache):
		if not r["counted"]:
			continue
		if r["relation"] == AMBIGUOUS:
			out["ambiguous"] += r["qty"]
			out["ambiguous_docs"].append((r["doctype"], r["name"], r["owners"]))
		elif r["relation"] == OTHERS:
			out["reserved"] += r["qty"]
			out["reserved_docs"].append((r["doctype"], r["name"], r["qty"]))
	return out


FREE, OWN, OTHERS, AMBIGUOUS = "free", "own", "others", "ambiguous"


def relation(owners: set, own_sources: set, live: set) -> str:
	"""How a supply document relates to the run whose demand sources are `own_sources` (see the table at the top of this module)."""
	if not owners:
		return FREE
	mine, others = owners & own_sources, owners & live
	if mine and others:
		return AMBIGUOUS
	if others:
		return OTHERS
	return OWN if mine else FREE


def describe(item: str, warehouses, own_sources: set, *, include_incoming: bool, include_wo: bool, cache: dict | None = None) -> list[dict]:
	"""Every open supply document row of the item with whose it is (`relation`: own / others / ambiguous / free), its quantity, whether
	the settings count it as supply at all (`counted`), and its owners. The netting (`allocation`) and the review screen both read this,
	so what a reviewer sees is exactly what was subtracted."""
	cache = cache if cache is not None else {}
	rows = pending_supply(item, warehouses)
	resolve_owners(rows)
	foreign = set().union(*(r["owners"] for r in rows)) - own_sources if rows else set()
	live = open_owners(foreign, cache)
	for r in rows:
		r["counted"] = bool(r["draft"] or (include_wo if r["doctype"] == WO else include_incoming))
		r["relation"] = relation(r["owners"], own_sources, live)
		r["owners"] = sorted(n for _d, n in r["owners"])
	return rows
