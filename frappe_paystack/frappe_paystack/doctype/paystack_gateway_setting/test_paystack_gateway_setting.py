# Copyright (c) 2023, Anthony C. Emmanuel and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_paystack.utils import SUPPORTED_CURRENCIES


class TestPaystackGatewaySetting(FrappeTestCase):
	"""Tests for the Paystack Gateway Setting doctype."""

	def test_validate_allows_single_enabled_gateway_per_company(self):
		"""One gateway can be enabled per company without error."""
		setting = self.create_setting(enabled=True)
		self.addCleanup(self.cleanup_setting, setting.name)

		setting.check_enabled()

	def test_validate_throws_when_second_gateway_enabled_for_same_company(self):
		"""Enabling a second gateway for the same company must throw."""
		setting1 = self.create_setting(gateway="Gateway A", enabled=True)
		self.addCleanup(self.cleanup_setting, setting1.name)

		setting2 = self.create_setting(gateway="Gateway B", enabled=True)
		self.addCleanup(self.cleanup_setting, setting2.name)

		with self.assertRaises(frappe.ValidationError):
			setting2.check_enabled()

	def test_validate_allows_disabled_gateway_alongside_enabled(self):
		"""A disabled gateway does not conflict with an enabled one."""
		setting1 = self.create_setting(gateway="Gateway C", enabled=True)
		self.addCleanup(self.cleanup_setting, setting1.name)

		setting2 = self.create_setting(gateway="Gateway D", enabled=False)
		self.addCleanup(self.cleanup_setting, setting2.name)

		setting2.check_enabled()

	def test_validate_transaction_currency_accepts_all_supported(self):
		"""validate_transaction_currency must accept every SUPPORTED_CURRENCIES entry."""
		setting = self.create_setting(enabled=False)
		self.addCleanup(self.cleanup_setting, setting.name)

		for currency in SUPPORTED_CURRENCIES:
			setting.validate_transaction_currency(currency)

	def test_validate_transaction_currency_throws_for_unsupported(self):
		"""validate_transaction_currency must throw for EUR (not supported by Paystack)."""
		setting = self.create_setting(enabled=False)
		self.addCleanup(self.cleanup_setting, setting.name)

		with self.assertRaises(frappe.ValidationError):
			setting.validate_transaction_currency("EUR")

	def test_get_secret_key_returns_plaintext_password(self):
		"""get_secret_key must return the actual secret, not a masked value."""
		setting = self.create_setting(enabled=False, secret="sk_test_my_secret")
		self.addCleanup(self.cleanup_setting, setting.name)

		self.assertEqual(setting.get_secret_key(), "sk_test_my_secret")

	def test_supported_currencies_constant_matches_utils(self):
		"""The doctype's supported_currencies must be the same list as utils."""
		setting = self.create_setting(enabled=False)
		self.addCleanup(self.cleanup_setting, setting.name)

		self.assertEqual(setting.supported_currencies, SUPPORTED_CURRENCIES)
		self.assertIn("KES", setting.supported_currencies)

	def test_get_supported_currency_returns_list(self):
		"""get_supported_currency must return the currency list, not a string."""
		setting = self.create_setting(enabled=False)
		self.addCleanup(self.cleanup_setting, setting.name)

		result = setting.get_supported_currency()
		self.assertIsInstance(result, list)
		self.assertEqual(len(result), len(SUPPORTED_CURRENCIES))

	def test_webhook_secret_stored_as_password_type(self):
		"""The webhook_secret field must be of Password type for security."""
		meta = frappe.get_meta("Paystack Gateway Setting")
		field = meta.get_field("webhook_secret")
		self.assertEqual(field.fieldtype, "Password")

	def test_allowed_webhook_ips_stored_as_small_text(self):
		"""The allowed_webhook_ips field must be Small Text for multi-line input."""
		meta = frappe.get_meta("Paystack Gateway Setting")
		field = meta.get_field("allowed_webhook_ips")
		self.assertEqual(field.fieldtype, "Small Text")

	def test_test_mode_defaults_to_zero(self):
		"""test_mode must default to 0 (unchecked) for safety."""
		meta = frappe.get_meta("Paystack Gateway Setting")
		field = meta.get_field("test_mode")
		self.assertEqual(field.default, "0")

	def test_insert_with_webhook_secret_and_allowed_ips(self):
		"""A gateway with webhook_secret and allowed_webhook_ips must save correctly."""
		setting = frappe.get_doc(
			{
				"doctype": "Paystack Gateway Setting",
				"gateway": "Full Config Gateway",
				"company": "_Test Company",
				"secret_key": "sk_test_123",
				"public_key": "pk_test_123",
				"webhook_secret": "wh_secret_xyz",
				"allowed_webhook_ips": "52.31.139.74\n52.31.139.75",
				"suspense_account": self.get_suspense_account(),
				"mode_of_payment": "Paystack",
				"currency": "NGN",
				"enabled": 0,
			}
		)
		setting.flags.ignore_permissions = True
		setting.insert()
		self.addCleanup(self.cleanup_setting, setting.name)

		self.assertTrue(frappe.db.exists("Paystack Gateway Setting", setting.name))
		self.assertEqual(setting.get("allowed_webhook_ips"), "52.31.139.74\n52.31.139.75")

	def create_setting(
		self, gateway="Test Gateway", enabled=False, secret="sk_test_123"
	):
		"""Create a Paystack Gateway Setting for testing."""
		setting = frappe.get_doc(
			{
				"doctype": "Paystack Gateway Setting",
				"gateway": gateway,
				"company": "_Test Company",
				"secret_key": secret,
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
		return frappe.db.get_value(
			"Account",
			{"company": "_Test Company", "account_type": "Bank"},
			"name",
		)

	def cleanup_setting(self, name):
		if frappe.db.exists("Paystack Gateway Setting", name):
			frappe.delete_doc(
				"Paystack Gateway Setting",
				name,
				force=True,
				ignore_permissions=True,
			)