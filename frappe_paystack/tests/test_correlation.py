"""The Paystack Payment Log name is the reference every row of a payment carries."""

from unittest.mock import patch

import frappe

from frappe_paystack.api import (
    notify_payment_authorized,
    process_charge_webhook_event,
    process_refund_webhook_event,
)
from frappe_paystack.tests.factories import GatewaySettingFactory, PaymentLogFactory, RefundLogFactory
from frappe_paystack.tests.test_base import PaystackTestCase
from frappe_paystack.utils import log_error_for, record_failure

PAYMENT_LOG = "Paystack Payment Log"
REFUND_LOG = "Paystack Refund Log"
INTEGRATION_REQUEST = "Integration Request"
ERROR_LOG = "Error Log"


class CorrelationTestCase(PaystackTestCase):
    """Shared lookups for rows filed against a payment."""

    def errors_for(self, doctype: str, name: str) -> list:
        """Return the Error Logs filed against a record by this test."""
        return frappe.get_all(
            ERROR_LOG,
            filters={
                "reference_doctype": doctype,
                "reference_name": name,
                "creation": [">=", self.started_at],
            },
            pluck="method",
        )

    def requests_for(self, doctype: str, name: str) -> list:
        """Return the Integration Requests filed against a record."""
        return frappe.get_all(
            INTEGRATION_REQUEST,
            filters={
                "reference_doctype": doctype,
                "reference_docname": name,
                "creation": [">=", self.started_at],
            },
            pluck="name",
        )


class TestErrorLogReference(CorrelationTestCase):
    """log_error_for files an Error Log under the record it traces."""

    def setUp(self) -> None:
        """Raise a payment log to file errors against."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)
        self.log = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, self.log)

    def test_an_error_is_filed_against_the_payment_log(self) -> None:
        """The Error Log carries the payment log as its reference."""
        log_error_for("Traced failure", "boom", PAYMENT_LOG, self.log)

        self.assertIn("Traced failure", self.errors_for(PAYMENT_LOG, self.log))

    def test_an_unresolvable_reference_is_dropped(self) -> None:
        """A reference to a missing record is dropped and the log still files."""
        name = log_error_for("Orphan failure", "boom", PAYMENT_LOG, "PAY-LOG-GONE")

        self.assertTrue(name)
        self.assertEqual(self.errors_for(PAYMENT_LOG, "PAY-LOG-GONE"), [])

    def test_an_unreferenced_error_is_still_logged(self) -> None:
        """A failure with no reference is still logged."""
        self.assertTrue(log_error_for("Unattached failure", "boom"))

    def test_a_traceback_is_used_when_no_message_is_given(self) -> None:
        """record_failure defaults to the traceback it was raised from."""
        try:
            raise ValueError("boom")
        except ValueError:
            message = record_failure(
                "Traced failure",
                reference_doctype=PAYMENT_LOG,
                reference_name=self.log,
            )

        self.assertIn("Error Log", message)  # pylint: disable=used-before-assignment
        self.assertIn("Traced failure", self.errors_for(PAYMENT_LOG, self.log))

    def test_a_failure_message_names_the_error_log(self) -> None:
        """The message written to the document links to the full trace."""
        message = record_failure("Traced failure", "boom", PAYMENT_LOG, self.log)

        self.assertIn("/app/error-log/", message)

    def test_a_failure_without_an_error_log_still_reads(self) -> None:
        """A failure with no Error Log still returns a readable message."""
        with patch("frappe_paystack.utils.utils.log_error_for", return_value=None):
            message = record_failure("Traced failure")

        self.assertIn("See the Error Log", message)


class TestWebhookCorrelation(CorrelationTestCase):
    """Every webhook outcome is filed under the log it concerns."""

    def setUp(self) -> None:
        """Raise a Pending payment log a webhook can settle."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)
        self.log = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, self.log)

    def charge_event(self, transaction_id: int) -> dict:
        """Build a charge.success payload naming this test's payment log."""
        return {
            "event": "charge.success",
            "data": {
                "id": transaction_id,
                "reference": f"mrc-{self.log}",
                "status": "success",
                "amount": 100000,
                "currency": "NGN",
                "paid_at": "2024-06-15T14:30:00Z",
                "metadata": {"reference": self.log},
            },
        }

    def test_a_charge_failure_is_filed_against_the_payment_log(self) -> None:
        """A failing charge handler files its Error Log against the payment log."""
        with patch("frappe_paystack.api.notify_pos_payment", side_effect=Exception("boom")):
            process_charge_webhook_event(self.charge_event(701_000_001))

        self.assertTrue(self.errors_for(PAYMENT_LOG, self.log))

    def test_a_charge_failure_without_a_reference_is_still_logged(self) -> None:
        """An unmatched charge payload returns None."""
        self.assertIsNone(
            process_charge_webhook_event({"event": "charge.success", "data": {"metadata": "not-an-object"}})
        )

    def test_a_replayed_webhook_is_filed_against_the_original_log(self) -> None:
        """The duplicate row points at the log that already recorded the event."""
        frappe.db.set_value(PAYMENT_LOG, self.log, "webhook_event_id", "701000002")
        frappe.clear_document_cache(PAYMENT_LOG, self.log)

        process_charge_webhook_event(self.charge_event(701_000_002))

        outputs = frappe.get_all(
            INTEGRATION_REQUEST,
            filters={
                "reference_doctype": PAYMENT_LOG,
                "reference_docname": self.log,
                "creation": [">=", self.started_at],
            },
            pluck="output",
        )
        self.assertTrue(any("duplicate event" in (out or "") for out in outputs))


class TestRefundWebhookCorrelation(CorrelationTestCase):
    """A refund webhook failure names the refund it was handling."""

    def setUp(self) -> None:
        """Create a Completed payment carrying a Pending refund."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

        self.payment_log = PaymentLogFactory.create_completed(amount=1000, amount_paid=1000)
        self.addCleanup(PaymentLogFactory.cleanup, self.payment_log)

        self.refund_log = RefundLogFactory.create(
            self.payment_log, refund_amount=100, refund_reference="rf_traced_001"
        )
        self.addCleanup(RefundLogFactory.cleanup, self.refund_log)

    def test_a_refund_failure_is_filed_against_the_refund_log(self) -> None:
        """A failing refund handler files its Error Log against the refund log."""
        with patch("frappe_paystack.api.json") as encoder:
            encoder.dumps.side_effect = ValueError("boom")
            process_refund_webhook_event(
                {
                    "event": "refund.processed",
                    "data": {"refund_reference": "rf_traced_001", "status": "processed"},
                }
            )

        self.assertTrue(self.errors_for(REFUND_LOG, self.refund_log))

    def test_an_unmatched_refund_failure_is_still_logged(self) -> None:
        """An unmatched refund failure returns None."""
        with patch("frappe_paystack.api.find_refund_log", side_effect=Exception("boom")):
            self.assertIsNone(
                process_refund_webhook_event(
                    {"event": "refund.processed", "data": {"reference": "rf_nowhere"}}
                )
            )


class TestSettlementCorrelation(CorrelationTestCase):
    """A settlement that fails is traceable back to its payment log."""

    def setUp(self) -> None:
        """Enable a Paystack gateway for the test company."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)
        self.log = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, self.log)

    def test_a_settlement_failure_is_filed_against_the_payment_log(self) -> None:
        """notify_payment_authorized records its failure on the payment log."""
        log = frappe._dict(name=self.log, payment_request="ACC-PRQ-BOOM")

        original = frappe.get_doc

        def failing_request_load(*args: object, **kwargs: object) -> object:
            if args and args[0] == "Payment Request":
                raise Exception("cannot load")
            return original(*args, **kwargs)

        with (
            patch("frappe.db.exists", return_value=True),
            patch("frappe.get_doc", side_effect=failing_request_load),
        ):
            notify_payment_authorized(log)

        self.assertTrue(self.errors_for(PAYMENT_LOG, self.log))
