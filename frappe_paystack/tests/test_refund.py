"""Tests for Paystack refunds: the API, the webhook and the Refund Log itself."""

from typing import Any
from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.utils import flt, fmt_money

from frappe_paystack.api import initiate_refund_from_log, process_refund_webhook_event
from frappe_paystack.frappe_paystack.doctype.paystack_refund_log.paystack_refund_log import (
    get_total_refunded,
    refund_status,
    send_refund_receipt,
)
from frappe_paystack.tests.factories import (
    TEST_COMPANY,
    UNPRIVILEGED_ROLE,
    CreditNoteFactory,
    CustomerFactory,
    GatewaySettingFactory,
    PaymentLogFactory,
    RefundLogFactory,
    SalesInvoiceFactory,
    cleanup_doc,
    cleanup_user,
)
from frappe_paystack.tests.test_base import PaystackTestCase
from frappe_paystack.utils import initiate_refund

REFUND_LOG = "Paystack Refund Log"
PAYMENT_LOG = "Paystack Payment Log"
GATEWAY_DOCTYPE = "Paystack Gateway Setting"

RECEIPT_CUSTOMER = "_Test Paystack Receipt Customer"
RECEIPT_EMAIL = "receipts@example.com"

# A signed-in account holding no Paystack-related role.
OUTSIDER_EMAIL = "paystack-outsider@example.com"

# The gateway credential lookup these tests patch.
GATEWAY_ROW_PATCH = "frappe_paystack.utils.utils.get_company_row_settings"

# A submission-time hook on the Payment Entry, run once the row is already inserted.
SUBMIT_PAYMENT_ENTRY = "erpnext.accounts.doctype.payment_entry.payment_entry.PaymentEntry.before_submit"

REFUND_RESPONSE = {
    "status": True,
    "data": {
        "status": "processed",
        "reference": "rfd_ledger_001",
        "amount": 20000,
        "currency": "NGN",
    },
}


class TestPaystackRefundApi(PaystackTestCase):
    """Tests for the Paystack refund API integration in utils.py."""

    @patch("frappe_paystack.utils.utils.requests.post")
    @patch(GATEWAY_ROW_PATCH)
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

    @patch("frappe_paystack.utils.utils.requests.post")
    @patch(GATEWAY_ROW_PATCH)
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

    @patch("frappe_paystack.utils.utils.requests.post")
    @patch(GATEWAY_ROW_PATCH)
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

    @patch(GATEWAY_ROW_PATCH)
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

    @patch("frappe_paystack.utils.utils.requests.post")
    @patch(GATEWAY_ROW_PATCH)
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

    @patch("frappe_paystack.utils.utils.requests.post")
    @patch(GATEWAY_ROW_PATCH)
    def test_the_card_token_never_reaches_the_returned_response(self, mock_settings, mock_post):
        """The authorization code is masked in the raw response returned."""
        mock_settings.return_value = {"secret_key": "sk_test_123"}

        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.json.return_value = {
            "status": True,
            "data": {
                "status": "processed",
                "reference": "rfd_token",
                "amount": 10000,
                "currency": "NGN",
                "transaction": {"authorization": {"authorization_code": "AUTH_refunded"}},
            },
        }
        mock_post.return_value = mock_response

        result = initiate_refund(
            transaction_id="ref_token",
            amount=100.0,
            currency="NGN",
            company="_Test Company",
        )

        stored = frappe.as_json(result["raw"])
        self.assertNotIn("AUTH_refunded", stored)
        self.assertIn("***", stored)


class TestPaystackRefundLogDoctype(PaystackTestCase):
    """Tests for the Paystack Refund Log doctype behavior."""

    def setUp(self):
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

    def test_validate_throws_without_payment_log(self):
        """validate throws when no payment_log is set."""
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
        """validate throws when the referenced Payment Log is not Completed."""
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
        """validate throws when refund_amount is zero or negative."""
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
        """validate throws when refund_amount exceeds the original amount_paid."""
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
        """validate throws when total refunds exceed amount_paid."""
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
        """validate allows a partial refund that fits within the available amount."""
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
        self.assertIsNone(new_refund.validate())

        # A penny past what the earlier refund left is refused.
        new_refund.refund_amount = 300.01
        with self.assertRaises(frappe.ValidationError):
            new_refund.validate()

    def test_on_trash_blocks_completed_refund(self):
        """on_trash throws for a Completed Refund Log."""
        log_name = PaymentLogFactory.create_completed(amount=500, amount_paid=500)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        refund_name = RefundLogFactory.create_bypass_validate(log_name, refund_amount=100, status="Completed")
        self.addCleanup(RefundLogFactory.cleanup, refund_name)

        refund_log = frappe.get_doc("Paystack Refund Log", refund_name)
        with self.assertRaises(frappe.ValidationError):
            refund_log.on_trash()

    def test_on_trash_allows_pending_without_reversal(self):
        """on_trash passes for a Pending Refund Log with no reversal entry."""
        log_name = PaymentLogFactory.create_completed(amount=500, amount_paid=500)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        refund_name = RefundLogFactory.create_bypass_validate(log_name, refund_amount=100, status="Pending")
        self.addCleanup(RefundLogFactory.cleanup, refund_name)

        frappe.delete_doc("Paystack Refund Log", refund_name, ignore_permissions=True)

        self.assertFalse(frappe.db.exists("Paystack Refund Log", refund_name))

    def test_get_total_refunded_returns_zero_for_no_refunds(self):
        """get_total_refunded returns 0 when no refunds exist."""
        log_name = PaymentLogFactory.create_completed(amount=500, amount_paid=500)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        self.assertEqual(get_total_refunded(log_name), 0.0)

    def test_get_total_refunded_sums_completed_refunds(self):
        """get_total_refunded sums only Processed/Completed refund amounts."""
        log_name = PaymentLogFactory.create_completed(amount=1000, amount_paid=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        r1 = RefundLogFactory.create_bypass_validate(log_name, refund_amount=200, status="Completed")
        self.addCleanup(RefundLogFactory.cleanup, r1)
        r2 = RefundLogFactory.create_bypass_validate(log_name, refund_amount=300, status="Processed")
        self.addCleanup(RefundLogFactory.cleanup, r2)
        r3 = RefundLogFactory.create_bypass_validate(log_name, refund_amount=100, status="Failed")
        self.addCleanup(RefundLogFactory.cleanup, r3)

        total = get_total_refunded(log_name)
        self.assertEqual(total, 500.0)

    def test_get_total_refunded_excludes_specified_log(self):
        """get_total_refunded excludes a specific log when computing available."""
        log_name = PaymentLogFactory.create_completed(amount=1000, amount_paid=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        r1 = RefundLogFactory.create_bypass_validate(log_name, refund_amount=200, status="Completed")
        self.addCleanup(RefundLogFactory.cleanup, r1)

        total = get_total_refunded(log_name, exclude=r1)
        self.assertEqual(total, 0.0)


class TestPaystackRefundWebhook(PaystackTestCase):
    """Tests for refund webhook event processing, in both the nested and flat payload forms."""

    def setUp(self):
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

    def paid_log(self, payment_reference: str) -> str:
        """Create a Completed Payment Log settled under a merchant reference."""
        log_name = PaymentLogFactory.create_completed(amount=500, amount_paid=500)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        frappe.db.set_value("Paystack Payment Log", log_name, "payment_reference", payment_reference)
        return log_name

    def refund_log(self, log_name: str, **kwargs) -> str:
        """Create a Refund Log against a Payment Log."""
        refund_name = RefundLogFactory.create_bypass_validate(log_name, refund_amount=200, **kwargs)
        self.addCleanup(RefundLogFactory.cleanup, refund_name)
        return refund_name

    def nested_payload(
        self,
        event: str = "refund.processed",
        transaction_reference: str = "",
        refund_reference: str = "",
        status: str = "processed",
    ) -> dict:
        """Build a refund payload shaped like Paystack's nested form."""
        return {
            "event": event,
            "data": {
                "id": 8_100_100,
                "domain": "test",
                "integration": 100_100,
                "transaction": {
                    "id": 302_961_713,
                    "reference": transaction_reference,
                    "amount": 50000,
                    "currency": "NGN",
                    "channel": "card",
                },
                "refund_reference": refund_reference,
                "amount": 20000,
                "currency": "NGN",
                "status": status,
                "refunded_by": "merchant",
                "merchant_note": "Customer request",
                "deducted_amount": 20000,
                "fully_deducted": False,
                "channel": "card",
            },
        }

    def flat_payload(
        self,
        event: str = "refund.processed",
        transaction_reference: str = "",
        refund_reference: str = "",
        status: str = "processed",
    ) -> dict:
        """Build a refund payload shaped like Paystack's flat form."""
        return {
            "event": event,
            "data": {
                "status": status,
                "transaction_reference": transaction_reference,
                "refund_reference": refund_reference,
                "amount": 20000,
                "currency": "NGN",
                "processor": "Interswitch",
                "domain": "test",
                "integration": 100_100,
            },
        }

    def status_of(self, refund_name: str) -> str:
        """Return the current status of a Refund Log."""
        return frappe.db.get_value("Paystack Refund Log", refund_name, "status")

    def test_nested_payload_processes_the_refund(self):
        """A refund.processed webhook matches on its refund reference."""
        log_name = self.paid_log("mrc_ref_nested")
        refund_name = self.refund_log(log_name, status="Pending", refund_reference="rfd_test_001")

        process_refund_webhook_event(
            self.nested_payload(
                transaction_reference="mrc_ref_nested",
                refund_reference="rfd_test_001",
            )
        )

        self.assertIn(self.status_of(refund_name), ("Processed", "Completed"))

    def test_flat_payload_processes_the_refund(self):
        """The flat transaction_reference/refund_reference form is matched too."""
        log_name = self.paid_log("mrc_ref_flat")
        refund_name = self.refund_log(log_name, status="Pending", refund_reference="rfd_test_002")

        process_refund_webhook_event(
            self.flat_payload(
                transaction_reference="mrc_ref_flat",
                refund_reference="rfd_test_002",
            )
        )

        self.assertIn(self.status_of(refund_name), ("Processed", "Completed"))

    def test_refund_without_reference_matches_on_the_transaction(self):
        """A refund with no stored reference matches through the merchant reference."""
        log_name = self.paid_log("mrc_ref_only")
        refund_name = self.refund_log(log_name, status="Pending")

        process_refund_webhook_event(
            self.nested_payload(transaction_reference="mrc_ref_only", refund_reference="rfd_dash_001")
        )

        self.assertIn(self.status_of(refund_name), ("Processed", "Completed"))
        self.assertEqual(
            frappe.db.get_value("Paystack Refund Log", refund_name, "refund_reference"),
            "rfd_dash_001",
        )

    def test_numeric_transaction_id_is_never_matched_against(self):
        """A transaction reference holding the numeric Paystack id matches nothing."""
        log_name = self.paid_log("mrc_ref_numeric")
        refund_name = self.refund_log(log_name, status="Pending")
        transaction_id = frappe.db.get_value("Paystack Payment Log", log_name, "transaction_id")

        process_refund_webhook_event(self.nested_payload(transaction_reference=str(transaction_id)))

        self.assertEqual(self.status_of(refund_name), "Pending")

    def test_failed_refund_marks_the_log_failed(self):
        """A refund.failed webhook sets the Refund Log status to Failed."""
        log_name = self.paid_log("mrc_ref_failed")
        refund_name = self.refund_log(log_name, status="Pending", refund_reference="rfd_fail_001")

        process_refund_webhook_event(
            self.flat_payload(
                event="refund.failed",
                transaction_reference="mrc_ref_failed",
                refund_reference="rfd_fail_001",
                status="failed",
            )
        )

        self.assertEqual(self.status_of(refund_name), "Failed")

    def test_already_processed_refund_is_not_reprocessed(self):
        """A refund webhook for an already-Processed log leaves it settled."""
        log_name = self.paid_log("mrc_ref_skip")
        refund_name = self.refund_log(log_name, status="Processed", refund_reference="rfd_skip_001")

        process_refund_webhook_event(
            self.nested_payload(transaction_reference="mrc_ref_skip", refund_reference="rfd_skip_001")
        )

        self.assertIn(self.status_of(refund_name), ("Processed", "Completed"))

    def test_unknown_refund_is_recorded_and_does_not_raise(self):
        """A refund webhook matching no log leaves an Integration Request behind."""
        before = frappe.db.count("Integration Request")

        process_refund_webhook_event(
            self.nested_payload(transaction_reference="mrc_ghost", refund_reference="rfd_ghost")
        )

        self.assertEqual(frappe.db.count("Integration Request"), before + 1)
        self.assertEqual(frappe.get_last_doc("Integration Request").status, "Failed")

    def test_the_card_token_never_reaches_the_stored_payload(self):
        """The authorization code is masked in the stored raw_response."""
        log_name = self.paid_log("mrc_ref_token")
        refund_name = self.refund_log(log_name, status="Pending", refund_reference="rfd_token_001")
        payload = self.nested_payload(
            event="refund.failed",
            transaction_reference="mrc_ref_token",
            refund_reference="rfd_token_001",
            status="failed",
        )
        payload["data"]["transaction"]["authorization"] = {"authorization_code": "AUTH_echoed"}

        process_refund_webhook_event(payload)

        stored = frappe.db.get_value("Paystack Refund Log", refund_name, "raw_response")
        self.assertNotIn("AUTH_echoed", stored)
        self.assertIn("***", stored)


class TestPaystackManualRefund(PaystackTestCase):
    """Tests for the manual refund API endpoint."""

    def setUp(self):
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

    @patch("frappe_paystack.utils.utils.requests.post")
    @patch(GATEWAY_ROW_PATCH)
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

    @patch("frappe_paystack.utils.utils.requests.post")
    @patch(GATEWAY_ROW_PATCH)
    def test_initiate_refund_from_log_books_the_reversal(self, mock_settings, mock_post):
        """A processed refund completes and books a reversal Payment Entry."""

        mock_settings.return_value = {"secret_key": "sk_test_123"}

        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.json.return_value = {
            "status": True,
            "data": {
                "status": "processed",
                "reference": "rfd_manual_002",
                "amount": 20000,
                "currency": "NGN",
            },
        }
        mock_post.return_value = mock_response

        log_name = PaymentLogFactory.create_completed(amount=500, amount_paid=500)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        refund_name = initiate_refund_from_log(payment_log_name=log_name, amount=200.0)
        self.addCleanup(RefundLogFactory.cleanup, refund_name)

        refund_log = frappe.get_doc("Paystack Refund Log", refund_name)
        self.assertEqual(refund_log.status, "Completed")
        self.assertTrue(frappe.db.exists("Payment Entry", refund_log.reversal_payment_entry))

    def test_initiate_refund_from_log_throws_for_non_completed_payment(self):
        """initiate_refund_from_log throws when the Payment Log is not Completed."""

        log_name = PaymentLogFactory.create(status="Pending", amount=100)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        with self.assertRaises(frappe.ValidationError):
            initiate_refund_from_log(
                payment_log_name=log_name,
                amount=100.0,
            )


class TestReversalLedgerDirection(PaystackTestCase):
    """The reversal Payment Entry debits the receivable and credits suspense."""

    def setUp(self):
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create(auto_refund=True)
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)
        self.suspense = self.suspense_account()

        self.invoice = SalesInvoiceFactory.create(rate=1000)
        self.addCleanup(SalesInvoiceFactory.cleanup, self.invoice)
        self.receivable = frappe.db.get_value("Sales Invoice", self.invoice, "debit_to")

        self.payment_log = PaymentLogFactory.create(
            linked_docname=self.invoice, amount=1000, amount_paid=1000, status="Completed"
        )
        self.addCleanup(PaymentLogFactory.cleanup, self.payment_log)
        self.addCleanup(self.cleanup_refund_logs)

    def cleanup_refund_logs(self) -> None:
        """Remove every Refund Log raised against the fixture payment."""
        for name in frappe.get_all(
            "Paystack Refund Log", filters={"payment_log": self.payment_log}, pluck="name"
        ):
            RefundLogFactory.cleanup(name)

    def reversal_of(self, refund_name: str) -> str:
        """Return the submitted reversal Payment Entry a refund booked."""
        reversal = frappe.db.get_value("Paystack Refund Log", refund_name, "reversal_payment_entry")
        self.assertTrue(reversal, "no reversal Payment Entry was booked")
        return reversal

    def assert_reversal_direction(self, reversal: str, amount: float) -> None:
        """Assert the reversal debits the receivable and credits suspense."""
        rows = self.gl_entries_by_account(reversal)

        self.assertIn(self.receivable, rows)
        self.assertIn(self.suspense, rows)
        self.assertAlmostEqual(rows[self.receivable]["debit"], amount, places=2)
        self.assertAlmostEqual(rows[self.receivable]["credit"], 0.0, places=2)
        self.assertAlmostEqual(rows[self.suspense]["credit"], amount, places=2)
        self.assertAlmostEqual(rows[self.suspense]["debit"], 0.0, places=2)

    @patch("frappe_paystack.utils.utils.requests.post")
    def test_manual_refund_debits_the_receivable(self, mock_post):
        """A refund without a credit note books an unallocated reversal."""
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.json.return_value = REFUND_RESPONSE
        mock_post.return_value = mock_response

        refund_name = initiate_refund_from_log(self.payment_log, 200.0)

        self.assert_reversal_direction(self.reversal_of(refund_name), 200.0)

    def test_credit_note_refund_debits_the_receivable(self):
        """A refund against a credit note allocates the reversal to it."""
        with patch(
            "frappe_paystack.events.initiate_refund",
            return_value={
                "status": "processed",
                "reference": "rfd_ledger_002",
                "amount": 400.0,
                "currency": "NGN",
                "raw": {"status": True},
            },
        ):
            credit_note = CreditNoteFactory.create(return_against=self.invoice, rate=400)
        self.addCleanup(CreditNoteFactory.cleanup, credit_note)

        refunds = frappe.get_all(
            "Paystack Refund Log", filters={"payment_log": self.payment_log}, pluck="name"
        )
        self.assertEqual(len(refunds), 1)

        self.assert_reversal_direction(self.reversal_of(refunds[0]), 400.0)


class TestOverRefundAcrossRefunds(PaystackTestCase):
    """One payment can be given back in instalments, up to what was captured."""

    def setUp(self):
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create(auto_refund=True)
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

        self.invoice = SalesInvoiceFactory.create(rate=1000)
        self.addCleanup(SalesInvoiceFactory.cleanup, self.invoice)

        self.payment_log = PaymentLogFactory.create(
            linked_docname=self.invoice, amount=1000, amount_paid=1000, status="Completed"
        )
        self.addCleanup(PaymentLogFactory.cleanup, self.payment_log)
        self.addCleanup(self.cleanup_refund_logs)

    def cleanup_refund_logs(self) -> None:
        """Remove every Refund Log raised against the fixture payment."""
        for name in frappe.get_all(
            "Paystack Refund Log", filters={"payment_log": self.payment_log}, pluck="name"
        ):
            RefundLogFactory.cleanup(name)

    def refund(self, amount: float) -> str:
        """Refund through the desk endpoint with Paystack stubbed."""
        response = MagicMock()
        response.ok = True
        response.json.return_value = {
            "status": True,
            "data": {
                "status": "processed",
                "reference": f"rfd_partial_{int(amount)}",
                "amount": int(amount * 100),
                "currency": "NGN",
            },
        }

        with patch("frappe_paystack.utils.utils.requests.post", return_value=response):
            return initiate_refund_from_log(self.payment_log, amount)

    def credit_note(self, rate: float) -> str:
        """Submit a credit note against the invoice with the refund call stubbed."""
        with patch(
            "frappe_paystack.events.initiate_refund",
            return_value={
                "status": "processed",
                "reference": f"rfd_note_{int(rate)}",
                "amount": rate,
                "currency": "NGN",
                "raw": {"status": True},
            },
        ):
            note = CreditNoteFactory.create(return_against=self.invoice, rate=rate)
        self.addCleanup(CreditNoteFactory.cleanup, note)
        return note

    def payment_log_row(self) -> frappe._dict:
        """Return the running refund total and status on the Payment Log."""
        return frappe.db.get_value(
            "Paystack Payment Log",
            self.payment_log,
            ["total_refunded", "status"],
            as_dict=True,
        )

    def refund_for(self, linked_docname: str) -> frappe._dict:
        """Return the single Refund Log raised for a billed document."""
        rows = frappe.get_all(
            "Paystack Refund Log",
            filters={"payment_log": self.payment_log, "linked_docname": linked_docname},
            fields=["name", "refund_amount", "status"],
        )
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_successive_partials_accumulate(self):
        """Two partials leave the payment part-refunded for the sum of both."""
        self.refund(400.0)
        self.refund(400.0)

        row = self.payment_log_row()
        self.assertAlmostEqual(flt(row.total_refunded), 800.0, places=2)
        self.assertEqual(row.status, "Partially Refunded")

    def test_a_later_partial_cannot_exceed_what_is_left(self):
        """The third refund is measured against the two before it."""
        self.refund(400.0)
        self.refund(400.0)

        with self.assertRaises(frappe.ValidationError):
            self.refund(300.0)

        self.assertAlmostEqual(flt(self.payment_log_row().total_refunded), 800.0, places=2)

    def test_partials_can_be_taken_up_to_the_whole_capture(self):
        """Refunds that exactly consume the capture are allowed and close it."""
        self.refund(400.0)
        self.refund(400.0)
        self.refund(200.0)

        row = self.payment_log_row()
        self.assertAlmostEqual(flt(row.total_refunded), 1000.0, places=2)
        self.assertEqual(row.status, "Refunded")

    def test_a_credit_note_is_capped_by_earlier_manual_refunds(self):
        """A credit-note refund draws on what the earlier desk refund left."""
        self.refund(800.0)

        note = self.credit_note(rate=500)

        self.assertAlmostEqual(flt(self.refund_for(note).refund_amount), 200.0, places=2)
        self.assertEqual(self.payment_log_row().status, "Refunded")

    def test_a_manual_refund_is_capped_by_an_earlier_credit_note(self):
        """A desk refund is capped by what an earlier credit note returned."""
        self.credit_note(rate=900)

        with self.assertRaises(frappe.ValidationError):
            self.refund(200.0)

        self.assertAlmostEqual(flt(self.payment_log_row().total_refunded), 900.0, places=2)


class RefundLogTestCase(PaystackTestCase):
    """Shared fixtures: an enabled gateway and a captured Paystack payment."""

    def setUp(self) -> None:
        """Create a Completed Payment Log a refund can be raised against."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

        self.payment_log = PaymentLogFactory.create_completed(amount=1000, amount_paid=1000)
        self.addCleanup(PaymentLogFactory.cleanup, self.payment_log)
        self.invoice = frappe.db.get_value(PAYMENT_LOG, self.payment_log, "linked_docname")

    def insert_refund_log(self, **fields: Any) -> Any:
        """Insert a Refund Log against the fixture payment and track it."""
        values = {
            "doctype": REFUND_LOG,
            "payment_log": self.payment_log,
            "company": TEST_COMPANY,
            "refund_amount": 100,
            "currency": "NGN",
            "status": "Pending",
        }
        values.update(fields)

        doc = frappe.get_doc(values)
        doc.flags.ignore_permissions = True
        doc.flags.ignore_links = True
        doc.insert()
        frappe.db.commit()
        self.addCleanup(RefundLogFactory.cleanup, doc.name)
        return doc


class TestRefundLogValidation(RefundLogTestCase):
    """validate refuses refunds that cannot be settled."""

    def test_missing_currency_is_left_alone(self) -> None:
        """A refund raised without a currency keeps an empty currency."""
        doc = self.insert_refund_log(currency=None)

        self.assertFalse(doc.currency)

    def test_currency_is_normalised(self) -> None:
        """An unsupported currency is coerced to the Paystack default."""
        doc = self.insert_refund_log(currency="eur")

        self.assertEqual(doc.currency, "NGN")

    def test_invalid_status_is_refused(self) -> None:
        """A status outside the known set is refused."""
        with self.assertRaises(frappe.ValidationError):
            self.insert_refund_log(status="Reticulating")

    def test_missing_payment_log_is_refused(self) -> None:
        """A refund with no Payment Log at all is refused."""
        with self.assertRaises(frappe.ValidationError):
            self.insert_refund_log(payment_log=None)

    def test_unknown_payment_log_is_refused(self) -> None:
        """A refund naming a Payment Log that does not exist is refused."""
        with self.assertRaises(frappe.ValidationError):
            self.insert_refund_log(payment_log="PAY-LOG-NOWHERE")

    def test_over_refund_is_refused(self) -> None:
        """A refund larger than the captured amount is refused."""
        with self.assertRaises(frappe.ValidationError):
            self.insert_refund_log(refund_amount=5000)


class TestRefundLogIsDraftOnly(RefundLogTestCase):
    """The Refund Log lives entirely in draft."""

    def test_the_doctype_is_not_submittable(self) -> None:
        """The doctype is not submittable and carries no amended_from field."""
        meta = frappe.get_meta(REFUND_LOG)

        self.assertFalse(meta.is_submittable)
        self.assertIsNone(meta.get_field("amended_from"))

    def test_submitting_a_refund_log_is_refused(self) -> None:
        """Submitting a Refund Log is refused and leaves it in draft."""
        doc = self.insert_refund_log()

        with self.assertRaises(frappe.ValidationError):
            doc.submit()

        self.assertEqual(frappe.db.get_value(REFUND_LOG, doc.name, "docstatus"), 0)

    def test_a_processed_refund_books_its_reversal(self) -> None:
        """A refund Paystack accepted books the reversal and completes."""
        doc = self.insert_refund_log(linked_doctype="Sales Invoice", linked_docname=self.invoice)
        doc.status = "Processed"

        doc.run_method("on_update")

        doc.reload()
        self.assertEqual(doc.status, "Completed")
        self.assertTrue(frappe.db.exists("Payment Entry", doc.reversal_payment_entry))

    def test_a_booked_reversal_marks_the_payment_log(self) -> None:
        """The reversal stamps the refunded total on the Payment Log."""
        doc = self.insert_refund_log(linked_doctype="Sales Invoice", linked_docname=self.invoice)
        doc.status = "Processed"

        doc.run_method("on_update")

        self.assert_field_value(PAYMENT_LOG, self.payment_log, "total_refunded", 100)
        self.assert_field_value(PAYMENT_LOG, self.payment_log, "status", "Partially Refunded")


class TestRefundLogReversalGuards(RefundLogTestCase):
    """on_update books a reversal entry only for a settled, resolvable refund."""

    def test_pending_log_books_nothing(self) -> None:
        """A Pending Refund Log books no reversal entry."""
        doc = self.insert_refund_log()

        doc.run_method("on_update")

        self.assertIsNone(frappe.db.get_value(REFUND_LOG, doc.name, "reversal_payment_entry"))

    def test_unfetchable_reference_is_logged_and_abandoned(self) -> None:
        """A reference document that cannot be loaded abandons the reversal."""
        doc = self.insert_refund_log(linked_doctype="Sales Invoice", linked_docname="SINV-NOWHERE")
        doc.status = "Processed"

        doc.run_method("on_update")

        self.assertIsNone(frappe.db.get_value(REFUND_LOG, doc.name, "reversal_payment_entry"))
        self.assertIsNone(frappe.db.get_value(REFUND_LOG, doc.name, "errors"))

    def test_missing_gateway_is_recorded_as_an_error(self) -> None:
        """Without an enabled gateway the reversal fails and is recorded."""
        doc = self.insert_refund_log()
        doc.status = "Processed"
        cleanup_doc(GATEWAY_DOCTYPE, self.gateway)

        doc.run_method("on_update")

        errors = frappe.db.get_value(REFUND_LOG, doc.name, "errors")
        self.assertIn("Error Log", errors)
        self.assertIsNone(frappe.db.get_value(REFUND_LOG, doc.name, "reversal_payment_entry"))


class TestReversalThatCannotBeSubmitted(RefundLogTestCase):
    """A reversal Payment Entry that is inserted and then refused leaves nothing behind."""

    def entries_for(self, refund: str) -> list:
        """Return the Payment Entries a refund raised, with their state."""
        return frappe.get_all("Payment Entry", filters={"reference_no": refund}, fields=["docstatus"])

    def refused_reversal(self) -> Any:
        """Book a reversal whose submission is refused, and return the Refund Log."""
        doc = self.insert_refund_log(linked_doctype="Sales Invoice", linked_docname=self.invoice)
        doc.status = "Processed"

        with patch(SUBMIT_PAYMENT_ENTRY, create=True, side_effect=frappe.ValidationError("refused")):
            doc.run_method("on_update")

        return doc

    def test_a_refused_reversal_leaves_no_payment_entry(self) -> None:
        """The Payment Entry the attempt inserted is discarded."""
        doc = self.refused_reversal()

        self.assertEqual(self.entries_for(doc.name), [])

    def test_a_refused_reversal_is_recorded_on_the_log(self) -> None:
        """The refund carries the failure and stays short of Completed."""
        doc = self.refused_reversal()

        record = frappe.db.get_value(
            REFUND_LOG, doc.name, ["reversal_payment_entry", "errors", "status"], as_dict=True
        )
        self.assertFalse(record.reversal_payment_entry)
        self.assertTrue(record.errors)
        self.assertNotEqual(record.status, "Completed")


class TestRefundLogDeletion(RefundLogTestCase):
    """on_trash protects settled refunds."""

    def test_pending_log_can_be_deleted(self) -> None:
        """A Pending refund with no reversal entry can be removed."""
        doc = self.insert_refund_log()

        frappe.delete_doc(REFUND_LOG, doc.name, force=True, ignore_permissions=True)

        self.assertFalse(frappe.db.exists(REFUND_LOG, doc.name))

    def test_processed_log_cannot_be_deleted(self) -> None:
        """A Processed refund is protected from deletion."""
        doc = self.insert_refund_log()
        frappe.db.set_value(REFUND_LOG, doc.name, "status", "Processed")
        frappe.clear_document_cache(REFUND_LOG, doc.name)

        with self.assertRaises(frappe.ValidationError):
            frappe.delete_doc(REFUND_LOG, doc.name, force=True, ignore_permissions=True)

    def test_log_linked_to_a_reversal_entry_cannot_be_deleted(self) -> None:
        """A refund holding a reversal Payment Entry is protected from deletion."""
        doc = self.insert_refund_log()
        frappe.db.set_value(REFUND_LOG, doc.name, "reversal_payment_entry", "PE-NOWHERE")
        frappe.clear_document_cache(REFUND_LOG, doc.name)

        with self.assertRaises(frappe.ValidationError):
            frappe.delete_doc(REFUND_LOG, doc.name, force=True, ignore_permissions=True)

        frappe.db.set_value(REFUND_LOG, doc.name, "reversal_payment_entry", None)


class TestRefundReceiptEmail(PaystackTestCase):
    """The refund receipt is emailed only to a customer that can receive it."""

    def setUp(self) -> None:
        """Bill a customer with an email address and capture a payment for them."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

        self.customer = CustomerFactory.create(customer_name=RECEIPT_CUSTOMER)
        self.addCleanup(CustomerFactory.cleanup, self.customer)

        self.invoice = SalesInvoiceFactory.create(rate=1000, customer=RECEIPT_CUSTOMER)
        self.addCleanup(SalesInvoiceFactory.cleanup, self.invoice)

        self.payment_log = PaymentLogFactory.create(
            linked_docname=self.invoice,
            amount=1000,
            amount_paid=1000,
            status="Completed",
        )
        self.addCleanup(PaymentLogFactory.cleanup, self.payment_log)

    def set_customer_email(self, email: str) -> None:
        """Give the receipt customer an email address."""
        frappe.db.set_value("Customer", self.customer, "email_id", email)
        frappe.clear_document_cache("Customer", self.customer)

    def completed_refund_log(self, linked: bool = True, currency: str = "NGN") -> Any:
        """Create a Refund Log recorded as Completed."""
        values = {
            "doctype": REFUND_LOG,
            "payment_log": self.payment_log,
            "company": TEST_COMPANY,
            "refund_amount": 100,
            "currency": currency,
            "status": "Pending",
        }
        if linked:
            values["linked_doctype"] = "Sales Invoice"
            values["linked_docname"] = self.invoice

        doc = frappe.get_doc(values)
        doc.flags.ignore_permissions = True
        doc.insert()
        self.addCleanup(RefundLogFactory.cleanup, doc.name)
        doc.db_set("status", "Completed", update_modified=False)
        frappe.db.commit()
        return doc

    def test_uncompleted_refund_sends_nothing(self) -> None:
        """A refund that is not Completed sends no receipt."""
        self.set_customer_email(RECEIPT_EMAIL)
        doc = self.completed_refund_log()
        doc.status = "Processed"

        with patch("frappe.enqueue") as mock_enqueue:
            doc.send_refund_receipt_email()

        mock_enqueue.assert_not_called()

    def test_refund_without_a_reference_sends_nothing(self) -> None:
        """A refund with no billed document has no customer to email."""
        self.set_customer_email(RECEIPT_EMAIL)
        doc = self.completed_refund_log(linked=False)

        with patch("frappe.enqueue") as mock_enqueue:
            doc.send_refund_receipt_email()

        mock_enqueue.assert_not_called()

    def test_customer_without_an_email_sends_nothing(self) -> None:
        """A customer with no email address receives no receipt."""
        self.set_customer_email("")
        doc = self.completed_refund_log()

        with patch("frappe.enqueue") as mock_enqueue:
            doc.send_refund_receipt_email()

        mock_enqueue.assert_not_called()

    def test_receipt_is_queued_with_the_printed_attachment(self) -> None:
        """A completed refund queues a receipt to the customer with a printout."""
        self.set_customer_email(RECEIPT_EMAIL)
        doc = self.completed_refund_log()

        with patch("frappe.attach_print", return_value={"fname": "receipt.pdf"}):
            with patch("frappe.enqueue") as mock_enqueue:
                doc.send_refund_receipt_email()

        kwargs = mock_enqueue.call_args.kwargs
        self.assertEqual(kwargs["recipients"], [RECEIPT_EMAIL])
        self.assertIn(doc.name, kwargs["subject"])
        self.assertEqual(kwargs["reference_doctype"], REFUND_LOG)
        self.assertEqual(kwargs["attachments"], [{"fname": "receipt.pdf"}])

    def test_receipt_waits_for_the_refund_to_commit(self) -> None:
        """The receipt holds its job until the refund is committed."""
        self.set_customer_email(RECEIPT_EMAIL)
        doc = self.completed_refund_log()

        with patch("frappe.attach_print", return_value={"fname": "receipt.pdf"}):
            with patch("frappe.enqueue") as mock_enqueue:
                doc.send_refund_receipt_email()

        self.assertTrue(mock_enqueue.call_args.kwargs["enqueue_after_commit"])

    def test_receipt_is_priced_in_the_refunds_own_currency(self) -> None:
        """The receipt formats refund_amount in the Refund Log's own currency."""
        self.set_customer_email(RECEIPT_EMAIL)
        doc = self.completed_refund_log(currency="USD")
        billed_in = frappe.db.get_value("Sales Invoice", self.invoice, "currency")
        self.assertNotEqual(doc.currency, billed_in)

        with patch("frappe.attach_print", return_value={"fname": "receipt.pdf"}):
            with patch("frappe.enqueue") as mock_enqueue:
                doc.send_refund_receipt_email()

        message = mock_enqueue.call_args.kwargs["message"]
        self.assertIn(fmt_money(doc.refund_amount, currency=doc.currency), message)
        self.assertNotIn(fmt_money(doc.refund_amount, currency=billed_in), message)

    def test_a_failed_printout_does_not_raise(self) -> None:
        """A receipt that cannot be printed is logged and returns None."""
        self.set_customer_email(RECEIPT_EMAIL)
        doc = self.completed_refund_log()

        with patch("frappe.attach_print", side_effect=Exception("no print format")):
            self.assertIsNone(doc.send_refund_receipt_email())

    def test_send_refund_receipt_reports_an_unknown_log(self) -> None:
        """Asking for a receipt for an unknown Refund Log reports failure."""
        self.assertFalse(send_refund_receipt("REFUND-NOWHERE"))

    def test_send_refund_receipt_sends_for_a_known_log(self) -> None:
        """Asking for a receipt for a known Refund Log queues the email."""
        self.set_customer_email(RECEIPT_EMAIL)
        doc = self.completed_refund_log()

        with patch("frappe.attach_print", return_value={"fname": "receipt.pdf"}):
            with patch("frappe.enqueue") as mock_enqueue:
                sent = send_refund_receipt(doc.name)

        self.assertTrue(sent)
        self.assertEqual(mock_enqueue.call_args.kwargs["recipients"], [RECEIPT_EMAIL])


class TestRefundReceiptPermission(PaystackTestCase):
    """A receipt is only sent by someone allowed to read the refund."""

    def setUp(self) -> None:
        """Record a refund, then sign in as a user with no Paystack access."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

        self.invoice = SalesInvoiceFactory.create(rate=1000)
        self.addCleanup(SalesInvoiceFactory.cleanup, self.invoice)

        self.payment_log = PaymentLogFactory.create(
            linked_docname=self.invoice,
            amount=1000,
            amount_paid=1000,
            status="Completed",
        )
        self.addCleanup(PaymentLogFactory.cleanup, self.payment_log)

        self.refund_log = RefundLogFactory.create(payment_log_name=self.payment_log, refund_amount=100)
        self.addCleanup(RefundLogFactory.cleanup, self.refund_log)

        self.become_outsider()

    def become_outsider(self) -> None:
        """Create a user holding no Paystack-related role and sign in as them."""
        if not frappe.db.exists("User", OUTSIDER_EMAIL):
            user = frappe.get_doc(
                {
                    "doctype": "User",
                    "email": OUTSIDER_EMAIL,
                    "first_name": "Paystack Outsider",
                    "send_welcome_email": 0,
                    "roles": [{"role": UNPRIVILEGED_ROLE}],
                }
            )
            user.flags.ignore_permissions = True
            user.insert()
            frappe.db.commit()

        self.addCleanup(cleanup_user, OUTSIDER_EMAIL)
        self.addCleanup(frappe.set_user, "Administrator")
        frappe.set_user(OUTSIDER_EMAIL)

    def test_an_outsider_cannot_send_a_receipt(self) -> None:
        """A user with no read on the refund cannot address its customer."""
        with patch("frappe.enqueue") as mock_enqueue:
            with self.assertRaises(frappe.PermissionError):
                send_refund_receipt(self.refund_log)

        mock_enqueue.assert_not_called()


class TestRefundStatusOnPaymentLog(PaystackTestCase):
    """A refund stamps its total and status onto the payment log."""

    def setUp(self) -> None:
        """Enable Paystack for the test company."""
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

    def refund(self, log_name: str, amount: float) -> None:
        """Record a completed refund against a payment log."""
        refund_name = RefundLogFactory.create_bypass_validate(
            log_name, refund_amount=amount, status="Completed"
        )
        self.addCleanup(RefundLogFactory.cleanup, refund_name)
        frappe.get_doc("Paystack Refund Log", refund_name).update_payment_log_total_refunded()

    def status_of(self, log_name: str) -> str:
        """Return the payment log's current status."""
        return frappe.db.get_value("Paystack Payment Log", log_name, "status")

    def test_full_refund_marks_the_log_refunded(self) -> None:
        """A refund covering the whole payment sets Refunded."""
        log_name = PaymentLogFactory.create_completed(amount=1000, amount_paid=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        self.refund(log_name, 1000)

        self.assertEqual(self.status_of(log_name), "Refunded")

    def test_partial_refund_marks_the_log_partially_refunded(self) -> None:
        """A refund below the captured amount sets Partially Refunded."""
        log_name = PaymentLogFactory.create_completed(amount=1000, amount_paid=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        self.refund(log_name, 400)

        self.assertEqual(self.status_of(log_name), "Partially Refunded")

    def test_refunded_log_is_not_payable(self) -> None:
        """A refunded payment reports is_payable as false."""
        log_name = PaymentLogFactory.create_completed(amount=1000, amount_paid=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        self.refund(log_name, 1000)
        doc = frappe.get_doc("Paystack Payment Log", log_name)

        self.assertFalse(doc.get_data()["is_payable"])

    def test_refund_status_helper(self) -> None:
        """refund_status maps a refunded total onto a log status."""
        self.assertEqual(refund_status(0, 1000), "Completed")
        self.assertEqual(refund_status(400, 1000), "Partially Refunded")
        self.assertEqual(refund_status(1000, 1000), "Refunded")
        self.assertEqual(refund_status(1200, 1000), "Refunded")
