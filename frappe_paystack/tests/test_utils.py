import hashlib
import hmac
import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_paystack.utils import (
	SUPPORTED_CURRENCIES,
	clamp_amount_to_positive,
	coalesce_currency,
	ensure_supported_currency,
	from_minor_units,
	hmac_sha512,
	is_ip_allowed,
	is_paystack_enabled,
	normalize_currency,
	parse_reference_company,
	safe_json_dumps,
	sanitize_reference,
	to_minor_units,
	verify_signature,
)


class TestPaystackUtils(FrappeTestCase):
	"""Tests for utility functions in frappe_paystack.utils."""

	def test_supported_currencies_includes_kenya(self):
		"""KES must be in the supported currencies list (was missing before Phase 1)."""
		self.assertIn("KES", SUPPORTED_CURRENCIES)
		self.assertIn("NGN", SUPPORTED_CURRENCIES)
		self.assertIn("USD", SUPPORTED_CURRENCIES)

	def test_normalize_currency_valid(self):
		self.assertEqual(normalize_currency("NGN"), "NGN")
		self.assertEqual(normalize_currency("usd"), "USD")
		self.assertEqual(normalize_currency(" ghs "), "GHS")

	def test_normalize_currency_invalid_defaults_to_ngn(self):
		self.assertEqual(normalize_currency("EUR"), "NGN")
		self.assertEqual(normalize_currency(None), "NGN")
		self.assertEqual(normalize_currency(""), "NGN")

	def test_to_minor_units(self):
		self.assertEqual(to_minor_units(100.0, "NGN"), 10000)
		self.assertEqual(to_minor_units(1.0, "USD"), 100)
		self.assertEqual(to_minor_units(0.5, "GHS"), 50)

	def test_from_minor_units(self):
		self.assertEqual(from_minor_units(10000, "NGN"), 100.0)
		self.assertEqual(from_minor_units(100, "USD"), 1.0)

	def test_to_minor_units_rounds_correctly(self):
		self.assertEqual(to_minor_units(99.99, "NGN"), 9999)

	def test_hmac_sha512(self):
		secret = "test_secret"
		payload = b"test payload"
		expected = hmac.new(
			secret.encode("utf-8"), payload, hashlib.sha512
		).hexdigest()
		self.assertEqual(hmac_sha512(payload, secret), expected)

	def test_verify_signature_valid(self):
		secret = "my_secret"
		payload = b'{"event": "charge.success"}'
		signature = hmac_sha512(payload, secret)
		self.assertTrue(verify_signature(payload, signature, secret))

	def test_verify_signature_invalid(self):
		self.assertFalse(verify_signature(b"payload", "wrong_signature", "secret"))

	def test_verify_signature_missing(self):
		"""A missing signature (None or empty) must return False."""
		self.assertFalse(verify_signature(b"payload", None, "secret"))
		self.assertFalse(verify_signature(b"payload", "", "secret"))

	def test_verify_signature_wrong_secret(self):
		secret = "correct_secret"
		payload = b"payload"
		signature = hmac_sha512(payload, secret)
		self.assertFalse(verify_signature(payload, signature, "wrong_secret"))

	def test_is_ip_allowed_empty_allows_all(self):
		"""When no allowlist is configured, all IPs are allowed."""
		self.assertTrue(is_ip_allowed(None, "1.2.3.4"))
		self.assertTrue(is_ip_allowed("", "1.2.3.4"))

	def test_is_ip_allowed_in_list(self):
		allowed = "52.31.139.74\n52.31.139.75"
		self.assertTrue(is_ip_allowed(allowed, "52.31.139.74"))

	def test_is_ip_allowed_not_in_list(self):
		allowed = "52.31.139.74"
		self.assertFalse(is_ip_allowed(allowed, "1.1.1.1"))

	def test_is_ip_allowed_no_request_ip(self):
		allowed = "52.31.139.74"
		self.assertFalse(is_ip_allowed(allowed, None))

	def test_is_ip_allowed_strips_whitespace(self):
		allowed = "  52.31.139.74  \n\n  52.31.139.75  \n"
		self.assertTrue(is_ip_allowed(allowed, "52.31.139.74"))
		self.assertTrue(is_ip_allowed(allowed, "52.31.139.75"))

	def test_safe_json_dumps_valid(self):
		result = safe_json_dumps({"key": "value"})
		self.assertEqual(json.loads(result), {"key": "value"})

	def test_safe_json_dumps_with_non_serializable(self):
		"""safe_json_dumps should not raise on non-serializable objects."""
		result = safe_json_dumps({"obj": object()})
		self.assertNotEqual(result, "{}")

	def test_safe_json_dumps_on_failure(self):
		"""safe_json_dumps should return '{}' when json.dumps fails."""
		result = safe_json_dumps(float("nan"))
		self.assertEqual(result, "{}")

	def test_sanitize_reference(self):
		ref = sanitize_reference("Sales Invoice", "ACC-SINV-2024-001", "My Company")
		self.assertEqual(ref, "Sales Invoice-ACC-SINV-2024-001-MyCompany")

	def test_sanitize_reference_no_company(self):
		ref = sanitize_reference("Sales Order", "SO-001", None)
		self.assertEqual(ref, "Sales Order-SO-001-")

	def test_parse_reference_company(self):
		ref = "Sales Invoice-ACC-SINV-2024-001-MyCompany"
		self.assertEqual(parse_reference_company(ref), "MyCompany")

	def test_parse_reference_company_invalid(self):
		self.assertIsNone(parse_reference_company("invalid"))
		self.assertIsNone(parse_reference_company(""))

	def test_clamp_amount_to_positive(self):
		self.assertEqual(clamp_amount_to_positive(100.0), 100.0)
		self.assertEqual(clamp_amount_to_positive(-50.0), 0.0)
		self.assertEqual(clamp_amount_to_positive(0.0), 0.0)

	def test_clamp_amount_to_positive_rounds(self):
		self.assertEqual(clamp_amount_to_positive(99.999), 100.0)

	def test_coalesce_currency_explicit(self):
		self.assertEqual(coalesce_currency("USD", None, None), "USD")

	def test_coalesce_currency_invalid_defaults_to_ngn(self):
		self.assertEqual(coalesce_currency("EUR", None, None), "NGN")

	def test_coalesce_currency_from_settings(self):
		settings = {"default_currency": "GHS"}
		self.assertEqual(coalesce_currency(None, None, settings), "GHS")

	def test_coalesce_currency_defaults_to_ngn(self):
		self.assertEqual(coalesce_currency(None, None, None), "NGN")

	def test_ensure_supported_currency_valid(self):
		ensure_supported_currency("NGN")
		ensure_supported_currency("USD")

	def test_ensure_supported_currency_invalid(self):
		with self.assertRaises(frappe.ValidationError):
			ensure_supported_currency("EUR")

	def test_is_paystack_enabled_no_settings(self):
		"""When no gateway is configured, is_paystack_enabled returns False."""
		self.assertFalse(is_paystack_enabled("_Test Company"))