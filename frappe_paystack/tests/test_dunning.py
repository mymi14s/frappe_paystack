"""Paystack payment links and settlement against a Dunning."""

import frappe
from frappe.utils import flt

from frappe_paystack.api import create_payment_link, get_payable_amount
from frappe_paystack.frappe_paystack.doctype.paystack_payment_log.paystack_payment_log import (
    create_payment_entry_from_log,
)
from frappe_paystack.tests.factories import (
    CustomerFactory,
    DunningFactory,
    GatewaySettingFactory,
    PaymentLogFactory,
    SalesInvoiceFactory,
    cleanup_doc,
)
from frappe_paystack.tests.test_base import PaystackTestCase

TEST_COMPANY = "_Test Company"
PAYMENT_LOG = "Paystack Payment Log"

DUNNING_CUSTOMER = "_Test Paystack Dunning Customer"

# 10% a year on 100 for 15 days, plus a 10 fee.
INVOICE_RATE = 100.0
OVERDUE_DAYS = 15
DUNNING_FEE = 10.0
RATE_OF_INTEREST = 10.0


class DunningTestCase(PaystackTestCase):
    """An overdue invoice with a Dunning raised against it."""

    def setUp(self) -> None:
        """Enable Paystack and raise a Dunning on an overdue invoice."""
        super().setUp()
        # Registered first, so it runs last.
        self.addCleanup(CustomerFactory.cleanup, DUNNING_CUSTOMER)

        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

        self.invoice = SalesInvoiceFactory.create(
            rate=INVOICE_RATE,
            customer=CustomerFactory.create(customer_name=DUNNING_CUSTOMER),
            overdue_days=OVERDUE_DAYS,
        )
        self.addCleanup(SalesInvoiceFactory.cleanup, self.invoice)

        self.dunning = DunningFactory.create(
            self.invoice,
            dunning_fee=DUNNING_FEE,
            rate_of_interest=RATE_OF_INTEREST,
        )
        self.addCleanup(DunningFactory.cleanup, self.dunning)

    def dunning_doc(self) -> object:
        """Return the Dunning under test."""
        return frappe.get_doc("Dunning", self.dunning)

    def pay(self, amount: float) -> str:
        """Raise a captured payment log against the Dunning."""
        log_name = PaymentLogFactory.create(
            linked_doctype="Dunning",
            linked_docname=self.dunning,
            amount=amount,
            currency="INR",
        )
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        frappe.db.set_value(
            PAYMENT_LOG,
            log_name,
            {
                "status": "Processed",
                "amount_paid": amount,
                "currency_paid": "INR",
                "payment_reference": f"ref-{log_name}",
                "payment_date": frappe.utils.today(),
            },
        )
        frappe.clear_document_cache(PAYMENT_LOG, log_name)
        return log_name

    def payment_entry_of(self, log_name: str) -> object:
        """Return the Payment Entry created for a log."""
        pe_name = frappe.db.get_value(PAYMENT_LOG, log_name, "payment_entry")
        self.assertTrue(pe_name, "no Payment Entry was created")
        self.addCleanup(cleanup_doc, "Payment Entry", pe_name)
        return frappe.get_doc("Payment Entry", pe_name)


class TestDunningFixture(DunningTestCase):
    """The Dunning carries interest on an overdue invoice."""

    def test_the_dunning_bills_interest_and_the_fee(self) -> None:
        """grand_total is the overdue balance plus what the dunning adds."""
        doc = self.dunning_doc()

        self.assertAlmostEqual(flt(doc.total_outstanding), INVOICE_RATE, places=2)
        self.assertGreater(flt(doc.total_interest), 0.0)
        self.assertAlmostEqual(flt(doc.dunning_fee), DUNNING_FEE, places=2)
        self.assertAlmostEqual(
            flt(doc.dunning_amount),
            flt(doc.total_interest) + DUNNING_FEE,
            places=2,
        )
        self.assertAlmostEqual(
            flt(doc.grand_total),
            flt(doc.total_outstanding) + flt(doc.dunning_amount),
            places=2,
        )

    def test_an_unresolved_dunning_is_payable(self) -> None:
        """The fixture Dunning is submitted and Unresolved."""
        doc = self.dunning_doc()

        self.assertEqual(doc.docstatus, 1)
        self.assertEqual(doc.status, "Unresolved")

    def test_the_payable_amount_is_the_whole_dunning(self) -> None:
        """get_payable_amount returns the Dunning's grand_total."""
        doc = self.dunning_doc()

        self.assertAlmostEqual(get_payable_amount(doc), flt(doc.grand_total), places=2)


class TestDunningPaymentLink(DunningTestCase):
    """Requesting a Paystack checkout link for a Dunning."""

    def link_log(self) -> object:
        """Return the Payment Log a link request raised for the Dunning."""
        url = create_payment_link("Dunning", self.dunning)
        self.assertIn("/paystack-checkout/", url)

        log_name = url.rsplit("/", 1)[-1]
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        return frappe.get_doc(PAYMENT_LOG, log_name)

    def test_the_link_bills_the_dunning_grand_total(self) -> None:
        """Interest and the fee are collected with the overdue balance."""
        log = self.link_log()

        self.assertAlmostEqual(flt(log.amount), flt(self.dunning_doc().grand_total), places=2)

    def test_the_log_points_back_at_the_dunning(self) -> None:
        """The log links back to the Dunning and starts Pending."""
        log = self.link_log()

        self.assertEqual(log.linked_doctype, "Dunning")
        self.assertEqual(log.linked_docname, self.dunning)
        self.assertEqual(log.status, "Pending")

    def test_no_payment_request_is_raised(self) -> None:
        """A Dunning link raises no Payment Request."""
        log = self.link_log()

        self.assertFalse(log.payment_request)
        self.assertFalse(
            frappe.db.exists(
                "Payment Request",
                {"reference_doctype": "Dunning", "reference_name": self.dunning},
            )
        )

    def test_the_checkout_page_prices_the_dunning(self) -> None:
        """The public page charges the dunning total at the document's rate."""
        data = self.link_log().get_data()

        self.assertEqual(data["reference_doctype"], "Dunning")
        self.assertTrue(data["is_payable"])
        self.assertAlmostEqual(
            flt(data["payment_amount"]),
            flt(self.dunning_doc().grand_total),
            places=2,
        )


class TestDunningSettlement(DunningTestCase):
    """Paying a Dunning books the invoice and the interest."""

    def settle(self) -> tuple:
        """Pay the whole dunning and return its log and Payment Entry."""
        doc = self.dunning_doc()
        log_name = self.pay(flt(doc.grand_total))

        create_payment_entry_from_log(log_name)
        return log_name, self.payment_entry_of(log_name)

    def test_the_payment_entry_references_the_overdue_invoice(self) -> None:
        """ERPNext appends one reference row per overdue payment."""
        _log_name, pe = self.settle()

        rows = [
            row
            for row in pe.references
            if row.reference_doctype == "Sales Invoice" and row.reference_name == self.invoice
        ]
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(flt(rows[0].allocated_amount), INVOICE_RATE, places=2)

    def test_the_interest_lands_in_a_deduction_row(self) -> None:
        """The dunning amount lands in a deduction row on the income account."""
        doc = self.dunning_doc()
        _log_name, pe = self.settle()

        self.assertEqual(len(pe.deductions), 1)
        deduction = pe.deductions[0]
        self.assertEqual(deduction.account, doc.income_account)
        self.assertAlmostEqual(flt(deduction.amount), -flt(doc.dunning_amount), places=2)

    def test_the_whole_dunning_is_collected(self) -> None:
        """The receipt is the overdue balance plus interest and the fee."""
        doc = self.dunning_doc()
        _log_name, pe = self.settle()

        self.assertEqual(pe.payment_type, "Receive")
        self.assertAlmostEqual(flt(pe.paid_amount), flt(doc.grand_total), places=2)
        self.assertAlmostEqual(flt(pe.difference_amount), 0.0, places=2)

    def test_the_overdue_invoice_is_cleared(self) -> None:
        """The reference row settles the invoice the dunning was raised for."""
        self.settle()

        outstanding = frappe.db.get_value("Sales Invoice", self.invoice, "outstanding_amount")
        self.assertAlmostEqual(flt(outstanding), 0.0, places=2)

    def test_the_dunning_is_resolved(self) -> None:
        """The dunning reads Resolved once its invoice is cleared."""
        self.settle()

        self.assertEqual(frappe.db.get_value("Dunning", self.dunning, "status"), "Resolved")

    def test_the_log_records_what_was_booked(self) -> None:
        """The log carries the Payment Entry and reads Completed."""
        log_name, pe = self.settle()

        self.assertEqual(frappe.db.get_value(PAYMENT_LOG, log_name, "payment_entry"), pe.name)
        self.assertEqual(frappe.db.get_value(PAYMENT_LOG, log_name, "status"), "Completed")

    def test_the_gl_is_balanced(self) -> None:
        """The GL entries balance and credit the dunning amount to income."""
        doc = self.dunning_doc()
        _log_name, pe = self.settle()

        rows = frappe.get_all(
            "GL Entry",
            filters={"voucher_no": pe.name, "is_cancelled": 0},
            fields=["account", "debit", "credit"],
        )
        self.assertTrue(rows)
        self.assertAlmostEqual(
            sum(flt(row.debit) for row in rows),
            sum(flt(row.credit) for row in rows),
            places=2,
        )
        income = [row for row in rows if row.account == doc.income_account]
        self.assertEqual(len(income), 1)
        self.assertAlmostEqual(flt(income[0].credit), flt(doc.dunning_amount), places=2)

    def test_a_resolved_dunning_takes_no_further_link(self) -> None:
        """A resolved Dunning refuses a new payment log."""
        self.settle()

        with self.assertRaises(frappe.ValidationError):
            PaymentLogFactory.create(
                linked_doctype="Dunning",
                linked_docname=self.dunning,
                amount=10,
                currency="INR",
            )
