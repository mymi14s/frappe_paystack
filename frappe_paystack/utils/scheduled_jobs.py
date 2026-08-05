"""Scheduled jobs for payment reconciliation."""

import frappe
from frappe.utils import add_days, today

from frappe_paystack.utils.reconciliation import ReconciliationEngine

# How far back the daily reconciliation pass looks.
DAILY_LOOKBACK_DAYS = 2


def paystack_companies() -> list:
    """Return companies with an enabled Paystack gateway."""
    # Deduplicated in Python; MySQL 8 rejects DISTINCT with Frappe's implicit sort.
    companies = frappe.get_all(
        "Paystack Gateway Setting",
        filters={"enabled": 1},
        pluck="company",
        order_by=None,
    )
    return sorted({company for company in companies if company})


def scheduled_companies() -> list:
    """Return Paystack companies, logging a lookup failure and answering with []."""
    try:
        return paystack_companies()
    except Exception:
        frappe.log_error(
            title="Paystack scheduled job: company lookup failed",
            message=frappe.get_traceback(),
        )
        return []


def run_daily_reconciliation() -> None:
    """Reconcile each enabled company's recent payments."""
    for company in scheduled_companies():
        try:
            engine = ReconciliationEngine(company)
            engine.run_full_reconciliation(add_days(today(), -DAILY_LOOKBACK_DAYS), today())
        except Exception:
            frappe.log_error(
                title=f"Paystack daily reconciliation failed: {company}",
                message=frappe.get_traceback(),
            )
