"""
The signed-in customer's view of their own Paystack activity.

Every list is built from documents that already belong to the resolved
customer, never from the Payment Log table on its own.
"""

from typing import Any, Optional

import frappe
from frappe import _

from erpnext.accounts.doctype.payment_request.payment_request import get_amount

PAYMENT_LOG_DOCTYPE = "Paystack Payment Log"
REFUND_LOG_DOCTYPE = "Paystack Refund Log"
SALES_INVOICE = "Sales Invoice"

RECEIPT_PRINT_FORMAT = "Paystack Payment Receipt"

# Everything Paystack can be paid against.
PAID_DOCTYPES = [SALES_INVOICE, "Sales Order", "POS Invoice"]

# Statuses that still owe money, so the page can offer to collect them.
PAYABLE_STATUSES = ["Unpaid", "Partly Paid"]

# Log statuses that mean money was captured, so a receipt can be issued.
RECEIPTABLE_STATUSES = (
    "Processed",
    "Needs Attention",
    "Completed",
    "Partially Refunded",
    "Refunded",
)

# Log statuses where Paystack holds the money and no Payment Entry books it.
IN_FLIGHT_LOG_STATUSES = ("Processed", "Needs Attention")

# Log statuses where the capture has been booked.
BOOKED_LOG_STATUSES = ("Completed", "Partially Refunded", "Refunded")

# What the portal offers for a payable document.
PAYMENT_STATE_PROCESSING = "processing"
PAYMENT_STATE_SETTLED = "settled"
PAYMENT_STATE_OPEN = "open"

# Shown in place of the Pay button while a capture settles.
PROCESSING_LABEL = "Processing Payment"

# The pill colour ERPNext's portal pages give a document still in progress.
PROCESSING_COLOUR = "orange"

PAYMENT_HISTORY_LIMIT = 20

# ERPNext's own customer-facing routes. A POS Invoice has no portal page.
PORTAL_ROUTES = {SALES_INVOICE: "invoices", "Sales Order": "orders"}

PAYMENT_FIELDS = [
    "name",
    "payment_reference as reference",
    "linked_doctype",
    "linked_docname",
    "amount",
    "currency",
    "status",
    "modified",
]

REFUND_FIELDS = [
    "name",
    "payment_log",
    "refund_reference",
    "refund_amount",
    "currency",
    "status",
    "modified",
]

INVOICE_FIELDS = ["name", "posting_date", "due_date", "outstanding_amount", "currency"]


def customer_for(user: str) -> Optional[str]:
    """Return the customer the signed-in contact belongs to."""
    contact = frappe.db.get_value("Contact", {"email_id": user}, "name")
    if not contact:
        return None

    return frappe.db.get_value(
        "Dynamic Link",
        {"parenttype": "Contact", "parent": contact, "link_doctype": "Customer"},
        "link_name",
    )


def invoices_for(customer: str) -> list:
    """Return the customer's invoices that still owe money, each with its payment state."""
    invoices = frappe.get_all(
        SALES_INVOICE,
        filters={"customer": customer, "status": ["in", PAYABLE_STATUSES]},
        fields=INVOICE_FIELDS,
    )

    for invoice in invoices:
        invoice.payment_state = payment_state(SALES_INVOICE, invoice.name)

    return invoices


def log_statuses_for(doctype: str, docname: str) -> set:
    """Return the statuses of every Paystack Payment Log raised against a document."""
    return set(
        frappe.get_all(
            PAYMENT_LOG_DOCTYPE,
            filters={"linked_doctype": doctype, "linked_docname": docname},
            pluck="status",
        )
    )


def payment_state(doctype: str, docname: str) -> str:
    """
    Return what the portal may offer for a payable document.

    "processing" while Paystack holds money nothing has booked, "settled" once a
    booked capture leaves nothing to collect, "open" while money is still owed.
    """
    statuses = log_statuses_for(doctype, docname)

    if statuses.intersection(IN_FLIGHT_LOG_STATUSES):
        return PAYMENT_STATE_PROCESSING

    if not statuses.intersection(BOOKED_LOG_STATUSES):
        return PAYMENT_STATE_OPEN

    if get_amount(frappe.get_doc(doctype, docname)) > 0:
        return PAYMENT_STATE_OPEN

    return PAYMENT_STATE_SETTLED


def apply_payment_state(context: Any) -> None:
    """
    Take the Pay button off a portal page whose payment is already under way.

    ERPNext's portal page reads show_pay_button, Webshop's reads enabled_checkout.
    """
    doc = context.get("doc")
    if not doc or doc.get("doctype") not in PAID_DOCTYPES:
        return

    state = payment_state(doc.get("doctype"), doc.get("name"))
    if state == PAYMENT_STATE_OPEN:
        return

    context.paystack_payment_state = state
    context.show_pay_button = False
    context.enabled_checkout = False

    if state == PAYMENT_STATE_PROCESSING:
        doc.indicator_title = PROCESSING_LABEL
        doc.indicator_color = PROCESSING_COLOUR


def payments_for(customer: str) -> list:
    """
    Return a customer's most recent Paystack payments, newest first.

    One query per paid doctype, each restricted to that customer's documents.
    """
    payments = []

    for doctype in PAID_DOCTYPES:
        documents = frappe.get_all(doctype, filters={"customer": customer}, pluck="name")
        if not documents:
            continue

        payments += frappe.get_all(
            PAYMENT_LOG_DOCTYPE,
            filters={"linked_doctype": doctype, "linked_docname": ["in", documents]},
            fields=PAYMENT_FIELDS,
            order_by="modified desc",
            limit=PAYMENT_HISTORY_LIMIT,
        )

    for payment in payments:
        payment.route = PORTAL_ROUTES.get(payment.linked_doctype)
        payment.has_receipt = payment.status in RECEIPTABLE_STATUSES

    payments.sort(key=lambda payment: payment.modified, reverse=True)
    return payments[:PAYMENT_HISTORY_LIMIT]


def refunds_for(payments: list) -> list:
    """Return the refunds raised against a listed set of payments."""
    names = [payment.name for payment in payments]
    if not names:
        return []

    return frappe.get_all(
        REFUND_LOG_DOCTYPE,
        filters={"payment_log": ["in", names]},
        fields=REFUND_FIELDS,
        order_by="modified desc",
        limit=PAYMENT_HISTORY_LIMIT,
    )


def owns_payment_log(customer: Optional[str], log: str) -> bool:
    """Report whether a payment log was raised against this customer's document."""
    if not customer:
        return False

    linked_doctype, linked_docname = frappe.db.get_value(
        PAYMENT_LOG_DOCTYPE, log, ["linked_doctype", "linked_docname"]
    )
    if linked_doctype not in PAID_DOCTYPES:
        return False

    return frappe.db.get_value(linked_doctype, linked_docname, "customer") == customer


def has_payment_log_website_permission(
    doc: Any, ptype: str = "read", user: Optional[str] = None, verbose: bool = False
) -> bool:
    """Let a customer read a payment log raised against their own document."""
    return owns_payment_log(customer_for(user or frappe.session.user), doc.name)


@frappe.whitelist()
def download_payment_receipt(reference: str) -> None:
    """
    Send the signed-in customer the PDF receipt for one of their payments.

    The receipt is issued only once the payment is captured.
    """
    if not frappe.db.exists(PAYMENT_LOG_DOCTYPE, reference):
        frappe.throw(_("That payment does not exist."), frappe.DoesNotExistError)

    customer = customer_for(frappe.session.user)
    if not owns_payment_log(customer, reference):
        frappe.throw(_("That payment is not yours to download."), frappe.PermissionError)

    status = frappe.db.get_value(PAYMENT_LOG_DOCTYPE, reference, "status")
    if status not in RECEIPTABLE_STATUSES:
        frappe.throw(_("A receipt is only issued once a payment is captured."))

    frappe.local.response.filename = f"{reference}.pdf"
    frappe.local.response.filecontent = frappe.get_print(
        PAYMENT_LOG_DOCTYPE,
        reference,
        print_format=RECEIPT_PRINT_FORMAT,
        as_pdf=True,
        no_letterhead=True,
    )
    frappe.local.response.type = "pdf"
