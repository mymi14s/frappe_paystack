# Copyright (c) 2026, Anthony Emmanuel and Contributors
# See license.txt

import frappe

from frappe_paystack.tests.factories import (
    TEST_COMPANY,
    GatewaySettingFactory,
    PaymentLogFactory,
    RefundLogFactory,
)
from frappe_paystack.tests.test_base import PaystackTestCase

REFUND_LOG = "Paystack Refund Log"
PAYMENT_LOG = "Paystack Payment Log"

CHARGE = 1000.0


class TestPaystackRefundLog(PaystackTestCase):
    """The Paystack Refund Log doctype."""

    def setUp(self) -> None:
        """Settle one payment, so there is money to refund."""
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

        self.payment_log = PaymentLogFactory.create_completed(amount=CHARGE, amount_paid=CHARGE)
        self.addCleanup(PaymentLogFactory.cleanup, self.payment_log)

    def build(self, **overrides):
        """Return an unsaved refund of the settled payment."""
        values = {
            "doctype": REFUND_LOG,
            "payment_log": self.payment_log,
            "company": TEST_COMPANY,
            "refund_amount": 100,
            "currency": "NGN",
            "status": "Pending",
        }
        values.update(overrides)
        return frappe.get_doc(values)

    def test_validate_normalizes_an_unsupported_currency_to_ngn(self) -> None:
        """validate answers NGN for a currency Paystack does not settle in."""
        refund = self.build(currency="EUR")
        refund.validate()

        self.assertEqual(refund.currency, "NGN")

    def test_validate_refuses_a_status_outside_the_options(self) -> None:
        """validate rejects a status the field does not offer."""
        with self.assertRaises(frappe.ValidationError):
            self.build(status="NotARealStatus").validate()

    def test_validate_refuses_a_zero_amount(self) -> None:
        """validate rejects a refund of nothing."""
        with self.assertRaises(frappe.ValidationError):
            self.build(refund_amount=0).validate()

    def test_validate_refuses_more_than_was_captured(self) -> None:
        """validate rejects a refund above the money the payment holds."""
        with self.assertRaises(frappe.ValidationError):
            self.build(refund_amount=CHARGE + 1).validate()

    def test_validate_requires_a_payment_log(self) -> None:
        """validate rejects a refund that names no payment."""
        with self.assertRaises(frappe.ValidationError):
            self.build(payment_log=None).validate()

    def test_validate_refuses_a_payment_that_was_never_captured(self) -> None:
        """validate rejects a refund of a payment still awaiting its money."""
        pending = PaymentLogFactory.create(status="Pending", amount=CHARGE)
        self.addCleanup(PaymentLogFactory.cleanup, pending)

        with self.assertRaises(frappe.ValidationError):
            self.build(payment_log=pending).validate()

    def test_the_log_stays_a_draft(self) -> None:
        """validate refuses a submitted refund; the reversal is booked on update."""
        refund = self.build()
        refund.docstatus = 1

        with self.assertRaises(frappe.ValidationError):
            refund.validate()

    def test_a_refund_within_the_balance_is_recorded(self) -> None:
        """A refund of part of the capture is accepted."""
        name = RefundLogFactory.create(self.payment_log, refund_amount=250)
        self.addCleanup(RefundLogFactory.cleanup, name)

        self.assertEqual(frappe.db.get_value(REFUND_LOG, name, "docstatus"), 0)
