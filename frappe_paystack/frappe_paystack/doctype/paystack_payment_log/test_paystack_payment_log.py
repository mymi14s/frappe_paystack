import frappe
from frappe.tests.utils import FrappeTestCase


class TestPaystackPaymentLog(FrappeTestCase):
	"""Tests for the Paystack Payment Log doctype."""

	def test_meta_exists(self):
		"""The doctype metadata must be loadable."""
		self.assertTrue(frappe.get_meta("Paystack Payment Log"))

	def test_transaction_id_field_is_unique(self):
		"""The transaction_id field must have unique=1 (Phase 1 idempotency)."""
		meta = frappe.get_meta("Paystack Payment Log")
		field = meta.get_field("transaction_id")
		self.assertTrue(field.unique)

	def test_idempotency_key_field_exists(self):
		"""The idempotency_key field must exist (Phase 1)."""
		meta = frappe.get_meta("Paystack Payment Log")
		self.assertTrue(meta.has_field("idempotency_key"))

	def test_integration_request_field_exists(self):
		"""The integration_request field must exist (Phase 1)."""
		meta = frappe.get_meta("Paystack Payment Log")
		self.assertTrue(meta.has_field("integration_request"))

	def test_on_trash_blocks_processed(self):
		"""on_trash must prevent deletion of Processed logs."""
		log = self.create_log(status="Processed")
		self.addCleanup(self.force_cleanup_log, log.name)

		with self.assertRaises(frappe.ValidationError):
			log.on_trash()

	def test_on_trash_blocks_completed(self):
		"""on_trash must prevent deletion of Completed logs."""
		log = self.create_log(status="Completed")
		self.addCleanup(self.force_cleanup_log, log.name)

		with self.assertRaises(frappe.ValidationError):
			log.on_trash()

	def test_on_trash_allows_pending(self):
		"""on_trash must allow deletion of Pending logs without a Payment Entry."""
		log = self.create_log(status="Pending")
		self.addCleanup(self.force_cleanup_log, log.name)

		log.on_trash()

	def test_on_trash_allows_failed(self):
		"""on_trash must allow deletion of Failed logs without a Payment Entry."""
		log = self.create_log(status="Failed")
		self.addCleanup(self.force_cleanup_log, log.name)

		log.on_trash()

	def test_validate_negative_amount(self):
		"""validate must throw for negative amounts."""
		log = self.create_log(status="Pending", amount=-100)
		self.addCleanup(self.force_cleanup_log, log.name)

		with self.assertRaises(frappe.ValidationError):
			log.validate()

	def test_validate_invalid_status(self):
		"""validate must throw for an invalid status value."""
		log = self.create_log(status="Pending")
		log.status = "InvalidStatus"
		self.addCleanup(self.force_cleanup_log, log.name)

		with self.assertRaises(frappe.ValidationError):
			log.validate()

	def test_validate_currency_normalization(self):
		"""validate must normalize the currency field."""
		log = self.create_log(status="Pending", currency="usd")
		self.addCleanup(self.force_cleanup_log, log.name)

		log.validate()
		self.assertEqual(log.currency, "USD")

	def test_validate_currency_invalid_defaults_to_ngn(self):
		"""validate must default invalid currencies to NGN."""
		log = self.create_log(status="Pending", currency="EUR")
		self.addCleanup(self.force_cleanup_log, log.name)

		log.validate()
		self.assertEqual(log.currency, "NGN")

	def test_get_payment_link(self):
		"""get_payment_link must return a URL containing the log name."""
		log = self.create_log(status="Pending")
		self.addCleanup(self.force_cleanup_log, log.name)

		url = log.get_payment_link()
		self.assertIn("/paystack-checkout/", url)
		self.assertIn(log.name, url)

	def test_get_payment_public_key_no_settings(self):
		"""get_payment_public_key must return None when no gateway is enabled."""
		log = self.create_log(status="Pending")
		self.addCleanup(self.force_cleanup_log, log.name)

		result = log.get_payment_public_key()
		self.assertIsNone(result)

	def test_validate_record_nonexistent_doc(self):
		"""validate_record must report an error for a nonexistent linked doc."""
		log = self.create_log(status="Pending")
		log.linked_doctype = "Sales Invoice"
		log.linked_docname = "NONEXISTENT-INV-999"
		self.addCleanup(self.force_cleanup_log, log.name)

		result = log.validate_record()
		self.assertIn("Document not found", result)

	def test_validate_record_completed_returns_empty(self):
		"""validate_record must return empty string for Completed status."""
		log = self.create_log(status="Completed")
		self.addCleanup(self.force_cleanup_log, log.name)

		result = log.validate_record()
		self.assertEqual(result, "")

	def create_log(self, status="Pending", amount=1000, currency="NGN"):
		"""Create a Paystack Payment Log for testing."""
		log = frappe.get_doc(
			{
				"doctype": "Paystack Payment Log",
				"company": "_Test Company",
				"linked_doctype": "Sales Invoice",
				"linked_docname": "NONEXISTENT-INV",
				"amount": amount,
				"currency": currency,
				"status": status,
			}
		)
		log.flags.ignore_permissions = True
		log.insert()
		return log

	def force_cleanup_log(self, name):
		"""Force-delete a Paystack Payment Log, bypassing on_trash."""
		if frappe.db.exists("Paystack Payment Log", name):
			frappe.db.set_value(
				"Paystack Payment Log", name, "status", "Pending"
			)
			frappe.db.set_value(
				"Paystack Payment Log", name, "payment_entry", None
			)
			frappe.delete_doc(
				"Paystack Payment Log",
				name,
				force=True,
				ignore_permissions=True,
			)