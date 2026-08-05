# Copyright (c) 2025, Anthony Emmanuel and Contributors
# See license.txt

import frappe

from frappe_paystack.tests.factories import TEST_COMPANY, GatewaySettingFactory, PaymentLogFactory
from frappe_paystack.tests.test_base import PaystackTestCase

PAYMENT_LOG = "Paystack Payment Log"
ERROR_LOG = "Error Log"

# A Sales Invoice name no record answers to.
MISSING_INVOICE = "NONEXISTENT-INV-999"


def company_currency() -> str:
    """
    Return the currency the test company books in.

    The doctype refuses a gateway in any other currency.
    """
    return frappe.db.get_value("Company", TEST_COMPANY, "default_currency")


def error_logs_for(log_name: str) -> list:
    """Return the messages of the Error Logs filed against a payment log."""
    return frappe.get_all(
        ERROR_LOG,
        filters={"reference_doctype": PAYMENT_LOG, "reference_name": log_name},
        pluck="error",
    )


def delete_error_logs(log_name: str) -> None:
    """Remove the Error Logs a refusal left behind; they outlive the rollback."""
    for name in frappe.get_all(
        ERROR_LOG,
        filters={"reference_doctype": PAYMENT_LOG, "reference_name": log_name},
        pluck="name",
    ):
        frappe.delete_doc(ERROR_LOG, name, force=True, ignore_permissions=True)


class TestPaystackPaymentLog(PaystackTestCase):
    """The Paystack Payment Log doctype."""

    def test_on_trash_blocks_processed_log(self):
        """on_trash refuses to delete a Processed log."""
        log_name = PaymentLogFactory.create(status="Processed", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        log = frappe.get_doc("Paystack Payment Log", log_name)
        with self.assertRaises(frappe.ValidationError):
            log.on_trash()

    def test_on_trash_blocks_completed_log(self):
        """on_trash refuses to delete a Completed log."""
        log_name = PaymentLogFactory.create_completed(amount=1000, amount_paid=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        log = frappe.get_doc("Paystack Payment Log", log_name)
        with self.assertRaises(frappe.ValidationError):
            log.on_trash()

    def test_on_trash_blocks_log_with_linked_payment_entry(self):
        """on_trash refuses to delete a log carrying a Payment Entry, at any status."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        frappe.db.set_value("Paystack Payment Log", log_name, "payment_entry", "PE-FAKE-001")
        log = frappe.get_doc("Paystack Payment Log", log_name)
        with self.assertRaises(frappe.ValidationError):
            log.on_trash()

    def test_on_trash_allows_pending_without_payment_entry(self):
        """on_trash deletes a Pending log carrying no Payment Entry."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        frappe.delete_doc("Paystack Payment Log", log_name, ignore_permissions=True)

        self.assertFalse(frappe.db.exists("Paystack Payment Log", log_name))

    def test_on_trash_allows_failed_without_payment_entry(self):
        """on_trash deletes a Failed log carrying no Payment Entry."""
        log_name = PaymentLogFactory.create(status="Failed", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        frappe.delete_doc("Paystack Payment Log", log_name, ignore_permissions=True)

        self.assertFalse(frappe.db.exists("Paystack Payment Log", log_name))

    def test_validate_throws_for_negative_amount(self):
        """validate refuses a negative amount."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        log = frappe.get_doc("Paystack Payment Log", log_name)
        log.amount = -100
        with self.assertRaises(frappe.ValidationError):
            log.validate()

    def test_validate_throws_for_invalid_status(self):
        """validate refuses a status outside the field's options."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        log = frappe.get_doc("Paystack Payment Log", log_name)
        log.status = "NotARealStatus"
        with self.assertRaises(frappe.ValidationError):
            log.validate()

    def test_validate_normalizes_currency_to_uppercase(self):
        """validate upper-cases a currency code."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        log = frappe.get_doc("Paystack Payment Log", log_name)
        log.currency = "usd"
        log.validate()
        self.assertEqual(log.currency, "USD")

    def test_validate_defaults_unsupported_currency_to_ngn(self):
        """validate answers NGN for an unsupported currency."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        log = frappe.get_doc("Paystack Payment Log", log_name)
        log.currency = "EUR"
        log.validate()
        self.assertEqual(log.currency, "NGN")

    def test_validate_throws_when_only_doctype_set(self):
        """validate throws for a linked_doctype with a blank linked_docname."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        log = frappe.get_doc("Paystack Payment Log", log_name)
        log.linked_doctype = "Sales Invoice"
        log.linked_docname = None
        with self.assertRaises(frappe.ValidationError):
            log.validate()

    def refuse_a_save(self, log_name: str) -> None:
        """Point a saved log at a document that is not there and validate it."""
        log = frappe.get_doc(PAYMENT_LOG, log_name)
        log.linked_docname = MISSING_INVOICE
        with self.assertRaises(frappe.ValidationError):
            log.validate()

        self.refused = log

    def test_a_refused_save_carries_the_reason_on_the_document(self):
        """The refused document holds the reason in its errors field."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        self.refuse_a_save(log_name)

        self.assertIn("Document not found", self.refused.errors)

    def test_a_refused_save_writes_nothing_to_the_row(self):
        """The refusal leaves the stored errors field untouched."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        self.refuse_a_save(log_name)

        self.assertFalse(frappe.db.get_value(PAYMENT_LOG, log_name, "errors"))

    def test_the_reason_a_save_was_refused_survives_a_rollback(self):
        """An Error Log against the log carries the reason through the rollback."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        self.addCleanup(delete_error_logs, log_name)
        frappe.db.commit()

        self.refuse_a_save(log_name)
        frappe.db.rollback()

        self.assertTrue(any("Document not found" in entry for entry in error_logs_for(log_name)))

    def test_get_payment_link_contains_log_name_and_checkout_path(self):
        """get_payment_link returns a /paystack-checkout/ URL carrying the log name."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        log = frappe.get_doc("Paystack Payment Log", log_name)
        url = log.get_payment_link()
        self.assertIn("/paystack-checkout/", url)
        self.assertIn(log_name, url)

    def test_get_payment_public_key_returns_none_when_no_gateway_enabled(self):
        """get_payment_public_key returns None with no gateway configured."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        self.disable_company_gateways()

        log = frappe.get_doc("Paystack Payment Log", log_name)
        self.assertIsNone(log.get_payment_public_key())

    def test_get_payment_public_key_returns_settings_when_gateway_enabled(self):
        """get_payment_public_key returns the keys of an enabled gateway."""
        gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, gateway_name)

        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        log = frappe.get_doc("Paystack Payment Log", log_name)
        result = log.get_payment_public_key()
        self.assertIsNotNone(result)
        self.assertEqual(result["public_key"], "pk_test_123")
        self.assertEqual(result["currency"], company_currency())
        self.assertIn("suspense_account", result)
        self.assertIn("mode_of_payment", result)

    def test_validate_record_reports_error_for_nonexistent_linked_doc(self):
        """validate_record returns an error message for a missing linked document."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        log = frappe.get_doc("Paystack Payment Log", log_name)
        log.linked_doctype = "Sales Invoice"
        log.linked_docname = "NONEXISTENT-INV-999"
        result = log.validate_record()
        self.assertIn("Document not found", result)

    def test_validate_record_returns_empty_for_completed_status(self):
        """validate_record returns an empty string for a Completed log."""
        log_name = PaymentLogFactory.create_completed(amount=1000, amount_paid=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        log = frappe.get_doc("Paystack Payment Log", log_name)
        result = log.validate_record()
        self.assertEqual(result, "")

    def test_get_data_returns_checkout_data_for_real_invoice(self):
        """get_data returns the customer, the amounts and the reference of an invoice."""
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
        """get_data carries public_key and currency for an enabled gateway."""
        gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, gateway_name)

        log_name = PaymentLogFactory.create(status="Pending", amount=3000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        log = frappe.get_doc("Paystack Payment Log", log_name)
        data = log.get_data()
        self.assertEqual(data["public_key"], "pk_test_123")
        self.assertEqual(data["currency"], company_currency())

    def test_transaction_id_has_unique_constraint(self):
        """The transaction_id field is unique."""
        meta = frappe.get_meta("Paystack Payment Log")
        field = meta.get_field("transaction_id")
        self.assertTrue(field.unique, "transaction_id must be unique for idempotency")
