"""
Migration logic the patches in patches/v15_0 drive.

Repoints rows naming a path or status this app has moved, and backfills the
Payment Request link on Paystack Payment Logs that carry none.
"""

from typing import Any, Optional

import frappe
from frappe.utils import flt

from frappe_paystack.utils.payment_request import BILLABLE_DOCTYPES, PAYMENT_GATEWAY
from frappe_paystack.utils.reconciliation import MANUAL_OVERRIDE, RECONCILIATION_LOG

PAYMENT_LOG = "Paystack Payment Log"
PAYMENT_REQUEST = "Payment Request"
PATCH_LOG = "Patch Log"
SCHEDULED_JOB_TYPE = "Scheduled Job Type"

# Amounts are matched to the minor unit.
AMOUNT_TOLERANCE = 0.01

# The package the patch modules answer to.
PATCH_HOME = "frappe_paystack.patches.v15_0"

# The packages each patch module has answered to, keyed by module name.
FORMER_PATCH_HOMES = {
    "abandon_expired_settlements": ("v_15",),
    "backfill_payment_request": ("v0_4_0", "v_15"),
    "create_dashboard_widgets": ("v0_5_0", "v_15"),
    "create_payment_gateway": ("v0_2_0", "v_15"),
    "drop_payment_log_raw_response": ("v0_3_0", "v_15"),
    "drop_refund_log_submittable": ("v0_3_0", "v_15"),
    "route_payments_per_company": ("v_15",),
    "seed_payment_log_retries": ("v_15",),
    "set_refunded_status": ("v0_3_0", "v_15"),
}

# Maps every path a patch has carried to the one it answers to now.
RENAMED_PATCHES = {
    f"frappe_paystack.patches.{home}.{module}": f"{PATCH_HOME}.{module}"
    for module, homes in FORMER_PATCH_HOMES.items()
    for home in homes
}

# The packages the scheduled hook methods answer to, now and before.
JOB_HOME = "frappe_paystack.utils"
FORMER_JOB_HOME = "frappe_paystack.helpers"

# The hook methods hooks.py schedules out of the moved package.
SCHEDULED_JOBS = (
    "scheduled_jobs.run_daily_reconciliation",
    "settlement.retry_unposted_settlements",
    "subscription.collect_subscription_payments",
)

# Maps the path each scheduled job has carried to the one hooks.py names now.
RENAMED_JOBS = {f"{FORMER_JOB_HOME}.{job}": f"{JOB_HOME}.{job}" for job in SCHEDULED_JOBS}

# The jobs hooks.py no longer schedules, under every path they have carried.
REMOVED_JOBS = tuple(
    f"{home}.{job}"
    for job in ("scheduled_jobs.generate_weekly_reconciliation_report",)
    for home in (FORMER_JOB_HOME, JOB_HOME)
)

# The reconciliation status a human sets, as rows carried it before.
FORMER_MANUAL_OVERRIDE = "Manual_Override"


def rename_patch_log_entries() -> list:
    """
    Point the Patch Log rows of the moved patches at their new paths.

    Returns the new paths written; a row already carrying one is skipped.
    """
    renamed = []

    for old, new in RENAMED_PATCHES.items():
        if frappe.db.exists(PATCH_LOG, {"patch": new}):
            continue

        if not frappe.db.exists(PATCH_LOG, {"patch": old}):
            continue

        frappe.db.set_value(PATCH_LOG, {"patch": old}, "patch", new, update_modified=False)
        renamed.append(new)

    return renamed


def rename_scheduled_jobs() -> list:
    """
    Point the Scheduled Job Type rows of the moved jobs at their new methods.

    Returns the new methods written; a second pass finds nothing.
    """
    renamed = []

    for old, new in RENAMED_JOBS.items():
        if not frappe.db.exists(SCHEDULED_JOB_TYPE, {"method": old}):
            continue

        frappe.db.set_value(SCHEDULED_JOB_TYPE, {"method": old}, "method", new, update_modified=False)
        renamed.append(new)

    return renamed


def drop_removed_scheduled_jobs() -> list:
    """
    Delete the Scheduled Job Type rows of the jobs this app no longer schedules.

    Returns the methods deleted; a second pass finds nothing.
    """
    dropped = []

    for method in REMOVED_JOBS:
        name = frappe.db.get_value(SCHEDULED_JOB_TYPE, {"method": method}, "name")
        if not name:
            continue

        frappe.delete_doc(SCHEDULED_JOB_TYPE, name, force=True, ignore_permissions=True)
        dropped.append(method)

    return dropped


def overridden_reconciliations() -> list:
    """Return the reconciliation rows still carrying the former override status."""
    return frappe.get_all(RECONCILIATION_LOG, filters={"status": FORMER_MANUAL_OVERRIDE}, pluck="name")


def rename_manual_overrides() -> list:
    """
    Move every human-set reconciliation onto the current override status.

    Returns the names rewritten; a second pass selects none.
    """
    renamed = []

    for name in overridden_reconciliations():
        frappe.db.set_value(RECONCILIATION_LOG, name, "status", MANUAL_OVERRIDE, update_modified=False)
        frappe.clear_document_cache(RECONCILIATION_LOG, name)
        renamed.append(name)

    return renamed


def stuck_payment_logs() -> list:
    """Return the logs holding a capture no Payment Entry has booked, unstamped."""
    return frappe.get_all(
        PAYMENT_LOG,
        filters={
            "status": "Processed",
            "payment_entry": ["in", ["", None]],
            "docstatus": ["<", 2],
            "last_retry": ["is", "not set"],
        },
        fields=["name", "modified"],
        order_by="creation asc",
    )


def seed_payment_log_retries() -> list:
    """
    Count one attempt against every log already stuck at Processed.

    The stamp is the log's own modified time. A log already carrying one is
    skipped. Returns the names stamped.
    """
    seeded = []

    for log in stuck_payment_logs():
        frappe.db.set_value(
            PAYMENT_LOG,
            log.name,
            {"retry_count": 1, "last_retry": log.modified},
            update_modified=False,
        )
        frappe.clear_document_cache(PAYMENT_LOG, log.name)
        seeded.append(log.name)

    return seeded


def company_filter(company: Optional[str]) -> dict:
    """Return the company clause, empty when every company is in scope."""
    return {"company": company} if company else {}


def unlinked_logs(company: Optional[str] = None) -> list:
    """Return logs on a billable document that carry no Payment Request."""
    return frappe.get_all(
        PAYMENT_LOG,
        filters={
            "payment_request": ["is", "not set"],
            "linked_doctype": ["in", list(BILLABLE_DOCTYPES)],
            "linked_docname": ["is", "set"],
            **company_filter(company),
        },
        fields=["name", "linked_doctype", "linked_docname", "amount", "amount_paid"],
        order_by="creation asc",
    )


def claimed_payment_requests(log: Any) -> set:
    """Return the requests on this log's document another log already points at."""
    return set(
        frappe.get_all(
            PAYMENT_LOG,
            filters={
                "payment_request": ["is", "set"],
                "linked_doctype": log.linked_doctype,
                "linked_docname": log.linked_docname,
            },
            pluck="payment_request",
        )
    )


def candidate_requests(log: Any, claimed: set) -> list:
    """Return the unclaimed submitted requests a log could have collected."""
    amount = flt(log.amount) or flt(log.amount_paid)
    if not amount:
        return []

    claimed = claimed | claimed_payment_requests(log)

    requests = frappe.get_all(
        PAYMENT_REQUEST,
        filters={
            "reference_doctype": log.linked_doctype,
            "reference_name": log.linked_docname,
            "payment_gateway": PAYMENT_GATEWAY,
            "docstatus": 1,
        },
        fields=["name", "grand_total"],
        order_by="creation asc",
    )

    return [
        row.name
        for row in requests
        if row.name not in claimed and abs(flt(row.grand_total) - amount) <= AMOUNT_TOLERANCE
    ]


def backfill_payment_requests(company: Optional[str] = None, dry_run: bool = False) -> dict:
    """
    Link payment logs to the Payment Request they collected.

    Returns a report of the logs examined, linked, ambiguous and unmatched. Each
    request goes to at most one log.
    """
    claimed = set()
    report: dict[str, Any] = {"examined": 0, "linked": [], "ambiguous": [], "unmatched": []}

    for log in unlinked_logs(company):
        report["examined"] += 1
        matches = candidate_requests(log, claimed)

        if not matches:
            report["unmatched"].append(log.name)
            continue

        if len(matches) > 1:
            report["ambiguous"].append(log.name)
            continue

        if not dry_run:
            frappe.db.set_value(
                PAYMENT_LOG,
                log.name,
                "payment_request",
                matches[0],
                update_modified=False,
            )
            frappe.clear_document_cache(PAYMENT_LOG, log.name)

        claimed.add(matches[0])
        report["linked"].append(log.name)

    return report


def linkage_report(company: Optional[str] = None) -> dict:
    """Return the billable, linked and unlinked payment log counts."""
    billable = {
        "linked_doctype": ["in", list(BILLABLE_DOCTYPES)],
        **company_filter(company),
    }
    total = frappe.db.count(PAYMENT_LOG, billable)
    linked = frappe.db.count(PAYMENT_LOG, {**billable, "payment_request": ["is", "set"]})

    return {"billable_logs": total, "linked": linked, "unlinked": total - linked}


def revert_payment_request_backfill(log_names: list) -> list:
    """
    Clear the Payment Request link the backfill wrote on these logs.

    A log that has booked a Payment Entry keeps its link. Returns the names cleared.
    """
    reverted = []

    for name in log_names:
        if not frappe.db.exists(PAYMENT_LOG, name):
            continue

        if frappe.db.get_value(PAYMENT_LOG, name, "payment_entry"):
            continue

        frappe.db.set_value(PAYMENT_LOG, name, "payment_request", None, update_modified=False)
        frappe.clear_document_cache(PAYMENT_LOG, name)
        reverted.append(name)

    return reverted
