import frappe, requests, json, hmac, math, hashlib
from frappe_paystack.utils import (
    compute_received_hash, getip, is_paystack_ip,
    generate_digest,
)

@frappe.whitelist(allow_guest=True)
def get_payment_request(**kwargs):
    # get payment request data
    try:
        data = frappe.form_dict
        
        payment_request = frappe.get_doc(data.reference_doctype, data.reference_docname)
        paystack_gateway = frappe.get_doc("Payment Gateway", payment_request.payment_gateway)
        paystack = frappe.get_doc("Paystack Settings", paystack_gateway.gateway_controller)
        if(payment_request.payment_request_type=='Inward'):
            ecommerce = frappe.get_single("E Commerce Settings")
            return dict(
                payment_request=payment_request,
                name=payment_request.name,
    		    email = payment_request.email_to,
    		    currency= payment_request.currency,
                status=payment_request.status,
                public_key = paystack.get_public_key(),
    		    metadata={
    				'doctype': payment_request.doctype,
    				'docname': payment_request.name,
                    'reference_doctype': payment_request.reference_doctype,
                    'reference_name': payment_request.reference_name,
                    'gateway': payment_request.payment_gateway,
    	    	}
            )
        else:
            frappe.throw('Only Inward payment allowed.')
    except Exception as e:
        frappe.log_error(str(e), 'Paystack')
        frappe.throw('Invalid Payment')


@frappe.whitelist(allow_guest=True)
def verify_transaction(transaction):
    frappe.enqueue(queue_verify_transaction, transaction=transaction)

def queue_verify_transaction(transaction):
    # check the authenticity of transaction
    try:
        transaction = frappe._dict(json.loads(transaction))
        gateway = frappe.get_doc("Paystack Settings", transaction.gateway)
        secret_key = gateway.get_secret_key()
        headers = {"Authorization": f"Bearer {secret_key}"}
        req = requests.get(
            f"https://api.paystack.co/transaction/verify/{transaction.reference}",
            headers=headers, timeout=10
        )
        if req.status_code in [200, 201]:
            response = frappe._dict(req.json())
            data = frappe._dict(response.data)
            metadata = frappe._dict(data.metadata)
            if not frappe.db.exists("Paystack Log", {'name':data.reference}):
                frappe.get_doc({
                    'doctype':"Paystack Log",
                    'amount':data.amount/100,
                    'currency':data.currency,
                    'message':response.message,
                    'status':data.status,
                    'reference': data.reference,
                    'payment_request': metadata.docname,
                    'reference_doctype': metadata.reference_doctype,
                    'reference_name': metadata.reference_name,
                    'transaction_id': data.id,
                    'data': response
                }).insert(ignore_permissions=True)
                frappe.db.commit()
                # clear payment
                payment_request = frappe.get_doc('Payment Request', metadata.docname)
                integration_request = frappe.get_doc("Integration Request", {
                    'reference_doctype':metadata.doctype,
                    'reference_docname':metadata.docname})
                payment_request.run_method("on_payment_authorized", 'Completed')
                integration_request.db_set('status', 'Completed')
                frappe.db.commit()
        else:
            # log error
            frappe.log_error(str(req.reason), 'Verify Transaction')
    except Exception as e:
        frappe.log_error(frappe.get_traceback()+ str(frappe.form_dict), 'Verify Transaction')



@frappe.whitelist(allow_guest=True)
def webhook(**kwargs):
    """
        End point where payment gateway sends payment info.
    """
    try:
        transaction = frappe.form_dict
        data = frappe._dict(json.loads(transaction.data))
        metadata = frappe._dict(data.metadata)
        gateway = frappe.get_doc("Paystack Settings", metadata.gateway)
        secret_key = gateway.get_secret_key()
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
                'doctype':"Paystack Log",
                'amount':data.amount/100,
                'currency':data.currency,
                'message':response.message,
                'status':data.status,
                'reference': data.reference,
                'payment_request': metadata.docname,
                'reference_doctype': metadata.reference_doctype,
                'reference_name': metadata.reference_name,
                'transaction_id': data.id,
                'data': response
            }).insert(ignore_permissions=True)
            # clear payment
            frappe.db.commit()
            payment_request = frappe.get_doc('Payment Request', metadata.docname)
            integration_request = frappe.get_doc("Integration Request", {
                'reference_doctype':metadata.doctype,
                'reference_docname':metadata.docname})
            payment_request.run_method("on_payment_authorized", 'Completed')
            integration_request.db_set('status', 'Completed')
            frappe.db.commit()
        else:
            # log error
            frappe.log_error(str(req.reason), 'Verify Transaction')
    except Exception as e:
        frappe.log_error(frappe.get_traceback() + str(frappe.form_dict), 'Verify Transaction')