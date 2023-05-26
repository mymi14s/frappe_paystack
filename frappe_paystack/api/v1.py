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
        if(payment_request.payment_request_type=='Inward'):
            ecommerce = frappe.get_single("E Commerce Settings")
            return dict(
                payment_request=payment_request,
                name=payment_request.name,
    		    email = payment_request.email_to,
    		    currency= payment_request.currency,
                status=payment_request.status,
                public_key = frappe.get_doc("Paystack Settings", frappe.get_doc("Payment Gateway", payment_request.payment_gateway).gateway_controller).live_public_key,
    		    metadata={
    				'doctype': payment_request.doctype,
    				'docname': payment_request.name,
                    'reference_doctype': payment_request.reference_doctype,
                    'reference_name': payment_request.reference_name,
    	    	}
            )
        else:
            frappe.throw('Only Inward payment allowed.')
    except Exception as e:
        frappe.throw('Invalid Payment')



@frappe.whitelist(allow_guest=True)
def webhook(**kwargs):
    form_dict = frappe.form_dict
    from_ip = frappe.local.request_ip
    v = verify_transaction(dict(
        gateway=frappe.form_dict.data['metadata']['gateway'],
        reference=frappe.form_dict.data['reference']
    ))


def verify_transaction(payload):
    try:
        gateway = frappe.get_doc("Paystack Settings", payload.get('gateway'))
        headers = {'Authorization': f"Bearer {gateway.get_password(fieldname='live_secret_key', raise_exception=False)}"}
        url = f"https://api.paystack.co/transaction/verify/{payload.get('reference')}"
        res = requests.get(url, headers=headers, timeout=60)
        resjson = res.json()
        if(res.status_code == 200):
            status = resjson.get('data').get('status')
            amount = resjson.get('data').get('amount')
            reference = resjson.get('data').get('reference')
            payment_request = frappe.get_doc('Payment Request', reference)
            if(status == 'success' and amount/100 == payment_request.grand_total):
                # make payment
                integration_request_query = frappe.db.sql(f"""
                    SELECT name FROM `tabIntegration Request`
                    WHERE reference_doctype="{payment_request.doctype}"
                    AND reference_docname="{payment_request.name}"
                    ORDER BY modified DESC
                """, as_dict=1)
                if(integration_request_query):
                    integration_request = frappe.get_doc("Integration Request", integration_request_query[0].name)
                    payment_request.run_method("on_payment_authorized", 'Completed')
                    integration_request.db_set('status', 'Completed')
                    # create log
                    create_log(resjson)
                    return True
                        # integration_request.db_set('status', 'Failed')
                else:
                    return False
            else:return False
        return False
    #
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), 'Paystack Payment')
        return False

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
