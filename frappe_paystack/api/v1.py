import frappe

@frappe.whitelist()
def get_payment_request(**kwargs):
    # get payment request data
    try:
        data = frappe.form_dict
        payment_request = frappe.get_doc(data.reference_doctype, data.reference_docname)
        if(payment_request.payment_request_type=='Inward'):
            payment_keys = frappe.get_doc("Paystack Settings", payment_request.payment_gateway)
            return dict(
                key= payment_keys.live_public_key,
    		    email= payment_request.email_to,
    		    amount= payment_request.grand_total * 100,
    		    ref= payment_request.name,
    		    currency= payment_request.currency,
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
        frappe.throw('Invalid Payment')
