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
from frappe_paystack.utils import hmac_sha512


class TestPaystackWebhookProcessing(FrappeTestCase):
	"""Tests for webhook event processing and idempotency."""

	def test_process_webhook_success_updates_all_log_fields(self):
		"""A charge.success webhook must set status, amount, references, and date."""
		log = self.create_pending_log()
		self.addCleanup(self.force_cleanup_log, log.name)

		webhook_data = {
			"event": "charge.success",
			"data": {
				"reference": "ref_pay_001",
				"status": "success",
				"amount": 50000,
				"currency": "NGN",
				"paid_at": "2024-06-15T14:30:00Z",
				"metadata": {"reference": log.name},
			},
		}

		process_webhook_event(webhook_data)

		log.reload()
		self.assertEqual(log.status, "Processed")
		self.assertEqual(log.amount_paid, 500.0)
		self.assertEqual(log.currency_paid, "NGN")
		self.assertEqual(log.payment_reference, "ref_pay_001")
		self.assertEqual(log.transaction_id, "ref_pay_001")
		self.assertEqual(log.idempotency_key, "ref_pay_001")
		self.assertEqual(log.payment_date, "2024-06-15")
		self.assertIn("ref_pay_001", log.raw_response)

	def test_process_webhook_failure_sets_failed_status(self):
		"""A charge.failed webhook must set the log status to Failed."""
		log = self.create_pending_log()
		self.addCleanup(self.force_cleanup_log, log.name)

		webhook_data = {
			"event": "charge.failed",
			"data": {
				"reference": "ref_pay_failed",
				"status": "failed",
				"amount": 50000,
				"currency": "NGN",
				"paid_at": "2024-06-15T14:30:00Z",
				"metadata": {"reference": log.name},
			},
		}

		process_webhook_event(webhook_data)

		log.reload()
		self.assertEqual(log.status, "Failed")

	def test_process_webhook_idempotency_prevents_duplicate_processing(self):
		"""Processing the same webhook twice must not change the log after the first time."""
		log = self.create_pending_log()
		self.addCleanup(self.force_cleanup_log, log.name)

		webhook_data = {
			"event": "charge.success",
			"data": {
				"reference": "ref_idemp_001",
				"status": "success",
				"amount": 30000,
				"currency": "NGN",
				"paid_at": "2024-06-15T14:30:00Z",
				"metadata": {"reference": log.name},
			},
		}

		process_webhook_event(webhook_data)
		log.reload()
		self.assertEqual(log.status, "Processed")
		self.assertEqual(log.amount_paid, 300.0)

		original_amount = log.amount_paid
		original_raw = log.raw_response

		process_webhook_event(webhook_data)
		log.reload()
		self.assertEqual(log.amount_paid, original_amount)
		self.assertEqual(log.raw_response, original_raw)

	def test_process_webhook_skips_already_completed_log(self):
		"""A webhook for a Completed log must be silently skipped."""
		log = self.create_pending_log()
		log.db_set("status", "Completed")
		self.addCleanup(self.force_cleanup_log, log.name)

		webhook_data = {
			"event": "charge.success",
			"data": {
				"reference": "ref_skip_001",
				"status": "success",
				"amount": 99999,
				"currency": "NGN",
				"paid_at": "2024-06-15T14:30:00Z",
				"metadata": {"reference": log.name},
			},
		}

		process_webhook_event(webhook_data)

		log.reload()
		self.assertEqual(log.status, "Completed")
		self.assertIsNone(log.amount_paid)

	def test_process_webhook_with_usd_currency(self):
		"""A webhook in USD must set currency_paid to USD."""
		log = self.create_pending_log(currency="USD")
		self.addCleanup(self.force_cleanup_log, log.name)

		webhook_data = {
			"event": "charge.success",
			"data": {
				"reference": "ref_usd_001",
				"status": "success",
				"amount": 1000,
				"currency": "USD",
				"paid_at": "2024-06-15T14:30:00Z",
				"metadata": {"reference": log.name},
			},
		}

		process_webhook_event(webhook_data)

		log.reload()
		self.assertEqual(log.currency_paid, "USD")
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

	def test_company_from_reference_returns_company_for_existing_log(self):
		"""company_from_reference returns the company for a real log."""
		log = self.create_pending_log()
		self.addCleanup(self.force_cleanup_log, log.name)

		company = company_from_reference(log.name)
		self.assertEqual(company, "_Test Company")

	def test_company_from_reference_returns_none_for_nonexistent(self):
		self.assertIsNone(company_from_reference("NONEXISTENT-LOG-001"))


class TestPaystackPaymentLink(FrappeTestCase):
	"""Tests for payment link creation against real Sales Invoices."""

	@patch("frappe_paystack.api.resolve_paystack_settings")
	def test_create_payment_link_for_sales_invoice_uses_outstanding(self, mock_settings):
		"""create_payment_link defaults to outstanding_amount for a Sales Invoice."""
		mock_settings.return_value = {
			"public_key": "pk_test_123",
			"secret_key": "sk_test_123",
			"webhook_secret": "sk_test_123",
			"default_currency": "NGN",
		}

		sinv = self.create_sales_invoice(rate=5000)
		self.addCleanup(self.cleanup_sales_invoice, sinv.name)

		url = create_payment_link("Sales Invoice", sinv.name)
		log_name = url.split("/paystack-checkout/")[-1]
		self.addCleanup(self.force_cleanup_log, log_name)

		log = frappe.get_doc("Paystack Payment Log", log_name)
		self.assertEqual(log.linked_doctype, "Sales Invoice")
		self.assertEqual(log.linked_docname, sinv.name)
		self.assertEqual(log.status, "Pending")
		self.assertEqual(log.company, "_Test Company")
		self.assertAlmostEqual(log.amount, sinv.outstanding_amount, places=2)

	@patch("frappe_paystack.api.resolve_paystack_settings")
	def test_create_payment_link_with_explicit_partial_amount(self, mock_settings):
		"""create_payment_link with an explicit amount creates a partial payment log."""
		mock_settings.return_value = {
			"public_key": "pk_test_123",
			"secret_key": "sk_test_123",
			"webhook_secret": "sk_test_123",
			"default_currency": "NGN",
		}

		sinv = self.create_sales_invoice(rate=10000)
		self.addCleanup(self.cleanup_sales_invoice, sinv.name)

		url = create_payment_link("Sales Invoice", sinv.name, amount=3000.0)
		log_name = url.split("/paystack-checkout/")[-1]
		self.addCleanup(self.force_cleanup_log, log_name)

		log = frappe.get_doc("Paystack Payment Log", log_name)
		self.assertEqual(log.amount, 3000.0)
		self.assertLess(log.amount, sinv.outstanding_amount)

	@patch("frappe_paystack.api.resolve_paystack_settings")
	def test_create_payment_link_returns_valid_checkout_url(self, mock_settings):
		"""The returned URL must contain /paystack-checkout/ and the log name."""
		mock_settings.return_value = {
			"public_key": "pk_test_123",
			"secret_key": "sk_test_123",
			"webhook_secret": "sk_test_123",
			"default_currency": "NGN",
		}

		sinv = self.create_sales_invoice(rate=1000)
		self.addCleanup(self.cleanup_sales_invoice, sinv.name)

		url = create_payment_link("Sales Invoice", sinv.name)
		log_name = url.split("/paystack-checkout/")[-1]
		self.addCleanup(self.force_cleanup_log, log_name)

		self.assertIn("/paystack-checkout/", url)
		self.assertTrue(frappe.db.exists("Paystack Payment Log", log_name))

	def test_create_payment_link_throws_when_not_enabled(self):
		"""create_payment_link must throw when Paystack is not configured."""
		sinv = self.create_sales_invoice(rate=1000)
		self.addCleanup(self.cleanup_sales_invoice, sinv.name)

		with self.assertRaises(frappe.ValidationError):
			create_payment_link("Sales Invoice", sinv.name)


class TestPaystackValidatePaymentLink(FrappeTestCase):
	"""Tests for the validate_payment_link endpoint used by the checkout page."""

	@patch("frappe_paystack.api.resolve_paystack_settings")
	def test_validate_payment_link_returns_data_for_existing_log(self, mock_settings):
		"""validate_payment_link returns checkout data including gateway settings."""
		mock_settings.return_value = {
			"public_key": "pk_test_123",
			"secret_key": "sk_test_123",
			"webhook_secret": "sk_test_123",
			"default_currency": "NGN",
		}

		sinv = self.create_sales_invoice(rate=2000)
		self.addCleanup(self.cleanup_sales_invoice, sinv.name)

		url = create_payment_link("Sales Invoice", sinv.name)
		log_name = url.split("/paystack-checkout/")[-1]
		self.addCleanup(self.force_cleanup_log, log_name)

		data = validate_payment_link(log_name)
		self.assertEqual(data["reference_doctype"], "Sales Invoice")
		self.assertEqual(data["reference_docname"], sinv.name)
		self.assertEqual(data["reference"], log_name)
		self.assertEqual(data["customer"], "_Test Customer")

	def test_validate_payment_link_returns_empty_for_nonexistent(self):
		result = validate_payment_link("NONEXISTENT-LOG-999")
		self.assertEqual(result, {})


class TestPaystackWebhookSignatureFlow(FrappeTestCase):
	"""End-to-end tests for webhook signature verification flow."""

	def test_webhook_with_valid_signature_processes_event(self):
		"""A webhook with a valid signature must process the event and update the log."""
		from frappe_paystack.api import paystack_webhook

		log = self.create_pending_log()
		self.addCleanup(self.force_cleanup_log, log.name)

		secret = "sk_test_webhook"
		webhook_data = {
			"event": "charge.success",
			"data": {
				"reference": "ref_e2e_001",
				"status": "success",
				"amount": 20000,
				"currency": "NGN",
				"paid_at": "2024-06-15T14:30:00Z",
				"metadata": {"reference": log.name},
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

		log.reload()
		self.assertEqual(log.status, "Processed")
		self.assertEqual(log.amount_paid, 200.0)

	def test_webhook_with_invalid_signature_throws(self):
		"""A webhook with an invalid signature must throw PermissionError."""
		from frappe_paystack.api import paystack_webhook

		log = self.create_pending_log()
		self.addCleanup(self.force_cleanup_log, log.name)

		webhook_data = {
			"event": "charge.success",
			"data": {
				"reference": "ref_bad_sig",
				"status": "success",
				"amount": 20000,
				"currency": "NGN",
				"paid_at": "2024-06-15T14:30:00Z",
				"metadata": {"reference": log.name},
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

		log.reload()
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

	def test_webhook_ip_not_in_allowlist_throws(self):
		"""A webhook from a non-allowlisted IP must throw PermissionError."""
		from frappe_paystack.api import paystack_webhook

		log = self.create_pending_log()
		self.addCleanup(self.force_cleanup_log, log.name)

		secret = "sk_test_ip"
		webhook_data = {
			"event": "charge.success",
			"data": {
				"reference": "ref_ip_block",
				"status": "success",
				"amount": 20000,
				"currency": "NGN",
				"paid_at": "2024-06-15T14:30:00Z",
				"metadata": {"reference": log.name},
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

		log.reload()
		self.assertEqual(log.status, "Pending")

	def create_pending_log(self, currency="NGN"):
		"""Create a Pending Paystack Payment Log for testing."""
		log = frappe.get_doc(
			{
				"doctype": "Paystack Payment Log",
				"company": "_Test Company",
				"linked_doctype": "Sales Invoice",
				"linked_docname": "NONEXISTENT-INV",
				"amount": 1000,
				"currency": currency,
				"status": "Pending",
			}
		)
		log.flags.ignore_permissions = True
		log.insert()
		return log

	def create_sales_invoice(self, rate=1000):
		"""Create a minimal submitted Sales Invoice for testing."""
		sinv = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"customer": "_Test Customer",
				"company": "_Test Company",
				"due_date": frappe.utils.today(),
				"posting_date": frappe.utils.today(),
				"items": [
					{
						"item_code": "_Test Item Home Products 100",
						"qty": 1,
						"rate": rate,
					}
				],
			}
		)
		sinv.flags.ignore_permissions = True
		sinv.insert()
		sinv.submit()
		return sinv

	def force_cleanup_log(self, name):
		if frappe.db.exists("Paystack Payment Log", name):
			frappe.db.set_value("Paystack Payment Log", name, "status", "Pending")
			frappe.db.set_value("Paystack Payment Log", name, "payment_entry", None)
			frappe.delete_doc(
				"Paystack Payment Log", name, force=True, ignore_permissions=True
			)

	def cleanup_sales_invoice(self, name):
		if frappe.db.exists("Sales Invoice", name):
			sinv = frappe.get_doc("Sales Invoice", name)
			if sinv.docstatus == 1:
				sinv.flags.ignore_permissions = True
				sinv.cancel()
			frappe.delete_doc(
				"Sales Invoice", name, force=True, ignore_permissions=True
			)