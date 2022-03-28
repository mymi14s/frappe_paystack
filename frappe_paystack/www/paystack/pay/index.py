import frappe

def get_context(context):
    data = frappe.form_dict
    # print(data)
    context.payment_data = data
    return context
