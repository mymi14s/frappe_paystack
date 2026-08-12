"""Read-only Jinja helpers showing the Paystack checkout link a document has open."""

from typing import Any, Optional

import frappe

from frappe_paystack.utils.qr import qr_data_uri

PAYMENT_LOG_DOCTYPE = "Paystack Payment Log"


def open_payment_log(doc: Any) -> Optional[Any]:
    """Return the newest unpaid, unexpired checkout log for a document."""
    name = frappe.db.get_value(
        PAYMENT_LOG_DOCTYPE,
        {
            "linked_doctype": doc.doctype,
            "linked_docname": doc.name,
            "status": "Pending",
        },
        "name",
        order_by="creation desc",
    )
    if not name:
        return None

    log = frappe.get_doc(PAYMENT_LOG_DOCTYPE, name)
    return None if log.is_expired() else log


def paystack_payment_link(doc: Any) -> str:
    """Return the checkout URL a document can currently be paid at."""
    log = open_payment_log(doc)
    return log.get_payment_link() if log else ""


def paystack_payment_qr(doc: Any) -> str:
    """Return the checkout URL as a scannable SVG data URI, or empty."""
    return qr_data_uri(paystack_payment_link(doc))
