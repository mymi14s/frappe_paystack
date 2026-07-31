import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_paystack.api import (
	company_from_reference,
	create_payment_link,
	process_webhook_event,
	validate_payment_link,
)
from frappe_paystack.tests.factories import (
	GatewaySettingFactory,
	PaymentLogFactory,
	SalesInvoiceFactory,
)
from frappe_paystack.utils import hmac_sha512

VALIDATE_PAYMENT_PATCH = (
	"frappe_paystack.frappe_paystack.doctype.paystack_payment_log."
	"paystack_payment_log.PaystackPaymentLog.validate_payment"
)


class TestPaystackWebhookProcessing(FrappeTestCase):
	"""Tests for webhook event processing and idempotency."""

	def setUp(self):
		self.gateway_name = GatewaySettingFactory.create()
		self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

	@patch(VALIDATE_PAYMENT_PATCH)
	def test_process_webhook_success_updates_all_log_fields(self, mock_vp):
		"""A charge.success webhook must set status, amount, references, and date."""
		mock_vp.return_value = {"status": True, "data": {"status": "success"}}
		log_name = PaymentLogFactory.create(status="Pending", amount=1000)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		webhook_data = {
			"event": "charge.success",
			"data": {
				"reference": "ref_pay_001",
				"status": "success",
				"amount": 50000,
				"currency": "NGN",
				"paid_at": "2024-06-15T14:30:00Z",
				"metadata": {"reference": log_name},
			},
		}

		process_webhook_event(webhook_data)

		log = frappe.get_doc("Paystack Payment Log", log_name)
		self.assertIn(log.status, ("Processed", "Completed"))
		self.assertEqual(log.amount_paid, 500.0)
		self.assertEqual(log.currency_paid, "NGN")
		self.assertEqual(log.payment_reference, "ref_pay_001")
		self.assertEqual(log.transaction_id, "ref_pay_001")
		self.assertEqual(log.idempotency_key, "ref_pay_001")
		self.assertEqual(log.payment_date, "2024-06-15")
		self.assertIn("ref_pay_001", log.raw_response)

	@patch(VALIDATE_PAYMENT_PATCH)
	def test_process_webhook_failure_sets_failed_status(self, mock_vp):
		"""A charge.failed webhook must set the log status to Failed."""
		mock_vp.return_value = {"status": True, "data": {"status": "success"}}
		log_name = PaymentLogFactory.create(status="Pending", amount=1000)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		webhook_data = {
			"event": "charge.failed",
			"data": {
				"reference": "ref_pay_failed",
				"status": "failed",
				"amount": 50000,
				"currency": "NGN",
				"paid_at": "2024-06-15T14:30:00Z",
				"metadata": {"reference": log_name},
			},
		}

		process_webhook_event(webhook_data)

		log = frappe.get_doc("Paystack Payment Log", log_name)
		self.assertEqual(log.status, "Failed")

	@patch(VALIDATE_PAYMENT_PATCH)
	def test_process_webhook_idempotency_prevents_duplicate_processing(self, mock_vp):
		"""Processing the same webhook twice must not change the log after the first time."""
		mock_vp.return_value = {"status": True, "data": {"status": "success"}}
		log_name = PaymentLogFactory.create(status="Pending", amount=1000)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		webhook_data = {
			"event": "charge.success",
			"data": {
				"reference": "ref_idemp_001",
				"status": "success",
				"amount": 30000,
				"currency": "NGN",
				"paid_at": "2024-06-15T14:30:00Z",
				"metadata": {"reference": log_name},
			},
		}

		process_webhook_event(webhook_data)
		log = frappe.get_doc("Paystack Payment Log", log_name)
		self.assertIn(log.status, ("Processed", "Completed"))
		self.assertEqual(log.amount_paid, 300.0)

		original_amount = log.amount_paid
		original_raw = log.raw_response

		process_webhook_event(webhook_data)
		log.reload()
		self.assertEqual(log.amount_paid, original_amount)
		self.assertEqual(log.raw_response, original_raw)

	@patch(VALIDATE_PAYMENT_PATCH)
	def test_process_webhook_skips_already_completed_log(self, mock_vp):
		"""A webhook for a Completed log must be silently skipped."""
		mock_vp.return_value = {"status": True, "data": {"status": "success"}}
		log_name = PaymentLogFactory.create_completed(
			amount=1000, amount_paid=1000
		)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		webhook_data = {
			"event": "charge.success",
			"data": {
				"reference": "ref_skip_001",
				"status": "success",
				"amount": 99999,
				"currency": "NGN",
				"paid_at": "2024-06-15T14:30:00Z",
				"metadata": {"reference": log_name},
			},
		}

		process_webhook_event(webhook_data)

		log = frappe.get_doc("Paystack Payment Log", log_name)
		self.assertEqual(log.status, "Completed")

	@patch(VALIDATE_PAYMENT_PATCH)
	def test_process_webhook_with_usd_currency(self, mock_vp):
		"""A webhook in USD must set currency_paid to USD."""
		mock_vp.return_value = {"status": True, "data": {"status": "success"}}
		log_name = PaymentLogFactory.create(
			status="Pending", amount=100, currency="USD"
		)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		webhook_data = {
			"event": "charge.success",
			"data": {
				"reference": "ref_usd_001",
				"status": "success",
				"amount": 1000,
				"currency": "USD",
				"paid_at": "2024-06-15T14:30:00Z",
				"metadata": {"reference": log_name},
			},
		}

		process_webhook_event(webhook_data)

		log = frappe.get_doc("Paystack Payment Log", log_name)
		self.assertIn(log.currency_paid, ("USD",))
		self.assertEqual(log.amount_paid, 10.0)

	def test_process_webhook_nonexistent_log_does_not_raise(self):
		"""A webhook referencing a nonexistent log must not raise."""
		webhook_data = {
			"event": "charge.success",
			"data": {
				"reference": "ref_ghost",
				"status": "success",
				"amount": 50000,
				"currency": "NGN",
				"paid_at": "2024-06-15T14:30:00Z",
				"metadata": {"reference": "NONEXISTENT-LOG-999"},
			},
		}
		process_webhook_event(webhook_data)

	def test_process_webhook_missing_metadata_reference_does_not_raise(self):
		"""A webhook with no reference in metadata must not raise."""
		webhook_data = {
			"event": "charge.success",
			"data": {
				"reference": "ref_no_meta",
				"status": "success",
				"amount": 50000,
				"currency": "NGN",
				"paid_at": "2024-06-15T14:30:00Z",
				"metadata": {},
			},
		}
		process_webhook_event(webhook_data)

	@patch(VALIDATE_PAYMENT_PATCH)
	def test_company_from_reference_returns_company_for_existing_log(self, mock_vp):
		"""company_from_reference returns the company for a real log."""
		mock_vp.return_value = {"status": True, "data": {"status": "success"}}
		log_name = PaymentLogFactory.create(status="Pending", amount=1000)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		company = company_from_reference(log_name)
		self.assertEqual(company, "_Test Company")

	def test_company_from_reference_returns_none_for_nonexistent(self):
		self.assertIsNone(company_from_reference("NONEXISTENT-LOG-001"))


class TestPaystackPaymentLink(FrappeTestCase):
	"""Tests for payment link creation against real Sales Invoices."""

	@patch(VALIDATE_PAYMENT_PATCH)
	@patch("frappe_paystack.api.resolve_paystack_settings")
	def test_create_payment_link_for_sales_invoice_uses_outstanding(
		self, mock_settings, mock_vp
	):
		"""create_payment_link defaults to outstanding_amount for a Sales Invoice."""
		mock_vp.return_value = {"status": True, "data": {"status": "success"}}
		mock_settings.return_value = {
			"public_key": "pk_test_123",
			"secret_key": "sk_test_123",
			"webhook_secret": "sk_test_123",
			"default_currency": "NGN",
		}

		sinv_name = SalesInvoiceFactory.create(rate=5000)
		self.addCleanup(SalesInvoiceFactory.cleanup, sinv_name)

		url = create_payment_link("Sales Invoice", sinv_name)
		log_name = url.split("/paystack-checkout/")[-1]
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		log = frappe.get_doc("Paystack Payment Log", log_name)
		self.assertEqual(log.linked_doctype, "Sales Invoice")
		self.assertEqual(log.linked_docname, sinv_name)
		self.assertEqual(log.status, "Pending")
		self.assertEqual(log.company, "_Test Company")
		self.assertAlmostEqual(log.amount, 5000, places=2)

	@patch(VALIDATE_PAYMENT_PATCH)
	@patch("frappe_paystack.api.resolve_paystack_settings")
	def test_create_payment_link_with_explicit_partial_amount(
		self, mock_settings, mock_vp
	):
		"""create_payment_link with an explicit amount creates a partial payment log."""
		mock_vp.return_value = {"status": True, "data": {"status": "success"}}
		mock_settings.return_value = {
			"public_key": "pk_test_123",
			"secret_key": "sk_test_123",
			"webhook_secret": "sk_test_123",
			"default_currency": "NGN",
		}

		sinv_name = SalesInvoiceFactory.create(rate=10000)
		self.addCleanup(SalesInvoiceFactory.cleanup, sinv_name)

		url = create_payment_link("Sales Invoice", sinv_name, amount=3000.0)
		log_name = url.split("/paystack-checkout/")[-1]
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		log = frappe.get_doc("Paystack Payment Log", log_name)
		self.assertEqual(log.amount, 3000.0)

	@patch(VALIDATE_PAYMENT_PATCH)
	@patch("frappe_paystack.api.resolve_paystack_settings")
	def test_create_payment_link_returns_valid_checkout_url(
		self, mock_settings, mock_vp
	):
		"""The returned URL must contain /paystack-checkout/ and the log name."""
		mock_vp.return_value = {"status": True, "data": {"status": "success"}}
		mock_settings.return_value = {
			"public_key": "pk_test_123",
			"secret_key": "sk_test_123",
			"webhook_secret": "sk_test_123",
			"default_currency": "NGN",
		}

		sinv_name = SalesInvoiceFactory.create(rate=1000)
		self.addCleanup(SalesInvoiceFactory.cleanup, sinv_name)

		url = create_payment_link("Sales Invoice", sinv_name)
		log_name = url.split("/paystack-checkout/")[-1]
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		self.assertIn("/paystack-checkout/", url)
		self.assertTrue(frappe.db.exists("Paystack Payment Log", log_name))

	@patch(VALIDATE_PAYMENT_PATCH)
	def test_create_payment_link_throws_when_not_enabled(self, mock_vp):
		"""create_payment_link must throw when Paystack is not configured."""
		mock_vp.return_value = {"status": True, "data": {"status": "success"}}

		# Disable any existing enabled gateways
		existing = frappe.get_all(
			"Paystack Gateway Setting",
			filters={"enabled": 1, "company": "_Test Company"},
			pluck="name",
		)
		for name in existing:
			frappe.db.set_value(
				"Paystack Gateway Setting", name, "enabled", 0
			)

		sinv_name = SalesInvoiceFactory.create(rate=1000)
		self.addCleanup(SalesInvoiceFactory.cleanup, sinv_name)

		with self.assertRaises(frappe.ValidationError):
			create_payment_link("Sales Invoice", sinv_name)


class TestPaystackValidatePaymentLink(FrappeTestCase):
	"""Tests for the validate_payment_link endpoint used by the checkout page."""

	@patch(VALIDATE_PAYMENT_PATCH)
	@patch("frappe_paystack.api.resolve_paystack_settings")
	def test_validate_payment_link_returns_data_for_existing_log(
		self, mock_settings, mock_vp
	):
		"""validate_payment_link returns checkout data including gateway settings."""
		mock_vp.return_value = {"status": True, "data": {"status": "success"}}
		mock_settings.return_value = {
			"public_key": "pk_test_123",
			"secret_key": "sk_test_123",
			"webhook_secret": "sk_test_123",
			"default_currency": "NGN",
		}

		sinv_name = SalesInvoiceFactory.create(rate=2000)
		self.addCleanup(SalesInvoiceFactory.cleanup, sinv_name)

		url = create_payment_link("Sales Invoice", sinv_name)
		log_name = url.split("/paystack-checkout/")[-1]
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		data = validate_payment_link(log_name)
		self.assertEqual(data["reference_doctype"], "Sales Invoice")
		self.assertEqual(data["reference_docname"], sinv_name)
		self.assertEqual(data["reference"], log_name)
		self.assertEqual(data["customer"], "_Test Customer")

	def test_validate_payment_link_returns_empty_for_nonexistent(self):
		result = validate_payment_link("NONEXISTENT-LOG-999")
		self.assertEqual(result, {})


class TestPaystackWebhookSignatureFlow(FrappeTestCase):
	"""End-to-end tests for webhook signature verification flow."""

	def setUp(self):
		self.gateway_name = GatewaySettingFactory.create()
		self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

	@patch(VALIDATE_PAYMENT_PATCH)
	def test_webhook_with_valid_signature_processes_event(self, mock_vp):
		"""A webhook with a valid signature must process the event and update the log."""
		from frappe_paystack.api import paystack_webhook

		mock_vp.return_value = {"status": True, "data": {"status": "success"}}
		log_name = PaymentLogFactory.create(status="Pending", amount=1000)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		secret = "sk_test_webhook"
		webhook_data = {
			"event": "charge.success",
			"data": {
				"reference": "ref_e2e_001",
				"status": "success",
				"amount": 20000,
				"currency": "NGN",
				"paid_at": "2024-06-15T14:30:00Z",
				"metadata": {"reference": log_name},
			},
		}
		payload = json.dumps(webhook_data).encode("utf-8")
		signature = hmac_sha512(payload, secret)

		with patch("frappe_paystack.api.company_from_reference") as mock_ref, \
			patch("frappe_paystack.api.resolve_paystack_settings") as mock_settings, \
			patch("frappe_paystack.api.frappe.request") as mock_request, \
			patch("frappe_paystack.api.frappe.local") as mock_local, \
			patch("frappe_paystack.api.frappe.get_request_header") as mock_header:

			mock_ref.return_value = "_Test Company"
			mock_settings.return_value = {
				"secret_key": secret,
				"webhook_secret": secret,
				"allowed_webhook_ips": None,
			}
			mock_request.get_json.return_value = webhook_data
			mock_request.data = payload
			mock_header.return_value = signature
			mock_local.request_ip = None

			paystack_webhook()

		log = frappe.get_doc("Paystack Payment Log", log_name)
		self.assertEqual(log.status, "Processed")
		self.assertEqual(log.amount_paid, 200.0)

	@patch(VALIDATE_PAYMENT_PATCH)
	def test_webhook_with_invalid_signature_throws(self, mock_vp):
		"""A webhook with an invalid signature must throw PermissionError."""
		from frappe_paystack.api import paystack_webhook

		mock_vp.return_value = {"status": True, "data": {"status": "success"}}
		log_name = PaymentLogFactory.create(status="Pending", amount=1000)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		webhook_data = {
			"event": "charge.success",
			"data": {
				"reference": "ref_bad_sig",
				"status": "success",
				"amount": 20000,
				"currency": "NGN",
				"paid_at": "2024-06-15T14:30:00Z",
				"metadata": {"reference": log_name},
			},
		}
		payload = json.dumps(webhook_data).encode("utf-8")

		with patch("frappe_paystack.api.company_from_reference") as mock_ref, \
			patch("frappe_paystack.api.resolve_paystack_settings") as mock_settings, \
			patch("frappe_paystack.api.frappe.request") as mock_request, \
			patch("frappe_paystack.api.frappe.local") as mock_local, \
			patch("frappe_paystack.api.frappe.get_request_header") as mock_header:

			mock_ref.return_value = "_Test Company"
			mock_settings.return_value = {
				"secret_key": "real_secret",
				"webhook_secret": "real_secret",
				"allowed_webhook_ips": None,
			}
			mock_request.get_json.return_value = webhook_data
			mock_request.data = payload
			mock_header.return_value = "completely_wrong_signature"
			mock_local.request_ip = None

			with self.assertRaises(frappe.PermissionError):
				paystack_webhook()

		log = frappe.get_doc("Paystack Payment Log", log_name)
		self.assertEqual(log.status, "Pending")

	def test_webhook_with_no_settings_throws(self):
		"""A webhook with no Paystack settings must throw PermissionError."""
		from frappe_paystack.api import paystack_webhook

		webhook_data = {
			"event": "charge.success",
			"data": {
				"reference": "ref_no_settings",
				"status": "success",
				"amount": 20000,
				"currency": "NGN",
				"paid_at": "2024-06-15T14:30:00Z",
				"metadata": {"reference": "NONEXISTENT"},
			},
		}
		payload = json.dumps(webhook_data).encode("utf-8")

		with patch("frappe_paystack.api.company_from_reference") as mock_ref, \
			patch("frappe_paystack.api.resolve_paystack_settings") as mock_settings, \
			patch("frappe_paystack.api.frappe.request") as mock_request, \
			patch("frappe_paystack.api.frappe.local") as mock_local, \
			patch("frappe_paystack.api.frappe.get_request_header") as mock_header:

			mock_ref.return_value = "_Test Company"
			mock_settings.return_value = None
			mock_request.get_json.return_value = webhook_data
			mock_request.data = payload
			mock_header.return_value = "sig"
			mock_local.request_ip = None

			with self.assertRaises(frappe.PermissionError):
				paystack_webhook()

	@patch(VALIDATE_PAYMENT_PATCH)
	def test_webhook_ip_not_in_allowlist_throws(self, mock_vp):
		"""A webhook from a non-allowlisted IP must throw PermissionError."""
		from frappe_paystack.api import paystack_webhook

		mock_vp.return_value = {"status": True, "data": {"status": "success"}}
		log_name = PaymentLogFactory.create(status="Pending", amount=1000)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		secret = "sk_test_ip"
		webhook_data = {
			"event": "charge.success",
			"data": {
				"reference": "ref_ip_block",
				"status": "success",
				"amount": 20000,
				"currency": "NGN",
				"paid_at": "2024-06-15T14:30:00Z",
				"metadata": {"reference": log_name},
			},
		}
		payload = json.dumps(webhook_data).encode("utf-8")
		signature = hmac_sha512(payload, secret)

		with patch("frappe_paystack.api.company_from_reference") as mock_ref, \
			patch("frappe_paystack.api.resolve_paystack_settings") as mock_settings, \
			patch("frappe_paystack.api.frappe.request") as mock_request, \
			patch("frappe_paystack.api.frappe.local") as mock_local, \
			patch("frappe_paystack.api.frappe.get_request_header") as mock_header:

			mock_ref.return_value = "_Test Company"
			mock_settings.return_value = {
				"secret_key": secret,
				"webhook_secret": secret,
				"allowed_webhook_ips": "52.31.139.74",
			}
			mock_request.get_json.return_value = webhook_data
			mock_request.data = payload
			mock_header.return_value = signature
			mock_local.request_ip = "1.1.1.1"

			with self.assertRaises(frappe.PermissionError):
				paystack_webhook()

		log = frappe.get_doc("Paystack Payment Log", log_name)
		self.assertEqual(log.status, "Pending")