import frappe

def get_context(context):
    data = frappe.form_dict
    print(context, "\n\n\n")
    print(data, "\n\n\n")
    print(frappe.locals.request, "\n\n\n")
    # context.payment_data = data
    return context
