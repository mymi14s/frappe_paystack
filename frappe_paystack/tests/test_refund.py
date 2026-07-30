import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from frappe_paystack.frappe_paystack.doctype.paystack_refund_log.paystack_refund_log import (
	get_total_refunded,
)
from frappe_paystack.utils import initiate_refund


class TestPaystackRefundApi(FrappeTestCase):
	"""Tests for the Paystack refund API integration in utils.py."""

	@patch("frappe_paystack.utils.requests.post")
	@patch("frappe_paystack.utils.resolve_paystack_settings")
	def test_initiate_refund_success(self, mock_settings, mock_post):
		"""initiate_refund returns parsed result on a successful API response."""
		mock_settings.return_value = {
			"secret_key": "sk_test_123",
			"webhook_secret": "sk_test_123",
		}

		mock_response = MagicMock()
		mock_response.ok = True
		mock_response.json.return_value = {
			"status": True,
			"message": "Refund processed",
			"data": {
				"status": "processed",
				"reference": "rfd_abc123",
				"amount": 50000,
				"currency": "NGN",
			},
		}
		mock_post.return_value = mock_response

		result = initiate_refund(
			transaction_id="ref_pay_001",
			amount=500.0,
			currency="NGN",
			company="_Test Company",
			merchant_note="Customer complaint",
		)

		self.assertEqual(result["status"], "processed")
		self.assertEqual(result["reference"], "rfd_abc123")
		self.assertEqual(result["amount"], 500.0)
		self.assertEqual(result["currency"], "NGN")
		mock_post.assert_called_once()

	@patch("frappe_paystack.utils.requests.post")
	@patch("frappe_paystack.utils.resolve_paystack_settings")
	def test_initiate_refund_api_failure_throws(self, mock_settings, mock_post):
		"""initiate_refund throws when the API returns status=False."""
		mock_settings.return_value = {"secret_key": "sk_test_123"}

		mock_response = MagicMock()
		mock_response.ok = True
		mock_response.json.return_value = {
			"status": False,
			"message": "Transaction not found",
		}
		mock_post.return_value = mock_response

		with self.assertRaises(frappe.ValidationError):
			initiate_refund(
				transaction_id="bad_ref",
				amount=100.0,
				currency="NGN",
				company="_Test Company",
			)

	@patch("frappe_paystack.utils.requests.post")
	@patch("frappe_paystack.utils.resolve_paystack_settings")
	def test_initiate_refund_network_error_throws(self, mock_settings, mock_post):
		"""initiate_refund throws when the API call raises a network error."""
		import requests as req

		mock_settings.return_value = {"secret_key": "sk_test_123"}
		mock_post.side_effect = req.ConnectionError("Connection refused")

		with self.assertRaises(frappe.ValidationError):
			initiate_refund(
				transaction_id="ref_net_err",
				amount=100.0,
				currency="NGN",
				company="_Test Company",
			)

	@patch("frappe_paystack.utils.resolve_paystack_settings")
	def test_initiate_refund_throws_when_not_enabled(self, mock_settings):
		"""initiate_refund throws when Paystack is not enabled for the company."""
		mock_settings.return_value = None

		with self.assertRaises(frappe.ValidationError):
			initiate_refund(
				transaction_id="ref_001",
				amount=100.0,
				currency="NGN",
				company="_Test Company",
			)

	@patch("frappe_paystack.utils.requests.post")
	@patch("frappe_paystack.utils.resolve_paystack_settings")
	def test_initiate_refund_sends_correct_minor_units(self, mock_settings, mock_post):
		"""initiate_refund converts the amount to minor units in the API body."""
		mock_settings.return_value = {"secret_key": "sk_test_123"}

		mock_response = MagicMock()
		mock_response.ok = True
		mock_response.json.return_value = {
			"status": True,
			"data": {"status": "processed", "reference": "rfd_1", "amount": 10000, "currency": "NGN"},
		}
		mock_post.return_value = mock_response

		initiate_refund(
			transaction_id="ref_100",
			amount=100.0,
			currency="NGN",
			company="_Test Company",
		)

		call_body = mock_post.call_args.kwargs.get("json", {})
		self.assertEqual(call_body["amount"], 10000)


class TestPaystackRefundLogDoctype(FrappeTestCase):
	"""Tests for the Paystack Refund Log doctype behavior."""

	def test_validate_throws_without_payment_log(self):
		"""validate must throw when no payment_log is set."""
		refund_log = frappe.get_doc(
			{
				"doctype": "Paystack Refund Log",
				"company": "_Test Company",
				"refund_amount": 100,
				"currency": "NGN",
				"status": "Pending",
			}
		)
		with self.assertRaises(frappe.ValidationError):
			refund_log.validate()

	def test_validate_throws_when_payment_log_not_completed(self):
		"""validate must throw when the referenced Payment Log is not Completed."""
		payment_log = self.create_payment_log(status="Pending")
		self.addCleanup(self.force_cleanup_payment_log, payment_log.name)

		refund_log = frappe.get_doc(
			{
				"doctype": "Paystack Refund Log",
				"payment_log": payment_log.name,
				"company": "_Test Company",
				"refund_amount": 100,
				"currency": "NGN",
				"status": "Pending",
			}
		)
		with self.assertRaises(frappe.ValidationError):
			refund_log.validate()

	def test_validate_throws_for_zero_refund_amount(self):
		"""validate must throw when refund_amount is zero or negative."""
		payment_log = self.create_payment_log(status="Completed", amount_paid=500)
		self.addCleanup(self.force_cleanup_payment_log, payment_log.name)

		refund_log = frappe.get_doc(
			{
				"doctype": "Paystack Refund Log",
				"payment_log": payment_log.name,
				"company": "_Test Company",
				"refund_amount": 0,
				"currency": "NGN",
				"status": "Pending",
			}
		)
		with self.assertRaises(frappe.ValidationError):
			refund_log.validate()

	def test_validate_throws_when_refund_exceeds_amount_paid(self):
		"""validate must throw when refund_amount exceeds the original amount_paid."""
		payment_log = self.create_payment_log(status="Completed", amount_paid=200)
		self.addCleanup(self.force_cleanup_payment_log, payment_log.name)

		refund_log = frappe.get_doc(
			{
				"doctype": "Paystack Refund Log",
				"payment_log": payment_log.name,
				"company": "_Test Company",
				"refund_amount": 300,
				"currency": "NGN",
				"status": "Pending",
			}
		)
		with self.assertRaises(frappe.ValidationError):
			refund_log.validate()

	def test_validate_prevents_over_refund_with_existing_refunds(self):
		"""validate must throw when total refunds exceed amount_paid."""
		payment_log = self.create_payment_log(status="Completed", amount_paid=500)
		self.addCleanup(self.force_cleanup_payment_log, payment_log.name)

		existing_refund = self.create_refund_log(
			payment_log.name, refund_amount=300, status="Completed"
		)
		self.addCleanup(self.force_cleanup_refund_log, existing_refund.name)

		new_refund = frappe.get_doc(
			{
				"doctype": "Paystack Refund Log",
				"payment_log": payment_log.name,
				"company": "_Test Company",
				"refund_amount": 250,
				"currency": "NGN",
				"status": "Pending",
			}
		)
		with self.assertRaises(frappe.ValidationError):
			new_refund.validate()

	def test_validate_allows_partial_refund_within_available(self):
		"""validate must allow a partial refund that fits within the available amount."""
		payment_log = self.create_payment_log(status="Completed", amount_paid=500)
		self.addCleanup(self.force_cleanup_payment_log, payment_log.name)

		existing_refund = self.create_refund_log(
			payment_log.name, refund_amount=200, status="Completed"
		)
		self.addCleanup(self.force_cleanup_refund_log, existing_refund.name)

		new_refund = frappe.get_doc(
			{
				"doctype": "Paystack Refund Log",
				"payment_log": payment_log.name,
				"company": "_Test Company",
				"refund_amount": 300,
				"currency": "NGN",
				"status": "Pending",
			}
		)
		new_refund.validate()

	def test_on_trash_blocks_completed_refund(self):
		"""on_trash must prevent deletion of a Completed Refund Log."""
		payment_log = self.create_payment_log(status="Completed", amount_paid=500)
		self.addCleanup(self.force_cleanup_payment_log, payment_log.name)

		refund_log = self.create_refund_log(
			payment_log.name, refund_amount=100, status="Completed"
		)
		self.addCleanup(self.force_cleanup_refund_log, refund_log.name)

		with self.assertRaises(frappe.ValidationError):
			refund_log.on_trash()

	def test_on_trash_allows_pending_without_reversal(self):
		"""on_trash must allow deletion of a Pending Refund Log without a reversal PE."""
		payment_log = self.create_payment_log(status="Completed", amount_paid=500)
		self.addCleanup(self.force_cleanup_payment_log, payment_log.name)

		refund_log = self.create_refund_log(
			payment_log.name, refund_amount=100, status="Pending"
		)
		self.addCleanup(self.force_cleanup_refund_log, refund_log.name)

		refund_log.on_trash()

	def test_get_total_refunded_returns_zero_for_no_refunds(self):
		"""get_total_refunded returns 0 when no refunds exist."""
		payment_log = self.create_payment_log(status="Completed", amount_paid=500)
		self.addCleanup(self.force_cleanup_payment_log, payment_log.name)

		self.assertEqual(get_total_refunded(payment_log.name), 0.0)

	def test_get_total_refunded_sums_completed_refunds(self):
		"""get_total_refunded sums only Processed/Completed refund amounts."""
		payment_log = self.create_payment_log(status="Completed", amount_paid=1000)
		self.addCleanup(self.force_cleanup_payment_log, payment_log.name)

		r1 = self.create_refund_log(payment_log.name, refund_amount=200, status="Completed")
		self.addCleanup(self.force_cleanup_refund_log, r1.name)
		r2 = self.create_refund_log(payment_log.name, refund_amount=300, status="Processed")
		self.addCleanup(self.force_cleanup_refund_log, r2.name)
		r3 = self.create_refund_log(payment_log.name, refund_amount=100, status="Failed")
		self.addCleanup(self.force_cleanup_refund_log, r3.name)

		total = get_total_refunded(payment_log.name)
		self.assertEqual(total, 500.0)

	def test_get_total_refunded_excludes_specified_log(self):
		"""get_total_refunded excludes a specific log when computing available."""
		payment_log = self.create_payment_log(status="Completed", amount_paid=1000)
		self.addCleanup(self.force_cleanup_payment_log, payment_log.name)

		r1 = self.create_refund_log(payment_log.name, refund_amount=200, status="Completed")
		self.addCleanup(self.force_cleanup_refund_log, r1.name)

		total = get_total_refunded(payment_log.name, exclude=r1.name)
		self.assertEqual(total, 0.0)

	def create_payment_log(self, status="Completed", amount_paid=500):
		"""Create a Paystack Payment Log for testing."""
		log = frappe.get_doc(
			{
				"doctype": "Paystack Payment Log",
				"company": "_Test Company",
				"linked_doctype": "Sales Invoice",
				"linked_docname": "NONEXISTENT-INV",
				"amount": amount_paid,
				"amount_paid": amount_paid,
				"currency": "NGN",
				"status": status,
				"transaction_id": f"ref_test_{frappe.utils.random_string(8)}",
			}
		)
		log.flags.ignore_permissions = True
		log.insert()
		return log

	def create_refund_log(self, payment_log_name, refund_amount=100, status="Pending"):
		"""Create a Paystack Refund Log for testing, bypassing validation."""
		refund_log = frappe.get_doc(
			{
				"doctype": "Paystack Refund Log",
				"payment_log": payment_log_name,
				"company": "_Test Company",
				"refund_amount": refund_amount,
				"currency": "NGN",
				"status": status,
				"transaction_id": f"ref_test_{frappe.utils.random_string(8)}",
			}
		)
		refund_log.flags.ignore_permissions = True
		refund_log.flags.ignore_validate = True
		refund_log.insert()
		return refund_log

	def force_cleanup_payment_log(self, name):
		if frappe.db.exists("Paystack Payment Log", name):
			frappe.db.set_value("Paystack Payment Log", name, "status", "Pending")
			frappe.db.set_value("Paystack Payment Log", name, "payment_entry", None)
			frappe.delete_doc(
				"Paystack Payment Log", name, force=True, ignore_permissions=True
			)

	def force_cleanup_refund_log(self, name):
		if frappe.db.exists("Paystack Refund Log", name):
			frappe.db.set_value("Paystack Refund Log", name, "status", "Pending")
			frappe.db.set_value(
				"Paystack Refund Log", name, "reversal_payment_entry", None
			)
			frappe.delete_doc(
				"Paystack Refund Log", name, force=True, ignore_permissions=True
			)


class TestPaystackRefundWebhook(FrappeTestCase):
	"""Tests for refund webhook event processing."""

	def test_process_refund_webhook_updates_log_to_processed(self):
		"""A refund.processed webhook updates the Refund Log to Processed."""
		from frappe_paystack.api import process_refund_webhook_event

		payment_log = self.create_payment_log(status="Completed", amount_paid=500)
		self.addCleanup(self.force_cleanup_payment_log, payment_log.name)

		refund_log = self.create_refund_log(
			payment_log.name, refund_amount=200, status="Pending"
		)
		self.addCleanup(self.force_cleanup_refund_log, refund_log.name)

		webhook_data = {
			"event": "refund.processed",
			"data": {
				"reference": refund_log.refund_reference or "rfd_test_001",
				"transaction": {"reference": payment_log.transaction_id},
				"amount": 20000,
				"currency": "NGN",
				"status": "processed",
			},
		}

		frappe.db.set_value(
			"Paystack Refund Log",
			refund_log.name,
			"refund_reference",
			"rfd_test_001",
		)

		process_refund_webhook_event(webhook_data)

		refund_log.reload()
		self.assertEqual(refund_log.status, "Processed")

	def test_process_refund_webhook_marks_failed(self):
		"""A refund.failed webhook sets the Refund Log status to Failed."""
		from frappe_paystack.api import process_refund_webhook_event

		payment_log = self.create_payment_log(status="Completed", amount_paid=500)
		self.addCleanup(self.force_cleanup_payment_log, payment_log.name)

		refund_log = self.create_refund_log(
			payment_log.name, refund_amount=200, status="Pending"
		)
		self.addCleanup(self.force_cleanup_refund_log, refund_log.name)

		frappe.db.set_value(
			"Paystack Refund Log", refund_log.name, "refund_reference", "rfd_fail_001"
		)

		webhook_data = {
			"event": "refund.failed",
			"data": {
				"reference": "rfd_fail_001",
				"transaction": {"reference": payment_log.transaction_id},
				"amount": 20000,
				"currency": "NGN",
				"status": "failed",
			},
		}

		process_refund_webhook_event(webhook_data)

		refund_log.reload()
		self.assertEqual(refund_log.status, "Failed")

	def test_process_refund_webhook_skips_already_processed(self):
		"""A refund webhook for an already-Processed log must not reprocess."""
		from frappe_paystack.api import process_refund_webhook_event

		payment_log = self.create_payment_log(status="Completed", amount_paid=500)
		self.addCleanup(self.force_cleanup_payment_log, payment_log.name)

		refund_log = self.create_refund_log(
			payment_log.name, refund_amount=200, status="Processed"
		)
		self.addCleanup(self.force_cleanup_refund_log, refund_log.name)

		frappe.db.set_value(
			"Paystack Refund Log", refund_log.name, "refund_reference", "rfd_skip_001"
		)

		webhook_data = {
			"event": "refund.processed",
			"data": {
				"reference": "rfd_skip_001",
				"transaction": {"reference": payment_log.transaction_id},
				"amount": 99999,
				"currency": "NGN",
				"status": "processed",
			},
		}

		process_refund_webhook_event(webhook_data)

		refund_log.reload()
		self.assertEqual(refund_log.status, "Processed")

	def test_process_refund_webhook_nonexistent_log_does_not_raise(self):
		"""A refund webhook for a nonexistent Refund Log must not raise."""
		from frappe_paystack.api import process_refund_webhook_event

		webhook_data = {
			"event": "refund.processed",
			"data": {
				"reference": "rfd_ghost",
				"transaction": {"reference": "ref_ghost"},
				"amount": 20000,
				"currency": "NGN",
			},
		}
		process_refund_webhook_event(webhook_data)

	def create_payment_log(self, status="Completed", amount_paid=500):
		log = frappe.get_doc(
			{
				"doctype": "Paystack Payment Log",
				"company": "_Test Company",
				"linked_doctype": "Sales Invoice",
				"linked_docname": "NONEXISTENT-INV",
				"amount": amount_paid,
				"amount_paid": amount_paid,
				"currency": "NGN",
				"status": status,
				"transaction_id": f"ref_wh_{frappe.utils.random_string(8)}",
			}
		)
		log.flags.ignore_permissions = True
		log.insert()
		return log

	def create_refund_log(self, payment_log_name, refund_amount=100, status="Pending"):
		refund_log = frappe.get_doc(
			{
				"doctype": "Paystack Refund Log",
				"payment_log": payment_log_name,
				"company": "_Test Company",
				"refund_amount": refund_amount,
				"currency": "NGN",
				"status": status,
				"transaction_id": f"rfd_wh_{frappe.utils.random_string(8)}",
			}
		)
		refund_log.flags.ignore_permissions = True
		refund_log.flags.ignore_validate = True
		refund_log.insert()
		return refund_log

	def force_cleanup_payment_log(self, name):
		if frappe.db.exists("Paystack Payment Log", name):
			frappe.db.set_value("Paystack Payment Log", name, "status", "Pending")
			frappe.db.set_value("Paystack Payment Log", name, "payment_entry", None)
			frappe.delete_doc(
				"Paystack Payment Log", name, force=True, ignore_permissions=True
			)

	def force_cleanup_refund_log(self, name):
		if frappe.db.exists("Paystack Refund Log", name):
			frappe.db.set_value("Paystack Refund Log", name, "status", "Pending")
			frappe.db.set_value(
				"Paystack Refund Log", name, "reversal_payment_entry", None
			)
			frappe.delete_doc(
				"Paystack Refund Log", name, force=True, ignore_permissions=True
			)


class TestPaystackManualRefund(FrappeTestCase):
	"""Tests for the manual refund API endpoint."""

	@patch("frappe_paystack.utils.requests.post")
	@patch("frappe_paystack.utils.resolve_paystack_settings")
	def test_initiate_refund_from_log_creates_refund_log(
		self, mock_settings, mock_post
	):
		"""initiate_refund_from_log creates a Refund Log and calls the Paystack API."""
		from frappe_paystack.api import initiate_refund_from_log

		mock_settings.return_value = {"secret_key": "sk_test_123"}

		mock_response = MagicMock()
		mock_response.ok = True
		mock_response.json.return_value = {
			"status": True,
			"data": {
				"status": "processed",
				"reference": "rfd_manual_001",
				"amount": 20000,
				"currency": "NGN",
			},
		}
		mock_post.return_value = mock_response

		payment_log = self.create_payment_log(status="Completed", amount_paid=500)
		self.addCleanup(self.force_cleanup_payment_log, payment_log.name)

		refund_log_name = initiate_refund_from_log(
			payment_log_name=payment_log.name,
			amount=200.0,
			reason="Customer request",
		)
		self.assertTrue(frappe.db.exists("Paystack Refund Log", refund_log_name))
		self.addCleanup(self.force_cleanup_refund_log, refund_log_name)

		refund_log = frappe.get_doc("Paystack Refund Log", refund_log_name)
		self.assertEqual(refund_log.payment_log, payment_log.name)
		self.assertEqual(refund_log.refund_amount, 200.0)

	def test_initiate_refund_from_log_throws_for_non_completed_payment(self):
		"""initiate_refund_from_log must throw when the Payment Log is not Completed."""
		from frappe_paystack.api import initiate_refund_from_log

		payment_log = self.create_payment_log(status="Pending", amount_paid=0)
		self.addCleanup(self.force_cleanup_payment_log, payment_log.name)

		with self.assertRaises(frappe.ValidationError):
			initiate_refund_from_log(
				payment_log_name=payment_log.name,
				amount=100.0,
			)

	def create_payment_log(self, status="Completed", amount_paid=500):
		log = frappe.get_doc(
			{
				"doctype": "Paystack Payment Log",
				"company": "_Test Company",
				"linked_doctype": "Sales Invoice",
				"linked_docname": "NONEXISTENT-INV",
				"amount": amount_paid,
				"amount_paid": amount_paid,
				"currency": "NGN",
				"status": status,
				"transaction_id": f"ref_manual_{frappe.utils.random_string(8)}",
			}
		)
		log.flags.ignore_permissions = True
		log.insert()
		return log

	def force_cleanup_payment_log(self, name):
		if frappe.db.exists("Paystack Payment Log", name):
			frappe.db.set_value("Paystack Payment Log", name, "status", "Pending")
			frappe.db.set_value("Paystack Payment Log", name, "payment_entry", None)
			frappe.delete_doc(
				"Paystack Payment Log", name, force=True, ignore_permissions=True
			)

	def force_cleanup_refund_log(self, name):
		if frappe.db.exists("Paystack Refund Log", name):
			frappe.db.set_value("Paystack Refund Log", name, "status", "Pending")
			frappe.db.set_value(
				"Paystack Refund Log", name, "reversal_payment_entry", None
			)
			frappe.delete_doc(
				"Paystack Refund Log", name, force=True, ignore_permissions=True
			)