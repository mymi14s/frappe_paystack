import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_paystack.tests.factories import (
	GatewaySettingFactory,
	PaymentLogFactory,
	SalesInvoiceFactory,
)


class TestPaystackPaymentLog(FrappeTestCase):
	"""Tests for the Paystack Payment Log doctype."""

	def test_on_trash_blocks_processed_log(self):
		"""on_trash must prevent deletion of a Processed log."""
		log_name = PaymentLogFactory.create(status="Processed", amount=1000)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		log = frappe.get_doc("Paystack Payment Log", log_name)
		with self.assertRaises(frappe.ValidationError):
			log.on_trash()

	def test_on_trash_blocks_completed_log(self):
		"""on_trash must prevent deletion of a Completed log."""
		log_name = PaymentLogFactory.create_completed(amount=1000, amount_paid=1000)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		log = frappe.get_doc("Paystack Payment Log", log_name)
		with self.assertRaises(frappe.ValidationError):
			log.on_trash()

	def test_on_trash_blocks_log_with_linked_payment_entry(self):
		"""on_trash must prevent deletion when payment_entry is linked, even if Pending."""
		log_name = PaymentLogFactory.create(status="Pending", amount=1000)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		frappe.db.set_value(
			"Paystack Payment Log", log_name, "payment_entry", "PE-FAKE-001"
		)
		log = frappe.get_doc("Paystack Payment Log", log_name)
		with self.assertRaises(frappe.ValidationError):
			log.on_trash()

	def test_on_trash_allows_pending_without_payment_entry(self):
		"""on_trash must allow deletion of a Pending log with no linked Payment Entry."""
		log_name = PaymentLogFactory.create(status="Pending", amount=1000)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		log = frappe.get_doc("Paystack Payment Log", log_name)
		log.on_trash()

	def test_on_trash_allows_failed_without_payment_entry(self):
		"""on_trash must allow deletion of a Failed log with no linked Payment Entry."""
		log_name = PaymentLogFactory.create(status="Failed", amount=1000)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		log = frappe.get_doc("Paystack Payment Log", log_name)
		log.on_trash()

	def test_validate_throws_for_negative_amount(self):
		"""validate must reject negative amounts."""
		log_name = PaymentLogFactory.create(status="Pending", amount=1000)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		log = frappe.get_doc("Paystack Payment Log", log_name)
		log.amount = -100
		with self.assertRaises(frappe.ValidationError):
			log.validate()

	def test_validate_throws_for_invalid_status(self):
		"""validate must reject an invalid status value."""
		log_name = PaymentLogFactory.create(status="Pending", amount=1000)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		log = frappe.get_doc("Paystack Payment Log", log_name)
		log.status = "NotARealStatus"
		with self.assertRaises(frappe.ValidationError):
			log.validate()

	def test_validate_normalizes_currency_to_uppercase(self):
		"""validate must normalize lowercase currency codes to uppercase."""
		log_name = PaymentLogFactory.create(status="Pending", amount=1000)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		log = frappe.get_doc("Paystack Payment Log", log_name)
		log.currency = "usd"
		log.validate()
		self.assertEqual(log.currency, "USD")

	def test_validate_defaults_unsupported_currency_to_ngn(self):
		"""validate must default an unsupported currency to NGN."""
		log_name = PaymentLogFactory.create(status="Pending", amount=1000)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		log = frappe.get_doc("Paystack Payment Log", log_name)
		log.currency = "EUR"
		log.validate()
		self.assertEqual(log.currency, "NGN")

	def test_validate_throws_when_only_doctype_set(self):
		"""validate must throw when linked_doctype is set but linked_docname is not."""
		log_name = PaymentLogFactory.create(status="Pending", amount=1000)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		log = frappe.get_doc("Paystack Payment Log", log_name)
		log.linked_doctype = "Sales Invoice"
		log.linked_docname = None
		with self.assertRaises(frappe.ValidationError):
			log.validate()

	def test_get_payment_link_contains_log_name_and_checkout_path(self):
		"""get_payment_link must return a URL with /paystack-checkout/ and the log name."""
		log_name = PaymentLogFactory.create(status="Pending", amount=1000)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		log = frappe.get_doc("Paystack Payment Log", log_name)
		url = log.get_payment_link()
		self.assertIn("/paystack-checkout/", url)
		self.assertIn(log_name, url)

	def test_get_payment_public_key_returns_none_when_no_gateway_enabled(self):
		"""get_payment_public_key must return None when no gateway is configured."""
		log_name = PaymentLogFactory.create(status="Pending", amount=1000)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		log = frappe.get_doc("Paystack Payment Log", log_name)
		self.assertIsNone(log.get_payment_public_key())

	def test_get_payment_public_key_returns_settings_when_gateway_enabled(self):
		"""get_payment_public_key must return a dict with keys when a gateway is enabled."""
		gateway_name = GatewaySettingFactory.create()
		self.addCleanup(GatewaySettingFactory.cleanup, gateway_name)

		log_name = PaymentLogFactory.create(status="Pending", amount=1000)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		log = frappe.get_doc("Paystack Payment Log", log_name)
		result = log.get_payment_public_key()
		self.assertIsNotNone(result)
		self.assertEqual(result["public_key"], "pk_test_123")
		self.assertEqual(result["currency"], "NGN")
		self.assertIn("suspense_account", result)
		self.assertIn("mode_of_payment", result)

	def test_validate_record_reports_error_for_nonexistent_linked_doc(self):
		"""validate_record must return an error message for a nonexistent linked doc."""
		log_name = PaymentLogFactory.create(status="Pending", amount=1000)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		log = frappe.get_doc("Paystack Payment Log", log_name)
		log.linked_doctype = "Sales Invoice"
		log.linked_docname = "NONEXISTENT-INV-999"
		result = log.validate_record()
		self.assertIn("Document not found", result)

	def test_validate_record_returns_empty_for_completed_status(self):
		"""validate_record must return empty string when status is Completed."""
		log_name = PaymentLogFactory.create_completed(amount=1000, amount_paid=1000)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		log = frappe.get_doc("Paystack Payment Log", log_name)
		result = log.validate_record()
		self.assertEqual(result, "")

	def test_get_data_returns_checkout_data_for_real_invoice(self):
		"""get_data must return customer, amounts, and reference for a real invoice."""
		log_name = PaymentLogFactory.create(status="Pending", amount=5000)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		log = frappe.get_doc("Paystack Payment Log", log_name)
		data = log.get_data()
		self.assertEqual(data["customer"], "_Test Customer")
		self.assertEqual(data["reference_doctype"], "Sales Invoice")
		self.assertEqual(data["reference"], log_name)
		self.assertEqual(data["status"], "Pending")
		self.assertAlmostEqual(data["grand_total"], 5000, places=2)

	def test_get_data_includes_gateway_settings_when_enabled(self):
		"""get_data must include public_key and currency when a gateway is enabled."""
		gateway_name = GatewaySettingFactory.create()
		self.addCleanup(GatewaySettingFactory.cleanup, gateway_name)

		log_name = PaymentLogFactory.create(status="Pending", amount=3000)
		self.addCleanup(PaymentLogFactory.cleanup, log_name)

		log = frappe.get_doc("Paystack Payment Log", log_name)
		data = log.get_data()
		self.assertEqual(data["public_key"], "pk_test_123")
		self.assertEqual(data["currency"], "NGN")

	def test_transaction_id_has_unique_constraint(self):
		"""The transaction_id field must be unique to prevent duplicate processing."""
		meta = frappe.get_meta("Paystack Payment Log")
		field = meta.get_field("transaction_id")
		self.assertTrue(field.unique, "transaction_id must be unique for idempotency")