"""
Collecting an ERPNext Subscription's invoices from a card the customer saved.

ERPNext raises the invoices; this module collects what they bill, for a gateway
whose merchant opted in.
"""

from typing import Any, Optional

import frappe
from frappe.utils import add_days, nowdate

from frappe_paystack.api import charge_saved_card
from frappe_paystack.frappe_paystack.doctype.paystack_customer_authorization import (
    paystack_customer_authorization as authorizations,
)
from frappe_paystack.utils import log_error_for

SALES_INVOICE = "Sales Invoice"
PAYMENT_LOG = "Paystack Payment Log"
GATEWAY_DOCTYPE = "Paystack Gateway Setting"

# Submitted invoice statuses that still owe money.
COLLECTABLE_STATUSES = ("Unpaid", "Overdue", "Partly Paid")

# Payment Log statuses that mean a collection is in flight or settled.
CLAIMED_LOG_STATUSES = (
    "Pending",
    "Processed",
    "Needs Attention",
    "Completed",
    "Partially Refunded",
    "Refunded",
)

# Oldest invoice the job collects.
COLLECTION_LOOKBACK_DAYS = 30

# Invoices one collection run attempts.
COLLECTION_LIMIT = 50


def auto_charge_companies() -> list:
    """Return companies whose gateway asks for subscription invoices to be collected."""
    companies = frappe.get_all(
        GATEWAY_DOCTYPE,
        filters={"enabled": 1, "auto_charge_subscriptions": 1},
        pluck="company",
        order_by=None,
    )
    return sorted({company for company in companies if company})


def collectable_invoices(company: str) -> list:
    """Return the subscription invoices a company may still collect."""
    return frappe.get_all(
        SALES_INVOICE,
        filters={
            "company": company,
            "docstatus": 1,
            "subscription": ["is", "set"],
            "status": ["in", list(COLLECTABLE_STATUSES)],
            "outstanding_amount": [">", 0],
            "posting_date": [">", add_days(nowdate(), -COLLECTION_LOOKBACK_DAYS)],
        },
        pluck="name",
        order_by="posting_date asc",
        limit=COLLECTION_LIMIT,
    )


def has_open_collection(invoice: str) -> bool:
    """Report whether this invoice already has a collection in flight or settled."""
    return bool(
        frappe.db.exists(
            PAYMENT_LOG,
            {
                "linked_doctype": SALES_INVOICE,
                "linked_docname": invoice,
                "status": ["in", list(CLAIMED_LOG_STATUSES)],
            },
        )
    )


def collectable_card(invoice: Any) -> Optional[str]:
    """Return the newest instrument this invoice's customer can be charged on."""
    cards = authorizations.usable_authorizations(invoice.customer, invoice.company)
    return cards[0].name if cards else None


def collect_invoice(invoice_name: str) -> Optional[str]:
    """
    Charge a subscription invoice to the customer's saved card.

    Returns the Payment Log raised, or None when the invoice is claimed or the
    customer has no chargeable card.
    """
    if has_open_collection(invoice_name):
        return None

    invoice = frappe.get_doc(SALES_INVOICE, invoice_name)
    card = collectable_card(invoice)
    if not card:
        return None

    return charge_saved_card(SALES_INVOICE, invoice_name, card)


def collection_queue() -> list:
    """Return every invoice due for collection, logging a lookup failure and answering with []."""
    try:
        return [invoice for company in auto_charge_companies() for invoice in collectable_invoices(company)]
    except Exception:
        frappe.log_error(
            title="Paystack subscription collection: lookup failed",
            message=frappe.get_traceback(),
        )
        return []


def collect_subscription_payments() -> None:
    """
    Collect the outstanding subscription invoices of every opted-in company.

    Runs as Administrator. A declined card is recorded against its own invoice
    and the batch carries on.
    """
    session_user = frappe.session.user
    try:
        frappe.set_user("Administrator")

        for invoice in collection_queue():
            try:
                collect_invoice(invoice)
            except Exception:
                log_error_for(
                    title=f"Paystack subscription collection failed: {invoice}",
                    message=frappe.get_traceback(),
                    reference_doctype=SALES_INVOICE,
                    reference_name=invoice,
                )
    finally:
        frappe.set_user(session_user)
