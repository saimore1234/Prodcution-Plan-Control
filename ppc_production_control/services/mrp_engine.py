"""MRP calculation engine - pure Python, no database access.

Everything the calculation needs from ERPNext (items, BOMs, stock positions) comes in through a
`ctx` object, so the netting rules can be tested without a site. `services/mrp.py` supplies the
real context; the tests supply a dictionary-backed one.

Rules (all quantities are in the item's stock UOM):

* Demand is netted per (item, warehouse). Supply is handed out to the earliest requirement first:
  usable stock, then pending supply (open Purchase Orders, Material Requests, Work Orders and any
  draft Material Request / Work Order).
* Only the *net* shortage of a manufactured item is exploded into its BOM. Components of stock that
  already exists are therefore never planned a second time (no double counting).
* Items are processed in low-level order (an item only after every parent that needs it), so a
  component used by several parents is netted once, with all of its demand.
* A BOM Item that has its own BOM is a sub-assembly (manufactured); anything else is bought.
* Planned order date = requirement date - lead time. A manufactured item's components are needed on
  the date its production has to start.
"""

import math
from collections import defaultdict, deque
from datetime import date, datetime, timedelta

ACTION_PURCHASE = "Purchase"
ACTION_MANUFACTURE = "Manufacture"
ACTION_REVIEW = "Review"
ACTION_NONE = "None"

EPSILON = 1e-9
QTY_PRECISION = 6
MAX_SOURCES = 5


class MRPError(Exception):
	"""A problem in the data that makes the plan impossible to calculate (e.g. a BOM loop)."""


def _as_date(value) -> date:
	if isinstance(value, datetime):
		return value.date()
	if isinstance(value, date):
		return value
	return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def _q(value: float) -> float:
	value = round(float(value or 0), QTY_PRECISION)
	return 0.0 if abs(value) < EPSILON else value


def _bom_graph(top: list[tuple[str, str | None]], ctx) -> dict[str, set[str]]:
	"""item -> items it needs, over every BOM reachable from the demanded items."""
	edges: dict[str, set[str]] = defaultdict(set)
	stack = list(top)
	seen: set[tuple[str, str | None]] = set()
	while stack:
		item, bom_no = stack.pop()
		edges.setdefault(item, set())
		if not bom_no or (item, bom_no) in seen:
			continue
		seen.add((item, bom_no))
		bom = ctx.bom(bom_no)
		if not bom:
			continue
		for child in bom["children"]:
			edges[item].add(child["item_code"])
			edges.setdefault(child["item_code"], set())
			if child.get("bom_no"):
				stack.append((child["item_code"], child["bom_no"]))
	return edges


def _low_level_order(edges: dict[str, set[str]]) -> list[str]:
	"""Parents before children (Kahn). A BOM loop leaves items unordered and is an error."""
	indegree = {n: 0 for n in edges}
	for children in edges.values():
		for c in children:
			indegree[c] += 1
	queue = deque(sorted(n for n, d in indegree.items() if d == 0))
	order = []
	while queue:
		n = queue.popleft()
		order.append(n)
		for c in sorted(edges[n]):
			indegree[c] -= 1
			if indegree[c] == 0:
				queue.append(c)
	if len(order) != len(edges):
		looped = sorted(set(edges) - set(order))
		raise MRPError("BOM loop: these items need each other: " + ", ".join(looped[:10]))
	return order


def _operation_days(bom: dict, qty: float, hours_per_day: float) -> int:
	minutes = bom.get("fixed_minutes", 0) + bom.get("scaled_minutes", 0) * (qty / (bom.get("quantity") or 1))
	if minutes <= 0:
		return 0
	return int(math.ceil(minutes / 60.0 / (hours_per_day or 8)))


def run_mrp(demands: list[dict], ctx) -> dict:
	"""Net the demands and explode what has to be manufactured.

	demands: [{key, item_code, qty, required_date, warehouse, bom_no, source}] (qty in stock UOM)
	ctx: today (date), hours_per_day, default_warehouse, warehouses (set | None = any),
	     item(code) -> {item_name, stock_uom, is_stock_item, is_purchase_item, lead_time_days} | None,
	     default_bom(code) -> str | None,
	     bom(name) -> {name, item, quantity, fixed_minutes, scaled_minutes, children: [{item_code,
	     stock_qty, source_warehouse, bom_no}]} | None,
	     position(item, warehouse) -> {stock, incoming, **breakdown shown on the line}
	Returns {"lines": [...], "warnings": [...]}.
	"""
	warnings: list[str] = []
	today = _as_date(ctx.today)

	# (item, warehouse) -> chunks of requirement; every chunk carries its date, BOM and where it came from
	needs: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
	level: dict[str, int] = defaultdict(int)
	top = []
	for d in demands:
		qty = _q(d["qty"])
		if qty <= 0:
			continue
		item = d["item_code"]
		bom_no = d.get("bom_no")
		if not bom_no or not ctx.bom(bom_no):  # a line's BOM that was since deactivated falls back to the item's current default
			bom_no = ctx.default_bom(item)
		warehouse = d.get("warehouse") or ctx.default_warehouse
		needs[item][warehouse].append(
			{"date": _as_date(d["required_date"]), "qty": qty, "bom_no": bom_no, "sources": [d.get("source") or d.get("key")]}
		)
		top.append((item, bom_no))

	order = _low_level_order(_bom_graph(top, ctx))
	lines: list[dict] = []
	skipped_non_stock: set[str] = set()

	for item in order:
		by_wh = needs.get(item)
		if not by_wh:
			continue
		info = ctx.item(item)
		for warehouse in sorted(by_wh):
			chunks = _merge(by_wh[warehouse])
			position = ctx.position(item, warehouse)
			stock_left = max(0.0, position.get("stock", 0))
			incoming_left = max(0.0, position.get("incoming", 0))
			for chunk in chunks:
				qty = chunk["qty"]
				from_stock = min(stock_left, qty)
				stock_left -= from_stock
				rest = qty - from_stock
				from_supply = min(incoming_left, rest)
				incoming_left -= from_supply
				net = _q(rest - from_supply)

				action, note = ACTION_NONE, ""
				bom = ctx.bom(chunk["bom_no"]) if chunk["bom_no"] else None
				if net > 0:
					if info is None:
						action, note = ACTION_REVIEW, "Item not found or disabled."
					elif chunk["bom_no"] and bom:
						action = ACTION_MANUFACTURE
					elif info.get("is_purchase_item"):
						action = ACTION_PURCHASE
					else:
						action, note = ACTION_REVIEW, "No active BOM and not a purchase item."
				lead = int((info or {}).get("lead_time_days") or 0)
				if action == ACTION_MANUFACTURE:
					lead = max(lead, _operation_days(bom, net, ctx.hours_per_day))
				planned = chunk["date"] - timedelta(days=lead)
				line = {
					"item_code": item,
					"item_name": (info or {}).get("item_name") or item,
					"uom": (info or {}).get("stock_uom"),
					"warehouse": warehouse,
					"required_date": chunk["date"],
					"gross_qty": _q(qty),
					"from_stock": _q(from_stock),
					"from_supply": _q(from_supply),
					"net_qty": net,
					"action": action,
					"bom_no": chunk["bom_no"] if action == ACTION_MANUFACTURE else None,
					"lead_time_days": lead,
					"planned_order_date": planned if net > 0 else None,
					"is_late": bool(net > 0 and planned < today),
					"level": level[item],
					"sources": ", ".join(dict.fromkeys(chunk["sources"]))[:140],
					"note": note,
				}
				for k in ("actual_qty", "reserved_qty", "incoming_qty"):
					line[k] = _q(position.get(k, 0))
				lines.append(line)

				if action == ACTION_MANUFACTURE:
					for child in bom["children"]:
						cinfo = ctx.item(child["item_code"])
						if cinfo is not None and not cinfo.get("is_stock_item"):
							skipped_non_stock.add(child["item_code"])
							continue
						cwh = child.get("source_warehouse")
						if not cwh or (ctx.warehouses is not None and cwh not in ctx.warehouses):
							cwh = ctx.default_warehouse
						cqty = net / (bom.get("quantity") or 1) * child["stock_qty"]
						if cqty <= 0:
							continue
						needs[child["item_code"]][cwh].append(
							{"date": planned, "qty": cqty, "bom_no": child.get("bom_no"), "sources": [item]}
						)
						level[child["item_code"]] = max(level[child["item_code"]], level[item] + 1)

	if skipped_non_stock:
		warnings.append("Non-stock components are not planned: " + ", ".join(sorted(skipped_non_stock)[:15]))
	lines.sort(key=lambda r: (r["level"], r["item_code"], r["warehouse"] or "", r["required_date"]))
	return {"lines": lines, "warnings": warnings}


def _merge(chunks: list[dict]) -> list[dict]:
	"""One chunk per (date, BOM): same-day requirements of one item are planned together."""
	merged: dict[tuple, dict] = {}
	for c in chunks:
		key = (c["date"], c["bom_no"])
		if key in merged:
			merged[key]["qty"] += c["qty"]
			merged[key]["sources"].extend(c["sources"][:MAX_SOURCES])
		else:
			merged[key] = {"date": c["date"], "qty": c["qty"], "bom_no": c["bom_no"], "sources": list(c["sources"][:MAX_SOURCES])}
	return sorted(merged.values(), key=lambda c: (c["date"], c["bom_no"] or ""))


def summarize(demands: list[dict], lines: list[dict]) -> dict:
	"""Dashboard totals for one calculation."""
	short = [r for r in lines if r["net_qty"] > 0]
	buy = [r for r in short if r["action"] == ACTION_PURCHASE]
	make = [r for r in short if r["action"] == ACTION_MANUFACTURE]
	review = [r for r in short if r["action"] == ACTION_REVIEW]
	return {
		"demand_lines": len(demands),
		"demand_qty": _q(sum(d["qty"] for d in demands)),
		"shortage_items": len({(r["item_code"], r["warehouse"]) for r in short}),
		"shortage_qty": _q(sum(r["net_qty"] for r in short)),
		"purchase_lines": len(buy),
		"purchase_qty": _q(sum(r["net_qty"] for r in buy)),
		"production_lines": len(make),
		"production_qty": _q(sum(r["net_qty"] for r in make)),
		"review_lines": len(review),
		"late_lines": len([r for r in short if r["is_late"]]),
	}
