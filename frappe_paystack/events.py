from typing import Any, Optional

import frappe
from frappe.utils import flt

from frappe_paystack.frappe_paystack.doctype.paystack_refund_log.paystack_refund_log import (
    REFUNDABLE_LOG_STATUSES,
    get_total_refunded,
)
from frappe_paystack.utils import charge_currency, initiate_refund, record_failure, resolve_paystack_settings

PAYMENT_LOG_DOCTYPE = "Paystack Payment Log"
REFUND_LOG_DOCTYPE = "Paystack Refund Log"


def sales_invoice_on_submit(doc: Any, method: Optional[str] = None) -> None:
    """
    Initiate a Paystack refund when a credit note is submitted.

    Applies to a return against an invoice paid via Paystack, when the gateway
    has auto_refund_on_credit_note enabled.
    """
    if not getattr(doc, "is_return", False):
        return

    if not getattr(doc, "return_against", None):
        return

    settings = resolve_paystack_settings(getattr(doc, "company", None))
    if not settings or not settings.get("auto_refund_on_credit_note"):
        return

    # Doctype, document and company together identify the capture reversed.
    payment_log_name = frappe.db.get_value(
        PAYMENT_LOG_DOCTYPE,
        {
            "linked_doctype": doc.doctype,
            "linked_docname": doc.return_against,
            "company": doc.company,
            "status": ["in", REFUNDABLE_LOG_STATUSES],
        },
        "name",
    )
    if not payment_log_name:
        return

    payment_log = frappe.get_doc(PAYMENT_LOG_DOCTYPE, payment_log_name)
    amount_paid = flt(payment_log.amount_paid)

    if amount_paid <= 0:
        return

    # The request is capped at the balance earlier credit notes left.
    refundable = amount_paid - get_total_refunded(payment_log_name)
    if refundable <= 0:
        return

    # A credit note's grand_total is negative; the refund is its magnitude.
    credit_note_total = abs(flt(getattr(doc, "base_grand_total", 0) or 0))
    refund_amount = min(credit_note_total, refundable)

    if refund_amount <= 0:
        return

    transaction_id = payment_log.transaction_id
    currency = charge_currency(payment_log.company, payment_log.currency_paid)

    refund_log = frappe.get_doc(
        {
            "doctype": REFUND_LOG_DOCTYPE,
            "payment_log": payment_log_name,
            "company": doc.company,
            "linked_doctype": doc.doctype,
            "linked_docname": doc.name,
            "transaction_id": transaction_id,
            "refund_amount": refund_amount,
            "currency": currency,
            "status": "Pending",
            "refund_reason": f"Auto-refund from credit note {doc.name}",
        }
    )
    refund_log.flags.ignore_permissions = True

    try:
        refund_log.insert()
        result = initiate_refund(
            transaction_id=transaction_id,
            amount=refund_amount,
            currency=currency,
            company=doc.company,
            merchant_note=f"Credit note {doc.name}",
            refund_log=refund_log.name,
        )
        refund_log.db_set("status", "Processed", update_modified=True)
        refund_log.db_set("refund_reference", result.get("reference"), update_modified=False)
        refund_log.db_set("raw_response", frappe.as_json(result.get("raw")), update_modified=False)
        refund_log.reload()
        refund_log.run_method("on_update")
    except Exception:
        errors = record_failure(f"Paystack auto-refund failed for credit note {doc.name}")
        # The status lands only on a Refund Log whose insert survived.
        if refund_log.name and frappe.db.exists(REFUND_LOG_DOCTYPE, refund_log.name):
            refund_log.db_set("status", "Failed", update_modified=True)
            refund_log.db_set("errors", errors, update_modified=True)
