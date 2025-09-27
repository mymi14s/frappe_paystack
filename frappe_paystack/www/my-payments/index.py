
import frappe
def get_context(context):
    if not frappe.session.user or frappe.session.user == "Guest":
        frappe.throw("You need to be logged in", frappe.PermissionError)
    customer = frappe.db.get_value("Contact", {"email_id": frappe.session.user}, "customer")
    invoices = frappe.get_all("Sales Invoice",
        filters={"customer": customer, "status": ["in", ["Unpaid","Partly Paid"]]},
        fields=["name","posting_date","due_date","outstanding_amount","currency"])
    payments = frappe.get_all("Paystack Payment Log",
        filters={"linked_doctype":"Sales Invoice"},
        fields=["name","reference","linked_docname","amount","currency","status","modified"],
        order_by="modified desc", limit=20)
    context.invoices = invoices; context.payments = payments; context.customer = customer
    return context
