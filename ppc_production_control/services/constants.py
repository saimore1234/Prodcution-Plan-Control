"""Shared constants for PPC Production Control."""

# The plan is ERPNext's own Production Plan; PPC adds fields, connections and a control screen to it.
PLAN_DT = "Production Plan"
AUDIT_DT = "PPC Audit Log"
SETTINGS_DT = "PPC Settings"

ROLE_MANAGER = "PPC Manager"
ROLE_PLANNER = "Production Planner"
ROLE_SUPERVISOR = "Production Supervisor"
ROLE_VIEWER = "PPC Viewer"
PPC_ROLES = [ROLE_MANAGER, ROLE_PLANNER, ROLE_SUPERVISOR, ROLE_VIEWER]

# Capability names are the option values of the "PPC Capability Role" doctype.
CAP_VIEW = "View"
CAP_MANAGE_PLANS = "Manage Plans"
CAP_CREATE_WO = "Create Work Orders"
CAP_SUBMIT_WO = "Submit Work Orders"
CAP_SUBMIT_JC = "Submit Job Cards"
CAP_RESCHEDULE = "Reschedule"
CAP_OVERRIDE = "Override Conflicts"
CAP_RETRY = "Retry Failed Operations"
CAP_RECONCILE = "Reconcile Schedules"

DEFAULT_CAPABILITY_ROLES = {
	CAP_VIEW: PPC_ROLES,
	CAP_MANAGE_PLANS: [ROLE_MANAGER, ROLE_PLANNER],
	CAP_CREATE_WO: [ROLE_MANAGER, ROLE_PLANNER],
	CAP_SUBMIT_WO: [ROLE_MANAGER, ROLE_PLANNER],
	CAP_SUBMIT_JC: [ROLE_MANAGER, ROLE_PLANNER, ROLE_SUPERVISOR],
	CAP_RESCHEDULE: [ROLE_MANAGER, ROLE_PLANNER],
	CAP_OVERRIDE: [ROLE_MANAGER],
	CAP_RETRY: [ROLE_MANAGER, ROLE_PLANNER],
	CAP_RECONCILE: [ROLE_MANAGER],
}

# Work Order statuses ---------------------------------------------------------------------------
WO_LOCKED_STATUSES = ("Completed", "Stopped", "Closed", "Cancelled")

# Demand documents that no longer need (and therefore no longer reserve) material -------------------
CLOSED_SO_STATUSES = ("Closed", "On Hold", "Completed", "Cancelled")
CLOSED_PLAN_STATUSES = ("Completed", "Closed", "Cancelled")

# MRP review and approval --------------------------------------------------------------------------
AP_AWAITING, AP_APPROVED, AP_CANCELLED = "Awaiting Review", "Approved", "Cancelled"
A_MRP_CALC_NOW, A_MRP_RECALC, A_MRP_REVISE, A_MRP_SKIP, A_MRP_APPROVE, A_MRP_CANCEL, A_MRP_POLICY, A_MRP_POLICY_CHANGE = (
	"MRP Calculate Now", "MRP Recalculated", "MRP Line Revised", "MRP Line Skipped", "MRP Approved", "MRP Review Cancelled",
	"MRP Automation Policy", "MRP Policy Changed",
)
MIN_REASON_LENGTH = 3  # a skip / cancel reason must say something

# Recommended PPC Settings (MRP). The DocType JSON carries the same values as field defaults; every `setting(field, default)`
# fallback reads this table, so "nothing stored" always means the recommended value (a test keeps the three in step).
MRP_SETTING_DEFAULTS = {
	"mrp_enabled": 1,
	"mrp_auto_on_sales_order": 1,
	"mrp_trigger_production_plan": 1,
	"mrp_auto_create_drafts": 0,  # drafts are created only after a person reviews, unless an administrator switches this on
	"mrp_review_mode": "Review First",
	"mrp_warehouse_policy": "Demand Warehouse",
	"mrp_include_safety_stock": 1,
	"mrp_include_incoming_po": 1,
	"mrp_include_existing_wo": 1,
	"mrp_planning_horizon_days": 365,
	"mrp_default_lead_time_days": 0,
	"mrp_max_retries": 2,
	"mrp_notify_on_failure": 1,
}

# An automatic MRP run that is still "Queued" after this long lost its background job (worker down, Redis restarted, job killed);
# the hourly recovery task (mrp_auto.recover_stuck_runs) calculates it again. At most this many runs per hourly pass.
MRP_STUCK_AFTER_MINUTES = 30
MRP_RECOVERY_BATCH = 50

# Job Card statuses ---------------------------------------------------------------------------
JC_DONE_STATUSES = ("Completed", "Submitted", "Cancelled")
# A Job Card can only be re-scheduled while ERPNext has not started or finished it.
JC_RESCHEDULABLE_STATUSES = ("Open", "Material Transferred", "On Hold")

# Audit ---------------------------------------------------------------------------------------
A_CREATE_WO = "Create Work Order"
A_SUBMIT_WO = "Submit Work Order"
A_GENERATE_JC = "Generate Job Cards"
A_SYNC_JC = "Sync Job Cards"
A_SUBMIT_JC = "Submit Job Card"
A_RESCHEDULE = "Reschedule Job Card"
A_CHANGE_WORKSTATION = "Change Workstation"
A_APPLY_MACHINE_PLAN = "Apply Machine Plan"
A_SYNC_WO_SCHEDULE = "Sync Work Order Schedule"
A_AUTO_SCHEDULE = "Auto Schedule"
A_RETRY = "Retry"
A_RECONCILE = "Reconcile"
A_INIT_BASELINE = "Initialize Baseline"
A_CORRECT_REVISION = "Correct Revision"

R_SUCCESS = "Success"
R_FAILED = "Failed"
R_SKIPPED = "Skipped"
R_WARNING = "Warning"

DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
