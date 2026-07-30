# Copyright (c) 2023, Anthony C. Emmanuel and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_paystack.tests.factories import GatewaySettingFactory
from frappe_paystack.utils import SUPPORTED_CURRENCIES


class TestPaystackGatewaySetting(FrappeTestCase):
	"""Tests for the Paystack Gateway Setting doctype."""

	def setUp(self):
		"""Disable any existing enabled gateways before each test."""
		existing = frappe.get_all(
			"Paystack Gateway Setting",
			filters={"enabled": 1, "company": "_Test Company"},
			pluck="name",
		)
		for name in existing:
			frappe.db.set_value(
				"Paystack Gateway Setting", name, "enabled", 0
			)

	def tearDown(self):
		"""Clean up test gateways."""
		created = frappe.get_all(
			"Paystack Gateway Setting",
			filters={"company": "_Test Company"},
			pluck="name",
		)
		for name in created:
			if name.startswith(("Test", "Gateway", "Full")):
				frappe.delete_doc(
					"Paystack Gateway Setting",
					name,
					force=True,
					ignore_permissions=True,
				)

	def test_validate_allows_single_enabled_gateway_per_company(self):
		"""One gateway can be enabled per company without error."""
		setting_name = GatewaySettingFactory.create(gateway="Gateway Single")
		self.assertIsNotNone(setting_name)

		setting = frappe.get_doc("Paystack Gateway Setting", setting_name)
		setting.check_enabled()

	def test_validate_throws_when_second_gateway_enabled_for_same_company(self):
		"""Enabling a second gateway for the same company must throw."""
		GatewaySettingFactory.create(gateway="Gateway A")

		setting2 = frappe.get_doc(
			{
				"doctype": "Paystack Gateway Setting",
				"gateway": "Gateway B",
				"company": "_Test Company",
				"secret_key": "sk_test_456",
				"public_key": "pk_test_456",
				"suspense_account": GatewaySettingFactory.create.__wrapped__
				if hasattr(GatewaySettingFactory.create, "__wrapped__")
				else None,
				"mode_of_payment": "Paystack",
				"currency": "NGN",
				"enabled": 1,
			}
		)
		setting2.flags.ignore_permissions = True
		setting2.flags.ignore_links = True

		with self.assertRaises(frappe.ValidationError):
			setting2.check_enabled()

	def test_validate_allows_disabled_gateway_alongside_enabled(self):
		"""A disabled gateway does not conflict with an enabled one."""
		GatewaySettingFactory.create(gateway="Gateway C")

		setting2 = frappe.get_doc(
			{
				"doctype": "Paystack Gateway Setting",
				"gateway": "Gateway D",
				"company": "_Test Company",
				"secret_key": "sk_test_789",
				"public_key": "pk_test_789",
				"mode_of_payment": "Paystack",
				"currency": "NGN",
				"enabled": 0,
			}
		)
		setting2.flags.ignore_permissions = True
		setting2.flags.ignore_links = True
		setting2.insert()

		setting2.check_enabled()

	def test_validate_transaction_currency_accepts_all_supported(self):
		"""validate_transaction_currency must accept every SUPPORTED_CURRENCIES entry."""
		setting_name = GatewaySettingFactory.create(gateway="Gateway Currency Test")
		setting = frappe.get_doc("Paystack Gateway Setting", setting_name)

		for currency in SUPPORTED_CURRENCIES:
			setting.validate_transaction_currency(currency)

	def test_validate_transaction_currency_throws_for_unsupported(self):
		"""validate_transaction_currency must throw for EUR (not supported by Paystack)."""
		setting_name = GatewaySettingFactory.create(gateway="Gateway EUR Test")
		setting = frappe.get_doc("Paystack Gateway Setting", setting_name)

		with self.assertRaises(frappe.ValidationError):
			setting.validate_transaction_currency("EUR")

	def test_get_secret_key_returns_plaintext_password(self):
		"""get_secret_key must return the actual secret, not a masked value."""
		setting_name = GatewaySettingFactory.create(gateway="Gateway Secret Test")
		setting = frappe.get_doc("Paystack Gateway Setting", setting_name)

		self.assertEqual(setting.get_secret_key(), "sk_test_123")

	def test_supported_currencies_constant_matches_utils(self):
		"""The doctype's supported_currencies must be the same list as utils."""
		setting_name = GatewaySettingFactory.create(gateway="Gateway Currencies Match")
		setting = frappe.get_doc("Paystack Gateway Setting", setting_name)

		self.assertEqual(setting.supported_currencies, SUPPORTED_CURRENCIES)
		self.assertIn("KES", setting.supported_currencies)

	def test_get_supported_currency_returns_list(self):
		"""get_supported_currency must return the currency list, not a string."""
		setting_name = GatewaySettingFactory.create(gateway="Gateway List Test")
		setting = frappe.get_doc("Paystack Gateway Setting", setting_name)

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
		from frappe_paystack.tests.factories import ensure_mode_of_payment, get_suspense_account

		ensure_mode_of_payment()
		setting = frappe.get_doc(
			{
				"doctype": "Paystack Gateway Setting",
				"gateway": "Full Config Gateway",
				"company": "_Test Company",
				"secret_key": "sk_test_123",
				"public_key": "pk_test_123",
				"webhook_secret": "wh_secret_xyz",
				"allowed_webhook_ips": "52.31.139.74\n52.31.139.75",
				"suspense_account": get_suspense_account(),
				"mode_of_payment": "Paystack",
				"currency": "NGN",
				"enabled": 0,
			}
		)
		setting.flags.ignore_permissions = True
		setting.flags.ignore_links = True
		setting.insert()

		self.assertTrue(frappe.db.exists("Paystack Gateway Setting", setting.name))
		self.assertEqual(
			setting.get("allowed_webhook_ips"),
			"52.31.139.74\n52.31.139.75",
		)