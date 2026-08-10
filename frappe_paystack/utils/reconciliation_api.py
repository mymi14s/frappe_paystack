"""API endpoints for payment reconciliation."""

from collections import Counter
from typing import Optional

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import add_days, cint, now, today

from frappe_paystack.utils import check_company_permission
from frappe_paystack.utils.reconciliation import MANUAL_OVERRIDE, RECONCILIATION_LOG, ReconciliationEngine

# Roles allowed to trigger reconciliation or read reconciliation data.
RECONCILIATION_ROLES = ("System Manager", "Accounts Manager")

# Roles additionally allowed read-only access to reports.
REPORT_ROLES = RECONCILIATION_ROLES + ("Accounts User",)

MAX_REPORT_LIMIT = 500


def check_reconciliation_permission(roles: tuple = RECONCILIATION_ROLES) -> None:
    """Abort unless the session user holds one of the given roles."""
    if frappe.session.user == "Administrator":
        return

    if not set(roles) & set(frappe.get_roles()):
        frappe.throw(
            _("You are not permitted to access Paystack reconciliation data."),
            frappe.PermissionError,
        )


def company_of(payment_log_name: str) -> str:
    """Return the company of a payment log, or abort if it does not exist."""
    company = frappe.db.get_value("Paystack Payment Log", payment_log_name, "company")
    if not company:
        frappe.throw(
            _("Payment Log {0} not found.").format(payment_log_name),
            frappe.DoesNotExistError,
        )
    return company


def permitted_companies() -> list:
    """Return companies the session user is allowed to see."""
    allowed = frappe.defaults.get_user_permissions().get("Company")
    if not allowed:
        return []
    return [entry.get("doc") for entry in allowed if entry.get("doc")]


def apply_company_filter(filters: dict) -> dict:
    """Restrict a filter set to the user's permitted companies."""
    companies = permitted_companies()
    if companies:
        filters["company"] = ["in", companies]
    return filters


@frappe.whitelist()
@rate_limit(limit=60, seconds=60)
def reconcile_payment(payment_log_name: str) -> dict:
    """Manually reconcile a single payment."""
    check_reconciliation_permission()

    company = company_of(payment_log_name)
    check_company_permission(company)

    engine = ReconciliationEngine(company)
    return engine.reconcile_payment(payment_log_name)


@frappe.whitelist()
@rate_limit(limit=5, seconds=60)
def run_reconciliation(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    company: Optional[str] = None,
) -> dict:
    """Run reconciliation for a company over a date range."""
    check_reconciliation_permission()

    date_from = date_from or add_days(today(), -30)
    date_to = date_to or today()

    engine = ReconciliationEngine(company)
    # An unnamed company falls back to the caller's default.
    check_company_permission(engine.company)

    return engine.run_full_reconciliation(date_from, date_to)


@frappe.whitelist()
def mark_manual_override(payment_log_name: str, notes: str) -> dict:
    """Flag a reconciliation as manually resolved so automated runs skip it."""
    check_reconciliation_permission()

    if not notes:
        frappe.throw(_("A note explaining the override is required."))

    if not frappe.db.exists(RECONCILIATION_LOG, payment_log_name):
        frappe.throw(
            _("No reconciliation log for {0}.").format(payment_log_name),
            frappe.DoesNotExistError,
        )

    log = frappe.get_doc(RECONCILIATION_LOG, payment_log_name)
    check_company_permission(log.company)

    log.status = MANUAL_OVERRIDE
    log.notes = notes
    log.reconciled_by = frappe.session.user
    log.reconciled_at = now()
    log.flags.ignore_permissions = True
    log.save()

    return {"payment_log": payment_log_name, "status": log.status}


@frappe.whitelist()
def get_reconciliation_report(status: Optional[str] = None, limit: int = 100) -> list:
    """Get reconciliation report filtered by status."""
    check_reconciliation_permission(REPORT_ROLES)

    limit = min(max(cint(limit) or 100, 1), MAX_REPORT_LIMIT)

    filters = {}
    if status:
        filters["status"] = status

    return frappe.get_all(
        RECONCILIATION_LOG,
        filters=apply_company_filter(filters),
        fields=[
            "name",
            "payment_log",
            "company",
            "status",
            "paystack_amount",
            "frappe_amount",
            "difference",
            "mismatch_reason",
            "reconciled_at",
        ],
        order_by="reconciled_at desc",
        limit_page_length=limit,
    )


@frappe.whitelist()
def get_reconciliation_stats(days: int = 30) -> dict:
    """Get reconciliation counts by status over a recent window."""
    check_reconciliation_permission(REPORT_ROLES)

    filters = {"reconciliation_date": [">=", add_days(today(), -cint(days or 30))]}

    rows = frappe.get_all(
        RECONCILIATION_LOG,
        filters=apply_company_filter(filters),
        fields=["status"],
    )

    # Counted here because version-15 and version-16 disagree on what get_all
    # accepts in fields.
    result = dict(Counter(row.status for row in rows))
    result["total"] = sum(result.values())

    return result
