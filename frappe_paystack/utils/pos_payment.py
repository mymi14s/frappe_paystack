"""
Paystack payment links for the POS counter.

POS settles through a Paystack Payment Log directly.
"""

import frappe
from frappe import _
from frappe.utils import flt, get_url

from frappe_paystack.utils import resolve_paystack_settings

MODE_OF_PAYMENT = "Paystack"
GATEWAY_TYPES = ("Phone", "Email")


def paystack_tender_amount(invoice) -> float:
    """Return what the customer still owes on the Paystack tender."""
    return sum(
        flt(row.amount)
        for row in invoice.payments
        if row.mode_of_payment == MODE_OF_PAYMENT or row.type in GATEWAY_TYPES
    )


def resolve_email(invoice, email: str = "") -> str:
    """Return the address a payment link should be sent to."""
    address = (
        email or invoice.get("contact_email") or frappe.db.get_value("Customer", invoice.customer, "email_id")
    )
    if not address:
        frappe.throw(_("Enter an email address for {0} to send the payment link.").format(invoice.customer))
    return address


def build_payment_log(invoice, amount: float):
    """Create the Paystack Payment Log backing this POS charge."""
    settings = resolve_paystack_settings(invoice.company)
    if not settings:
        frappe.throw(_("Paystack is not enabled for {0}.").format(invoice.company))

    log = frappe.new_doc("Paystack Payment Log")
    log.company = invoice.company
    log.linked_doctype = "POS Invoice"
    log.linked_docname = invoice.name
    log.amount = amount
    log.currency = invoice.currency
    log.status = "Pending"
    log.flags.ignore_permissions = True
    # A POS Invoice stays in draft until the sale closes.
    log.flags.ignore_links = True
    log.insert()
    return log


def render_email(invoice, amount: float, url: str) -> str:
    """Render the payment link email, showing what is being paid for."""
    return frappe.render_template(
        "frappe_paystack/templates/emails/pos_payment_link.html",
        {
            "invoice": invoice,
            "amount": frappe.utils.fmt_money(amount, currency=invoice.currency),
            "url": url,
            "items": invoice.items,
        },
    )


@frappe.whitelist()
def send_pos_payment_link(pos_invoice: str, email: str = "") -> dict:
    """Email a Paystack checkout link for a POS Invoice, returning the log and address."""
    invoice = frappe.get_doc("POS Invoice", pos_invoice)
    invoice.check_permission("read")

    amount = paystack_tender_amount(invoice)
    if amount <= 0:
        frappe.throw(_("Enter the amount to collect through Paystack first."))

    address = resolve_email(invoice, email)
    log = build_payment_log(invoice, amount)
    url = get_url(f"/paystack-checkout/{log.name}")

    frappe.sendmail(
        recipients=[address],
        subject=_("Payment link for {0}").format(invoice.name),
        message=render_email(invoice, amount, url),
        reference_doctype="POS Invoice",
        reference_name=invoice.name,
        now=True,
    )

    frappe.publish_realtime(
        "paystack_pos_awaiting",
        {"pos_invoice": invoice.name, "log": log.name, "email": address, "amount": amount},
        user=frappe.session.user,
    )

    return {"log": log.name, "email": address, "amount": amount, "url": url}


def notify_pos_charge_started(payment_request, reference: str, response: dict) -> None:
    """Push the checkout URL of a POS phone charge to the till."""
    url = (response.get("data") or {}).get("authorization_url")
    if not url:
        return

    frappe.publish_realtime(
        "paystack_pos_charge_url",
        {
            "pos_invoice": payment_request.reference_name,
            "payment_request": payment_request.name,
            "reference": reference,
            "url": url,
        },
        user=frappe.session.user,
    )


def notify_pos_link_paid(log) -> None:
    """Tell the POS screen how much of the tender has arrived."""
    if log.linked_doctype != "POS Invoice":
        return

    outstanding = flt(log.amount) - flt(log.amount_paid)
    frappe.publish_realtime(
        "paystack_pos_paid",
        {
            "pos_invoice": log.linked_docname,
            "log": log.name,
            "amount_paid": flt(log.amount_paid),
            "outstanding": outstanding if outstanding > 0 else 0,
            "fully_paid": outstanding <= 0,
        },
        user=log.owner,
    )


@frappe.whitelist()
def pos_payment_status(log: str) -> dict:
    """Report how much of a POS payment link has been paid."""
    record = frappe.db.get_value(
        "Paystack Payment Log",
        log,
        ["name", "status", "amount", "amount_paid", "linked_docname"],
        as_dict=True,
    )
    if not record:
        frappe.throw(_("Payment link {0} not found.").format(log))

    frappe.get_doc("Paystack Payment Log", log).check_permission("read")

    outstanding = flt(record.amount) - flt(record.amount_paid)
    return {
        "log": record.name,
        "status": record.status,
        "pos_invoice": record.linked_docname,
        "amount_paid": flt(record.amount_paid),
        "outstanding": outstanding if outstanding > 0 else 0,
        "fully_paid": record.status in ("Processed", "Completed") and outstanding <= 0,
    }
