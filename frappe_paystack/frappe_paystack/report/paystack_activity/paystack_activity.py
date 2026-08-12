"""
One chronological feed of everything Paystack has been doing.

Merges the captures, the refunds, the payouts and the API calls into one
stream, newest first, and grades each row by severity.
"""

from typing import Any, Optional

import frappe
from frappe import _
from frappe.utils import flt

from frappe_paystack.utils import PAYSTACK_SERVICE, check_company_permission
from frappe_paystack.utils.reconciliation_api import apply_company_filter, permitted_companies

PAYMENT_LOG = "Paystack Payment Log"
REFUND_LOG = "Paystack Refund Log"
SETTLEMENT = "Paystack Settlement"
INTEGRATION_REQUEST = "Integration Request"

# Doctypes an Integration Request can name that carry a company of their own.
COMPANY_SOURCES = (PAYMENT_LOG, REFUND_LOG, SETTLEMENT)

# Maximum rows the feed returns.
ACTIVITY_LIMIT = 500

ERROR = "Error"
WARNING = "Warning"
INFO = "Info"

# An API call with no error carries the literal string null.
NO_ERROR = "null"


def get_columns() -> list:
    return [
        {
            "label": _("Time"),
            "fieldname": "timestamp",
            "fieldtype": "Datetime",
            "width": 165,
        },
        {
            "label": _("Severity"),
            "fieldname": "severity",
            "fieldtype": "Data",
            "width": 90,
        },
        {"label": _("Source"), "fieldname": "source", "fieldtype": "Data", "width": 110},
        {
            "label": _("Record"),
            "fieldname": "record",
            "fieldtype": "Dynamic Link",
            "options": "source_doctype",
            "width": 210,
        },
        {
            "label": _("Source Doctype"),
            "fieldname": "source_doctype",
            "fieldtype": "Data",
            "width": 175,
        },
        {"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 110},
        {
            "label": _("Company"),
            "fieldname": "company",
            "fieldtype": "Link",
            "options": "Company",
            "width": 150,
        },
        {
            "label": _("Amount"),
            "fieldname": "amount",
            "fieldtype": "Currency",
            "options": "currency",
            "width": 130,
        },
        {
            "label": _("Currency"),
            "fieldname": "currency",
            "fieldtype": "Data",
            "width": 80,
        },
        {"label": _("Detail"), "fieldname": "detail", "fieldtype": "Data", "width": 420},
    ]


def one_line(text: Optional[str]) -> str:
    """Collapse a Small Text into something a grid cell can show."""
    return " ".join((text or "").split())


def creation_condition(filters: dict) -> Optional[list]:
    """
    Return the creation window the date range asks for, if any.

    to_date is closed at 23:59:59 to cover the whole day it names.
    """
    from_date = filters.get("from_date")
    to_date = filters.get("to_date")
    end = f"{to_date} 23:59:59" if to_date else None

    if from_date and end:
        return ["between", [from_date, end]]
    if from_date:
        return [">=", from_date]
    if end:
        return ["<=", end]
    return None


def base_filters(filters: dict, company: bool = True) -> dict:
    """
    Return the window and tenant conditions every source shares.

    A feed asking for no company in particular is narrowed to the ones the
    caller may read.
    """
    conditions = {}

    creation = creation_condition(filters)
    if creation:
        conditions["creation"] = creation

    if not company:
        return conditions

    if filters.get("company"):
        conditions["company"] = filters["company"]
        return conditions

    return apply_company_filter(conditions)


def payment_severity(log: Any) -> str:
    """
    Grade a capture.

    Processed with no Payment Entry warns; Failed and Needs Attention are errors.
    """
    if log.status in ("Failed", "Needs Attention"):
        return ERROR
    if log.status == "Processed" and not log.payment_entry:
        return WARNING
    return INFO


def payment_rows(filters: dict) -> list:
    """Return the captures in range."""
    rows = []
    for log in frappe.get_all(
        PAYMENT_LOG,
        filters=base_filters(filters),
        fields=[
            "name",
            "creation",
            "company",
            "status",
            "amount_paid",
            "currency_paid",
            "payment_entry",
            "linked_doctype",
            "linked_docname",
            "errors",
        ],
    ):
        rows.append(
            {
                "timestamp": log.creation,
                "severity": payment_severity(log),
                "source": _("Payment"),
                "source_doctype": PAYMENT_LOG,
                "record": log.name,
                "status": log.status,
                "company": log.company,
                "amount": flt(log.amount_paid),
                "currency": log.currency_paid,
                "detail": one_line(log.errors)
                or f"{log.linked_doctype or ''} {log.linked_docname or ''}".strip(),
            }
        )
    return rows


def refund_severity(log: Any) -> str:
    """Grade a refund; Pending means the customer is still waiting on Paystack."""
    if log.status == "Failed":
        return ERROR
    if log.status == "Pending":
        return WARNING
    return INFO


def refund_rows(filters: dict) -> list:
    """Return the refunds in range."""
    rows = []
    for log in frappe.get_all(
        REFUND_LOG,
        filters=base_filters(filters),
        fields=[
            "name",
            "creation",
            "company",
            "status",
            "refund_amount",
            "currency",
            "payment_log",
            "refund_reason",
            "errors",
        ],
    ):
        rows.append(
            {
                "timestamp": log.creation,
                "severity": refund_severity(log),
                "source": _("Refund"),
                "source_doctype": REFUND_LOG,
                "record": log.name,
                "status": log.status,
                "company": log.company,
                "amount": flt(log.refund_amount),
                "currency": log.currency,
                "detail": one_line(log.errors) or one_line(log.refund_reason) or log.payment_log,
            }
        )
    return rows


def settlement_severity(payout: Any) -> str:
    """
    Grade a payout.

    A payout with no journal entry warns; a failed or errored one is an error.
    """
    if payout.status == "Failed" or payout.errors:
        return ERROR
    if not payout.journal_entry:
        return WARNING
    return INFO


def settlement_rows(filters: dict) -> list:
    """Return the payouts in range."""
    rows = []
    for payout in frappe.get_all(
        SETTLEMENT,
        filters=base_filters(filters),
        fields=[
            "name",
            "creation",
            "company",
            "status",
            "net_amount",
            "currency",
            "journal_entry",
            "errors",
        ],
    ):
        rows.append(
            {
                "timestamp": payout.creation,
                "severity": settlement_severity(payout),
                "source": _("Payout"),
                "source_doctype": SETTLEMENT,
                "record": payout.name,
                "status": payout.status,
                "company": payout.company,
                "amount": flt(payout.net_amount),
                "currency": payout.currency,
                "detail": one_line(payout.errors)
                or payout.journal_entry
                or _("Not posted: no journal entry cleared this payout."),
            }
        )
    return rows


def request_companies(requests: list) -> dict:
    """Return the company behind each referenced record, keyed by (doctype, name)."""
    companies = {}

    for doctype in COMPANY_SOURCES:
        names = {
            request.reference_docname
            for request in requests
            if request.reference_doctype == doctype and request.reference_docname
        }
        if not names:
            continue

        for row in frappe.get_all(doctype, filters={"name": ["in", list(names)]}, fields=["name", "company"]):
            companies[(doctype, row.name)] = row.company

    return companies


def request_error(request: Any) -> str:
    """Return what an API call recorded going wrong, if anything did."""
    error = one_line(request.error)
    return "" if error == NO_ERROR else error


def request_rows(filters: dict) -> list:
    """
    Return the Paystack API calls in range, including unattributable ones.

    A call with no resolvable company is kept under any company filter.
    """
    conditions = base_filters(filters, company=False)
    conditions["integration_request_service"] = PAYSTACK_SERVICE

    requests = frappe.get_all(
        INTEGRATION_REQUEST,
        filters=conditions,
        fields=[
            "name",
            "creation",
            "status",
            "url",
            "error",
            "reference_doctype",
            "reference_docname",
        ],
    )

    companies = request_companies(requests)
    company = filters.get("company")
    allowed = permitted_companies()

    rows = []
    for request in requests:
        owner = companies.get((request.reference_doctype, request.reference_docname))
        if company and owner and owner != company:
            continue

        if allowed and owner and owner not in allowed:
            continue

        rows.append(
            {
                "timestamp": request.creation,
                "severity": ERROR if request.status == "Failed" else INFO,
                "source": _("API Call"),
                "source_doctype": INTEGRATION_REQUEST,
                "record": request.name,
                "status": request.status,
                "company": owner,
                "amount": None,
                "currency": None,
                "detail": request_error(request) or request.url,
            }
        )

    return rows


SOURCES = {
    "Payments": payment_rows,
    "Refunds": refund_rows,
    "Payouts": settlement_rows,
    "API Calls": request_rows,
}


def get_data(filters: dict) -> list:
    """Merge the sources the filters ask for into one stream, newest first."""
    source = filters.get("source")
    severity = filters.get("severity")

    rows = []
    for label, fetch in SOURCES.items():
        if source and source != label:
            continue
        rows.extend(fetch(filters))

    if severity:
        rows = [row for row in rows if row["severity"] == severity]

    rows.sort(key=lambda row: row["timestamp"], reverse=True)
    return rows[:ACTIVITY_LIMIT]


def execute(filters: Optional[dict] = None) -> tuple:
    filters = filters or {}
    if filters.get("company"):
        check_company_permission(filters["company"])

    return get_columns(), get_data(filters)
