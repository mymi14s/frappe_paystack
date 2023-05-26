import frappe, requests, json, hmac, math, hashlib
from frappe_paystack.utils import (
    compute_received_hash, getip, is_paystack_ip,
    generate_digest,
)

@frappe.whitelist()
def get_payment_request(**kwargs):
    # get payment request data
    try:
        data = frappe.form_dict
        payment_request = frappe.get_doc(data.reference_doctype, data.reference_docname)
        paystack_setting = frappe.get_doc("Payment Gateway", payment_request.payment_gateway)
        if(payment_request.payment_request_type=='Inward'):
            ecommerce = frappe.get_single("E Commerce Settings")
            return dict(
                payment_request=payment_request,
                name=payment_request.name,
    		    email = payment_request.email_to,
    		    currency= payment_request.currency,
                status=payment_request.status,
                public_key = frappe.get_doc("Paystack Settings", paystack_setting.gateway_controller).live_public_key,
    		    metadata={
    				'doctype': payment_request.doctype,
    				'docname': payment_request.name,
                    'reference_doctype': payment_request.reference_doctype,
                    'reference_name': payment_request.reference_name,
                    'gateway': paystack_setting.gateway_controller,
    	    	}
            )
        else:
            frappe.throw('Only Inward payment allowed.')
    except Exception as e:
        frappe.throw('Invalid Payment')



# @frappe.whitelist(allow_guest=True)
# def webhook(**kwargs):
#     form_dict = frappe.form_dict
#     from_ip = frappe.local.request_ip
#     v = verify_transaction(dict(
#         gateway=frappe.form_dict.data['metadata']['gateway'],
#         reference=frappe.form_dict.data['reference']
#     ))


def create_log(resjson):
    try:
        data = resjson['data']
        payload = {
            'doctype': 'Paystack Payment Request',
            'reference' : data.get('reference'),
            'transaction_id' : data.get('id'),
            'amount' : data.get('amount'),
            'event' : data.get('status'),
            'order_id': data.get('reference'),
            'paid_at' : data.get('paid_at'),
            'created_at' : data.get('created_at'),
            'currency' : data.get('currency'),
            'channel' : data.get('channel'),
            'reference_doctype' : data.get('metadata').get('reference_doctype'),
            'reference_docname' : data.get('metadata').get('reference_name'),
            'gateway' : data.get('metadata').get('gateway'),
            'customer_email' : data.get('customer').get('email'),
            'signature': data.get('authorization').get('signature'),
            'data': json.dumps(data),
        }
        doc = frappe.get_doc(payload)
        doc.insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), 'Paystack log')



@frappe.whitelist(allow_guest=True)
def verify_transaction(transaction):
#     frappe.enqueue(queue_verify_transaction, transaction=transaction)

# def queue_verify_transaction(transaction):
    # check the authenticity of transaction
    # print(transaction)
    try:
        transaction = frappe._dict(json.loads(transaction))
        gateway = frappe.get_doc("Paystack Settings", transaction.gateway)
        secret_key = gateway.get_secret_key()
        headers = {"Authorization": f"Bearer {secret_key}"}
        print(transaction.reference, secret_key)
        req = requests.get(
            f"https://api.paystack.co/transaction/verify/{transaction.reference}",
            headers=headers, timeout=10
        )
        if req.status_code in [200, 201]:
            response = frappe._dict(req.json())
            print(response)
            data = frappe._dict(response.data)
            metadata = frappe._dict(data.metadata)
            frappe.get_doc({
                'doctype':"Paystack Log",
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
            # clear payment
            payment_request = frappe.get_doc('Payment Request', metadata.docname)
            integration_request = frappe.get_doc("Integration Request", {
                'reference_doctype':metadata.doctype,
                'reference_docname':metadata.docname})
            payment_request.run_method("on_payment_authorized", 'Completed')
            integration_request.db_set('status', 'Completed')
        else:
            # log error
            print(req.reason, req.text)
            frappe.log_error(str(req.reason), 'Verify Transaction')
    except Exception as e:
        print(frappe.get_traceback())
        frappe.log_error(frappe.get_traceback(), 'Verify Transaction')



@frappe.whitelist(allow_guest=True)
def webhook(**kwargs):
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

