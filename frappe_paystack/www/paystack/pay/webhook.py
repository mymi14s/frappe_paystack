import frappe, requests, json

@frappe.whitelist()
def make_doc(**kwargs):
    print(frappe.form_dict)
    # process_payment(**kwargs)
    frappe.enqueue(method=process_payment, queue='short', timeout=300, **kwargs)


def process_payment(**kwargs):
    print(frappe.form_dict)
    reference = kwargs.get('reference')
    integration_request = frappe.get_doc("Integration Request", reference)
    try:
        response = verify_transaction(integration_request, reference)
        # frappe.log_error(str(integration_request.as_dict()))
        # create doc
        print(response)
        if(response and response.get('status') and response.get('message')=='Verification successful'):
            data = response.get('data')
            payload = {
            'doctype': 'Paystack Payment Request',
            'reference' : data.get('reference'),
            'transaction_id' : data.get('id'),
            'amount' : data.get('amount')/100,
            'event' : 'charge.success',
            'order_id': data.get('metadata').get('order_id'),
            'paid_at' : data.get('paid_at'),
            'created_at' : data.get('created_at'),
            'currency' : data.get('currency'),
            'channel' : data.get('channel'),
            'reference_doctype' : data.get('metadata').get('reference_doctype'),
            'reference_docname' : data.get('metadata').get('reference_docname'),
            'gateway' : data.get('metadata').get('gateway'),
            'customer_email' : response.get('data').get('customer').get('email'),
            'signature': response.get('data').get('authorization').get('signature'),
            'data': json.dumps(data),
            }

            integration_data = frappe._dict(json.loads(integration_request.data))
            print(integration_data)
            if(round(integration_data.amount)==round(payload.get('amount'))):
                # payload.update({'amount' : data.get('amount')/100})
                doc = frappe.get_doc(payload)
                doc.insert(ignore_permissions=True)
            # docment created
            # complete_payment
                complete_payment('Completed', integration_request)
            else:
                complete_payment('Failed', integration_request)
        else:
            complete_payment('Failed', integration_request)
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), e)
    return None


def verify_transaction(gateway, reference):
    try:
        gateway = frappe.get_doc("Paystack Settings", gateway.integration_request_service)
        headers = {'Authorization': f"Bearer {gateway.get_password(fieldname='live_secret_key', raise_exception=False)}"}
        url = f"https://api.paystack.co/transaction/verify/{reference}"
        res = requests.get(url, headers=headers)
        # print(res.status_code, res.json(), 'status_code')
        resjson = res.json()
        if(res.status_code == 200):
            return res.json()
        else:
            return False
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), e)
        print(str(e))
        return False

def complete_payment(status, integration_request):
    try:
        payment_request = frappe.get_doc(integration_request.reference_doctype, integration_request.reference_docname)
        payment_request.run_method("on_payment_authorized", status)
        integration_request.db_set('status', status)
        frappe.db.commit()
    except Exception:
        integration_request.db_set('status', 'Failed')
        frappe.log_error(frappe.get_traceback())

    return


# def get_context(context):
#     data = frappe.form_dict
#     print(context, "\n\n\n")
#     print(data, "\n\n\n")
#     print(frappe.locals.request, "\n\n\n")
#     # context.payment_data = data
#     return context
