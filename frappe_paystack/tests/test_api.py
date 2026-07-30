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
	verify_paystack_signature,
)
from frappe_paystack.utils import hmac_sha512


class TestPaystackAPI(FrappeTestCase):
	"""Tests for API endpoints in frappe_paystack.api."""

	def test_verify_paystack_signature_valid(self):
		secret = "test_secret"
		payload = b'{"event": "charge.success"}'
		signature = hmac.new(
			secret.encode("utf-8"), payload, hashlib.sha512
		).hexdigest()
		self.assertTrue(verify_paystack_signature(payload, signature, secret))

	def test_verify_paystack_signature_invalid(self):
		self.assertFalse(
			verify_paystack_signature(b"payload", "bad_sig", "secret")
		)

	def test_company_from_reference_nonexistent(self):
		"""company_from_reference returns None for a nonexistent log."""
		result = company_from_reference("NONEXISTENT-LOG-001")
		self.assertIsNone(result)

	def test_process_webhook_event_skips_already_processed(self):
		"""Idempotency: a webhook for an already-Processed log must not reprocess."""
		log = frappe.get_doc(
			{
				"doctype": "Paystack Payment Log",
				"company": "_Test Company",
				"linked_doctype": "Sales Invoice",
				"linked_docname": "NONEXISTENT-INV",
				"amount": 1000,
				"currency": "NGN",
				"status": "Processed",
				"transaction_id": "ref_already_processed",
			}
		)
		log.flags.ignore_permissions = True
		log.insert()
		self.addCleanup(self.cleanup_log, log.name)

		webhook_data = {
			"event": "charge.success",
			"data": {
				"reference": "ref_already_processed",
				"status": "success",
				"amount": 100000,
				"currency": "NGN",
				"paid_at": "2024-01-01T10:00:00Z",
				"metadata": {"reference": log.name},
			},
		}

		process_webhook_event(webhook_data)

		log.reload()
		self.assertEqual(log.status, "Processed")

	def test_process_webhook_event_updates_log_on_success(self):
		"""A success webhook updates the log to Processed with payment details."""
		log = self.create_pending_log()
		self.addCleanup(self.cleanup_log, log.name)

		webhook_data = {
			"event": "charge.success",
			"data": {
				"reference": "ref_test_001",
				"status": "success",
				"amount": 50000,
				"currency": "NGN",
				"paid_at": "2024-01-01T10:00:00Z",
				"metadata": {"reference": log.name},
			},
		}

		process_webhook_event(webhook_data)

		log.reload()
		self.assertEqual(log.status, "Processed")
		self.assertEqual(log.amount_paid, 500.0)
		self.assertEqual(log.currency_paid, "NGN")
		self.assertEqual(log.payment_reference, "ref_test_001")
		self.assertEqual(log.transaction_id, "ref_test_001")
		self.assertEqual(log.idempotency_key, "ref_test_001")
		self.assertEqual(log.payment_date, "2024-01-01")

	def test_process_webhook_event_marks_failed_on_failure(self):
		"""A failed transaction webhook sets the log status to Failed."""
		log = self.create_pending_log()
		self.addCleanup(self.cleanup_log, log.name)

		webhook_data = {
			"event": "charge.failed",
			"data": {
				"reference": "ref_test_failed",
				"status": "failed",
				"amount": 50000,
				"currency": "NGN",
				"paid_at": "2024-01-01T10:00:00Z",
				"metadata": {"reference": log.name},
			},
		}

		process_webhook_event(webhook_data)

		log.reload()
		self.assertEqual(log.status, "Failed")

	def test_process_webhook_event_no_reference(self):
		"""A webhook with no reference in metadata must not raise."""
		webhook_data = {
			"event": "charge.success",
			"data": {
				"reference": "ref_no_meta",
				"status": "success",
				"amount": 50000,
				"currency": "NGN",
				"paid_at": "2024-01-01T10:00:00Z",
				"metadata": {},
			},
		}
		process_webhook_event(webhook_data)

	def test_process_webhook_event_nonexistent_log(self):
		"""A webhook for a nonexistent log must not raise."""
		webhook_data = {
			"event": "charge.success",
			"data": {
				"reference": "ref_nonexistent",
				"status": "success",
				"amount": 50000,
				"currency": "NGN",
				"paid_at": "2024-01-01T10:00:00Z",
				"metadata": {"reference": "NONEXISTENT-LOG-999"},
			},
		}
		process_webhook_event(webhook_data)

	def test_create_payment_link_no_settings(self):
		"""create_payment_link must throw when Paystack is not enabled."""
		sinv = self.create_sales_invoice()
		self.addCleanup(self.cleanup_sales_invoice, sinv.name)

		with self.assertRaises(frappe.ValidationError):
			create_payment_link("Sales Invoice", sinv.name)

	@patch("frappe_paystack.api.resolve_paystack_settings")
	def test_create_payment_link_with_settings(self, mock_settings):
		"""create_payment_link creates a Pending log and returns a URL."""
		mock_settings.return_value = {
			"public_key": "pk_test_123",
			"secret_key": "sk_test_123",
			"webhook_secret": "sk_test_123",
			"default_currency": "NGN",
		}

		sinv = self.create_sales_invoice()
		self.addCleanup(self.cleanup_sales_invoice, sinv.name)

		url = create_payment_link("Sales Invoice", sinv.name)
		self.assertIn("/paystack-checkout/", url)

		log_name = url.split("/paystack-checkout/")[-1]
		log = frappe.get_doc("Paystack Payment Log", log_name)
		self.addCleanup(self.cleanup_log, log_name)

		self.assertEqual(log.status, "Pending")
		self.assertEqual(log.linked_doctype, "Sales Invoice")
		self.assertEqual(log.linked_docname, sinv.name)

	@patch("frappe_paystack.api.resolve_paystack_settings")
	def test_create_payment_link_custom_amount(self, mock_settings):
		"""create_payment_link with an explicit amount uses that amount."""
		mock_settings.return_value = {
			"public_key": "pk_test_123",
			"secret_key": "sk_test_123",
			"webhook_secret": "sk_test_123",
			"default_currency": "NGN",
		}

		sinv = self.create_sales_invoice()
		self.addCleanup(self.cleanup_sales_invoice, sinv.name)

		url = create_payment_link("Sales Invoice", sinv.name, amount=500.0)
		log_name = url.split("/paystack-checkout/")[-1]
		self.addCleanup(self.cleanup_log, log_name)

		log = frappe.get_doc("Paystack Payment Log", log_name)
		self.assertEqual(log.amount, 500.0)

	def test_validate_payment_link_nonexistent(self):
		"""validate_payment_link returns empty dict for nonexistent log."""
		from frappe_paystack.api import validate_payment_link

		result = validate_payment_link("NONEXISTENT-LOG-999")
		self.assertEqual(result, {})

	def create_pending_log(self):
		"""Create a Pending Paystack Payment Log for testing."""
		log = frappe.get_doc(
			{
				"doctype": "Paystack Payment Log",
				"company": "_Test Company",
				"linked_doctype": "Sales Invoice",
				"linked_docname": "NONEXISTENT-INV",
				"amount": 1000,
				"currency": "NGN",
				"status": "Pending",
			}
		)
		log.flags.ignore_permissions = True
		log.insert()
		return log

	def create_sales_invoice(self):
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
						"rate": 1000,
					}
				],
			}
		)
		sinv.flags.ignore_permissions = True
		sinv.insert()
		sinv.submit()
		return sinv

	def cleanup_log(self, name):
		"""Delete a Paystack Payment Log if it exists."""
		if frappe.db.exists("Paystack Payment Log", name):
			frappe.delete_doc(
				"Paystack Payment Log",
				name,
				force=True,
				ignore_permissions=True,
			)

	def cleanup_sales_invoice(self, name):
		"""Cancel and delete a Sales Invoice if it exists."""
		if frappe.db.exists("Sales Invoice", name):
			sinv = frappe.get_doc("Sales Invoice", name)
			if sinv.docstatus == 1:
				sinv.flags.ignore_permissions = True
				sinv.cancel()
			frappe.delete_doc(
				"Sales Invoice",
				name,
				force=True,
				ignore_permissions=True,
			)