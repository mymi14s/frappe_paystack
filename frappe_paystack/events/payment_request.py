import frappe



def before_insert(doc, event=None):
    """
        Check if gateway is paystack
    """
    if not doc.mode_of_payment:
        if frappe.db.exists("Paystack Settings", {'name':doc.payment_gateway}):
            gateway = frappe.get_doc("Paystack Settings", doc.payment_gateway)
            doc.mode_of_payment =gateway.mode_of_payment
            
    if not doc.cost_center:
        if frappe.db.exists("Paystack Settings", {'name':doc.payment_gateway}):
            gateway = frappe.get_doc("Paystack Settings", doc.payment_gateway)
            doc.cost_center = gateway.cost_center