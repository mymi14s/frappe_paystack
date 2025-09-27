import frappe, json, requests

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


@frappe.whitelist(allow_guest=True)
def get_payment_request(reference_doctype, reference_docname):
    if not (reference_doctype and reference_docname):
        return {'error':"Invalid payment link."}
    payment_request = frappe.db.get_value(
        reference_doctype, {
            "name":reference_docname,
            "docstatus":1,
            "status":["=", "Requested"],
            "payment_request_type": "Inward"
        }, 
        "*", as_dict=1
    )
    if not payment_request:
        return {'error':"Invalid payment link."}
    if payment_request.status=='Paid':
        return {'error':"Payment has already been made."}
    public_key = frappe.db.get_value(
        "Paystack Gateway Setting",
        {'enabled':1,},
        ["public_key"]
    )
    if not public_key:
        return {'error':"Payment method is unavailable at the moment, please contact us directly.."}
    payment_request.public_key = public_key
    return payment_request

@frappe.whitelist(allow_guest=True)
def verify_transaction():
    try:
        transaction = frappe.form_dict
        frappe.get_doc({
            'doctype':"Paystack Log",
            'message':transaction.message,
            'status':transaction.status,
            'reference': transaction.reference,
            'transaction': transaction.transaction,
        }).insert(ignore_permissions=1)
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), 'Verify Transaction')



@frappe.whitelist(allow_guest=True)
def paystack_webhook(**kwargs):
    """
        End point where payment gateway sends payment info.
    """
    try:
        data = frappe._dict(frappe.form_dict.data)
        if not (frappe.db.exists("Payment Gateway Request", {"name":data.reference})):
            secret_key = frappe.get_doc(
                "Payment Gateway Integration Settings",
                {'enabled':1, 'gateway':'Paystack'}
            ).get_secret_key()
            headers = {"Authorization": f"Bearer {secret_key}"}
            req = requests.get(
                f"https://api.paystack.co/transaction/verify/{data.reference}",
                headers=headers, timeout=10
            )
            if req.status_code in [200, 201]:
                response = frappe._dict(req.json())
                data = frappe._dict(response.data)
                metadata = frappe._dict(data.metadata)
                frappe.get_doc({
                    'doctype':"Payment Gateway Log",
                    'amount':data.amount/100,
                    'currency':data.currency,
                    'message':response.message,
                    'status':data.status,
                    'payment_gateway_request': metadata.log_id,
                    'reference': data.reference,
                    'reference_doctype': metadata.reference_doctype,
                    'reference_name': metadata.reference_name,
                    'transaction_id': data.id,
                    'data': response
                }).insert(ignore_permissions=True)
            else:
                # log error
                frappe.log_error(str(req.reason), 'Verify Transaction')
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), 'Verify Transaction')

