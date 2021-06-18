import frappe

def get_context(context):
    data = frappe.form_dict
    context.payment_data = data
    return context
