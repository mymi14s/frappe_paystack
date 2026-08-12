import frappe
from frappe import _

from frappe_paystack.utils.portal import (
    PROCESSING_LABEL,
    customer_for,
    invoices_for,
    payments_for,
    refunds_for,
)


def get_context(context: dict) -> dict:
    """Build the customer's outstanding invoices, payments and refunds."""
    if not frappe.session.user or frappe.session.user == "Guest":
        frappe.throw(_("You need to be logged in"), frappe.PermissionError)

    customer = customer_for(frappe.session.user)

    context.customer = customer
    context.invoices = []
    context.payments = []
    context.refunds = []
    # Labels a row whose payment is already under way.
    context.processing_label = PROCESSING_LABEL

    # The queries below run only once a customer is resolved.
    if not customer:
        return context

    context.invoices = invoices_for(customer)
    context.payments = payments_for(customer)
    context.refunds = refunds_for(context.payments)

    return context
