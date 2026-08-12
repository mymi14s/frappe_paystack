import json

import frappe
from frappe import _

PAYMENT_LOG = "Paystack Payment Log"

# Escapes for the characters that close the script element the payload sits in.
SCRIPT_ESCAPES = (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"))

no_cache = 1


def script_payload(data: dict) -> str:
    """Return data as JSON whose text cannot close the script tag holding it."""
    payload = json.dumps(data)

    for character, escape in SCRIPT_ESCAPES:
        payload = payload.replace(character, escape)

    return payload


def get_context(context: dict) -> dict:
    """
    Build the checkout page context for a payment log reference.

    An unknown reference yields a context with no doc and a null payload.
    """
    context.no_cache = 1
    context.title = _("Complete Payment")
    context.reference = None
    context.doc = None
    # Serialised separately so the template's <script> block embeds JSON.
    context.payload = "null"

    reference = frappe.form_dict.reference
    if not reference or not frappe.db.exists(PAYMENT_LOG, reference):
        return context

    doc = frappe.get_doc(PAYMENT_LOG, reference)
    data = doc.get_data()

    context.reference = reference
    context.doc = data
    context.payload = script_payload(data)

    return context
