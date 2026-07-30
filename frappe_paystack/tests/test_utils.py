import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase

from frappe_paystack.tests.factories import GatewaySettingFactory
from frappe_paystack.utils import (
	SUPPORTED_CURRENCIES,
	clamp_amount_to_positive,
	coalesce_currency,
	ensure_supported_currency,
	from_minor_units,
	hmac_sha512,
	is_ip_allowed,
	is_paystack_enabled,
	log_integration_request,
	normalize_currency,
	parse_reference_company,
	resolve_paystack_settings,
	safe_json_dumps,
	sanitize_reference,
	to_minor_units,
	verify_signature,
)


class TestPaystackCurrency(FrappeTestCase):
	"""Tests for currency normalization, conversion, and coalescing."""

	def test_normalize_currency_lowercases_and_strips(self):
		self.assertEqual(normalize_currency("usd"), "USD")
		self.assertEqual(normalize_currency(" ghs "), "GHS")

	def test_normalize_currency_unsupported_falls_back_to_ngn(self):
		self.assertEqual(normalize_currency("EUR"), "NGN")
		self.assertEqual(normalize_currency(None), "NGN")
		self.assertEqual(normalize_currency(""), "NGN")

	def test_to_minor_units_converts_decimal_to_kobo(self):
		self.assertEqual(to_minor_units(100.0, "NGN"), 10000)
		self.assertEqual(to_minor_units(99.99, "NGN"), 9999)

	def test_to_minor_units_handles_all_supported_currencies(self):
		for currency in SUPPORTED_CURRENCIES:
			result = to_minor_units(1.0, currency)
			self.assertEqual(result, 100, f"Failed for {currency}")

	def test_from_minor_units_reverses_to_minor_units(self):
		for amount in [0.01, 1.0, 99.99, 1000.0]:
			minor = to_minor_units(amount, "NGN")
			result = from_minor_units(minor, "NGN")
			self.assertAlmostEqual(result, amount, places=2)

	def test_coalesce_currency_prefers_explicit_over_company_and_settings(self):
		settings = {"default_currency": "GHS"}
		self.assertEqual(coalesce_currency("USD", "_Test Company", settings), "USD")

	def test_coalesce_currency_falls_back_to_settings_when_no_explicit(self):
		settings = {"default_currency": "GHS"}
		self.assertEqual(coalesce_currency(None, None, settings), "GHS")

	def test_coalesce_currency_falls_back_to_ngn_when_nothing_provided(self):
		self.assertEqual(coalesce_currency(None, None, None), "NGN")

	def test_ensure_supported_currency_throws_for_unsupported(self):
		with self.assertRaises(frappe.ValidationError):
			ensure_supported_currency("EUR")

	def test_ensure_supported_currency_accepts_all_supported(self):
		for currency in SUPPORTED_CURRENCIES:
			ensure_supported_currency(currency)


class TestPaystackSignature(FrappeTestCase):
	"""Tests for HMAC-SHA512 signature verification used by the webhook."""

	def test_verify_signature_accepts_correct_hmac(self):
		secret = "sk_test_abc123"
		payload = b'{"event":"charge.success","data":{"id":12345}}'
		signature = hmac.new(
			secret.encode("utf-8"), payload, hashlib.sha512
		).hexdigest()
		self.assertTrue(verify_signature(payload, signature, secret))

	def test_verify_signature_rejects_tampered_payload(self):
		secret = "sk_test_abc123"
		payload = b'{"event":"charge.success"}'
		signature = hmac_sha512(payload, secret)
		tampered = b'{"event":"charge.failed"}'
		self.assertFalse(verify_signature(tampered, signature, secret))

	def test_verify_signature_rejects_wrong_secret(self):
		payload = b"payload"
		signature = hmac_sha512(payload, "correct_secret")
		self.assertFalse(verify_signature(payload, signature, "wrong_secret"))

	def test_verify_signature_rejects_missing_signature(self):
		self.assertFalse(verify_signature(b"payload", None, "secret"))
		self.assertFalse(verify_signature(b"payload", "", "secret"))

	def test_hmac_sha512_matches_python_stdlib(self):
		secret = "test"
		payload = b"data"
		expected = hmac.new(
			secret.encode("utf-8"), payload, hashlib.sha512
		).hexdigest()
		self.assertEqual(hmac_sha512(payload, secret), expected)


class TestPaystackIpAllowlist(FrappeTestCase):
	"""Tests for webhook IP allowlisting."""

	def test_empty_allowlist_allows_any_ip(self):
		self.assertTrue(is_ip_allowed(None, "1.2.3.4"))
		self.assertTrue(is_ip_allowed("", "10.0.0.1"))

	def test_ip_in_allowlist_is_allowed(self):
		allowed = "52.31.139.74\n52.31.139.75"
		self.assertTrue(is_ip_allowed(allowed, "52.31.139.74"))
		self.assertTrue(is_ip_allowed(allowed, "52.31.139.75"))

	def test_ip_not_in_allowlist_is_rejected(self):
		self.assertFalse(is_ip_allowed("52.31.139.74", "1.1.1.1"))

	def test_allowlist_strips_whitespace_and_blank_lines(self):
		allowed = "  52.31.139.74  \n\n  52.31.139.75  \n"
		self.assertTrue(is_ip_allowed(allowed, "52.31.139.74"))
		self.assertTrue(is_ip_allowed(allowed, "52.31.139.75"))

	def test_no_request_ip_with_allowlist_is_rejected(self):
		self.assertFalse(is_ip_allowed("52.31.139.74", None))


class TestPaystackSerialization(FrappeTestCase):
	"""Tests for safe_json_dumps used in Integration Request logging."""

	def test_safe_json_dumps_serializes_dict(self):
		result = safe_json_dumps({"key": "value", "num": 42})
		parsed = json.loads(result)
		self.assertEqual(parsed["key"], "value")
		self.assertEqual(parsed["num"], 42)

	def test_safe_json_dumps_handles_nested_structures(self):
		result = safe_json_dumps({"data": {"list": [1, 2, 3]}})
		parsed = json.loads(result)
		self.assertEqual(parsed["data"]["list"], [1, 2, 3])

	def test_safe_json_dumps_returns_empty_on_failure(self):
		self.assertEqual(safe_json_dumps(object()), "{}")


class TestPaystackReference(FrappeTestCase):
	"""Tests for reference encoding/decoding used in payment metadata."""

	def test_sanitize_reference_encodes_doctype_docname_company(self):
		ref = sanitize_reference("Sales Invoice", "ACC-SINV-2024-001", "My Company")
		self.assertEqual(ref, "Sales Invoice-ACC-SINV-2024-001-MyCompany")

	def test_sanitize_reference_handles_none_company(self):
		ref = sanitize_reference("Sales Order", "SO-001", None)
		self.assertEqual(ref, "Sales Order-SO-001-")

	def test_parse_reference_company_round_trips(self):
		company = "TestCompanyLtd"
		ref = sanitize_reference("Sales Invoice", "ACC_SINV_001", company)
		self.assertEqual(parse_reference_company(ref), company)

	def test_parse_reference_company_returns_none_for_malformed(self):
		self.assertIsNone(parse_reference_company("invalid"))
		self.assertIsNone(parse_reference_company(""))


class TestPaystackAmount(FrappeTestCase):
	"""Tests for amount clamping utility."""

	def test_clamp_positive_unchanged(self):
		self.assertEqual(clamp_amount_to_positive(100.0), 100.0)

	def test_clamp_negative_becomes_zero(self):
		self.assertEqual(clamp_amount_to_positive(-50.0), 0.0)

	def test_clamp_rounds_to_two_decimals(self):
		self.assertEqual(clamp_amount_to_positive(99.999), 100.0)
		self.assertEqual(clamp_amount_to_positive(10.005), 10.01)


class TestPaystackSettingsResolution(FrappeTestCase):
	"""Tests for resolving Paystack Gateway Settings for a company."""

	def setUp(self):
		"""Clean up any existing enabled gateways before each test."""
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
		"""Clean up any gateways created during the test."""
		created = frappe.get_all(
			"Paystack Gateway Setting",
			filters={"company": "_Test Company"},
			pluck="name",
		)
		for name in created:
			if name.startswith("Test"):
				frappe.delete_doc(
					"Paystack Gateway Setting",
					name,
					force=True,
					ignore_permissions=True,
				)

	def test_is_paystack_enabled_returns_false_when_no_gateway(self):
		self.assertFalse(is_paystack_enabled("_Test Company"))

	def test_resolve_paystack_settings_returns_none_when_no_gateway(self):
		self.assertIsNone(resolve_paystack_settings("_Test Company"))

	def test_resolve_paystack_settings_returns_dict_when_gateway_enabled(self):
		setting_name = GatewaySettingFactory.create()
		self.addCleanup(GatewaySettingFactory.cleanup, setting_name)

		settings = resolve_paystack_settings("_Test Company")
		self.assertIsNotNone(settings)
		self.assertEqual(settings["public_key"], "pk_test_123")
		self.assertEqual(settings["secret_key"], "sk_test_123")
		self.assertTrue(settings["test_mode"] is False)

	def test_resolve_paystack_settings_uses_webhook_secret_when_set(self):
		setting_name = GatewaySettingFactory.create(
			webhook_secret="wh_secret_abc"
		)
		self.addCleanup(GatewaySettingFactory.cleanup, setting_name)

		settings = resolve_paystack_settings("_Test Company")
		self.assertEqual(settings["webhook_secret"], "wh_secret_abc")

	def test_resolve_paystack_settings_falls_back_to_secret_key_for_webhook(self):
		setting_name = GatewaySettingFactory.create(webhook_secret=None)
		self.addCleanup(GatewaySettingFactory.cleanup, setting_name)

		settings = resolve_paystack_settings("_Test Company")
		self.assertEqual(settings["webhook_secret"], settings["secret_key"])

	def test_resolve_paystack_settings_returns_allowed_ips(self):
		setting_name = GatewaySettingFactory.create(
			allowed_ips="52.31.139.74\n52.31.139.75"
		)
		self.addCleanup(GatewaySettingFactory.cleanup, setting_name)

		settings = resolve_paystack_settings("_Test Company")
		self.assertIn("52.31.139.74", settings["allowed_webhook_ips"])


class TestPaystackIntegrationRequestLogging(FrappeTestCase):
	"""Tests for Integration Request audit logging."""

	def test_log_integration_request_creates_record(self):
		name = log_integration_request(
			status="Completed",
			url="webhook",
			request_data={"event": "charge.success"},
			response_data={"processed": True},
			reference_doctype="Paystack Payment Log",
			reference_docname="TEST-LOG-001",
		)
		self.assertIsNotNone(name)
		self.assertTrue(frappe.db.exists("Integration Request", name))

		ir = frappe.get_doc("Integration Request", name)
		self.assertEqual(ir.status, "Completed")
		self.assertEqual(ir.integration_request_service, "webhook")
		self.assertEqual(ir.reference_doctype, "Paystack Payment Log")
		parsed = json.loads(ir.data)
		self.assertEqual(parsed["event"], "charge.success")

		frappe.delete_doc(
			"Integration Request", name, force=True, ignore_permissions=True
		)

	def test_log_integration_request_with_error(self):
		name = log_integration_request(
			status="Failed",
			url="https://api.paystack.co/transaction/verify/xxx",
			request_data={"transaction_id": "xxx"},
			error="Connection timeout",
			reference_doctype="Paystack Payment Log",
			reference_docname="TEST-LOG-002",
		)
		self.assertIsNotNone(name)

		ir = frappe.get_doc("Integration Request", name)
		self.assertEqual(ir.status, "Failed")
		self.assertIn("Connection timeout", ir.error)

		frappe.delete_doc(
			"Integration Request", name, force=True, ignore_permissions=True
		)


class TestPaystackValidatePayment(FrappeTestCase):
	"""Tests for the Paystack transaction verification API call."""

	@patch("frappe_paystack.utils.requests.get")
	@patch("frappe_paystack.utils.is_paystack_enabled")
	@patch("frappe_paystack.utils.resolve_paystack_settings")
	def test_validate_payment_success(
		self, mock_settings, mock_enabled, mock_get
	):
		"""validate_payment returns API data and logs a Completed Integration Request."""
		from frappe_paystack.utils import validate_payment

		mock_enabled.return_value = True
		mock_settings.return_value = {"secret_key": "sk_test_123"}

		mock_response = MagicMock()
		mock_response.ok = True
		mock_response.json.return_value = {
			"status": True,
			"data": {"status": "success", "amount": 50000},
		}
		mock_get.return_value = mock_response

		doc = MagicMock()
		doc.company = "_Test Company"
		doc.transaction_id = "ref_test_001"
		doc.name = "TEST-LOG-VALIDATE"

		result = validate_payment(doc)

		self.assertTrue(result["status"])
		self.assertEqual(result["data"]["status"], "success")
		mock_get.assert_called_once()

	@patch("frappe_paystack.utils.requests.get")
	@patch("frappe_paystack.utils.is_paystack_enabled")
	@patch("frappe_paystack.utils.resolve_paystack_settings")
	def test_validate_payment_api_failure_throws(
		self, mock_settings, mock_enabled, mock_get
	):
		"""validate_payment throws when the API call raises an exception."""
		from frappe_paystack.utils import validate_payment

		mock_enabled.return_value = True
		mock_settings.return_value = {"secret_key": "sk_test_123"}
		mock_get.side_effect = requests.ConnectionError("Connection refused")

		doc = MagicMock()
		doc.company = "_Test Company"
		doc.transaction_id = "ref_test_002"
		doc.name = "TEST-LOG-VALIDATE-FAIL"

		with self.assertRaises(frappe.ValidationError):
			validate_payment(doc)

	@patch("frappe_paystack.utils.is_paystack_enabled")
	def test_validate_payment_throws_when_not_enabled(self, mock_enabled):
		"""validate_payment throws when Paystack is not enabled for the company."""
		from frappe_paystack.utils import validate_payment

		mock_enabled.return_value = False

		doc = MagicMock()
		doc.company = "_Test Company"

		with self.assertRaises(frappe.ValidationError):
			validate_payment(doc)