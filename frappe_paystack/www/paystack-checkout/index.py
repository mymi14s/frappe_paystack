import frappe

PAYMENT_LOG = "Paystack Payment Log"

def get_context(context):
    context.title = "Paystack Checkout"
    reference = frappe.form_dict.reference
    if not reference:
        context.reference = None
    else:
        if frappe.db.exists(PAYMENT_LOG, {"name": reference}):
            doc = frappe.get_doc(PAYMENT_LOG, reference)
            context.doc = doc.get_data()
            context.reference = reference
        else:
            context.reference = None

    return context

