import frappe


@frappe.whitelist()
def get_sales_order_status(**kwargs):
    print(frappe.form_dict)
    data = frappe.form_dict
    return frappe.db.get_value("Payment Request", {
        'reference_doctype':data.doctype,
        'reference_name':data.doctype_name
        }, 'status'
    )
    