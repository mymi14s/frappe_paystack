# Copyright (c) 2023, Anthony C. Emmanuel and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_paystack.utils import SUPPORTED_CURRENCIES


class TestPaystackGatewaySetting(FrappeTestCase):
	"""Tests for the Paystack Gateway Setting doctype."""

	def test_meta_exists(self):
		"""The doctype metadata must be loadable."""
		self.assertTrue(frappe.get_meta("Paystack Gateway Setting"))

	def test_supported_currencies_matches_utils(self):
		"""The doctype must use the same currency list as utils.SUPPORTED_CURRENCIES."""
		setting = frappe.get_doc(
			{
				"doctype": "Paystack Gateway Setting",
				"gateway": "Test Gateway",
				"company": "_Test Company",
				"secret_key": "sk_test_123",
				"public_key": "pk_test_123",
				"suspense_account": self.get_suspense_account(),
				"mode_of_payment": "Paystack",
				"currency": "NGN",
			}
		)
		self.assertEqual(setting.supported_currencies, SUPPORTED_CURRENCIES)

	def test_validate_transaction_currency_supported(self):
		"""validate_transaction_currency must not throw for supported currencies."""
		setting = self.create_setting(enabled=False)
		self.addCleanup(self.cleanup_setting, setting.name)

		for currency in ["NGN", "USD", "GHS", "ZAR", "KES"]:
			setting.validate_transaction_currency(currency)

	def test_validate_transaction_currency_unsupported(self):
		"""validate_transaction_currency must throw for unsupported currencies."""
		setting = self.create_setting(enabled=False)
		self.addCleanup(self.cleanup_setting, setting.name)

		with self.assertRaises(frappe.ValidationError):
			setting.validate_transaction_currency("EUR")

	def test_check_enabled_prevents_duplicate(self):
		"""Only one gateway can be enabled per company."""
		setting1 = self.create_setting(enabled=True)
		self.addCleanup(self.cleanup_setting, setting1.name)

		setting2 = self.create_setting(
			gateway="Test Gateway 2", enabled=True
		)
		with self.assertRaises(frappe.ValidationError):
			setting2.check_enabled()

	def test_get_secret_key(self):
		"""get_secret_key must return the password value."""
		setting = self.create_setting(enabled=False)
		self.addCleanup(self.cleanup_setting, setting.name)

		self.assertEqual(setting.get_secret_key(), "sk_test_123")

	def test_get_supported_currency(self):
		"""get_supported_currency must return the currency list."""
		setting = self.create_setting(enabled=False)
		self.addCleanup(self.cleanup_setting, setting.name)

		self.assertEqual(setting.get_supported_currency(), SUPPORTED_CURRENCIES)

	def test_webhook_secret_field_exists(self):
		"""The webhook_secret field must exist on the doctype (Phase 1)."""
		meta = frappe.get_meta("Paystack Gateway Setting")
		self.assertTrue(meta.has_field("webhook_secret"))

	def test_test_mode_field_exists(self):
		"""The test_mode field must exist on the doctype (Phase 1)."""
		meta = frappe.get_meta("Paystack Gateway Setting")
		self.assertTrue(meta.has_field("test_mode"))

	def test_allowed_webhook_ips_field_exists(self):
		"""The allowed_webhook_ips field must exist on the doctype (Phase 1)."""
		meta = frappe.get_meta("Paystack Gateway Setting")
		self.assertTrue(meta.has_field("allowed_webhook_ips"))

	def create_setting(self, gateway="Test Gateway", enabled=False):
		"""Create a Paystack Gateway Setting for testing."""
		setting = frappe.get_doc(
			{
				"doctype": "Paystack Gateway Setting",
				"gateway": gateway,
				"company": "_Test Company",
				"secret_key": "sk_test_123",
				"public_key": "pk_test_123",
				"suspense_account": self.get_suspense_account(),
				"mode_of_payment": "Paystack",
				"currency": "NGN",
				"enabled": 1 if enabled else 0,
			}
		)
		setting.flags.ignore_permissions = True
		setting.insert()
		return setting

	def get_suspense_account(self):
		"""Return a valid account for _Test Company."""
		return frappe.db.get_value(
			"Account",
			{"company": "_Test Company", "account_type": "Bank"},
			"name",
		)

	def cleanup_setting(self, name):
		"""Delete a Paystack Gateway Setting if it exists."""
		if frappe.db.exists("Paystack Gateway Setting", name):
			frappe.delete_doc(
				"Paystack Gateway Setting",
				name,
				force=True,
				ignore_permissions=True,
			)