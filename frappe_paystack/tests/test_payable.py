"""What a document owes, and the clamp that holds a payment link to the outstanding amount."""

from typing import Any
from unittest.mock import patch

import frappe
from frappe.utils import flt

from frappe_paystack.api import create_payment_link
from frappe_paystack.frappe_paystack.doctype.paystack_payment_log.paystack_payment_log import PAYABLE_STATUSES
from frappe_paystack.tests.factories import (
    VALIDATE_PAYMENT_PATCH_TARGET,
    ChargeableInvoiceFactory,
    CreditNoteFactory,
    GatewaySettingFactory,
    PaymentLogFactory,
    POSInvoiceFactory,
    SalesInvoiceFactory,
    SalesOrderFactory,
    cleanup_doc,
)
from frappe_paystack.tests.test_base import PaystackTestCase

TEST_COMPANY = "_Test Company"
PAYMENT_LOG = "Paystack Payment Log"
POS_INVOICE = "POS Invoice"

INVOICE_TOTAL = 1000.0

# The dollar invoice bills a customer of its own.
CHARGE_CUSTOMER = "_Test Paystack Link Customer"

# Submitted Sales Invoice statuses that still owe money.
PAYABLE_INVOICE_STATUSES = [
    "Unpaid",
    "Partly Paid",
    "Overdue",
    "Unpaid and Discounted",
    "Partly Paid and Discounted",
    "Overdue and Discounted",
]

# Submitted Sales Invoice statuses that are settled and refuse payment.
SETTLED_INVOICE_STATUSES = [
    "Paid",
    "Return",
    "Credit Note Issued",
    "Internal Transfer",
]


class PayableStatusTestCase(PaystackTestCase):
    """Shared helpers for exercising the payable-status guard."""

    def setUp(self) -> None:
        """Enable Paystack for the test company."""
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

    def force_status(self, doctype: str, docname: str, status: str) -> None:
        """Set a document's status directly, bypassing the state machine."""
        frappe.db.set_value(doctype, docname, "status", status)
        frappe.clear_document_cache(doctype, docname)

    def create_payment_log(self, doctype: str, docname: str) -> str:
        """Attempt to create a payment log against a document."""
        log = frappe.get_doc(
            {
                "doctype": "Paystack Payment Log",
                "company": TEST_COMPANY,
                "linked_doctype": doctype,
                "linked_docname": docname,
                "amount": 100,
                "currency": "NGN",
                "status": "Pending",
            }
        )
        log.flags.ignore_permissions = True
        with patch(
            VALIDATE_PAYMENT_PATCH_TARGET,
            return_value={"status": True, "data": {"status": "success"}},
        ):
            log.insert()
        self.addCleanup(cleanup_doc, "Paystack Payment Log", log.name)
        return log.name


class TestPayableInvoiceStatuses(PayableStatusTestCase):
    """A Sales Invoice that still owes money accepts payment."""

    def test_payable_statuses_are_accepted(self) -> None:
        """Every unsettled invoice status permits a payment log."""
        sinv = SalesInvoiceFactory.create(rate=1000)
        self.addCleanup(SalesInvoiceFactory.cleanup, sinv)

        for status in PAYABLE_INVOICE_STATUSES:
            with self.subTest(status=status):
                self.force_status("Sales Invoice", sinv, status)
                log_name = self.create_payment_log("Sales Invoice", sinv)
                self.assertTrue(
                    frappe.db.exists("Paystack Payment Log", log_name),
                    f"{status} should be payable",
                )
                cleanup_doc("Paystack Payment Log", log_name)

    def test_discounted_statuses_are_payable(self) -> None:
        """Bill-discounted statuses are listed as payable."""
        for status in (
            "Unpaid and Discounted",
            "Partly Paid and Discounted",
            "Overdue and Discounted",
        ):
            self.assertIn(status, PAYABLE_STATUSES)


class TestSettledInvoiceStatuses(PayableStatusTestCase):
    """A settled Sales Invoice refuses a new payment."""

    def test_settled_statuses_are_refused(self) -> None:
        """Paid / Return / Credit Note Issued reject a payment log."""
        sinv = SalesInvoiceFactory.create(rate=1000)
        self.addCleanup(SalesInvoiceFactory.cleanup, sinv)

        for status in SETTLED_INVOICE_STATUSES:
            with self.subTest(status=status):
                self.force_status("Sales Invoice", sinv, status)
                with self.assertRaises(frappe.ValidationError):
                    self.create_payment_log("Sales Invoice", sinv)

    def test_settled_error_names_the_document(self) -> None:
        """The refusal message identifies the offending document."""
        sinv = SalesInvoiceFactory.create(rate=1000)
        self.addCleanup(SalesInvoiceFactory.cleanup, sinv)
        self.force_status("Sales Invoice", sinv, "Paid")

        with self.assertRaises(frappe.ValidationError) as ctx:
            self.create_payment_log("Sales Invoice", sinv)

        self.assertIn(sinv, str(ctx.exception))
        self.assertIn("already settled", str(ctx.exception))

    def test_credit_note_itself_is_not_payable(self) -> None:
        """A credit note refuses a payment log."""
        sinv = SalesInvoiceFactory.create(rate=1000)
        self.addCleanup(SalesInvoiceFactory.cleanup, sinv)

        credit_note = CreditNoteFactory.create(return_against=sinv, rate=1000)
        self.addCleanup(CreditNoteFactory.cleanup, credit_note)

        with self.assertRaises(frappe.ValidationError):
            self.create_payment_log("Sales Invoice", credit_note)


class TestCancelledAndDraftDocuments(PayableStatusTestCase):
    """Cancelled and draft documents refuse payment."""

    def test_cancelled_invoice_is_refused(self) -> None:
        """A cancelled invoice (docstatus 2) rejects a payment log."""
        sinv = SalesInvoiceFactory.create(rate=1000)
        self.addCleanup(cleanup_doc, "Sales Invoice", sinv)

        doc = frappe.get_doc("Sales Invoice", sinv)
        doc.flags.ignore_permissions = True
        doc.cancel()

        with self.assertRaises(frappe.ValidationError) as ctx:
            self.create_payment_log("Sales Invoice", sinv)

        self.assertIn("cancelled or in draft", str(ctx.exception))

    def test_draft_invoice_is_refused(self) -> None:
        """A draft invoice (docstatus 0) rejects a payment log."""
        doc = frappe.get_doc(
            {
                "doctype": "Sales Invoice",
                "customer": "_Test Customer",
                "company": TEST_COMPANY,
                "due_date": frappe.utils.today(),
                "posting_date": frappe.utils.today(),
                # Matches the party account currency.
                "currency": frappe.db.get_value("Company", TEST_COMPANY, "default_currency"),
                "items": [{"item_code": "_Test Item Home Products 100", "qty": 1, "rate": 500}],
            }
        )
        doc.flags.ignore_permissions = True
        doc.flags.ignore_mandatory = True
        doc.insert()
        self.addCleanup(cleanup_doc, "Sales Invoice", doc.name)

        with self.assertRaises(frappe.ValidationError) as ctx:
            self.create_payment_log("Sales Invoice", doc.name)

        self.assertIn("cancelled or in draft", str(ctx.exception))

    def test_missing_document_is_refused(self) -> None:
        """A payment log against a nonexistent document is rejected."""
        with self.assertRaises(frappe.ValidationError) as ctx:
            self.create_payment_log("Sales Invoice", "NONEXISTENT-INV-999")

        self.assertIn("Document not found", str(ctx.exception))


class TestSalesOrderStatuses(PayableStatusTestCase):
    """Sales Order status guard."""

    def test_open_order_is_payable(self) -> None:
        """A submitted order awaiting delivery/billing accepts payment."""
        so = SalesOrderFactory.create(rate=1000)
        self.addCleanup(SalesOrderFactory.cleanup, so)

        status = frappe.db.get_value("Sales Order", so, "status")
        self.assertIn(status, PAYABLE_STATUSES)

        log_name = self.create_payment_log("Sales Order", so)
        self.assertTrue(frappe.db.exists("Paystack Payment Log", log_name))

    def test_completed_and_closed_orders_are_refused(self) -> None:
        """Completed and Closed orders are settled and reject payment."""
        so = SalesOrderFactory.create(rate=1000)
        self.addCleanup(SalesOrderFactory.cleanup, so)
        # Restores a cancellable status before the factory cleanup runs.
        self.addCleanup(self.force_status, "Sales Order", so, "To Deliver and Bill")

        for status in ("Completed", "Closed"):
            with self.subTest(status=status):
                self.force_status("Sales Order", so, status)
                with self.assertRaises(frappe.ValidationError):
                    self.create_payment_log("Sales Order", so)


class PaymentLinkAmountTestCase(PaystackTestCase):
    """A live Paystack gateway for the test company."""

    def setUp(self) -> None:
        """Enable Paystack so a link can be raised."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

    def log_of(self, url: str) -> Any:
        """Return the Paystack Payment Log a checkout URL points at."""
        name = url.rsplit("/", 1)[-1]
        self.addCleanup(PaymentLogFactory.cleanup, name)
        return frappe.get_doc(PAYMENT_LOG, name)


class TestBillableAmount(PaymentLinkAmountTestCase):
    """A Sales Invoice is billed through ERPNext's Payment Request."""

    def setUp(self) -> None:
        """Raise an unpaid invoice in a currency Paystack can charge."""
        super().setUp()
        self.invoice = ChargeableInvoiceFactory.create(rate=INVOICE_TOTAL, customer=CHARGE_CUSTOMER)
        self.addCleanup(ChargeableInvoiceFactory.cleanup, self.invoice)

    def billed_amount(self, url: str) -> float:
        """Return what the Payment Request behind a link asks for."""
        log = self.log_of(url)
        self.assertTrue(log.payment_request)
        return flt(frappe.db.get_value("Payment Request", log.payment_request, "grand_total"))

    def test_the_default_is_what_the_document_owes(self) -> None:
        """No amount means the whole outstanding."""
        url = create_payment_link("Sales Invoice", self.invoice)

        self.assertEqual(self.billed_amount(url), INVOICE_TOTAL)

    def test_a_partial_amount_is_respected(self) -> None:
        """An amount below the outstanding is billed as given."""
        url = create_payment_link("Sales Invoice", self.invoice, amount=400)

        self.assertEqual(self.billed_amount(url), 400.0)

    def test_an_amount_over_the_outstanding_is_clamped(self) -> None:
        """An amount above the outstanding is clamped to the outstanding."""
        url = create_payment_link("Sales Invoice", self.invoice, amount=999_999)

        self.assertEqual(self.billed_amount(url), INVOICE_TOTAL)

    def test_a_zero_amount_is_refused(self) -> None:
        """A zero amount raises a ValidationError naming the rule."""
        with self.assertRaises(frappe.ValidationError) as caught:
            create_payment_link("Sales Invoice", self.invoice, amount=0)

        self.assertIn("greater than zero", str(caught.exception))

    def test_a_negative_amount_is_refused(self) -> None:
        """A negative amount raises a ValidationError."""
        with self.assertRaises(frappe.ValidationError):
            create_payment_link("Sales Invoice", self.invoice, amount=-50)

    def test_a_settled_document_cannot_be_billed(self) -> None:
        """A document with nothing outstanding refuses a link."""
        with patch("frappe_paystack.api.get_payable_amount", return_value=0.0):
            with self.assertRaises(frappe.ValidationError) as caught:
                create_payment_link("Sales Invoice", self.invoice)

        self.assertIn("nothing left to pay", str(caught.exception))


class TestUnbillableAmount(PaymentLinkAmountTestCase):
    """A POS Invoice gets a Paystack Payment Log of its own, carrying the charged amount."""

    def setUp(self) -> None:
        """Insert a draft POS Invoice to collect against."""
        super().setUp()
        self.invoice = POSInvoiceFactory.create(amount=INVOICE_TOTAL)
        self.addCleanup(POSInvoiceFactory.cleanup, self.invoice)

    def test_the_default_is_what_the_document_owes(self) -> None:
        """No amount means the whole total."""
        url = create_payment_link(POS_INVOICE, self.invoice)

        self.assertEqual(flt(self.log_of(url).amount), INVOICE_TOTAL)

    def test_a_partial_amount_is_respected(self) -> None:
        """A part payment writes what was asked for."""
        url = create_payment_link(POS_INVOICE, self.invoice, amount=250)

        self.assertEqual(flt(self.log_of(url).amount), 250.0)

    def test_an_amount_over_the_total_is_clamped(self) -> None:
        """An amount above the total is clamped to the total."""
        url = create_payment_link(POS_INVOICE, self.invoice, amount=999_999)

        self.assertEqual(flt(self.log_of(url).amount), INVOICE_TOTAL)

    def test_a_zero_amount_is_refused(self) -> None:
        """A zero amount raises a ValidationError."""
        with self.assertRaises(frappe.ValidationError):
            create_payment_link(POS_INVOICE, self.invoice, amount=0)

    def test_an_unsupported_currency_is_refused(self) -> None:
        """A currency Paystack does not support raises a ValidationError."""
        with self.assertRaises(frappe.ValidationError):
            create_payment_link(POS_INVOICE, self.invoice, currency="EUR")
