import requests, json
import frappe

@frappe.whitelist(allow_guest=True)
def webhook(**args):
    # print(frappe.form_dict)
    make_doc(args)
    return 200


def make_doc(request):
    # print(request)
    data = request.get('data')
    try:
        payload = {
            'doctype': 'Paystack Payment Request',
            'reference' : data.get('reference'),
            'transaction_id' : data.get('id'),
            'amount' : data.get('amount'),
            'event' : request.get('event'),
            'order_id': data.get('metadata').get('order_id'),
            'paid_at' : data.get('paid_at'),
            'created_at' : data.get('created_at'),
            'currency' : data.get('currency'),
            'channel' : data.get('channel'),
            'reference_doctype' : data.get('metadata').get('reference_doctype'),
            'reference_docname' : data.get('metadata').get('reference_docname'),
            'gateway' : data.get('metadata').get('gateway'),
            'customer_email' : request.get('data').get('customer').get('email'),
            'signature': request.get('data').get('authorization').get('signature'),
            'data': json.dumps(data),
        }
        # sk = 'sk_test_91d6c3c33f4fc332d2f6b85726735ec972d8eab9'
        response = verify_transaction(payload)
        # create doc
        if(response):
            payload.update({'amount' : data.get('amount')/100})
            doc = frappe.get_doc(payload)
            doc.insert(ignore_permissions=True)
            # docment created
            # complete_payment
            complete_payment(payload, 'Completed')
        else:
            complete_payment(payload, 'Failed')

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
