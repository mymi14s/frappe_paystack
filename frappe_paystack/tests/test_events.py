"""The Sales Invoice document events that drive auto-refunds."""

from typing import Any
from unittest.mock import patch

import frappe
from frappe.utils import flt

from frappe_paystack.events import sales_invoice_on_submit
from frappe_paystack.tests.factories import (
    TEST_COMPANY,
    CreditNoteFactory,
    GatewaySettingFactory,
    PaymentLogFactory,
    RefundLogFactory,
    SalesInvoiceFactory,
)
from frappe_paystack.tests.test_base import PaystackTestCase

INITIATE_REFUND = "frappe_paystack.events.initiate_refund"

COMPANY_WITHOUT_PAYSTACK = "_Test Company 2"

REFUND_RESPONSE = {
    "status": "processed",
    "reference": "rf_events_001",
    "amount": 400.0,
    "currency": "NGN",
    "raw": {"status": True},
}


class AutoRefundTestCase(PaystackTestCase):
    """Shared fixtures for credit-note driven auto-refunds."""

    auto_refund = True

    def setUp(self) -> None:
        """Enable a Paystack gateway and raise an invoice to refund against."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create(auto_refund=self.auto_refund)
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

        self.invoice = SalesInvoiceFactory.create(rate=1000)
        self.addCleanup(SalesInvoiceFactory.cleanup, self.invoice)

        self.refund_call = None

    def create_payment_log(self, amount_paid: float = 1000) -> str:
        """Create a Completed Payment Log against the fixture invoice."""
        log_name = PaymentLogFactory.create(
            linked_docname=self.invoice,
            amount=1000,
            amount_paid=amount_paid,
            status="Completed",
        )
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        self.addCleanup(self.cleanup_refund_logs, log_name)
        return log_name

    def cleanup_refund_logs(self, payment_log_name: str) -> None:
        """Remove every Refund Log raised against a Payment Log."""
        names = frappe.get_all(
            "Paystack Refund Log",
            filters={"payment_log": payment_log_name},
            pluck="name",
        )
        for name in names:
            RefundLogFactory.cleanup(name)

    def create_credit_note(self, rate: float = 400, **stub: Any) -> str:
        """Submit a credit note against the fixture invoice with Paystack stubbed."""
        replacement = stub or {"return_value": REFUND_RESPONSE}
        with patch(INITIATE_REFUND, **replacement) as refund_call:
            credit_note = CreditNoteFactory.create(return_against=self.invoice, rate=rate)
        self.refund_call = refund_call
        self.addCleanup(CreditNoteFactory.cleanup, credit_note)
        return credit_note

    def credit_note_payload(self, **overrides: Any) -> frappe._dict:
        """Return an in-memory credit note the submit event can be run over."""
        payload = frappe._dict(
            doctype="Sales Invoice",
            name="SINV-RET-GUARD",
            company=TEST_COMPANY,
            is_return=1,
            return_against=self.invoice,
            grand_total=-400,
            base_grand_total=-400,
        )
        payload.update(overrides)
        return payload

    def refund_logs_for(self, payment_log_name: str) -> list:
        """Return the Refund Logs raised against a Payment Log."""
        return frappe.get_all(
            "Paystack Refund Log",
            filters={"payment_log": payment_log_name},
            fields=["name", "status", "refund_amount", "refund_reference", "errors"],
        )


class TestAutoRefundGuards(AutoRefundTestCase):
    """sales_invoice_on_submit refuses to refund unless every condition holds."""

    def test_ordinary_invoice_is_ignored(self) -> None:
        """A non-return invoice raises no Refund Log."""
        log_name = self.create_payment_log()

        sales_invoice_on_submit(frappe.get_doc("Sales Invoice", self.invoice))

        self.assertEqual(self.refund_logs_for(log_name), [])

    def test_return_without_return_against_is_ignored(self) -> None:
        """A standalone return with no original invoice raises no Refund Log."""
        log_name = self.create_payment_log()

        sales_invoice_on_submit(self.credit_note_payload(return_against=None))

        self.assertEqual(self.refund_logs_for(log_name), [])

    def test_company_without_paystack_is_ignored(self) -> None:
        """A credit note for a company with no gateway raises no Refund Log."""
        log_name = self.create_payment_log()

        sales_invoice_on_submit(self.credit_note_payload(company=COMPANY_WITHOUT_PAYSTACK))

        self.assertEqual(self.refund_logs_for(log_name), [])

    def test_unpaid_payment_log_is_ignored(self) -> None:
        """A Completed log with nothing captured raises no Refund Log."""
        log_name = self.create_payment_log(amount_paid=0)

        self.create_credit_note()

        self.assertEqual(self.refund_logs_for(log_name), [])

    def test_zero_value_credit_note_is_ignored(self) -> None:
        """A zero-value credit note raises no Refund Log."""
        log_name = self.create_payment_log()

        self.create_credit_note(rate=0)

        self.assertEqual(self.refund_logs_for(log_name), [])

    def move_payment_log(self, payment_log_name: str, **values: Any) -> None:
        """Rewrite a Payment Log's identifying columns, restoring them after."""
        previous = frappe.db.get_value("Paystack Payment Log", payment_log_name, list(values), as_dict=True)
        self.addCleanup(
            frappe.db.set_value,
            "Paystack Payment Log",
            payment_log_name,
            dict(previous),
            update_modified=False,
        )
        frappe.db.set_value("Paystack Payment Log", payment_log_name, values, update_modified=False)

    def test_a_capture_on_another_doctype_is_not_refunded(self) -> None:
        """A capture against a document of another doctype is left alone."""
        log_name = self.create_payment_log()
        self.move_payment_log(log_name, linked_doctype="Sales Order")

        with patch(INITIATE_REFUND) as refund_call:
            sales_invoice_on_submit(self.credit_note_payload())

        refund_call.assert_not_called()
        self.assertEqual(self.refund_logs_for(log_name), [])

    def test_a_capture_of_another_company_is_not_refunded(self) -> None:
        """A capture booked to another company is left alone."""
        log_name = self.create_payment_log()
        self.move_payment_log(log_name, company=COMPANY_WITHOUT_PAYSTACK)

        with patch(INITIATE_REFUND) as refund_call:
            sales_invoice_on_submit(self.credit_note_payload())

        refund_call.assert_not_called()
        self.assertEqual(self.refund_logs_for(log_name), [])

    def test_missing_payment_log_is_ignored(self) -> None:
        """An invoice with no Paystack payment raises no Refund Log."""
        credit_note = self.create_credit_note()

        # Scoped to the credit note the event ran over.
        self.assertEqual(frappe.db.count("Paystack Refund Log", {"linked_docname": credit_note}), 0)


class TestAutoRefundDisabled(AutoRefundTestCase):
    """Auto-refund is opt-in per gateway."""

    auto_refund = False

    def test_disabled_gateway_raises_no_refund(self) -> None:
        """A gateway without auto_refund_on_credit_note raises no Refund Log."""
        log_name = self.create_payment_log()

        self.create_credit_note()

        self.assertEqual(self.refund_logs_for(log_name), [])


class TestAutoRefundSuccess(AutoRefundTestCase):
    """A credit note against a Paystack payment refunds automatically."""

    def test_refund_log_is_created_and_processed(self) -> None:
        """One credit note raises exactly one refund carrying the Paystack reference."""
        log_name = self.create_payment_log()

        self.create_credit_note()

        refunds = self.refund_logs_for(log_name)
        self.assertEqual(len(refunds), 1)
        self.assertEqual(flt(refunds[0]["refund_amount"]), 400)
        self.assertEqual(refunds[0]["refund_reference"], "rf_events_001")

    def test_negative_credit_note_total_refunds_its_magnitude(self) -> None:
        """A credit note booked with a negative total refunds that amount as positive."""
        log_name = self.create_payment_log()

        credit_note = self.create_credit_note()

        self.assertLess(flt(frappe.db.get_value("Sales Invoice", credit_note, "grand_total")), 0)
        self.assertEqual(flt(self.refund_logs_for(log_name)[0]["refund_amount"]), 400)

    def test_raw_paystack_response_is_stored(self) -> None:
        """The Paystack response body is kept on the Refund Log."""
        log_name = self.create_payment_log()

        self.create_credit_note()

        raw = frappe.db.get_value(
            "Paystack Refund Log", self.refund_logs_for(log_name)[0]["name"], "raw_response"
        )
        self.assertIn("status", raw)

    def test_reversal_entry_is_booked(self) -> None:
        """A processed refund books the reversal Payment Entry and completes."""
        log_name = self.create_payment_log()

        self.create_credit_note()

        refund = self.refund_logs_for(log_name)[0]
        self.assertEqual(refund["status"], "Completed")
        self.assertIsNotNone(
            frappe.db.get_value("Paystack Refund Log", refund["name"], "reversal_payment_entry")
        )

    def test_refund_is_capped_at_the_amount_captured(self) -> None:
        """A credit note larger than the payment refunds only what was captured."""
        log_name = self.create_payment_log(amount_paid=250)

        self.create_credit_note(rate=400)

        self.assertEqual(flt(self.refund_logs_for(log_name)[0]["refund_amount"]), 250)

    def test_paystack_is_called_with_the_credit_note_note(self) -> None:
        """The Paystack call carries a merchant note naming the credit note."""
        self.create_payment_log()

        credit_note = self.create_credit_note()

        kwargs = self.refund_call.call_args.kwargs
        self.assertEqual(kwargs["amount"], 400)
        self.assertEqual(kwargs["currency"], "NGN")
        self.assertEqual(kwargs["company"], TEST_COMPANY)
        self.assertIn(credit_note, kwargs["merchant_note"])


class TestAutoRefundFailure(AutoRefundTestCase):
    """A rejected refund is recorded on the Refund Log."""

    def test_failed_refund_marks_the_log_failed(self) -> None:
        """A Paystack error leaves the single Refund Log Failed."""
        log_name = self.create_payment_log()

        self.create_credit_note(side_effect=Exception("gateway down"))

        refunds = self.refund_logs_for(log_name)
        self.assertEqual(len(refunds), 1)
        self.assertEqual(refunds[0]["status"], "Failed")

    def test_failed_refund_records_the_error_log(self) -> None:
        """A failed refund names the Error Log holding the traceback."""
        log_name = self.create_payment_log()

        self.create_credit_note(side_effect=Exception("gateway down"))

        self.assertIn("Error Log", self.refund_logs_for(log_name)[0]["errors"])

    def test_failed_refund_books_no_reversal_entry(self) -> None:
        """A failed refund books no reversal Payment Entry."""
        log_name = self.create_payment_log()

        self.create_credit_note(side_effect=Exception("gateway down"))

        self.assertIsNone(
            frappe.db.get_value(
                "Paystack Refund Log",
                self.refund_logs_for(log_name)[0]["name"],
                "reversal_payment_entry",
            )
        )


class TestSecondCreditNote(AutoRefundTestCase):
    """A payment that was already partly refunded still takes credit notes."""

    def submit_credit_notes(self, *totals: float) -> None:
        """Run the submit event for a series of credit notes."""
        with patch(INITIATE_REFUND, return_value=REFUND_RESPONSE):
            for index, total in enumerate(totals, start=1):
                sales_invoice_on_submit(
                    self.credit_note_payload(
                        name=f"SINV-RET-{index}",
                        grand_total=-total,
                        base_grand_total=-total,
                    )
                )

    def test_second_credit_note_refunds_only_what_is_left(self) -> None:
        """The second refund is capped at the payment less the first refund."""
        log_name = self.create_payment_log()

        self.submit_credit_notes(400, 800)

        refunds = sorted(self.refund_logs_for(log_name), key=lambda row: flt(row["refund_amount"]))
        self.assertEqual(len(refunds), 2)
        self.assertEqual(flt(refunds[0]["refund_amount"]), 400)
        self.assertEqual(flt(refunds[1]["refund_amount"]), 600)

    def test_fully_refunded_payment_raises_no_further_refund(self) -> None:
        """A fully refunded payment raises no second Refund Log."""
        log_name = self.create_payment_log()

        self.submit_credit_notes(1000, 500)

        self.assertEqual(len(self.refund_logs_for(log_name)), 1)

    def test_a_rejected_refund_never_blocks_the_credit_note(self) -> None:
        """A refund the validation refuses leaves the credit note submitted."""
        log_name = self.create_payment_log()
        spent = RefundLogFactory.create(log_name, refund_amount=1000)
        self.addCleanup(RefundLogFactory.cleanup, spent)
        # set_value skips on_update.
        frappe.db.set_value("Paystack Refund Log", spent, "status", "Processed", update_modified=False)

        with patch("frappe_paystack.events.get_total_refunded", return_value=0):
            credit_note = self.create_credit_note(rate=400)

        self.assertTrue(frappe.db.exists("Sales Invoice", credit_note))
        self.assertEqual(len(self.refund_logs_for(log_name)), 1)
        self.assertTrue(
            frappe.db.exists(
                "Error Log",
                {"method": f"Paystack auto-refund failed for credit note {credit_note}"},
            ),
            "the rejected insert was never reported",
        )
