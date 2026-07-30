import frappe
from frappe.tests.utils import FrappeTestCase


class TestPaystackPaymentLog(FrappeTestCase):
	"""Tests for the Paystack Payment Log doctype."""

	def test_on_trash_blocks_processed_log(self):
		"""on_trash must prevent deletion of a Processed log."""
		log = self.create_log(status="Processed")
		self.addCleanup(self.force_cleanup_log, log.name)

		with self.assertRaises(frappe.ValidationError):
			log.on_trash()

	def test_on_trash_blocks_completed_log(self):
		"""on_trash must prevent deletion of a Completed log."""
		log = self.create_log(status="Completed")
		self.addCleanup(self.force_cleanup_log, log.name)

		with self.assertRaises(frappe.ValidationError):
			log.on_trash()

	def test_on_trash_blocks_log_with_linked_payment_entry(self):
		"""on_trash must prevent deletion when a payment_entry is linked, even if Pending."""
		log = self.create_log(status="Pending")
		log.db_set("payment_entry", "PE-FAKE-001")
		self.addCleanup(self.force_cleanup_log, log.name)

		with self.assertRaises(frappe.ValidationError):
			log.on_trash()

	def test_on_trash_allows_pending_without_payment_entry(self):
		"""on_trash must allow deletion of a Pending log with no linked Payment Entry."""
		log = self.create_log(status="Pending")
		self.addCleanup(self.force_cleanup_log, log.name)

		log.on_trash()

	def test_on_trash_allows_failed_without_payment_entry(self):
		"""on_trash must allow deletion of a Failed log with no linked Payment Entry."""
		log = self.create_log(status="Failed")
		self.addCleanup(self.force_cleanup_log, log.name)

		log.on_trash()

	def test_validate_throws_for_negative_amount(self):
		"""validate must reject negative amounts."""
		log = self.create_log(status="Pending", amount=-100)
		self.addCleanup(self.force_cleanup_log, log.name)

		with self.assertRaises(frappe.ValidationError):
			log.validate()

	def test_validate_throws_for_invalid_status(self):
		"""validate must reject an invalid status value."""
		log = self.create_log(status="Pending")
		log.status = "NotARealStatus"
		self.addCleanup(self.force_cleanup_log, log.name)

		with self.assertRaises(frappe.ValidationError):
			log.validate()

	def test_validate_normalizes_currency_to_uppercase(self):
		"""validate must normalize lowercase currency codes to uppercase."""
		log = self.create_log(status="Pending", currency="usd")
		self.addCleanup(self.force_cleanup_log, log.name)

		log.validate()
		self.assertEqual(log.currency, "USD")

	def test_validate_defaults_unsupported_currency_to_ngn(self):
		"""validate must default an unsupported currency to NGN."""
		log = self.create_log(status="Pending", currency="EUR")
		self.addCleanup(self.force_cleanup_log, log.name)

		log.validate()
		self.assertEqual(log.currency, "NGN")

	def test_validate_throws_when_only_doctype_set(self):
		"""validate must throw when linked_doctype is set but linked_docname is not."""
		log = self.create_log(status="Pending")
		log.linked_doctype = "Sales Invoice"
		log.linked_docname = None
		self.addCleanup(self.force_cleanup_log, log.name)

		with self.assertRaises(frappe.ValidationError):
			log.validate()

	def test_get_payment_link_contains_log_name_and_checkout_path(self):
		"""get_payment_link must return a URL with /paystack-checkout/ and the log name."""
		log = self.create_log(status="Pending")
		self.addCleanup(self.force_cleanup_log, log.name)

		url = log.get_payment_link()
		self.assertIn("/paystack-checkout/", url)
		self.assertIn(log.name, url)

	def test_get_payment_public_key_returns_none_when_no_gateway_enabled(self):
		"""get_payment_public_key must return None when no gateway is configured."""
		log = self.create_log(status="Pending")
		self.addCleanup(self.force_cleanup_log, log.name)

		self.assertIsNone(log.get_payment_public_key())

	def test_get_payment_public_key_returns_settings_when_gateway_enabled(self):
		"""get_payment_public_key must return a dict with keys when a gateway is enabled."""
		setting = self.create_enabled_gateway()
		self.addCleanup(self.cleanup_gateway, setting.name)

		log = self.create_log(status="Pending")
		self.addCleanup(self.force_cleanup_log, log.name)

		result = log.get_payment_public_key()
		self.assertIsNotNone(result)
		self.assertEqual(result["public_key"], "pk_test_123")
		self.assertEqual(result["currency"], "NGN")
		self.assertIn("suspense_account", result)
		self.assertIn("mode_of_payment", result)

	def test_validate_record_reports_error_for_nonexistent_linked_doc(self):
		"""validate_record must return an error message for a nonexistent linked doc."""
		log = self.create_log(status="Pending")
		log.linked_doctype = "Sales Invoice"
		log.linked_docname = "NONEXISTENT-INV-999"
		self.addCleanup(self.force_cleanup_log, log.name)

		result = log.validate_record()
		self.assertIn("Document not found", result)

	def test_validate_record_returns_empty_for_completed_status(self):
		"""validate_record must return empty string when status is Completed."""
		log = self.create_log(status="Completed")
		self.addCleanup(self.force_cleanup_log, log.name)

		result = log.validate_record()
		self.assertEqual(result, "")

	def test_get_data_returns_checkout_data_for_real_invoice(self):
		"""get_data must return customer, amounts, and reference for a real invoice."""
		sinv = self.create_sales_invoice(rate=5000)
		self.addCleanup(self.cleanup_sales_invoice, sinv.name)

		log = frappe.get_doc(
			{
				"doctype": "Paystack Payment Log",
				"company": "_Test Company",
				"linked_doctype": "Sales Invoice",
				"linked_docname": sinv.name,
				"amount": sinv.outstanding_amount,
				"currency": "NGN",
				"status": "Pending",
			}
		)
		log.flags.ignore_permissions = True
		log.insert()
		self.addCleanup(self.force_cleanup_log, log.name)

		data = log.get_data()
		self.assertEqual(data["customer"], "_Test Customer")
		self.assertEqual(data["reference_doctype"], "Sales Invoice")
		self.assertEqual(data["reference_docname"], sinv.name)
		self.assertEqual(data["reference"], log.name)
		self.assertEqual(data["order_no"], sinv.name)
		self.assertEqual(data["status"], "Pending")
		self.assertAlmostEqual(data["grand_total"], sinv.outstanding_amount, places=2)

	def test_get_data_includes_gateway_settings_when_enabled(self):
		"""get_data must include public_key and currency when a gateway is enabled."""
		setting = self.create_enabled_gateway()
		self.addCleanup(self.cleanup_gateway, setting.name)

		sinv = self.create_sales_invoice(rate=3000)
		self.addCleanup(self.cleanup_sales_invoice, sinv.name)

		log = frappe.get_doc(
			{
				"doctype": "Paystack Payment Log",
				"company": "_Test Company",
				"linked_doctype": "Sales Invoice",
				"linked_docname": sinv.name,
				"amount": 3000,
				"currency": "NGN",
				"status": "Pending",
			}
		)
		log.flags.ignore_permissions = True
		log.insert()
		self.addCleanup(self.force_cleanup_log, log.name)

		data = log.get_data()
		self.assertEqual(data["public_key"], "pk_test_123")
		self.assertEqual(data["currency"], "NGN")

	def test_transaction_id_has_unique_constraint(self):
		"""The transaction_id field must be unique to prevent duplicate processing."""
		meta = frappe.get_meta("Paystack Payment Log")
		field = meta.get_field("transaction_id")
		self.assertTrue(field.unique, "transaction_id must be unique for idempotency")

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

	def create_enabled_gateway(self):
		"""Create an enabled Paystack Gateway Setting for _Test Company."""
		setting = frappe.get_doc(
			{
				"doctype": "Paystack Gateway Setting",
				"gateway": "Test Gateway for Log Tests",
				"company": "_Test Company",
				"secret_key": "sk_test_123",
				"public_key": "pk_test_123",
				"suspense_account": self.get_suspense_account(),
				"mode_of_payment": "Paystack",
				"currency": "NGN",
				"enabled": 1,
			}
		)
		setting.flags.ignore_permissions = True
		setting.insert()
		return setting

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

	def get_suspense_account(self):
		return frappe.db.get_value(
			"Account",
			{"company": "_Test Company", "account_type": "Bank"},
			"name",
		)

	def force_cleanup_log(self, name):
		if frappe.db.exists("Paystack Payment Log", name):
			frappe.db.set_value("Paystack Payment Log", name, "status", "Pending")
			frappe.db.set_value("Paystack Payment Log", name, "payment_entry", None)
			frappe.delete_doc(
				"Paystack Payment Log", name, force=True, ignore_permissions=True
			)

	def cleanup_gateway(self, name):
		if frappe.db.exists("Paystack Gateway Setting", name):
			frappe.delete_doc(
				"Paystack Gateway Setting", name, force=True, ignore_permissions=True
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