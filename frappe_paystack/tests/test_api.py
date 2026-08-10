"""Tests for the Paystack API module: webhooks, payment links and guard paths."""

import json
import random
from typing import Any, Optional
from unittest.mock import patch

import frappe
import requests
from frappe.handler import is_valid_http_method
from frappe.integrations.utils import create_request_log
from frappe.utils import add_to_date, now_datetime, random_string
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from frappe_paystack.api import (
    HOSTED_CHECKOUT_LIMIT,
    HOSTED_CHECKOUT_WINDOW,
    checkout_reference,
    create_payment_link,
    get_payable_amount,
    get_webhook_request_data,
    initiate_refund_from_log,
    is_enabled_for_company,
    notify_payment_authorized,
    notify_pos_payment,
    paystack_webhook,
    process_charge_webhook_event,
    process_refund_webhook_event,
    process_webhook_event,
    settlement_mismatch,
    start_hosted_checkout,
    validate_payment_link,
)
from frappe_paystack.frappe_paystack.doctype.paystack_payment_log.paystack_payment_log import (
    create_payment_entry_from_log,
)
from frappe_paystack.tests.factories import (
    FRAPPE_MAJOR_VERSION,
    TEST_COMPANY,
    UNPRIVILEGED_ROLE,
    ChargeableInvoiceFactory,
    CustomerFactory,
    GatewaySettingFactory,
    PaymentLogFactory,
    RefundLogFactory,
    SalesInvoiceFactory,
    SalesOrderFactory,
    cleanup_doc,
    cleanup_user,
)
from frappe_paystack.tests.test_base import PaystackTestCase
from frappe_paystack.utils import get_customer_email, hmac_sha512
from frappe_paystack.utils.pos_payment import pos_payment_status

VALIDATE_PAYMENT_PATCH = (
    "frappe_paystack.frappe_paystack.doctype.paystack_payment_log."
    "paystack_payment_log.PaystackPaymentLog.validate_payment"
)

PAYMENT_LOG = "Paystack Payment Log"
REFUND_LOG = "Paystack Refund Log"
INTEGRATION_REQUEST = "Integration Request"
USER_PERMISSION = "User Permission"
COMPANY_WITHOUT_PAYSTACK = "_Test Company 2"

# A company the fixture payment does not belong to.
OTHER_COMPANY = "_Test Company 1"

# The currency the desk links are raised in.
CHARGE_CURRENCY = ChargeableInvoiceFactory.CURRENCY

# The customer these invoices bill.
CHARGE_CUSTOMER = "_Test Paystack USD Customer"

WEBHOOK_CUSTOMER = "_Test Paystack Webhook Customer"
WEBHOOK_EMAIL = "webhook.buyer@example.com"

# The caller IP the rate limiter counts these calls under.
CHECKOUT_CALLER_IP = "203.0.113.77"

HOSTED_CHECKOUT_URL = "https://checkout.paystack.com/abc"


def unique_transaction_id() -> int:
    """Generate a random numeric Paystack transaction ID for webhook payloads."""
    return random.randint(200_000_000, 999_999_999)


class TestPaystackWebhookProcessing(PaystackTestCase):
    """Tests for webhook event processing and idempotency."""

    def setUp(self):
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.track_doc("Paystack Gateway Setting", self.gateway_name)
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

    @patch(VALIDATE_PAYMENT_PATCH)
    def test_process_webhook_success_updates_all_log_fields(self, mock_vp):
        """A charge.success webhook sets status, amount, references and date."""

        mock_vp.return_value = {"status": True, "data": {"status": "success"}}
        tx_ref = f"ref_pay_{random_string(6)}"
        log_name = PaymentLogFactory.create(status="Pending", amount=1000, transaction_id=tx_ref)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        webhook_data = {
            "event": "charge.success",
            "data": {
                "id": unique_transaction_id(),
                "reference": tx_ref,
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
        self.assertEqual(log.payment_reference, tx_ref)

    @patch(VALIDATE_PAYMENT_PATCH)
    def test_process_webhook_failure_sets_failed_status(self, mock_vp):
        """A charge.failed webhook sets the log status to Failed."""
        mock_vp.return_value = {"status": True, "data": {"status": "success"}}
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        webhook_data = {
            "event": "charge.failed",
            "data": {
                "id": unique_transaction_id(),
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
        """A second delivery leaves the log as the first delivery set it."""
        mock_vp.return_value = {"status": True, "data": {"status": "success"}}
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        webhook_data = {
            "event": "charge.success",
            "data": {
                "id": unique_transaction_id(),
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
        original_reference = log.payment_reference

        process_webhook_event(webhook_data)
        log.reload()
        self.assertEqual(log.amount_paid, original_amount)
        self.assertEqual(log.payment_reference, original_reference)

    @patch(VALIDATE_PAYMENT_PATCH)
    def test_process_webhook_skips_already_completed_log(self, mock_vp):
        """A webhook for a Completed log leaves it Completed."""
        mock_vp.return_value = {"status": True, "data": {"status": "success"}}
        log_name = PaymentLogFactory.create_completed(amount=1000, amount_paid=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        webhook_data = {
            "event": "charge.success",
            "data": {
                "id": unique_transaction_id(),
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
        """A webhook in USD sets currency_paid to USD."""
        mock_vp.return_value = {"status": True, "data": {"status": "success"}}
        log_name = PaymentLogFactory.create(status="Pending", amount=100, currency="USD")
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        webhook_data = {
            "event": "charge.success",
            "data": {
                "id": unique_transaction_id(),
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

    def unmatched_webhook_error(self) -> str:
        """Return what the newest failed Paystack webhook call recorded."""
        return frappe.db.get_value(
            INTEGRATION_REQUEST,
            {"integration_request_service": "Paystack", "url": "webhook", "status": "Failed"},
            "error",
            order_by="creation desc",
        )

    def test_process_webhook_nonexistent_log_does_not_raise(self):
        """A webhook naming a log the site does not carry is filed as unmatched."""
        reference = "NONEXISTENT-LOG-999"
        webhook_data = {
            "event": "charge.success",
            "data": {
                "id": unique_transaction_id(),
                "reference": "ref_ghost",
                "status": "success",
                "amount": 50000,
                "currency": "NGN",
                "paid_at": "2024-06-15T14:30:00Z",
                "metadata": {"reference": reference},
            },
        }
        process_webhook_event(webhook_data)

        self.assertEqual(
            self.unmatched_webhook_error(),
            f"No Paystack Payment Log for reference {reference}",
        )

    def test_process_webhook_missing_metadata_reference_does_not_raise(self):
        """A webhook carrying no reference at all is filed as unmatched."""
        webhook_data = {
            "event": "charge.success",
            "data": {
                "id": unique_transaction_id(),
                "reference": "ref_no_meta",
                "status": "success",
                "amount": 50000,
                "currency": "NGN",
                "paid_at": "2024-06-15T14:30:00Z",
                "metadata": {},
            },
        }
        process_webhook_event(webhook_data)

        self.assertEqual(self.unmatched_webhook_error(), "No Paystack Payment Log for reference ")


class TestPaystackPaymentLink(PaystackTestCase):
    """Tests for payment link creation against real Sales Invoices."""

    def setUp(self):
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

    @patch(VALIDATE_PAYMENT_PATCH)
    def test_create_payment_link_for_sales_invoice_uses_outstanding(self, mock_vp):
        """create_payment_link defaults to outstanding_amount for a Sales Invoice."""
        mock_vp.return_value = {"status": True, "data": {"status": "success"}}

        sinv_name = ChargeableInvoiceFactory.create(customer=CHARGE_CUSTOMER, rate=5000)
        self.addCleanup(ChargeableInvoiceFactory.cleanup, sinv_name)

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
    def test_create_payment_link_with_explicit_partial_amount(self, mock_vp):
        """create_payment_link with an explicit amount creates a partial payment log."""
        mock_vp.return_value = {"status": True, "data": {"status": "success"}}

        sinv_name = ChargeableInvoiceFactory.create(customer=CHARGE_CUSTOMER, rate=10000)
        self.addCleanup(ChargeableInvoiceFactory.cleanup, sinv_name)

        url = create_payment_link("Sales Invoice", sinv_name, amount=3000.0)
        log_name = url.split("/paystack-checkout/")[-1]
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        log = frappe.get_doc("Paystack Payment Log", log_name)
        self.assertEqual(log.amount, 3000.0)

    @patch(VALIDATE_PAYMENT_PATCH)
    def test_create_payment_link_returns_valid_checkout_url(self, mock_vp):
        """The returned URL contains /paystack-checkout/ and the log name."""
        mock_vp.return_value = {"status": True, "data": {"status": "success"}}

        sinv_name = ChargeableInvoiceFactory.create(customer=CHARGE_CUSTOMER, rate=1000)
        self.addCleanup(ChargeableInvoiceFactory.cleanup, sinv_name)

        url = create_payment_link("Sales Invoice", sinv_name)
        log_name = url.split("/paystack-checkout/")[-1]
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        self.assertIn("/paystack-checkout/", url)
        self.assertTrue(frappe.db.exists("Paystack Payment Log", log_name))

    @patch(VALIDATE_PAYMENT_PATCH)
    def test_create_payment_link_carries_the_payment_request(self, mock_vp):
        """A payment link carries the Payment Request billing the document."""
        mock_vp.return_value = {"status": True, "data": {"status": "success"}}

        sinv_name = ChargeableInvoiceFactory.create(customer=CHARGE_CUSTOMER, rate=1000)
        self.addCleanup(ChargeableInvoiceFactory.cleanup, sinv_name)

        url = create_payment_link("Sales Invoice", sinv_name)
        log_name = url.split("/paystack-checkout/")[-1]
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        request = frappe.db.get_value("Paystack Payment Log", log_name, "payment_request")
        self.assertTrue(request)
        self.assertEqual(frappe.db.get_value("Payment Request", request, "reference_name"), sinv_name)

    @patch(VALIDATE_PAYMENT_PATCH)
    def test_create_payment_link_throws_when_not_enabled(self, mock_vp):
        """create_payment_link throws when Paystack is not configured."""
        mock_vp.return_value = {"status": True, "data": {"status": "success"}}

        self.disable_company_gateways()

        sinv_name = SalesInvoiceFactory.create(rate=1000)
        self.addCleanup(SalesInvoiceFactory.cleanup, sinv_name)

        with self.assertRaises(frappe.ValidationError):
            create_payment_link("Sales Invoice", sinv_name)


class TestPaystackValidatePaymentLink(PaystackTestCase):
    """Tests for the validate_payment_link endpoint used by the checkout page."""

    def setUp(self):
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

    @patch(VALIDATE_PAYMENT_PATCH)
    def test_validate_payment_link_returns_data_for_existing_log(self, mock_vp):
        """validate_payment_link returns checkout data including gateway settings."""
        mock_vp.return_value = {"status": True, "data": {"status": "success"}}

        sinv_name = ChargeableInvoiceFactory.create(customer=CHARGE_CUSTOMER, rate=2000)
        self.addCleanup(ChargeableInvoiceFactory.cleanup, sinv_name)

        url = create_payment_link("Sales Invoice", sinv_name)
        log_name = url.split("/paystack-checkout/")[-1]
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        data = validate_payment_link(log_name)
        self.assertEqual(data["reference_doctype"], "Sales Invoice")
        self.assertEqual(data["reference_docname"], sinv_name)
        self.assertEqual(data["reference"], log_name)
        self.assertEqual(data["customer"], CHARGE_CUSTOMER)

    def test_validate_payment_link_returns_empty_for_nonexistent(self):
        result = validate_payment_link("NONEXISTENT-LOG-999")
        self.assertEqual(result, {})


class TestPaystackWebhookSignatureFlow(PaystackTestCase):
    """End-to-end tests for webhook signature verification flow."""

    def setUp(self):
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

    @patch(VALIDATE_PAYMENT_PATCH)
    def test_webhook_with_valid_signature_processes_event(self, mock_vp):
        """A webhook with a valid signature processes the event and updates the log."""
        mock_vp.return_value = {"status": True, "data": {"status": "success"}}
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        secret = "sk_test_webhook"
        webhook_data = {
            "event": "charge.success",
            "data": {
                "id": unique_transaction_id(),
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

        with (
            patch("frappe_paystack.api.resolve_settings_for_signature") as mock_settings,
            patch("frappe_paystack.api.get_webhook_request_data") as mock_req,
        ):

            mock_settings.return_value = {
                "secret_key": secret,
                "webhook_secret": secret,
                "allowed_webhook_ips": None,
            }
            mock_req.return_value = (webhook_data, payload, signature, None)

            paystack_webhook()

        log = frappe.get_doc("Paystack Payment Log", log_name)
        self.assertIn(log.status, ("Processed", "Completed"))
        self.assertEqual(log.amount_paid, 200.0)

    @patch(VALIDATE_PAYMENT_PATCH)
    def test_webhook_with_invalid_signature_throws(self, mock_vp):
        """A webhook with an invalid signature throws PermissionError."""
        mock_vp.return_value = {"status": True, "data": {"status": "success"}}
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        webhook_data = {
            "event": "charge.success",
            "data": {
                "id": unique_transaction_id(),
                "reference": "ref_bad_sig",
                "status": "success",
                "amount": 20000,
                "currency": "NGN",
                "paid_at": "2024-06-15T14:30:00Z",
                "metadata": {"reference": log_name},
            },
        }
        payload = json.dumps(webhook_data).encode("utf-8")

        with (
            patch("frappe_paystack.api.resolve_settings_for_signature") as mock_settings,
            patch("frappe_paystack.api.get_webhook_request_data") as mock_req,
        ):

            mock_settings.return_value = None
            mock_req.return_value = (webhook_data, payload, "wrong_sig", None)

            with self.assertRaises(frappe.PermissionError):
                paystack_webhook()

        log = frappe.get_doc("Paystack Payment Log", log_name)
        self.assertEqual(log.status, "Pending")

    def test_webhook_with_no_matching_gateway_throws(self):
        """A webhook no enabled gateway can sign throws PermissionError."""
        webhook_data = {
            "event": "charge.success",
            "data": {
                "id": unique_transaction_id(),
                "reference": "ref_no_settings",
                "status": "success",
                "amount": 20000,
                "currency": "NGN",
                "paid_at": "2024-06-15T14:30:00Z",
                "metadata": {"reference": "NONEXISTENT"},
            },
        }
        payload = json.dumps(webhook_data).encode("utf-8")

        with (
            patch("frappe_paystack.api.resolve_settings_for_signature") as mock_settings,
            patch("frappe_paystack.api.get_webhook_request_data") as mock_req,
        ):

            mock_settings.return_value = None
            mock_req.return_value = (webhook_data, payload, "sig", None)

            with self.assertRaises(frappe.PermissionError):
                paystack_webhook()

    @patch(VALIDATE_PAYMENT_PATCH)
    def test_webhook_ip_not_in_allowlist_throws(self, mock_vp):
        """A webhook from a non-allowlisted IP throws PermissionError."""
        mock_vp.return_value = {"status": True, "data": {"status": "success"}}
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        secret = "sk_test_ip"
        webhook_data = {
            "event": "charge.success",
            "data": {
                "id": unique_transaction_id(),
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

        with (
            patch("frappe_paystack.api.resolve_settings_for_signature") as mock_settings,
            patch("frappe_paystack.api.get_webhook_request_data") as mock_req,
        ):

            mock_settings.return_value = {
                "secret_key": secret,
                "webhook_secret": secret,
                "allowed_webhook_ips": "52.31.139.74",
            }
            mock_req.return_value = (webhook_data, payload, signature, "1.1.1.1")

            with self.assertRaises(frappe.PermissionError):
                paystack_webhook()

        log = frappe.get_doc("Paystack Payment Log", log_name)
        self.assertEqual(log.status, "Pending")


class TestPaymentLinkVerification(PaystackTestCase):
    """Payment link creation completes without calling Paystack."""

    def setUp(self):
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

    def link_for(self, rate: float = 1000) -> str:
        """Raise an invoice and return the log behind its payment link."""
        sinv_name = ChargeableInvoiceFactory.create(customer=CHARGE_CUSTOMER, rate=rate)
        self.addCleanup(ChargeableInvoiceFactory.cleanup, sinv_name)

        url = create_payment_link("Sales Invoice", sinv_name)
        log_name = url.split("/paystack-checkout/")[-1]
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        return log_name

    def test_no_verification_call_is_made(self):
        """No Paystack request is sent while a payment link is created."""
        with patch("frappe_paystack.utils.utils.requests.get") as mock_get:
            log_name = self.link_for()

        mock_get.assert_not_called()
        self.assertTrue(frappe.db.exists("Paystack Payment Log", log_name))

    def test_link_is_created_while_paystack_is_unreachable(self):
        """A payment link is created while Paystack is unreachable."""
        with patch(
            "frappe_paystack.utils.utils.requests.get",
            side_effect=requests.ConnectionError("paystack down"),
        ):
            log_name = self.link_for()

        self.assertEqual(frappe.db.get_value("Paystack Payment Log", log_name, "status"), "Pending")

    def test_a_paid_log_is_still_verified(self):
        """A log that carries a reference is verified against Paystack."""
        log_name = self.link_for()
        log = frappe.get_doc("Paystack Payment Log", log_name)
        log.payment_reference = "mrc_ref_verify"

        with patch("frappe_paystack.utils.utils.requests.get") as mock_get:
            mock_get.return_value.ok = True
            mock_get.return_value.json.return_value = {"status": True, "data": {}}
            log.validate_payment()

        mock_get.assert_called_once()
        self.assertIn("mrc_ref_verify", mock_get.call_args.args[0])


class TestPaymentLinkCurrency(PaystackTestCase):
    """A currency Paystack cannot charge is refused."""

    def setUp(self):
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

    def company_currency_invoice(self) -> str:
        """Raise a submitted invoice in NGN, which Paystack cannot charge."""
        invoice = SalesInvoiceFactory.create(rate=1000)
        self.addCleanup(SalesInvoiceFactory.cleanup, invoice)
        return invoice

    @patch(VALIDATE_PAYMENT_PATCH)
    def test_unsupported_currency_throws(self, mock_vp):
        """A euro charge is refused and writes no Payment Log."""
        mock_vp.return_value = {"status": True, "data": {"status": "success"}}
        invoice = self.company_currency_invoice()

        with self.assertRaises(frappe.ValidationError):
            create_payment_link("Sales Invoice", invoice, currency="EUR")

        self.assertEqual(frappe.db.count("Paystack Payment Log", {"linked_docname": invoice}), 0)

    @patch(VALIDATE_PAYMENT_PATCH)
    def test_unchargeable_document_currency_throws(self, mock_vp):
        """The rupee invoice is refused, whatever currency the caller asks for."""
        mock_vp.return_value = {"status": True, "data": {"status": "success"}}
        invoice = self.company_currency_invoice()

        with self.assertRaises(frappe.ValidationError):
            create_payment_link("Sales Invoice", invoice)

        self.assertEqual(frappe.db.count("Paystack Payment Log", {"linked_docname": invoice}), 0)

    @patch(VALIDATE_PAYMENT_PATCH)
    def test_document_currency_reaches_the_log(self, mock_vp):
        """A supported document currency is charged unchanged."""
        mock_vp.return_value = {"status": True, "data": {"status": "success"}}

        invoice = ChargeableInvoiceFactory.create(customer=CHARGE_CUSTOMER, rate=1000)
        self.addCleanup(ChargeableInvoiceFactory.cleanup, invoice)

        url = create_payment_link("Sales Invoice", invoice, currency=CHARGE_CURRENCY)
        log_name = url.split("/paystack-checkout/")[-1]
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        self.assertEqual(
            frappe.db.get_value("Paystack Payment Log", log_name, "currency"),
            CHARGE_CURRENCY,
        )


class TestChargeWebhookIdempotency(PaystackTestCase):
    """A replayed charge webhook leaves the log unchanged."""

    def setUp(self):
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

    def charge_payload(self, log_name: str, reference: str) -> dict:
        """Build a charge.success payload naming a Payment Log."""
        return {
            "event": "charge.success",
            "data": {
                "id": unique_transaction_id(),
                "reference": reference,
                "status": "success",
                "amount": 50000,
                "currency": "NGN",
                "paid_at": "2024-06-15T14:30:00Z",
                "metadata": {"reference": log_name},
            },
        }

    def test_refunded_log_is_not_dragged_back_to_processed(self):
        """A late webhook leaves a Partially Refunded log at that status."""
        log_name = PaymentLogFactory.create_completed(amount=500, amount_paid=500)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        frappe.db.set_value("Paystack Payment Log", log_name, "status", "Partially Refunded")

        process_webhook_event(self.charge_payload(log_name, "ref_late_001"))

        self.assertEqual(
            frappe.db.get_value("Paystack Payment Log", log_name, "status"),
            "Partially Refunded",
        )

    def test_unmatched_reference_leaves_an_audit_trail(self):
        """A webhook naming no known log records an Integration Request."""
        before = frappe.db.count("Integration Request")

        process_webhook_event(self.charge_payload("NONEXISTENT-LOG-999", "ref_unmatched_001"))

        self.assertEqual(frappe.db.count("Integration Request"), before + 1)
        self.assertEqual(frappe.get_last_doc("Integration Request").status, "Failed")

    @patch(VALIDATE_PAYMENT_PATCH)
    def test_replayed_webhook_records_the_duplicate(self, mock_vp):
        """The second delivery of one event is logged and otherwise ignored."""
        mock_vp.return_value = {"status": True, "data": {"status": "success"}}
        log_name = PaymentLogFactory.create(status="Pending", amount=500)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        self.addCleanup(self.cleanup_reconciliation_log, log_name)

        payload = self.charge_payload(log_name, "ref_replay_001")
        process_webhook_event(payload)

        before = frappe.db.count("Integration Request")
        with patch("frappe_paystack.api.notify_pos_payment") as mock_pos:
            process_webhook_event(payload)

        mock_pos.assert_not_called()
        self.assertEqual(frappe.db.count("Integration Request"), before + 1)


class TestCompanyGate(PaystackTestCase):
    """is_enabled_for_company answers the desk's Paystack availability check."""

    def test_company_without_a_gateway_is_not_enabled(self) -> None:
        """A company with no gateway reports Paystack as unavailable."""
        self.assertFalse(is_enabled_for_company(COMPANY_WITHOUT_PAYSTACK))

    def test_company_with_a_gateway_is_enabled(self) -> None:
        """A company with an enabled gateway reports Paystack as available."""
        gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, gateway)

        self.assertTrue(is_enabled_for_company(TEST_COMPANY))


class TestWebhookRequestData(PaystackTestCase):
    """get_webhook_request_data reads the body, signature and IP off the request."""

    def build_request(self, body: Optional[str], signature: Optional[str] = None) -> None:
        """Install a werkzeug request as the current Frappe request."""
        headers = {"x-paystack-signature": signature} if signature else {}
        builder = EnvironBuilder(
            method="POST",
            data=body,
            content_type="application/json",
            headers=headers,
        )
        previous_request = getattr(frappe.local, "request", None)
        previous_ip = getattr(frappe.local, "request_ip", None)
        self.addCleanup(setattr, frappe.local, "request", previous_request)
        self.addCleanup(setattr, frappe.local, "request_ip", previous_ip)

        frappe.local.request = Request(builder.get_environ())
        frappe.local.request_ip = "52.31.139.74"

    def test_signed_body_is_returned_with_its_signature_and_ip(self) -> None:
        """A signed JSON body yields the parsed data, raw payload, signature and IP."""
        body = json.dumps({"event": "charge.success", "data": {"id": 1}})
        self.build_request(body, signature="deadbeef")

        data, payload, signature, request_ip = get_webhook_request_data()

        self.assertEqual(data["event"], "charge.success")
        self.assertEqual(payload, body.encode("utf-8"))
        self.assertEqual(signature, "deadbeef")
        self.assertEqual(request_ip, "52.31.139.74")

    def test_empty_body_yields_an_empty_payload(self) -> None:
        """A request with no JSON body yields empty data and no signature."""
        self.build_request("null")

        data, payload, signature, _unused = get_webhook_request_data()

        self.assertEqual(data, {})
        self.assertEqual(payload, b"null")
        self.assertIsNone(signature)


class TestNotifyPaymentAuthorized(PaystackTestCase):
    """notify_payment_authorized settles through the Payment Request."""

    def setUp(self) -> None:
        """Enable a Paystack gateway for the test company."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

    def test_log_without_a_payment_request_notifies_nothing(self) -> None:
        """A log raised outside a Payment Request has nothing to settle."""
        log = frappe._dict(name="PAY-LOG-1", payment_request=None)

        self.assertIsNone(notify_payment_authorized(log))

    def test_missing_payment_request_notifies_nothing(self) -> None:
        """A log naming a Payment Request that no longer exists is skipped."""
        log = frappe._dict(name="PAY-LOG-2", payment_request="ACC-PRQ-NOWHERE")

        self.assertIsNone(notify_payment_authorized(log))

    def test_draft_payment_request_is_skipped(self) -> None:
        """A draft Payment Request is skipped."""
        log = frappe._dict(name="PAY-LOG-3", payment_request="ACC-PRQ-DRAFT")

        with (
            patch("frappe.db.exists", return_value=True),
            patch("frappe.get_doc", return_value=frappe._dict(docstatus=0)),
        ):
            self.assertIsNone(notify_payment_authorized(log))

    def test_settled_payment_request_is_not_paid_twice(self) -> None:
        """A Payment Request already at Paid is left alone."""
        log = frappe._dict(name="PAY-LOG-4", payment_request="ACC-PRQ-PAID")

        with (
            patch("frappe.db.exists", return_value=True),
            patch(
                "frappe.get_doc",
                return_value=frappe._dict(docstatus=1, status="Paid"),
            ),
        ):
            self.assertIsNone(notify_payment_authorized(log))

    def test_settlement_failure_is_logged_not_raised(self) -> None:
        """A Payment Request that cannot be loaded is logged and returns None."""
        log = frappe._dict(name="PAY-LOG-5", payment_request="ACC-PRQ-BOOM")

        original = frappe.get_doc

        def failing_request_load(*args: object, **kwargs: object) -> object:
            if args and args[0] == "Payment Request":
                raise Exception("cannot load")
            return original(*args, **kwargs)

        with (
            patch("frappe.db.exists", return_value=True),
            patch("frappe.get_doc", side_effect=failing_request_load),
        ):
            self.assertIsNone(notify_payment_authorized(log))


class TestNotifyPosPaymentGuards(PaystackTestCase):
    """notify_pos_payment only publishes for a resolvable POS Invoice."""

    def test_payment_request_without_a_reference_publishes_nothing(self) -> None:
        """A Payment Request that bills nothing releases no POS screen."""
        payment_request = frappe.get_doc(
            {
                "doctype": "Payment Request",
                "payment_gateway": "Paystack",
                "reference_doctype": "POS Invoice",
                "reference_name": "POS-TEST-200",
                "payment_request_type": "Inward",
                "grand_total": 500,
                "currency": "NGN",
                "email_to": "customer@example.com",
            }
        )
        payment_request.flags.ignore_permissions = True
        payment_request.flags.ignore_mandatory = True
        payment_request.flags.ignore_links = True
        payment_request.flags.ignore_validate = True
        payment_request.insert()
        frappe.db.commit()
        self.addCleanup(cleanup_doc, "Payment Request", payment_request.name)
        frappe.db.set_value("Payment Request", payment_request.name, "reference_name", None)

        integration_request = create_request_log(
            {},
            service_name="Paystack",
            status="Queued",
            reference_doctype="Payment Request",
            reference_docname=payment_request.name,
        )

        with patch("frappe.publish_realtime") as mock_publish:
            notify_pos_payment(integration_request.name, 500, True)

        mock_publish.assert_not_called()


class TestRefundWebhookRouting(PaystackTestCase):
    """process_webhook_event routes refund events to the refund handler."""

    def setUp(self) -> None:
        """Create a Completed payment with a Pending refund to settle."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

        self.payment_log = PaymentLogFactory.create_completed(amount=1000, amount_paid=1000)
        self.addCleanup(PaymentLogFactory.cleanup, self.payment_log)
        self.transaction_id = frappe.db.get_value(PAYMENT_LOG, self.payment_log, "transaction_id")

    def create_refund_log(self, refund_reference: str) -> str:
        """Create a Pending Refund Log awaiting a webhook."""
        name = RefundLogFactory.create(self.payment_log, refund_amount=100, refund_reference=refund_reference)
        self.addCleanup(RefundLogFactory.cleanup, name)
        return name

    def refund_event(self, event: str, refund_reference: str) -> dict:
        """Build a Paystack refund webhook payload."""
        return {
            "event": event,
            "data": {
                "refund_reference": refund_reference,
                "transaction": {"reference": "mrc_routed_ref"},
                "amount": 10000,
            },
        }

    def test_refund_event_is_routed_to_the_refund_handler(self) -> None:
        """A refund.failed event reaches the refund handler through the router."""
        refund = self.create_refund_log("rf_routed_001")

        process_webhook_event(self.refund_event("refund.failed", "rf_routed_001"))

        self.assertEqual(frappe.db.get_value(REFUND_LOG, refund, "status"), "Failed")

    def test_charge_event_is_routed_to_the_charge_handler(self) -> None:
        """A charge event leaves an open Refund Log Pending."""
        refund = self.create_refund_log("rf_routed_002")

        process_webhook_event({"event": "charge.success", "data": {"id": unique_transaction_id()}})

        self.assertEqual(frappe.db.get_value(REFUND_LOG, refund, "status"), "Pending")

    def test_event_without_any_reference_is_ignored(self) -> None:
        """A refund event carrying no reference leaves the Refund Log Pending."""
        refund = self.create_refund_log("rf_routed_003")

        process_refund_webhook_event({"event": "refund.processed", "data": {"amount": 10000}})

        self.assertEqual(frappe.db.get_value(REFUND_LOG, refund, "status"), "Pending")

    def test_refund_reference_alone_settles_the_log(self) -> None:
        """A refund reference alone settles the Refund Log."""
        refund = self.create_refund_log("rf_routed_005")

        process_refund_webhook_event(
            {
                "event": "refund.processed",
                "data": {"refund_reference": "rf_routed_005", "status": "processed"},
            }
        )

        self.assertIn(
            frappe.db.get_value(REFUND_LOG, refund, "status"),
            ("Processed", "Completed"),
        )

    def test_malformed_payload_is_logged_not_raised(self) -> None:
        """A payload whose transaction is not an object returns None."""
        payload = {
            "event": "refund.processed",
            "data": {"reference": "rf_routed_004", "transaction": "not-an-object"},
        }

        self.assertIsNone(process_refund_webhook_event(payload))


class TestWebhookCompanyScope(PaystackTestCase):
    """A signed webhook reaches only the records of the company that signed it."""

    def setUp(self) -> None:
        """Raise a capture and a refund booked to the test company."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

        self.payment_log = PaymentLogFactory.create(amount=1000, status="Pending")
        self.addCleanup(PaymentLogFactory.cleanup, self.payment_log)

    def charge_event(self) -> dict:
        """Build a successful charge payload naming the fixture capture."""
        return {
            "event": "charge.success",
            "data": {
                "id": unique_transaction_id(),
                "reference": "ref_company_scope",
                "status": "success",
                "amount": 100000,
                "currency": "NGN",
                "metadata": {"reference": self.payment_log},
            },
        }

    def test_a_charge_cannot_settle_another_companys_capture(self) -> None:
        """A charge signed by another company leaves the capture Pending."""
        process_charge_webhook_event(self.charge_event(), OTHER_COMPANY)

        self.assertEqual(frappe.db.get_value(PAYMENT_LOG, self.payment_log, "status"), "Pending")

    def test_a_charge_signed_by_the_owner_still_settles(self) -> None:
        """A charge signed by the capture's own company settles it."""
        process_charge_webhook_event(self.charge_event(), TEST_COMPANY)

        self.assertEqual(frappe.db.get_value(PAYMENT_LOG, self.payment_log, "amount_paid"), 1000)

    def refund_event(self) -> dict:
        """Build a processed refund payload naming the fixture refund."""
        return {
            "event": "refund.processed",
            "data": {"refund_reference": "rf_company_scope", "status": "processed"},
        }

    def settled_refund_log(self) -> str:
        """Record a Pending Refund Log the refund payload resolves to."""
        frappe.db.set_value(
            PAYMENT_LOG,
            self.payment_log,
            {"status": "Completed", "amount_paid": 1000},
            update_modified=False,
        )
        frappe.clear_document_cache(PAYMENT_LOG, self.payment_log)

        refund = RefundLogFactory.create(
            payment_log_name=self.payment_log,
            refund_amount=100,
            refund_reference="rf_company_scope",
        )
        self.addCleanup(RefundLogFactory.cleanup, refund)
        return refund

    def test_a_refund_cannot_settle_another_companys_refund(self) -> None:
        """A refund signed by another company leaves the Refund Log Pending."""
        refund = self.settled_refund_log()

        process_refund_webhook_event(self.refund_event(), OTHER_COMPANY)

        self.assertEqual(frappe.db.get_value(REFUND_LOG, refund, "status"), "Pending")


class TestChargeWebhookCustomerEmail(PaystackTestCase):
    """A charge webhook leaves the billed customer's address alone."""

    def setUp(self) -> None:
        """Bill a customer with no email address on file."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

        self.customer = CustomerFactory.create(customer_name=WEBHOOK_CUSTOMER)
        self.addCleanup(CustomerFactory.cleanup, self.customer)

        self.invoice = SalesInvoiceFactory.create(rate=1000, customer=WEBHOOK_CUSTOMER)
        self.addCleanup(SalesInvoiceFactory.cleanup, self.invoice)

        self.log_name = PaymentLogFactory.create(linked_docname=self.invoice, amount=1000, status="Pending")
        self.addCleanup(PaymentLogFactory.cleanup, self.log_name)

    def charge_event(self, metadata: dict) -> dict:
        """Build a successful Paystack charge webhook payload."""
        return {
            "event": "charge.success",
            "data": {
                "id": unique_transaction_id(),
                "reference": "ref_email_backfill",
                "status": "success",
                "amount": 100000,
                "currency": "NGN",
                "paid_at": "2026-06-15T14:30:00Z",
                "metadata": metadata,
            },
        }

    def test_the_payload_cannot_set_the_customer_email(self) -> None:
        """The address the payer typed at checkout never reaches the Customer."""
        frappe.db.set_value("Customer", self.customer, "email_id", None)

        process_charge_webhook_event(
            self.charge_event(
                {
                    "reference": self.log_name,
                    "customer": self.customer,
                    "email": WEBHOOK_EMAIL,
                }
            )
        )

        self.assertIsNone(frappe.db.get_value("Customer", self.customer, "email_id"))

    def test_existing_customer_email_is_kept(self) -> None:
        """A customer who already has an email keeps it."""
        frappe.db.set_value("Customer", self.customer, "email_id", "kept@example.com")

        process_charge_webhook_event(
            self.charge_event(
                {
                    "reference": self.log_name,
                    "customer": self.customer,
                    "email": WEBHOOK_EMAIL,
                }
            )
        )

        self.assertEqual(
            frappe.db.get_value("Customer", self.customer, "email_id"),
            "kept@example.com",
        )

    def test_the_capture_is_still_settled(self) -> None:
        """The charge the payload carries is processed as it always was."""
        process_charge_webhook_event(
            self.charge_event(
                {
                    "reference": self.log_name,
                    "customer": self.customer,
                    "email": WEBHOOK_EMAIL,
                }
            )
        )

        self.assertEqual(
            frappe.db.get_value(PAYMENT_LOG, self.log_name, "amount_paid"),
            1000,
        )

    def test_malformed_charge_payload_is_logged_not_raised(self) -> None:
        """A payload whose metadata is not an object returns None."""
        payload = {"event": "charge.success", "data": {"metadata": "not-an-object"}}

        self.assertIsNone(process_charge_webhook_event(payload))


class TestPayableAmount(PaystackTestCase):
    """get_payable_amount mirrors ERPNext's outstanding calculation."""

    def test_outstanding_amount_wins(self) -> None:
        """A document with an outstanding amount is billed for exactly that."""
        doc = frappe._dict(outstanding_amount=250, grand_total=1000, advance_paid=0)

        self.assertEqual(get_payable_amount(doc), 250)

    def test_order_falls_back_to_grand_total_less_advances(self) -> None:
        """A Sales Order is billed for its total less what has already been paid."""
        order = SalesOrderFactory.create(rate=1000)
        self.addCleanup(SalesOrderFactory.cleanup, order)
        doc = frappe.get_doc("Sales Order", order)

        self.assertEqual(get_payable_amount(doc), doc.grand_total)

    def test_a_fully_advanced_order_is_not_payable(self) -> None:
        """An order whose advances cover it is not payable again."""
        doc = frappe._dict(outstanding_amount=0, grand_total=1000, advance_paid=1500)

        self.assertEqual(get_payable_amount(doc), 0.0)


class TestManualRefundGuards(PaystackTestCase):
    """initiate_refund_from_log refuses refunds Paystack cannot process."""

    def setUp(self) -> None:
        """Create a Completed payment that can be refunded."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

        self.payment_log = PaymentLogFactory.create_completed(amount=1000, amount_paid=1000)
        self.addCleanup(PaymentLogFactory.cleanup, self.payment_log)
        self.addCleanup(self.cleanup_refund_logs)

    def cleanup_refund_logs(self) -> None:
        """Remove every Refund Log raised against the fixture payment."""
        names = frappe.get_all(REFUND_LOG, filters={"payment_log": self.payment_log}, pluck="name")
        for name in names:
            RefundLogFactory.cleanup(name)

    def test_payment_without_a_transaction_cannot_be_refunded(self) -> None:
        """A Completed payment with no Paystack transaction cannot be refunded."""
        frappe.db.set_value(PAYMENT_LOG, self.payment_log, "transaction_id", None)
        frappe.clear_document_cache(PAYMENT_LOG, self.payment_log)

        with self.assertRaises(frappe.ValidationError):
            initiate_refund_from_log(self.payment_log, 100)

    def test_paystack_failure_marks_the_refund_failed(self) -> None:
        """A rejected refund leaves the Refund Log Failed with the Error Log named."""
        with patch("frappe_paystack.api.initiate_refund", side_effect=Exception("declined")):
            refund = initiate_refund_from_log(self.payment_log, 100, reason="Damaged")

        status, errors = frappe.db.get_value(REFUND_LOG, refund, ["status", "errors"])
        self.assertEqual(status, "Failed")
        self.assertIn("Error Log", errors)


class TestMoneyEndpointPermissions(PaystackTestCase):
    """The whitelisted endpoints that move money check who is calling."""

    def setUp(self) -> None:
        """Create a refundable payment and an order to bill."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

        self.payment_log = PaymentLogFactory.create_completed(amount=1000, amount_paid=1000)
        self.addCleanup(PaymentLogFactory.cleanup, self.payment_log)
        self.addCleanup(self.cleanup_refund_logs)

        self.invoice = frappe.db.get_value(PAYMENT_LOG, self.payment_log, "linked_docname")

    def cleanup_refund_logs(self) -> None:
        """Remove every Refund Log raised against the fixture payment."""
        for name in frappe.get_all(REFUND_LOG, filters={"payment_log": self.payment_log}, pluck="name"):
            RefundLogFactory.cleanup(name)

    def sign_in_as(self, *roles: str) -> str:
        """Create a user holding exactly these roles and switch to it."""
        email = f"paystack-perm-{random_string(8).lower()}@example.com"
        user = frappe.get_doc(
            {
                "doctype": "User",
                "email": email,
                "first_name": "Paystack Permission",
                "send_welcome_email": 0,
                "user_type": "System User",
                "roles": [{"role": role} for role in roles],
            }
        )
        user.flags.ignore_permissions = True
        user.insert()

        self.addCleanup(cleanup_user, email)
        self.addCleanup(frappe.set_user, "Administrator")
        frappe.set_user(email)
        return email

    def test_an_accounts_user_cannot_refund(self) -> None:
        """An Accounts User raising a refund gets a PermissionError."""
        self.sign_in_as("Accounts User")

        with self.assertRaises(frappe.PermissionError):
            initiate_refund_from_log(self.payment_log, 100)

    def test_an_accounts_manager_may_refund(self) -> None:
        """An Accounts Manager raises a Refund Log."""
        self.sign_in_as("Accounts Manager")

        with patch(
            "frappe_paystack.api.initiate_refund",
            return_value={"status": "processed", "reference": "rfd_perm", "raw": {}},
        ):
            refund = initiate_refund_from_log(self.payment_log, 100)

        self.assertTrue(frappe.db.exists(REFUND_LOG, refund))

    def test_a_user_without_the_document_cannot_raise_a_payment_link(self) -> None:
        """A user without access to the document gets a PermissionError."""
        self.sign_in_as(UNPRIVILEGED_ROLE)

        with self.assertRaises(frappe.PermissionError):
            create_payment_link("Sales Invoice", self.invoice)

    def test_a_customer_email_is_not_enumerable(self) -> None:
        """get_customer_email throws for a user without read on Customer."""
        customer = CustomerFactory.create()
        self.sign_in_as(UNPRIVILEGED_ROLE)

        with self.assertRaises(frappe.PermissionError):
            get_customer_email(customer)

    def restrict_to_other_company(self, user: str) -> None:
        """Give a user a company permission the fixture payment falls outside of."""
        permission = frappe.get_doc(
            {
                "doctype": USER_PERMISSION,
                "user": user,
                "allow": "Company",
                "for_value": OTHER_COMPANY,
            }
        )
        permission.flags.ignore_permissions = True
        permission.insert()
        self.addCleanup(
            frappe.delete_doc,
            USER_PERMISSION,
            permission.name,
            force=True,
            ignore_permissions=True,
        )
        frappe.db.commit()
        frappe.clear_cache(user=user)

    def test_a_payment_outside_the_users_company_cannot_be_refunded(self) -> None:
        """An Accounts Manager restricted to another company is refused."""
        user = self.sign_in_as("Accounts Manager")
        self.restrict_to_other_company(user)

        with self.assertRaises(frappe.PermissionError):
            initiate_refund_from_log(self.payment_log, 100)

    def test_a_refused_company_raises_no_refund_log(self) -> None:
        """The refusal happens before any Refund Log is written."""
        user = self.sign_in_as("Accounts Manager")
        self.restrict_to_other_company(user)

        with self.assertRaises(frappe.PermissionError):
            initiate_refund_from_log(self.payment_log, 100)

        frappe.set_user("Administrator")
        self.assertEqual(frappe.db.count(REFUND_LOG, {"payment_log": self.payment_log}), 0)

    def test_a_pos_link_outside_the_users_company_is_not_reported(self) -> None:
        """pos_payment_status refuses a log the caller may not read."""
        user = self.sign_in_as("Accounts User")
        self.restrict_to_other_company(user)

        with self.assertRaises(frappe.PermissionError):
            pos_payment_status(self.payment_log)

    def test_a_user_with_no_log_access_is_told_nothing(self) -> None:
        """pos_payment_status refuses a role holding no read on the log."""
        self.sign_in_as(UNPRIVILEGED_ROLE)

        with self.assertRaises(frappe.PermissionError):
            pos_payment_status(self.payment_log)


class StubPaymentRequest:
    """A Payment Request as ERPNext ships it, carrying no on_payment_authorized."""

    def __init__(self, name: str, status: str = "Requested") -> None:
        """Build a submitted, collectable request."""
        self.name = name
        self.docstatus = 1
        self.status = status
        self.outstanding_amount = 0
        self.flags = frappe._dict()
        self.paid = False

    def set_as_paid(self) -> None:
        """Record that ERPNext was asked to settle this request."""
        self.paid = True


class TestSettlementMismatch(PaystackTestCase):
    """settlement_mismatch reports only when an amount was captured."""

    def test_a_capture_of_nothing_is_not_a_mismatch(self) -> None:
        """A log with no amount_paid reports no mismatch."""
        log = frappe._dict(name="PAY-LOG-EMPTY", amount_paid=0)

        self.assertIsNone(settlement_mismatch(log, frappe._dict()))


class TestSettlementFallback(PaystackTestCase):
    """Settlement through set_as_paid, and what it records afterwards."""

    def setUp(self) -> None:
        """Raise a Pending payment log to settle."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)
        self.log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, self.log_name)

    def settle(self, request: StubPaymentRequest, payment_entry: Optional[str]) -> None:
        """Run the settlement against a stubbed Payment Request."""
        log = frappe._dict(name=self.log_name, payment_request=request.name, amount_paid=0)

        with (
            patch("frappe.db.exists", return_value=True),
            patch("frappe.get_doc", return_value=request),
            patch("frappe_paystack.api.resolve_payment_entry", return_value=payment_entry),
        ):
            notify_payment_authorized(log)

    def test_a_request_without_the_hook_is_settled_directly(self) -> None:
        """A request carrying no on_payment_authorized is settled by set_as_paid."""
        request = StubPaymentRequest("ACC-PRQ-STUB-1")

        self.settle(request, None)

        self.assertTrue(request.paid)

    def test_nothing_booked_leaves_the_log_alone(self) -> None:
        """A settlement that produced no Payment Entry records none."""
        request = StubPaymentRequest("ACC-PRQ-STUB-2")

        self.settle(request, None)

        self.assertFalse(frappe.db.get_value(PAYMENT_LOG, self.log_name, "payment_entry"))

    def test_an_already_booked_request_is_not_paid_twice(self) -> None:
        """A request with a Payment Entry already booked is left as it stands."""
        request = StubPaymentRequest("ACC-PRQ-STUB-3")

        self.settle(request, "ACC-PAY-STUB-3")

        self.assertFalse(request.paid)

    def test_the_booked_payment_entry_reaches_the_log(self) -> None:
        """The booked Payment Entry is recorded on the log."""
        request = StubPaymentRequest("ACC-PRQ-STUB-4")

        self.settle(request, "ACC-PAY-STUB-4")

        self.assertEqual(
            frappe.db.get_value(PAYMENT_LOG, self.log_name, "payment_entry"),
            "ACC-PAY-STUB-4",
        )


class TestPosNotificationReplay(PaystackTestCase):
    """A POS charge is released once, however many webhooks arrive."""

    def build_charge_request(self, status: str) -> str:
        """File an Integration Request standing for a POS charge."""
        payment_request = frappe.get_doc(
            {
                "doctype": "Payment Request",
                "payment_gateway": "Paystack",
                "reference_doctype": "POS Invoice",
                "reference_name": "POS-TEST-300",
                "payment_request_type": "Inward",
                "grand_total": 500,
                "currency": "NGN",
                "email_to": "customer@example.com",
            }
        )
        payment_request.flags.ignore_permissions = True
        payment_request.flags.ignore_mandatory = True
        payment_request.flags.ignore_links = True
        payment_request.flags.ignore_validate = True
        payment_request.insert()
        frappe.db.commit()
        self.addCleanup(cleanup_doc, "Payment Request", payment_request.name)

        integration_request = create_request_log(
            {},
            service_name="Paystack",
            status="Queued",
            reference_doctype="Payment Request",
            reference_docname=payment_request.name,
        )
        frappe.db.set_value("Integration Request", integration_request.name, "status", status)
        return integration_request.name

    def test_a_notified_charge_is_not_released_again(self) -> None:
        """An Integration Request already Authorized publishes nothing on retry."""
        reference = self.build_charge_request("Authorized")

        with patch("frappe.publish_realtime") as mock_publish:
            notify_pos_payment(reference, 500, True)

        mock_publish.assert_not_called()


class TestRefundWebhookWithoutAReference(PaystackTestCase):
    """Paystack sends some refunds under the transaction alone."""

    def setUp(self) -> None:
        """Create a paid transaction carrying an unsettled refund."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

        self.payment_log = PaymentLogFactory.create_completed(amount=1000, amount_paid=1000)
        self.addCleanup(PaymentLogFactory.cleanup, self.payment_log)

        self.merchant_reference = f"mrc_{random_string(8)}"
        frappe.db.set_value(PAYMENT_LOG, self.payment_log, "payment_reference", self.merchant_reference)

        self.refund_log = RefundLogFactory.create(self.payment_log, refund_amount=100)
        self.addCleanup(RefundLogFactory.cleanup, self.refund_log)

    def test_the_transaction_alone_settles_the_refund(self) -> None:
        """A payload with no refund reference is matched through the payment."""
        process_refund_webhook_event(
            {
                "event": "refund.processed",
                "data": {"transaction": {"reference": self.merchant_reference}},
            }
        )

        self.assertIn(
            frappe.db.get_value(REFUND_LOG, self.refund_log, "status"),
            ("Processed", "Completed"),
        )

    def test_the_recorded_refund_reference_is_kept(self) -> None:
        """A payload naming no refund keeps the stored refund reference."""
        frappe.db.set_value(REFUND_LOG, self.refund_log, "refund_reference", "rf_already_known")

        process_refund_webhook_event(
            {
                "event": "refund.processed",
                "data": {"transaction": {"reference": self.merchant_reference}},
            }
        )

        self.assertEqual(
            frappe.db.get_value(REFUND_LOG, self.refund_log, "refund_reference"),
            "rf_already_known",
        )


class TestPaymentEntryFailure(PaystackTestCase):
    """A settlement that cannot build a Payment Entry records why."""

    def setUp(self) -> None:
        """Raise a Processed log awaiting its Payment Entry."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)
        self.log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, self.log_name)

        frappe.db.set_value(PAYMENT_LOG, self.log_name, {"status": "Processed", "amount_paid": 1000})
        frappe.clear_document_cache(PAYMENT_LOG, self.log_name)

    def test_the_failure_is_written_to_the_log(self) -> None:
        """A Payment Entry that cannot be built writes its reason to the log."""
        with patch(
            "frappe_paystack.frappe_paystack.doctype.paystack_payment_log"
            ".paystack_payment_log.get_payment_entry",
            side_effect=Exception("cannot build"),
        ):
            create_payment_entry_from_log(self.log_name)

        self.assertIn(
            "Paystack Payment Entry creation failed",
            frappe.db.get_value(PAYMENT_LOG, self.log_name, "errors") or "",
        )


class TestCustomerEmailLookup(PaystackTestCase):
    """The desk reads a customer's email to address a payment link."""

    def test_a_permitted_user_reads_the_email(self) -> None:
        """A user who can read the Customer gets the address."""
        customer = CustomerFactory.create(customer_name=WEBHOOK_CUSTOMER)
        self.addCleanup(cleanup_doc, "Customer", customer)
        frappe.db.set_value("Customer", customer, "email_id", WEBHOOK_EMAIL)

        self.assertEqual(get_customer_email(customer), WEBHOOK_EMAIL)


class TestHostedCheckout(PaystackTestCase):
    """A gateway in Hosted mode sends the customer to Paystack's own page."""

    def setUp(self) -> None:
        """Enable Paystack and raise an open checkout link."""
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

        self.log = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, self.log)

    def start(self, **kwargs: str) -> tuple:
        """Open a hosted checkout against a stubbed initialise call."""
        with patch(
            "frappe_paystack.api.initialize_transaction",
            return_value="https://checkout.paystack.com/abc",
        ) as initialize:
            url = start_hosted_checkout(self.log, **kwargs)
        return url, initialize

    def test_the_hosted_url_is_returned(self) -> None:
        """The customer is handed the page Paystack opened for this payment."""
        url, _ = self.start()

        self.assertEqual(url, "https://checkout.paystack.com/abc")

    def test_the_url_is_stored_and_reused(self) -> None:
        """The stored authorization URL is returned without a second initialise."""
        self.start()

        with patch("frappe_paystack.api.initialize_transaction") as initialize:
            again = start_hosted_checkout(self.log)

        initialize.assert_not_called()
        self.assertEqual(again, "https://checkout.paystack.com/abc")

    def test_the_charge_carries_the_webhook_metadata(self) -> None:
        """The charge carries the log name and doctype in its metadata."""
        _, initialize = self.start()

        metadata = initialize.call_args.kwargs["metadata"]
        self.assertEqual(metadata["reference"], self.log)
        self.assertEqual(metadata["reference_doctype"], "Sales Invoice")

    def test_a_supplied_email_is_used(self) -> None:
        """A supplied email reaches both the charge and its metadata."""
        _, initialize = self.start(email="typed@example.com")

        self.assertEqual(initialize.call_args.kwargs["email"], "typed@example.com")
        self.assertEqual(initialize.call_args.kwargs["metadata"]["email"], "typed@example.com")

    def test_the_customer_email_is_the_default(self) -> None:
        """The email already on the customer is used when none is typed."""
        invoice = frappe.db.get_value(PAYMENT_LOG, self.log, "linked_docname")
        customer = frappe.db.get_value("Sales Invoice", invoice, "customer")
        frappe.db.set_value("Customer", customer, "email_id", "on.file@example.com")
        self.addCleanup(frappe.db.set_value, "Customer", customer, "email_id", None)

        _, initialize = self.start()

        self.assertEqual(initialize.call_args.kwargs["email"], "on.file@example.com")

    def test_the_callback_returns_to_the_app(self) -> None:
        """Paystack sends the customer back to the checkout page afterwards."""
        _, initialize = self.start()

        self.assertTrue(
            initialize.call_args.kwargs["callback_url"].endswith(f"/paystack-checkout/{self.log}")
        )

    def refuse(self, reference: str, message: str) -> None:
        """Assert the guard refuses a reference before Paystack is called."""
        with patch("frappe_paystack.api.initialize_transaction") as initialize:
            with self.assertRaises(frappe.ValidationError) as caught:
                start_hosted_checkout(reference)

        self.assertIn(message, str(caught.exception))
        initialize.assert_not_called()

    def test_a_settled_link_cannot_be_reopened(self) -> None:
        """A Completed log is refused a hosted checkout."""
        frappe.db.set_value(PAYMENT_LOG, self.log, "status", "Completed")
        frappe.clear_document_cache(PAYMENT_LOG, self.log)

        self.refuse(self.log, "can no longer be paid")

    def test_an_expired_link_cannot_be_reopened(self) -> None:
        """An expired log is refused a hosted checkout."""
        frappe.db.set_value(PAYMENT_LOG, self.log, "expires_at", add_to_date(now_datetime(), hours=-1))
        frappe.clear_document_cache(PAYMENT_LOG, self.log)

        self.refuse(self.log, "can no longer be paid")

    def test_an_unknown_reference_is_refused(self) -> None:
        """Nothing is initialised for a link that does not exist."""
        self.refuse("PSLOG-DOES-NOT-EXIST", "no longer available")


class HostedCheckoutRequestTestCase(PaystackTestCase):
    """Drives the hosted checkout with a real request in scope."""

    def install_request(self, method: str = "POST") -> None:
        """Install a werkzeug request as the current Frappe request, clearing the job marker."""
        previous_request = getattr(frappe.local, "request", None)
        previous_ip = getattr(frappe.local, "request_ip", None)
        self.addCleanup(self.restore_job, getattr(frappe.local, "job", None))
        self.addCleanup(setattr, frappe.local, "request", previous_request)
        self.addCleanup(setattr, frappe.local, "request_ip", previous_ip)

        if hasattr(frappe.local, "job"):
            del frappe.local.job

        frappe.local.request = Request(EnvironBuilder(method=method).get_environ())
        frappe.local.request_ip = CHECKOUT_CALLER_IP

    def restore_job(self, job: Optional[Any]) -> None:
        """Put the background-job marker back, if there was one."""
        if job is not None:
            frappe.local.job = job


class TestHostedCheckoutMethod(HostedCheckoutRequestTestCase):
    """The endpoint is POST only."""

    def test_a_get_is_refused(self) -> None:
        """A GET raises PermissionError."""
        self.install_request("GET")

        with self.assertRaises(frappe.PermissionError):
            is_valid_http_method(start_hosted_checkout)

    def test_a_post_is_accepted(self) -> None:
        """A POST reaches the handler."""
        self.install_request("POST")

        self.assertIsNone(is_valid_http_method(start_hosted_checkout))


class TestHostedCheckoutRateLimit(HostedCheckoutRequestTestCase):
    """Hosted checkout calls are capped per caller IP."""

    def setUp(self) -> None:
        """Start from an empty per-IP counter and clear it on cleanup."""
        super().setUp()
        self.install_request()
        self.addCleanup(frappe.cache.delete, self.counter_key())
        frappe.cache.delete(self.counter_key())

    def counter_key(self) -> bytes:
        """Return the redis key frappe's rate limiter counts this caller in."""
        key = frappe.cache.make_key(f"rl:{frappe.form_dict.cmd}:{CHECKOUT_CALLER_IP}")

        # version-16 counts each window in a key of its own.
        if FRAPPE_MAJOR_VERSION >= 16:
            key += f":{HOSTED_CHECKOUT_WINDOW}".encode()

        return key

    def open_checkout(self) -> str:
        """Call the endpoint against a log whose checkout is already open."""
        with patch(
            "frappe_paystack.api.payable_log",
            return_value=frappe._dict(authorization_url=HOSTED_CHECKOUT_URL),
        ):
            return start_hosted_checkout("PSLOG-1")

    def test_traffic_under_the_limit_passes(self) -> None:
        """Exactly the limit is served."""
        for _unused in range(HOSTED_CHECKOUT_LIMIT):
            self.assertEqual(self.open_checkout(), HOSTED_CHECKOUT_URL)

    def test_the_limit_is_the_last_accepted_call(self) -> None:
        """The call after the limit is refused."""
        for _unused in range(HOSTED_CHECKOUT_LIMIT):
            self.open_checkout()

        with self.assertRaises(frappe.RateLimitExceededError):
            self.open_checkout()


class TestCheckoutReference(PaystackTestCase):
    """The Payment Log a checkout URL names."""

    def test_the_last_segment_is_the_log(self) -> None:
        """The last path segment of a checkout URL is the log name."""
        self.assertEqual(checkout_reference("https://site.test/paystack-checkout/PSLOG-1"), "PSLOG-1")

    def test_a_trailing_slash_is_ignored(self) -> None:
        """A URL that ends in a slash still names its log."""
        self.assertEqual(
            checkout_reference("https://site.test/paystack-checkout/PSLOG-1/"),
            "PSLOG-1",
        )
