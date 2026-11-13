# Material Planning (MRP)

An MRP run turns open demand into purchase and production recommendations. A person reviews them
(**MRP Review**), and only after an explicit confirmation are **draft** Material Requests and Work Orders
created. The app never submits anything.

Where: workspace **MRP Planning** (also a section in PPC Control Center): MRP Review, New MRP Run, MRP Runs,
MRP Dashboard and three reports (Item Wise, Warehouse Wise, Date Wise).

## Two triggers, one engine

| | Manual | Automatic |
|---|---|---|
| Started by | *PPC MRP Run* form -> **Calculate** (workspace *New MRP Run*) | Submitting a **Sales Order** or a **Production Plan** |
| Demand | All open Sales Orders / Production Plans in a date range, **or** the Sales Orders / Production Plans listed in *Plan Only These Documents* | The submitted document |
| Code path | `mrp.calculate` -> `mrp.calculate_run` | queued job `mrp_auto.run_for_source` -> `mrp_auto.process_run` -> `mrp.calculate_run` |
| Result | Run with recommendations, status **Calculated** | Same, `Trigger = Automatic`, `Source Sales Order` / `Source Production Plan` filled |

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

## The review dialog and the approval (Step 2)

Submitting a **Sales Order** (or a **Production Plan**) with automatic MRP on opens a dialog: *Calculating…* and, only when the run is
really **Calculated**, *Material Requirement Planning Completed* with the review. The dialog polls the server every ~2 s (and reacts at
once to the realtime event `ppc_mrp_ready`). If nothing has happened after ~20 s it says *Waiting for the background worker* and offers
**Calculate Now** (the same calculation, here; it cannot create a second run or calculate twice, and the queued job that may still come
finds the run and does nothing). It never opens for users without a PPC role, with automatic MRP off, or on a site that has not been
migrated. The same review is the **MRP Review** page: one renderer (`public/js/ppc_mrp_review_ui.js`), one set of server rules.

**Two independent states.** `status` (Draft / Queued / Calculated / Documents Created / Failed) is what the calculation and the creation
did - unchanged. `approval_status` (**Awaiting Review / Approved / Cancelled**) is what people did with the recommendations.

| Action | API (`ppc_production_control.api.`) | Rule |
|---|---|---|
| Approve & Create Drafts | `mrp_approve_and_create(run, approval_key, token, lines?, confirm)` | one approval per run; repeats of the same key only replay the result; stock / supply / demand re-read; one transaction; nothing submitted |
| Revise | `mrp_revise_lines(run, [{name, qty}])` / `mrp_revise_line` | > 0, whole numbers for whole-number UOMs, at most 10 x the shortage; the calculated quantity stays in *Original Quantity*; a skipped line that is revised is wanted again |
| Skip | `mrp_skip_line(run, line, reason)` | a reason is mandatory (also when the approval finds an unticked line without one) |
| Cancel | `mrp_cancel_run(run, reason)` | reason mandatory; nothing can be approved, revised, skipped or created afterwards; refused once documents exist |
| Recalculate | `mrp_recalculate(run)` | today's stock and supply; revisions and skips are discarded; a cancelled review is reopened; refused once documents exist |
| Calculate Now | `mrp_calculate_now(doctype, name)` | the dialog's fallback when no worker answers |
| State for the dialog | `mrp_run_for_source(doctype, name)` (GET) | `waiting / calculating / calculated / approved / cancelled / failed` |

Every change is POST only, needs the **Manage Plans** capability and write permission on the run (looking needs **View**); the run row is
locked and re-read under the lock, so two simultaneous requests are serialized and the second sees the first. `approval_key` is unique in
the database. `token` is the run's `modified` as the reviewer saw it: a run changed since (revised, skipped, recalculated by someone else)
is refused with "reload". Every action - and every refusal - leaves a PPC Audit Log row naming the run, the source document(s), the user,
the time, the original and revised quantity, the reason, the approval status, the documents created and any error.

**Automation policy.** *Automatic Draft Creation* (and Review Mode "Automatic") stay **off by default**. When an administrator switches
one on, PPC Settings records who and when (`mrp_policy_set_by/on`, audited as *MRP Policy Changed*). Documents created by the policy have
no reviewer: the run says `Approved`, carries the policy text including who enabled it, and the execution is audited (*MRP Automation
Policy*). A cancelled or already approved review is never touched by the policy, and nothing is ever submitted.

**Upgrading.** `bench migrate` adds the approval fields (PPC MRP Run / Line, PPC Settings, new PPC Audit Log actions). The shared app code
is harmless before that: the dialog stays closed, the review page shows the classic selection, and the new APIs say "run bench migrate".

## How the numbers are built

| Step | Rule |
|---|---|
| Demand | Open Sales Order lines: (ordered - delivered) x conversion factor. Production Plan rows that belong to a Sales Order line are **not** added again; plan rows without a Sales Order add (planned - produced). Closed / on-hold orders, drop-ship lines and Product Bundles are skipped (the last with a warning). |
| Usable stock | Bin actual qty minus: stock reserved for Work Orders and subcontracting; Stock Reservation Entries outside this run's lines; **Sales Order demand that is not part of this run** (the Bin's reserved qty beyond this run's own lines). A run for one order therefore does not spend stock other open orders wait for. |
| Pending supply / production coverage | open Purchase Orders, Material Requests and Work Orders (Bin ordered / indented / planned; each switchable by the two *Include* settings) **plus, always, every draft purchase Material Request and draft Work Order** - the Include settings never hide a draft. Cancelled documents never count. Supply that was made for another open order is taken out again (next section). |
| Netting | per item and warehouse, earliest requirement first: stock, then the supply that is free for this run, remainder = shortage. A group warehouse counts its children. |
| Lead time | order / start date = required date - lead time (the larger of the Item's lead time and the BOM operation time at 8 h/day, per run). Components are needed when their parent's production must start. A past date marks the line **Late**. |
| Warehouses | only demand in the selected warehouses (none = all); components without a source warehouse use the run's default warehouse. |

## Whose supply is it? (allocation between orders)

ERPNext's Bin only knows totals per item and warehouse, so a Work Order made for Sales Order A used to look free to Sales
Order B. `services/mrp_supply.py` lists the open supply documents behind those totals and decides who each is *for*:

1. **Explicit link:** `Work Order.sales_order`, `Material Request Item.sales_order`, `Purchase Order Item.sales_order` (a Purchase Order
   row without a link inherits the one of the Material Request row it came from). The row's whole open quantity belongs to that order,
   so one Material Request with two rows for two orders gives each its own quantity.
2. **PPC-generated:** a document made by an MRP run carries `ppc_mrp_run`; its owners are the *structured* demand rows of that
   run (PPC MRP Demand). The free-text "MRP Source Documents" field is never read.
3. **No link:** free supply, usable by any order.

For the run being calculated:

| the document is for | counts for this run? |
|---|---|
| only this run's own Sales Orders / Production Plans | yes |
| another **open** Sales Order / Production Plan | **no** - reserved for it (the run's warnings say how much and which documents) |
| this run's order *and* another open one, without a quantity split | **no** - nothing is invented; the run's warnings name the document so a person can look |
| nobody, or only orders that are cancelled, closed, completed, on hold or still draft | yes (free) |

Only what is counted is subtracted (drafts always, submitted documents according to the Include settings), each document row once,
and warehouse stock is never touched by the allocation. Limits: supply is reserved for its owner even beyond the owner's own demand
(a duplicate draft for the same order stays reserved for that order), and ownership through Production Plans is only known for
documents made by PPC runs.

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
`services/mrp_auto.py` (Sales Order / Production Plan trigger, retry, hourly recovery), `services/mrp_supply.py` (whose supply is it),
`services/mrp_approval.py` (dialog state, revise / skip / cancel / recalculate / approve), `services/item_rules.py` (Procurement Type
rules), `services/settings_defaults.py` (+ patch `fill_missing_mrp_settings`), `doctype/ppc_mrp_run` (+ source / warehouse / demand /
line tables), `page/ppc_mrp_review` and `public/js/ppc_mrp_review_ui.js` (the one review renderer, used by the page and the dialog),
`page/ppc_mrp_dashboard`, `report/ppc_mrp_*`, `workspace/mrp_planning`, `deploy/` (systemd units and queue scripts, see
[WORKERS.md](WORKERS.md)); tests: see *Running the tests* below.

## Settings defaults (how an upgrade treats them)

Recommended: Enable Automatic MRP **on**, Calculate on Sales Order **on**, Calculate on Production Plan **on**, Automatic Draft Creation
**off**, Review Mode **Review First**, Include Existing Work Orders **on**, Include Incoming Purchase Orders / Material Requests **on**,
Include Safety Stock **on**. A new site gets exactly these. On an upgraded site a setting that was never stored is loaded - and, by the
migrate patch `fill_missing_mrp_settings`, stored - with its recommended value; **a value that is stored is never changed** (a saved
"off" is a choice), and running the patch again writes nothing. (Before this, the settings form showed a never-stored checkbox as
unticked and the next Save stored that 0.) Every code fallback reads the same table, `constants.MRP_SETTING_DEFAULTS`.

## 1.7.0 - automatic MRP

* **Procurement Type** is now `Item.custom_procurement_type` (1.6.x used `ppc_procurement_type`; the patch
  `patches/v1_7/rename_procurement_type` copies the values and hides the old field). Saving an Item checks it: *Purchase* needs
  "Allow Purchase"; *Manufacture* needs an active submitted BOM. Blank stays allowed and MRP warns.
* **Two triggers:** submitting a Sales Order or a Production Plan queues a background run. Each document gets exactly one
  automatic run - the run's `idempotency_key` (`auto::<doctype>::<name>`) is unique in the database, so repeated events and
  concurrent jobs cannot create a second one. A Production Plan row whose Sales Order already has its own run is not counted again.
* **Automatic Draft Creation** (PPC Settings, **off by default**): after the automatic calculation the same re-validation and
  creation as the manual *Create Draft Documents* runs, for the lines the engine ticked. Everything stays draft; a manufactured
  sub-assembly's Work Order points to its parent (`Work Order.ppc_parent_work_order`); every document carries the MRP Run and the
  source documents.
* **Manual runs:** *Review Mode* in PPC Settings - *Review First* (default) or *Automatic* (Calculate also creates the drafts).
* **Failures:** the run becomes Failed with the reason, PPC Managers get a notification, and the job retries by itself up to
  *Automatic Retries* times. A retry recalculates (existing drafts count as supply) and never creates a document twice.
* **Settings:** master switch, both triggers, draft creation, review mode, warehouse policy, safety stock, incoming POs/MRs,
  existing Work Orders, horizon, default lead time, retries, failure notification.
* **Dashboard data:** `api.mrp_overview` returns cross-run KPIs with filters (company, warehouse, dates, item, procurement type,
  status, source).

Not in 1.7.0: a dedicated "MRP Planning Log" DocType (the PPC Audit Log and Error Log are used), KPI tiles and filter controls on
the dashboard *page* (the API exists), pending stock-transfer supply, and special handling of cancelled generated drafts (a
cancelled draft is simply no longer supply, so a new calculation recommends it again).

## Installation

    cd ~/frappe-bench
    bench get-app <repo-url> --branch feature/mrp-material-planning     # or copy the app folder into apps/
    bench --site <site> backup
    bench --site <site> install-app ppc_production_control              # new site
    bench --site <site> migrate && bench build --app ppc_production_control   # existing site (after the backup)

Requirements: Frappe / ERPNext v15, MariaDB, Redis, **a running background worker** (see [WORKERS.md](WORKERS.md)). Without a worker
the review dialog offers *Calculate Now*, but nothing is calculated in the background.

## Configuration (PPC Settings -> Material Planning (MRP))

| Setting | Recommended | Effect |
|---|---|---|
| Enable Automatic MRP | on | master switch; off = no automatic run, no dialog, no hourly recovery |
| Calculate on Sales Order / Production Plan | on / on | which submits queue a run |
| Automatic Draft Creation | **off** | on = drafts are created without a reviewer (policy is recorded and audited) |
| Review Mode (manual runs) | Review First | *Automatic* = Calculate also creates the drafts |
| Warehouse policy / Default Warehouse for Automatic MRP | Demand Warehouse / empty | where demand without a warehouse is planned |
| Include Safety Stock / Incoming POs and MRs / Existing Work Orders | on | what counts as stock and supply |
| Planning Horizon, Default Lead Time | 365 d, 0 d | |
| Automatic Retries | 2 | retries of a failed run **and** recoveries of a run stuck in Queued |
| Notify on MRP Failure | on | Notification Log for PPC Managers |

Permissions: looking needs the PPC *View* capability, every change (calculate, revise, skip, cancel, recalculate, approve) needs
*Manage Plans* and write permission on the run; approving also needs permission to create Material Requests and Work Orders.

## Approval workflow in one picture

Sales Order submitted -> background run (*Queued* -> *Calculated*, approval *Awaiting Review*) -> dialog "Material Requirement Planning
Completed" -> reviewer revises / skips (reason mandatory) / cancels (reason mandatory) -> **Approve & Create Drafts** (confirmation,
one approval per run) -> draft Material Request + draft Work Orders, linked to the run and the source order -> the people who
own those documents review and submit them. The app never submits.

## Troubleshooting

| Symptom | Likely cause / what to do |
|---|---|
| Dialog says *Waiting for the background worker* | no worker consumes the queue: `bench doctor`, `deploy/inspect_queues.sh`, [WORKERS.md](WORKERS.md). *Calculate Now* works meanwhile |
| Run stays *Queued* | its job was lost. The hourly recovery calculates it again (needs the scheduler, or the systemd timer, see WORKERS.md); or press *Retry Calculation* |
| Run is *Failed* "Stuck in Queued ..." | the automatic retries are used up: fix the cause (worker), then *Retry Calculation* |
| Run *Failed* with another reason | read the reason on the run and the Error Log; fix; *Retry Calculation* |
| No dialog after submit | automatic MRP is off, the user has no PPC role, or the site is not migrated (`bench migrate`) |
| "The recommendations changed since you opened this review" | someone else revised / skipped / recalculated: reload the review |
| *Nothing was created: no longer short* | stock or supply arrived since the calculation: recalculate |
| Pending supply "belongs to other open orders" warning | a draft / open document is linked to another open Sales Order and is not counted for this run (by design) |

## API reference (`/api/method/ppc_production_control.api.<name>`)

Reads are GET, every change is POST (CSRF token as for any Frappe call). Errors come back as the usual Frappe exception
(`exc_type`, `_server_messages`).

| Method | Verb | Arguments | Notes |
|---|---|---|---|
| `mrp_run_for_source` | GET | doctype, name | `state`: waiting / calculating / calculated / approved / cancelled / failed |
| `mrp_calculate_now` | POST | doctype, name | calculates here when no worker answers; never a second run |
| `mrp_review_data` | GET | run | recommendations, KPIs, per-line warehouse stock, BOM, supply, history, `token` |
| `mrp_revise_lines` / `mrp_revise_line` | POST | run, lines `[{name, qty}]` / line, qty | > 0, whole-number UOMs, <= 10 x shortage |
| `mrp_skip_line` | POST | run, line, reason | reason mandatory |
| `mrp_cancel_run` | POST | run, reason | reason mandatory; refused once documents exist |
| `mrp_recalculate` | POST | run | discards revisions and skips; reopens a cancelled review |
| `mrp_approve_and_create` | POST | run, approval_key, token, lines?, confirm=1 | once per run; same key replays the result |
| `mrp_calculate`, `mrp_retry`, `mrp_save_review`, `mrp_create_documents` | POST | see docstrings | manual / classic path, same engine and rules |
| `mrp_dashboard`, `mrp_overview` | GET | filters | KPIs |

## Running the tests

Tests must run on a **copy**, never on a production site (`dev.local` has `allow_tests: false` on purpose). The copy used here is
`ppcbase.local` (own MariaDB :3308, Redis :11002 / :13002, see the staging notes).

    cd ~/frappe-bench
    bench --site ppcbase.local run-tests --app ppc_production_control \
        --module ppc_production_control.tests.<module> --skip-test-records --skip-before-tests

Modules: `test_mrp_engine` (pure rules, no data), `test_item_rules`, `test_mrp`, `test_mrp_supply`, `test_mrp_settings_defaults`,
`test_mrp_auto_drafts`, `test_mrp_approval`, `test_mrp_recovery`, `test_api_security`, plus the older PPC modules. Every MRP test sets
the PPC Settings it relies on (`tests/utils.use_mrp_settings`) and works inside a rolled-back transaction.

Browser end-to-end (Playwright with Edge; it **commits** disposable `PPCE2E-MRP-*` records on the staging copy and removes them):

    PPC_E2E_BASE=http://localhost:8001  PPC_E2E_SITE=ppcbase.local  PPC_E2E_BROWSER=<msedge.exe>
    PPC_E2E_RUNNER="wsl.exe -d Ubuntu-22.04 -u sai -e bash <app>/ppc_production_control/tests/e2e/run_fixture.sh"
    node tests/e2e/ppc_mrp_popup_e2e.js
