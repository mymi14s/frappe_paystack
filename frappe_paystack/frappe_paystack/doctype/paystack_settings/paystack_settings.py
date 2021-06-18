# Copyright (c) 2021, Anthony Emmanuel (Ghorz.com) and contributors
# For license information, please see license.txt


from __future__ import unicode_literals
import frappe
from frappe import _
import json
import hmac
import razorpay
import hashlib
from six.moves.urllib.parse import urlencode
from frappe.model.document import Document
from frappe.utils import get_url, call_hook_method, cint, get_timestamp
from frappe.integrations.utils import (make_get_request, make_post_request, create_request_log,
	create_payment_gateway)


class PaystackSettings(Document):

	supported_currencies = ["NGN", "USD", "GHS", "ZAR"]

	def on_insert(self):
		doc = frappe.new_doc("Payment Gateway")
		doc.gateway_settings = "Paystack Settings"
		doc.gateway_controller = self.name
		doc.gateway = self.name
		doc.save(ignore_permisions=True)


	def validate(self):
		create_payment_gateway('Paystack')
		call_hook_method('payment_gateway_enabled', gateway='Paystack')

	def validate_transaction_currency(self, currency):
		if currency not in self.supported_currencies:
			frappe.throw(_("Please select another payment method. Paystack does not support transactions in currency '{0}'").format(currency))

	def get_payment_url(self, **kwargs):
		'''Return payment url with several params'''
		# create unique order id by making it equal to the integration request
		integration_request = create_request_log(kwargs, "Host", "Paystack")
		kwargs.update(dict(order_id=integration_request.name))
		# add payment gateway name
		kwargs.update({'gateway':self.name})

		return get_url("./paystack/pay?{0}".format(urlencode(kwargs)))



@frappe.whitelist()
def get_payment_info(data):
	try:
		data = clean_data(data)
		# print(data)
		payment_keys = frappe.get_doc("Paystack Settings", data.get('gateway'))
		payment_request = frappe.get_doc(data.get('reference_doctype'), data.get('reference_docname'))
		# print(payment_request.as_dict())
		order = frappe.get_doc(payment_request.reference_doctype, payment_request.reference_name)
		customer = frappe.get_doc("Customer", order.customer)
		payload = {
			'key': payment_keys.live_public_key,
		    'email': data.get('payer_email'),
		    'amount': payment_request.grand_total * 100,
		    'ref': data.get('order_id'),
		    'currency': payment_request.currency,
		    'metadata':{
				'reference_doctype': payment_request.reference_doctype,
				'reference_docname': payment_request.reference_name,
				'gateway': data.get('gateway'),
				'order_id': data.get('order_id')
	    	}
		}
		result = {
			'data': {
				'payload': payload,
				'cart':data,
			},
		'status': 200
		}
	except Exception as e:
		result = {
			'status': 400,
			'error': str(e)
		}
	return result

def clean_data(data):
	try:
		split_first  = data.split(',')
		split_first[0] = split_first[0].replace('{', '')
		split_first[-1] = split_first[-1].replace('}', '')
		# print(split_first)
		# make dict
		result = {}
		for i in split_first:
			i = i.replace(" '", '').replace("'", '')
			_d = i.split(':')
			# print("_d", _d)
			result[_d[0]] = _d[1]
	except Exception as e:
		result = str(e)
	return result

# webhook
@frappe.whitelist(allow_guest=True)
def webhook(request):
	print(request)
