# Material Planning (MRP)

An MRP run turns open demand into purchase and production recommendations and, on request, into **draft**
Material Requests and Work Orders. Nothing is ever submitted by the app.

Where: PPC Control Center workspace -> *Material Planning (MRP)* (dashboard), *MRP Runs*, and three reports
(*MRP Item Wise*, *MRP Warehouse Wise*, *MRP Date Wise*).

## Using it

1. **New MRP Run**: company, *Demand Until* (and optionally *Demand From*), the warehouses to plan, a default
   warehouse. Save.
2. **Calculate**. The run now shows its summary, warnings and one line per item / warehouse / required date.
3. Untick *Create Draft* on lines you do not want, then **Create Draft Documents**. Stock is read again first.
4. Review and submit the drafts yourself (Material Request, Work Orders).

A run whose documents were created is closed; start a new run for the next planning round.

## How the numbers are built

| Step | Rule |
|---|---|
| Demand | Open Sales Order lines: (ordered - delivered) x conversion factor. Plan rows that belong to a Sales Order line are **not** added again; Production Plan rows without a Sales Order add (planned - produced). Closed / on-hold orders, drop-ship lines and Product Bundles are skipped (the last with a warning). |
| BOM explosion | Multilevel. A BOM Item with its own BOM is a sub-assembly (manufactured); every other component is bought. Only the **shortage** of a manufactured item is exploded. A line's BOM that is no longer active falls back to the item's default BOM. A BOM loop stops the run with the items named. |
| Usable stock | Bin actual qty, minus quantity reserved for Work Orders and subcontracting, minus Stock Reservation Entries that do not belong to a Sales Order line of this run. |
| Pending supply | Open Purchase Orders, Material Requests and Work Orders (the Bin's ordered / indented / planned qty) **plus every draft purchase Material Request and draft Work Order**, which ERPNext does not count yet. |
| Netting | Per item and warehouse, earliest requirement first: stock, then pending supply, remainder = shortage. A group warehouse counts its child warehouses. |
| Lead time | Order / start date = required date - lead time. Lead time = the larger of the Item's lead time and the BOM operation time (8 working hours per day by default, per run). Components are needed on the date their parent's production must start. A date in the past marks the line **Late**. |
| Warehouses | Demand is planned only for the selected warehouses (none selected = all). A component without a source warehouse on the BOM uses the run's default warehouse. |

## Protection against double counting

* One demand row = one key (`Sales Order Item:<row>` / `Production Plan Item:<row>`); a duplicate key is
  rejected when the run is saved.
* Drafts created by a run count as supply in every later run, so the same shortage is not recommended twice.
  If a run's demand was already turned into documents by another run, the new run says so in its warnings.
* Before creating anything the whole calculation is repeated. A line is created with the smaller of its stored and
  its fresh shortage and skipped if it is no longer short. Creation is one transaction (all or nothing), takes a
  row lock on the run, and a run can create documents once.
* Material Request lines carry today's date as *required by* when the order date has already passed (ERPNext
  refuses past dates); the run still shows the line as Late.
* Work Orders are created single-level (`use_multi_level_bom = 0`) because each sub-assembly has its own line and
  Work Order.

## Not covered (by design)

* Minimum order quantities, reorder levels, batch sizes and supplier selection - the Material Request is for you to
  complete. Subcontracting and non-stock items are not planned.
* Sales Orders outside the run's date range are not demand of that run, although their stock reservations in the Bin
  are also not subtracted: set *Demand Until* far enough ahead.

## Files

`services/mrp_engine.py` (pure netting rules, tested without a site), `services/mrp.py` (ERPNext data, creation),
`doctype/ppc_mrp_run` (+ warehouse / demand / line tables), `report/ppc_mrp_*`, `page/ppc_mrp_dashboard`,
tests `tests/test_mrp_engine.py` and the read-only site check `tests/mrp_smoke.py`.
