import json, hmac, math, hashlib, requests, frappe
from frappe_paystack.utils import (
    compute_received_hash, getip, is_paystack_ip,
    generate_digest,
)

@frappe.whitelist(allow_guest=True)
def webhook(*args, **kwargs):
    # print('FORM DICT \n\n',frappe.form_dict)
    form_dict = frappe.form_dict
    paystack_signature = frappe.get_request_header("x-paystack-signature")
    from_ip = frappe.local.request_ip

    make_doc(paystack_signature, from_ip, form_dict)
    return {'status_code':200}


def make_doc(paystack_signature, from_ip, form_dict):
    # print(request)
    data = form_dict.data
    try:
        integration_request = frappe.get_doc(
            "Integration Request",
            data.get('reference'))
        if not (integration_request.status=='Completed'):
            payload = {
                'doctype': 'Paystack Payment Request',
                'reference' : data.get('reference'),
                'transaction_id' : data.get('id'),
                'amount' : data.get('amount'),
                'event' : form_dict.get('event'),
                'order_id': data.get('metadata').get('order_id'),
                'paid_at' : data.get('paid_at'),
                'created_at' : data.get('created_at'),
                'currency' : data.get('currency'),
                'channel' : data.get('channel'),
                'reference_doctype' : data.get('metadata').get('reference_doctype'),
                'reference_docname' : data.get('metadata').get('reference_docname'),
                'gateway' : data.get('metadata').get('gateway'),
                'customer_email' : form_dict.get('data').get('customer').get('email'),
                'signature': form_dict.get('data').get('authorization').get('signature'),
                'data': json.dumps(data),
            }
            # validate ip
            gateway = frappe.get_doc("Paystack Settings", payload.get('gateway'))
            paystack_ip = is_paystack_ip(gateway, from_ip)
            is_same_hash = compute_received_hash(gateway.get_password(fieldname='live_secret_key', raise_exception=False),
                form_dict.data)
            # digest = generate_digest(form_dict.data,
            #     gateway.get_password(fieldname='live_secret_key', raise_exception=False))
            # print(f"P: {paystack_signature}, IS: {is_same_hash} \n\n")

            if(paystack_ip):
                response = verify_transaction(payload)
                # create doc
                if(response):
                    complete_payment(payload, 'Completed')
                    payload.update({'amount' : data.get('amount')/100})
                    doc = frappe.get_doc(payload)
                    doc.insert(ignore_permissions=True)
                    # docment created
                    # complete_payment
                else:
                    complete_payment(payload, 'Failed')
            else:
                # not paystack ip
                frappe.log_error("Invalid paystack ip\n"+str(payload), 'SUSPICIOUS PAYMENT')
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), e)
    return None

def verify_transaction(payload):
    try:
        gateway = frappe.get_doc("Paystack Settings", payload.get('gateway'))
        headers = {'Authorization': f"Bearer {gateway.get_password(fieldname='live_secret_key', raise_exception=False)}"}
        url = f"https://api.paystack.co/transaction/verify/{payload.get('reference')}"
        res = requests.get(url, headers=headers)
        resjson = res.json()
        if(res.status_code == 200):

            status = resjson.get('data').get('status')
            amount = resjson.get('data').get('amount')
            signature = resjson.get('data').get('authorization').get('signature')
            reference = resjson.get('data').get('reference')

            if(
                status == 'success' and amount == payload.get('amount')
                and signature == payload.get('signature') and
                reference == payload.get('reference')):
                return True
            else:return False
        return False

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), e)
        return False

def complete_payment(payload, status):
    integration_request = frappe.get_doc("Integration Request", payload.get('reference'))
    try:
        payment_request = frappe.get_doc(integration_request.reference_doctype, integration_request.reference_docname)
        payment_request.run_method("on_payment_authorized", status)
        integration_request.db_set('status', status)
        frappe.db.commit()
    except Exception:
        integration_request.db_set('status', 'Failed')
        frappe.log_error(frappe.get_traceback())

    return
