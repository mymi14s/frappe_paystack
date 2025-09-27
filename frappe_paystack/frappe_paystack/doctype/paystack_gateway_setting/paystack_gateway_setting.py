# Copyright (c) 2024, Anthony C. Emmanuel and contributors
# For license information, please see license.txt

from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.utils import call_hook_method, get_url
from frappe.model.document import Document

class PaystackGatewaySetting(Document):
	supported_currencies = ['NGN', 'GHS', 'ZAR', 'USD']
	
	def validate(self):
		self.check_enabled()

	def get_secret_key(self):
		return self.get_password('secret_key')

	def check_enabled(self):
		"""
			Ensure only one gateway is enabled for each gate type.
		"""
		if self.enabled:
			enabled_gateway = frappe.db.get_list(self.doctype, filters={
				"enabled":1,
				"company":self.company,
				"name":["!=", self.name]
			},
			fields=["name"])
			if enabled_gateway:
				frappe.throw(f"""
					Another gateway is enabled, disable it before enabling this one.<br>
					<a class="text-danger" href="/app/{self.doctype.lower().replace(' ', '-')}/{enabled_gateway[0].name}">{enabled_gateway[0].name}</a>
				""")
	def validate_transaction_currency(self, currency):
		if currency not in self.supported_currencies:
			frappe.throw(
				_(
					"Please select another payment method. Paystack does not support transactions in currency '{0}'"
				).format(currency)
			)
	
	def get_supported_currency(self):
		return self.supported_currencies
	
	def get_payment_url(self, **kwargs):
		return get_url(f"./paystack_checkout?{urlencode(kwargs)}")


