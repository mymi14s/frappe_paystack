import requests
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from frappe_paystack.api import (
	initiate_refund_from_log,
	process_refund_webhook_event,
)
from frappe_paystack.frappe_paystack.doctype.paystack_refund_log.paystack_refund_log import (
	get_total_refunded,
)
from frappe_paystack.tests.factories import (
	GatewaySettingFactory,
	PaymentLogFactory,
	RefundLogFactory,
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

		mock_settings.return_value = {"secret_key": "sk_test_123"}
		mock_post.side_effect = requests.ConnectionError("Connection refused")

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
			"data": {
				"status": "processed",
				"reference": "rfd_1",
				"amount": 10000,
				"currency": "NGN",
			},
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

	def setUp(self):
		self.gateway_name = GatewaySettingFactory.create()
		self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

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
		log_name = PaymentLogFactory.create(status="Pending", amount=500)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		refund_log = frappe.get_doc(
			{
				"doctype": "Paystack Refund Log",
				"payment_log": log_name,
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
		log_name = PaymentLogFactory.create_completed(amount=500, amount_paid=500)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		refund_log = frappe.get_doc(
			{
				"doctype": "Paystack Refund Log",
				"payment_log": log_name,
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
		log_name = PaymentLogFactory.create_completed(amount=200, amount_paid=200)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		refund_log = frappe.get_doc(
			{
				"doctype": "Paystack Refund Log",
				"payment_log": log_name,
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
		log_name = PaymentLogFactory.create_completed(amount=500, amount_paid=500)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		existing_refund = RefundLogFactory.create_bypass_validate(
			log_name, refund_amount=300, status="Completed"
		)
		self.addCleanup(RefundLogFactory.cleanup, existing_refund)

		new_refund = frappe.get_doc(
			{
				"doctype": "Paystack Refund Log",
				"payment_log": log_name,
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
		log_name = PaymentLogFactory.create_completed(amount=500, amount_paid=500)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		existing_refund = RefundLogFactory.create_bypass_validate(
			log_name, refund_amount=200, status="Completed"
		)
		self.addCleanup(RefundLogFactory.cleanup, existing_refund)

		new_refund = frappe.get_doc(
			{
				"doctype": "Paystack Refund Log",
				"payment_log": log_name,
				"company": "_Test Company",
				"refund_amount": 300,
				"currency": "NGN",
				"status": "Pending",
			}
		)
		new_refund.validate()

	def test_on_trash_blocks_completed_refund(self):
		"""on_trash must prevent deletion of a Completed Refund Log."""
		log_name = PaymentLogFactory.create_completed(amount=500, amount_paid=500)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		refund_name = RefundLogFactory.create_bypass_validate(
			log_name, refund_amount=100, status="Completed"
		)
		self.addCleanup(RefundLogFactory.cleanup, refund_name)

		refund_log = frappe.get_doc("Paystack Refund Log", refund_name)
		with self.assertRaises(frappe.ValidationError):
			refund_log.on_trash()

	def test_on_trash_allows_pending_without_reversal(self):
		"""on_trash must allow deletion of a Pending Refund Log without a reversal PE."""
		log_name = PaymentLogFactory.create_completed(amount=500, amount_paid=500)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		refund_name = RefundLogFactory.create_bypass_validate(
			log_name, refund_amount=100, status="Pending"
		)
		self.addCleanup(RefundLogFactory.cleanup, refund_name)

		refund_log = frappe.get_doc("Paystack Refund Log", refund_name)
		refund_log.on_trash()

	def test_get_total_refunded_returns_zero_for_no_refunds(self):
		"""get_total_refunded returns 0 when no refunds exist."""
		log_name = PaymentLogFactory.create_completed(amount=500, amount_paid=500)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		self.assertEqual(get_total_refunded(log_name), 0.0)

	def test_get_total_refunded_sums_completed_refunds(self):
		"""get_total_refunded sums only Processed/Completed refund amounts."""
		log_name = PaymentLogFactory.create_completed(amount=1000, amount_paid=1000)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		r1 = RefundLogFactory.create_bypass_validate(
			log_name, refund_amount=200, status="Completed"
		)
		self.addCleanup(RefundLogFactory.cleanup, r1)
		r2 = RefundLogFactory.create_bypass_validate(
			log_name, refund_amount=300, status="Processed"
		)
		self.addCleanup(RefundLogFactory.cleanup, r2)
		r3 = RefundLogFactory.create_bypass_validate(
			log_name, refund_amount=100, status="Failed"
		)
		self.addCleanup(RefundLogFactory.cleanup, r3)

		total = get_total_refunded(log_name)
		self.assertEqual(total, 500.0)

	def test_get_total_refunded_excludes_specified_log(self):
		"""get_total_refunded excludes a specific log when computing available."""
		log_name = PaymentLogFactory.create_completed(amount=1000, amount_paid=1000)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		r1 = RefundLogFactory.create_bypass_validate(
			log_name, refund_amount=200, status="Completed"
		)
		self.addCleanup(RefundLogFactory.cleanup, r1)

		total = get_total_refunded(log_name, exclude=r1)
		self.assertEqual(total, 0.0)


class TestPaystackRefundWebhook(FrappeTestCase):
	"""Tests for refund webhook event processing."""

	def setUp(self):
		self.gateway_name = GatewaySettingFactory.create()
		self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

	def test_process_refund_webhook_updates_log_to_processed(self):
		"""A refund.processed webhook updates the Refund Log to Processed."""

		log_name = PaymentLogFactory.create_completed(amount=500, amount_paid=500)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		refund_name = RefundLogFactory.create_bypass_validate(
			log_name, refund_amount=200, status="Pending", refund_reference="rfd_test_001"
		)
		self.addCleanup(RefundLogFactory.cleanup, refund_name)

		transaction_id = frappe.db.get_value(
			"Paystack Payment Log", log_name, "transaction_id"
		)

		webhook_data = {
			"event": "refund.processed",
			"data": {
				"reference": "rfd_test_001",
				"transaction": {"reference": transaction_id},
				"amount": 20000,
				"currency": "NGN",
				"status": "processed",
			},
		}

		process_refund_webhook_event(webhook_data)

		refund_log = frappe.get_doc("Paystack Refund Log", refund_name)
		self.assertEqual(refund_log.status, "Processed")

	def test_process_refund_webhook_marks_failed(self):
		"""A refund.failed webhook sets the Refund Log status to Failed."""

		log_name = PaymentLogFactory.create_completed(amount=500, amount_paid=500)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		refund_name = RefundLogFactory.create_bypass_validate(
			log_name, refund_amount=200, status="Pending", refund_reference="rfd_fail_001"
		)
		self.addCleanup(RefundLogFactory.cleanup, refund_name)

		transaction_id = frappe.db.get_value(
			"Paystack Payment Log", log_name, "transaction_id"
		)

		webhook_data = {
			"event": "refund.failed",
			"data": {
				"reference": "rfd_fail_001",
				"transaction": {"reference": transaction_id},
				"amount": 20000,
				"currency": "NGN",
				"status": "failed",
			},
		}

		process_refund_webhook_event(webhook_data)

		refund_log = frappe.get_doc("Paystack Refund Log", refund_name)
		self.assertEqual(refund_log.status, "Failed")

	def test_process_refund_webhook_skips_already_processed(self):
		"""A refund webhook for an already-Processed log must not reprocess."""

		log_name = PaymentLogFactory.create_completed(amount=500, amount_paid=500)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		refund_name = RefundLogFactory.create_bypass_validate(
			log_name, refund_amount=200, status="Processed", refund_reference="rfd_skip_001"
		)
		self.addCleanup(RefundLogFactory.cleanup, refund_name)

		transaction_id = frappe.db.get_value(
			"Paystack Payment Log", log_name, "transaction_id"
		)

		webhook_data = {
			"event": "refund.processed",
			"data": {
				"reference": "rfd_skip_001",
				"transaction": {"reference": transaction_id},
				"amount": 99999,
				"currency": "NGN",
				"status": "processed",
			},
		}

		process_refund_webhook_event(webhook_data)

		refund_log = frappe.get_doc("Paystack Refund Log", refund_name)
		self.assertEqual(refund_log.status, "Processed")

	def test_process_refund_webhook_nonexistent_log_does_not_raise(self):
		"""A refund webhook for a nonexistent Refund Log must not raise."""

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


class TestPaystackManualRefund(FrappeTestCase):
	"""Tests for the manual refund API endpoint."""

	def setUp(self):
		self.gateway_name = GatewaySettingFactory.create()
		self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

	@patch("frappe_paystack.utils.requests.post")
	@patch("frappe_paystack.utils.resolve_paystack_settings")
	def test_initiate_refund_from_log_creates_refund_log(self, mock_settings, mock_post):
		"""initiate_refund_from_log creates a Refund Log and calls the Paystack API."""

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

		log_name = PaymentLogFactory.create_completed(amount=500, amount_paid=500)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		refund_name = initiate_refund_from_log(
			payment_log_name=log_name,
			amount=200.0,
			reason="Customer request",
		)
		self.assertTrue(frappe.db.exists("Paystack Refund Log", refund_name))
		self.addCleanup(RefundLogFactory.cleanup, refund_name)

		refund_log = frappe.get_doc("Paystack Refund Log", refund_name)
		self.assertEqual(refund_log.payment_log, log_name)
		self.assertEqual(refund_log.refund_amount, 200.0)

	def test_initiate_refund_from_log_throws_for_non_completed_payment(self):
		"""initiate_refund_from_log must throw when the Payment Log is not Completed."""

		log_name = PaymentLogFactory.create(status="Pending", amount=100)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		with self.assertRaises(frappe.ValidationError):
			initiate_refund_from_log(
				payment_log_name=log_name,
				amount=100.0,
			)