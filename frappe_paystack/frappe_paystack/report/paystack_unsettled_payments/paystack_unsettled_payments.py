"""
Captures whose money Paystack has not yet paid out.

The list behind the gateway's suspense balance, with how long each has waited.
"""

from typing import Optional

import frappe
from frappe import _
from frappe.utils import date_diff, flt, nowdate

from frappe_paystack.utils import check_company_permission

PAYMENT_LOG = "Paystack Payment Log"

# Log statuses that mean the money was captured and is therefore owed.
CAPTURED_STATUSES = (
    "Processed",
    "Needs Attention",
    "Completed",
    "Partially Refunded",
    "Refunded",
)


def get_columns() -> list:
    return [
        {
            "label": _("Payment Log"),
            "fieldname": "name",
            "fieldtype": "Link",
            "options": PAYMENT_LOG,
            "width": 200,
        },
        {"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 130},
        {
            "label": _("Captured On"),
            "fieldname": "payment_date",
            "fieldtype": "Date",
            "width": 120,
        },
        {
            "label": _("Days Waiting"),
            "fieldname": "days_waiting",
            "fieldtype": "Int",
            "width": 110,
        },
        {
            "label": _("Amount Captured"),
            "fieldname": "amount_paid",
            "fieldtype": "Currency",
            "options": "currency_paid",
            "width": 150,
        },
        {
            "label": _("Paystack Fee"),
            "fieldname": "paystack_fee",
            "fieldtype": "Currency",
            "options": "currency_paid",
            "width": 130,
        },
        {
            "label": _("Currency"),
            "fieldname": "currency_paid",
            "fieldtype": "Data",
            "width": 90,
        },
        {
            "label": _("Document"),
            "fieldname": "linked_docname",
            "fieldtype": "Dynamic Link",
            "options": "linked_doctype",
            "width": 200,
        },
        {
            "label": _("Doctype"),
            "fieldname": "linked_doctype",
            "fieldtype": "Data",
            "width": 150,
        },
        {
            "label": _("Payment Entry"),
            "fieldname": "payment_entry",
            "fieldtype": "Data",
            "width": 200,
        },
    ]


def get_data(filters: dict) -> list:
    conditions = {
        "company": filters.get("company"),
        "status": ["in", list(CAPTURED_STATUSES)],
        "settlement": ["in", ["", None]],
    }

    if filters.get("to_date"):
        conditions["payment_date"] = ["<=", filters["to_date"]]

    rows = frappe.get_all(
        PAYMENT_LOG,
        filters=conditions,
        fields=[
            "name",
            "status",
            "payment_date",
            "amount_paid",
            "paystack_fee",
            "currency_paid",
            "linked_doctype",
            "linked_docname",
            "payment_entry",
        ],
        order_by="payment_date asc, creation asc",
    )

    today = nowdate()
    for row in rows:
        row["amount_paid"] = flt(row.amount_paid)
        # A capture with no paid_at reads as today.
        row["days_waiting"] = date_diff(today, row.payment_date)

    return rows


def execute(filters: Optional[dict] = None) -> tuple:
    filters = filters or {}
    check_company_permission(filters.get("company"))
    return get_columns(), get_data(filters)
