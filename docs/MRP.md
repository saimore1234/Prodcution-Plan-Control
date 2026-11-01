# Material Planning (MRP)

An MRP run turns open demand into purchase and production recommendations. A person reviews them
(**MRP Review**), and only after an explicit confirmation are **draft** Material Requests and Work Orders
created. The app never submits anything.

Where: workspace **MRP Planning** (also a section in PPC Control Center): MRP Review, New MRP Run, MRP Runs,
MRP Dashboard and three reports (Item Wise, Warehouse Wise, Date Wise).

## Two triggers, one engine

| | Manual | Automatic |
|---|---|---|
| Started by | *PPC MRP Run* form -> **Calculate** (workspace *New MRP Run*) | Submitting a **Sales Order** |
| Demand | All open Sales Orders / Production Plans in a date range, **or** the Sales Orders / Production Plans listed in *Plan Only These Documents* | The submitted Sales Order |
| Code path | `mrp.calculate` -> `mrp.calculate_run` | queued job `mrp_auto.run_for_sales_order` -> `mrp.calculate_run` |
| Result | Run with recommendations, status **Calculated** | Same, `Trigger = Automatic`, `Source Sales Order` filled |

Both end in the same `calculate_run` and the same netting engine (`services/mrp_engine.py`), so the same demand gives
the same lines (there is a test for exactly that). The automatic run **only prepares recommendations**; nothing is created
from it without a person.

Switch the automatic calculation off, or choose its default warehouse, in **PPC Settings -> Material Planning (MRP)**.
Automatic runs use that warehouse, else the Sales Order's own, else Stock Settings' default.

### Automatic runs never block the Sales Order

The submit hook only queues a background job and swallows its own errors (logged to the Error Log). The job creates
the run first and then calculates it; if the calculation fails the run becomes **Failed** with the reason, an audit entry
and an Error Log row, and the Sales Order is untouched. Retry from the run (**Retry Calculation**), the MRP Review
screen, or the dashboard (it lists failed runs). An open automatic run for an order prevents a second one.

## Procurement Type (Item)

New Item field **Procurement Type**: *Purchase* or *Manufacture*. It is checked before every recommendation:

| Item | Shortage becomes |
|---|---|
| Purchase | a Material Request line (even if the item happens to have a BOM) |
| Manufacture | a Work Order from its active BOM; the BOM is exploded and each component is judged by *its own* Procurement Type |
| Manufacture, but no active BOM | **Review** (needs a person) |
| Purchase, but the item is not "Allow Purchase" | **Review** |
| *blank* | inferred: active BOM -> Manufacture, otherwise "Allow Purchase" -> Purchase; the run lists such items in its warnings and the review marks them |

So purchased components inside a manufacturing BOM get Material Requests, manufactured sub-assemblies get Work Orders
and their own components are exploded in turn (multilevel).

## Review, then confirm

1. **MRP Review** (`/app/ppc-mrp-review/<run>`) lists every recommendation with source documents, dates and the shortage.
2. Tick / untick lines and edit the quantity. Rules (checked in the browser **and** again on the server):
   quantity > 0; whole numbers for whole-number UOMs; at most 10 x the shortage. Lines with no shortage or already
   created cannot be selected. **Save Selection** stores it.
3. **Create Draft Documents** asks for confirmation. The API refuses without `confirm=1`.
4. Stock, supply and demand are read again. A line is skipped when it is no longer short, reduced when stock arrived,
   and never created above the shortage unless the reviewer deliberately raised it. Creation is one transaction.

Unticking a Work Order does not remove the component lines that were planned for it - untick those too if they are not wanted.

## How the numbers are built

| Step | Rule |
|---|---|
| Demand | Open Sales Order lines: (ordered - delivered) x conversion factor. Production Plan rows that belong to a Sales Order line are **not** added again; plan rows without a Sales Order add (planned - produced). Closed / on-hold orders, drop-ship lines and Product Bundles are skipped (the last with a warning). |
| Usable stock | Bin actual qty minus: stock reserved for Work Orders and subcontracting; Stock Reservation Entries outside this run's lines; **Sales Order demand that is not part of this run** (the Bin's reserved qty beyond this run's own lines). A run for one order therefore does not spend stock other open orders wait for. |
| Pending supply / production coverage | open Purchase Orders, Material Requests and Work Orders (Bin ordered / indented / planned) **plus every draft purchase Material Request and draft Work Order**. |
| Netting | per item and warehouse, earliest requirement first: stock, then pending supply, remainder = shortage. A group warehouse counts its children. |
| Lead time | order / start date = required date - lead time (the larger of the Item's lead time and the BOM operation time at 8 h/day, per run). Components are needed when their parent's production must start. A past date marks the line **Late**. |
| Warehouses | only demand in the selected warehouses (none = all); components without a source warehouse use the run's default warehouse. |

## References and duplicate protection

* Every line shows the Sales Orders / Production Plans it ultimately serves (also for components, through the BOM);
  the generated Material Request / Work Order carry **PPC MRP Run** and **MRP Source Documents**
  (and each Material Request Item its own sources). The Sales Order's Connections panel lists its MRP runs.
* One demand row = one key; duplicates are rejected. Drafts created by a run count as supply in every later run, which
  says so in its warnings. A run creates documents once and then closes.
* Creation takes a row lock on the run, is all-or-nothing, and re-reads stock, supply and demand first.
* Audit history: *MRP Calculate*, *MRP Auto Calculate*, *MRP Review Saved*, *MRP Create Documents*, *MRP Retry*,
  *MRP Calculation Failed* in **PPC Audit Log**; the run itself tracks changes.

## Upgrading (migration steps)

No core ERPNext file is touched; everything is custom fields, new DocTypes and hooks.

1. **Back up** the site.
2. Deploy the app (Frappe Cloud: fetch the update, deploy) or `bench --site <site> migrate` and `bench build --app ppc_production_control`.
   Migrate adds: Item `Procurement Type`; `MRP Source Documents` on Material Request, Material Request Item and Work Order;
   the *PPC MRP Source* table and the new fields on *PPC MRP Run* / *PPC MRP Line*; the new Audit Log actions; the PPC
   Settings fields; the **MRP Planning** workspace and **MRP Review** page. It does not change any existing record.
   Runs and lines from the earlier 1.6.0 build keep working (a line without a reviewed quantity uses its shortage).
3. **Decide the automatic run.** It is *on* after the upgrade (a site that never saved PPC Settings reads as on).
   To start with it off: PPC Settings -> Material Planning (MRP) -> untick, Save.
4. **Fill Procurement Type** on the Items. Until then they are inferred and reported. To pre-fill blanks from what
   the items already are (active default BOM -> Manufacture, else Allow Purchase -> Purchase), check first:

       bench --site <site> execute ppc_production_control.services.mrp.backfill_procurement_type
       bench --site <site> execute ppc_production_control.services.mrp.backfill_procurement_type --kwargs "{'dry_run': 0}"

   The first command is a dry run that only reports counts.
5. Optional: set **Default Warehouse for Automatic MRP** in PPC Settings.
6. Roles: the existing PPC roles apply (*Manage Plans* may calculate, review and create; everyone with *View* can read).

**Rollback:** the new fields are additive. Switch the automatic run off in PPC Settings, or redeploy the previous version;
existing Material Requests / Work Orders created by MRP are ordinary drafts.

## Not covered (by design)

Minimum order quantities, reorder levels, batch sizes, supplier choice, subcontracting, non-stock items.

## Files

`services/mrp_engine.py` (pure rules), `services/mrp.py` (ERPNext data, calculate, review, create, dashboard, reports),
`services/mrp_auto.py` (Sales Order trigger), `doctype/ppc_mrp_run` (+ source / warehouse / demand / line tables),
`page/ppc_mrp_review`, `page/ppc_mrp_dashboard`, `report/ppc_mrp_*`, `workspace/mrp_planning`,
tests `tests/test_mrp_engine.py` (no site needed), `tests/test_mrp.py` (real site), `tests/mrp_smoke.py` (read-only check).
