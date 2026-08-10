"""Paystack payments against foreign-currency documents: an NGN company billing in USD."""

from unittest.mock import MagicMock, patch

import frappe
from frappe.utils import flt

from frappe_paystack.api import get_payable_amount, initiate_refund_from_log
from frappe_paystack.frappe_paystack.doctype.paystack_payment_log.paystack_payment_log import (
    create_payment_entry_from_log,
)
from frappe_paystack.tests.factories import (
    CreditNoteFactory,
    CurrencyExchangeFactory,
    CustomerFactory,
    GatewaySettingFactory,
    PaymentLogFactory,
    RefundLogFactory,
    SalesInvoiceFactory,
    SalesOrderFactory,
    cleanup_doc,
)
from frappe_paystack.tests.test_base import PaystackTestCase
from frappe_paystack.utils import outstanding_rate, party_account_for, party_account_rate

from erpnext.setup.utils import get_exchange_rate

TEST_COMPANY = "_Test Company"
COMPANY_CURRENCY = "NGN"
FOREIGN_CURRENCY = "USD"
USD_RECEIVABLE = "_Test Receivable USD - _TC"
CONVERSION_RATE = 60.0

# The currency amount_paid and every refund amount are held in.
CHARGE_CURRENCY = "NGN"

# The customer the dollar invoices bill.
FOREIGN_CUSTOMER = "_Test Paystack FX Customer"

# The customer kept on the company's default receivable.
BASE_RECEIVABLE_CUSTOMER = "_Test Paystack Base Receivable Customer"

ACCOUNTS_SETTINGS = "Accounts Settings"

# Whether ERPNext lets one party account carry invoices in several currencies.
SINGLE_PARTY_ACCOUNT_FIELD = "allow_multi_currency_invoices_against_single_party_account"


def restore_single_party_account_setting(value) -> None:
    """Put the Accounts Settings multi-currency party account flag back."""
    frappe.db.set_single_value(ACCOUNTS_SETTINGS, SINGLE_PARTY_ACCOUNT_FIELD, value)
    frappe.db.commit()


class MultiCurrencyTestCase(PaystackTestCase):
    """Shared setup for foreign-currency payments."""

    auto_refund = False

    def setUp(self) -> None:
        """Enable Paystack for the test company and post the rate the fixtures convert at."""
        super().setUp()
        # Runs last, once every invoice is gone.
        self.addCleanup(CustomerFactory.cleanup, BASE_RECEIVABLE_CUSTOMER)
        self.addCleanup(CustomerFactory.cleanup, FOREIGN_CUSTOMER)

        # Outranks every USD rate the site already carries.
        self.exchange_rate_name = CurrencyExchangeFactory.create(
            FOREIGN_CURRENCY, COMPANY_CURRENCY, CONVERSION_RATE
        )
        self.addCleanup(CurrencyExchangeFactory.cleanup, self.exchange_rate_name)

        self.gateway_name = GatewaySettingFactory.create(auto_refund=self.auto_refund)
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

    def make_usd_invoice(self, rate: float = 100) -> str:
        """Raise a submitted USD invoice against an NGN company."""
        invoice = SalesInvoiceFactory.create(
            rate=rate,
            customer=CustomerFactory.create(customer_name=FOREIGN_CUSTOMER),
            currency=FOREIGN_CURRENCY,
            conversion_rate=CONVERSION_RATE,
            debit_to=USD_RECEIVABLE,
        )
        self.addCleanup(SalesInvoiceFactory.cleanup, invoice)
        return invoice

    def pay(self, invoice: str, document_amount: float) -> str:
        """Create a payment log for an invoice and mark it paid.

        document_amount is in the invoice's currency; amount_paid carries the converted figure.
        """
        log_name = PaymentLogFactory.create(
            linked_docname=invoice,
            amount=frappe.db.get_value("Sales Invoice", invoice, "grand_total"),
            currency=FOREIGN_CURRENCY,
        )
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        frappe.db.set_value(
            "Paystack Payment Log",
            log_name,
            {
                "status": "Processed",
                "amount_paid": document_amount * CONVERSION_RATE,
                "currency_paid": CHARGE_CURRENCY,
                "payment_reference": f"ref-{log_name}",
                "payment_date": frappe.utils.today(),
            },
        )
        frappe.clear_document_cache("Paystack Payment Log", log_name)
        return log_name

    def payment_entry_of(self, log_name: str) -> object:
        """Return the Payment Entry created for a log."""
        pe_name = frappe.db.get_value("Paystack Payment Log", log_name, "payment_entry")
        self.assertTrue(pe_name, "no Payment Entry was created")
        self.addCleanup(cleanup_doc, "Payment Entry", pe_name)
        return frappe.get_doc("Payment Entry", pe_name)


class TestForeignCurrencyInvoice(MultiCurrencyTestCase):
    """A USD invoice raised against an NGN company."""

    def test_invoice_is_raised_in_foreign_currency(self) -> None:
        """The fixture invoice is raised in USD at the conversion rate."""
        invoice = self.make_usd_invoice(rate=100)
        doc = frappe.get_doc("Sales Invoice", invoice)

        self.assertEqual(doc.currency, FOREIGN_CURRENCY)
        self.assertNotEqual(doc.currency, COMPANY_CURRENCY)
        self.assertAlmostEqual(flt(doc.conversion_rate), CONVERSION_RATE, places=2)
        # grand_total is in USD; base_grand_total in NGN.
        self.assertAlmostEqual(flt(doc.grand_total), 100.0, places=2)
        self.assertAlmostEqual(flt(doc.base_grand_total), 6000.0, places=2)

    def test_payable_amount_is_in_document_currency(self) -> None:
        """The charged amount is the document's own outstanding."""
        invoice = self.make_usd_invoice(rate=100)
        doc = frappe.get_doc("Sales Invoice", invoice)

        self.assertAlmostEqual(get_payable_amount(doc), 100.0, places=2)


class TestAmbientExchangeRate(MultiCurrencyTestCase):
    """The site-wide rate ERPNext converts these fixtures by."""

    def test_the_site_rate_is_the_rate_the_fixtures_are_raised_at(self) -> None:
        """Every rate ERPNext reads for USD is the rate the invoices carry."""
        for purpose in ("for_selling", "for_buying", None):
            self.assertAlmostEqual(
                flt(get_exchange_rate(FOREIGN_CURRENCY, COMPANY_CURRENCY, frappe.utils.today(), purpose)),
                CONVERSION_RATE,
                places=4,
            )


class TestForeignCurrencyPaymentEntry(MultiCurrencyTestCase):
    """Payment Entry creation for a foreign-currency invoice."""

    def test_exchange_rate_follows_the_invoice(self) -> None:
        """The Payment Entry settles at the rate the invoice was raised at."""
        invoice = self.make_usd_invoice(rate=100)
        log_name = self.pay(invoice, 100)

        create_payment_entry_from_log(log_name)
        pe = self.payment_entry_of(log_name)

        self.assertAlmostEqual(flt(pe.source_exchange_rate), CONVERSION_RATE, places=2)

    def test_base_amount_uses_the_exchange_rate(self) -> None:
        """Base (company currency) amounts are the converted figures."""
        invoice = self.make_usd_invoice(rate=100)
        log_name = self.pay(invoice, 100)

        create_payment_entry_from_log(log_name)
        pe = self.payment_entry_of(log_name)

        self.assertAlmostEqual(flt(pe.paid_amount), 100.0, places=2)
        self.assertAlmostEqual(flt(pe.base_paid_amount), 6000.0, places=2)

    def test_invoice_is_settled(self) -> None:
        """A full foreign-currency payment clears the outstanding."""
        invoice = self.make_usd_invoice(rate=100)
        log_name = self.pay(invoice, 100)

        create_payment_entry_from_log(log_name)
        self.payment_entry_of(log_name)

        outstanding = frappe.db.get_value("Sales Invoice", invoice, "outstanding_amount")
        self.assertAlmostEqual(flt(outstanding), 0.0, places=2)

    def test_partial_foreign_payment_allocates_correctly(self) -> None:
        """A partial payment allocates in the document's currency."""
        invoice = self.make_usd_invoice(rate=100)
        log_name = self.pay(invoice, 40)

        create_payment_entry_from_log(log_name)
        pe = self.payment_entry_of(log_name)

        self.assertAlmostEqual(flt(pe.references[0].allocated_amount), 40.0, places=2)
        outstanding = frappe.db.get_value("Sales Invoice", invoice, "outstanding_amount")
        self.assertAlmostEqual(flt(outstanding), 60.0, places=2)

    def test_no_phantom_exchange_difference(self) -> None:
        """Settling at the invoice rate books no exchange gain or loss."""
        invoice = self.make_usd_invoice(rate=100)
        log_name = self.pay(invoice, 100)

        create_payment_entry_from_log(log_name)
        pe = self.payment_entry_of(log_name)

        difference = sum(flt(row.amount) for row in pe.deductions)
        self.assertAlmostEqual(difference, 0.0, places=2)

        self.assertFalse(
            frappe.db.exists(
                "Journal Entry",
                {
                    "voucher_type": "Exchange Gain Or Loss",
                    "docstatus": 1,
                    "cheque_no": pe.name,
                },
            )
        )

    def test_gl_entries_balance_in_company_currency(self) -> None:
        """The posted GL is balanced in NGN."""
        invoice = self.make_usd_invoice(rate=100)
        log_name = self.pay(invoice, 100)

        create_payment_entry_from_log(log_name)
        pe = self.payment_entry_of(log_name)

        rows = frappe.get_all(
            "GL Entry",
            filters={"voucher_no": pe.name, "is_cancelled": 0},
            fields=["debit", "credit"],
        )
        self.assertTrue(rows)
        self.assertAlmostEqual(
            sum(flt(r.debit) for r in rows),
            sum(flt(r.credit) for r in rows),
            places=2,
        )


class ForeignCurrencyRefundTestCase(MultiCurrencyTestCase):
    """A settled USD invoice that money can be returned against."""

    def setUp(self) -> None:
        """Pay a USD invoice in full so it can be refunded."""
        super().setUp()
        self.suspense = self.suspense_account()
        self.invoice = self.make_usd_invoice(rate=100)
        self.receivable = frappe.db.get_value("Sales Invoice", self.invoice, "debit_to")

        self.log_name = self.pay(self.invoice, 100)
        create_payment_entry_from_log(self.log_name)
        self.payment_entry_of(self.log_name)
        self.addCleanup(self.cleanup_refund_logs)

    def cleanup_refund_logs(self) -> None:
        """Remove every Refund Log raised against the fixture payment."""
        for name in frappe.get_all(
            "Paystack Refund Log", filters={"payment_log": self.log_name}, pluck="name"
        ):
            RefundLogFactory.cleanup(name)

    def refund_log_of(self, refund_name: str) -> object:
        """Return a Refund Log with its reversal Payment Entry tracked."""
        refund = frappe.get_doc("Paystack Refund Log", refund_name)
        self.assertTrue(refund.reversal_payment_entry, "no reversal Payment Entry was booked")
        return refund

    def assert_reversal_ledger(self, reversal: str, base_amount: float) -> None:
        """Assert the reversal debits the receivable and credits suspense in NGN."""
        rows = self.gl_entries_by_account(reversal)

        self.assertIn(self.receivable, rows)
        self.assertIn(self.suspense, rows)
        self.assertAlmostEqual(rows[self.receivable]["debit"], base_amount, places=2)
        self.assertAlmostEqual(rows[self.receivable]["credit"], 0.0, places=2)
        self.assertAlmostEqual(rows[self.suspense]["credit"], base_amount, places=2)


class TestForeignCurrencyManualRefund(ForeignCurrencyRefundTestCase):
    """A desk refund of a foreign-currency payment, offered in the charge currency."""

    def initiate(self, amount: float) -> str:
        """Refund an amount through the desk endpoint with Paystack stubbed."""
        response = MagicMock()
        response.ok = True
        response.json.return_value = {
            "status": True,
            "data": {
                "status": "processed",
                "reference": "rfd_fx_001",
                "amount": int(amount * 100),
                "currency": CHARGE_CURRENCY,
            },
        }

        with patch("frappe_paystack.utils.utils.requests.post", return_value=response):
            return initiate_refund_from_log(self.log_name, amount)

    def test_refund_log_records_the_charge_currency(self) -> None:
        """The Refund Log records the charge currency."""
        refund = self.refund_log_of(self.initiate(6000.0))

        self.assertEqual(refund.currency, CHARGE_CURRENCY)

    def test_reversal_is_booked_in_the_invoice_currency(self) -> None:
        """6000 NGN came in; 100 USD is what leaves the receivable."""
        refund = self.refund_log_of(self.initiate(6000.0))
        pe = frappe.get_doc("Payment Entry", refund.reversal_payment_entry)

        self.assertAlmostEqual(flt(pe.received_amount), 100.0, places=2)
        self.assertAlmostEqual(flt(pe.paid_amount), 6000.0, places=2)

    def test_full_refund_reverses_the_whole_receivable(self) -> None:
        """A full refund reverses the whole receivable."""
        refund = self.refund_log_of(self.initiate(6000.0))

        self.assert_reversal_ledger(refund.reversal_payment_entry, 6000.0)

    def test_partial_refund_converts_at_the_invoice_rate(self) -> None:
        """Half the captured charge reverses half the receivable."""
        refund = self.refund_log_of(self.initiate(3000.0))
        pe = frappe.get_doc("Payment Entry", refund.reversal_payment_entry)

        self.assertAlmostEqual(flt(pe.received_amount), 50.0, places=2)
        self.assert_reversal_ledger(refund.reversal_payment_entry, 3000.0)


class TestForeignCurrencyAutoRefund(ForeignCurrencyRefundTestCase):
    """A credit note against a foreign-currency invoice refunds automatically."""

    auto_refund = True

    def credit_note(self, rate: float = 100) -> tuple:
        """Submit a USD credit note with the Paystack call stubbed."""
        with patch(
            "frappe_paystack.events.initiate_refund",
            return_value={
                "status": "processed",
                "reference": "rfd_fx_auto",
                "amount": rate * CONVERSION_RATE,
                "currency": CHARGE_CURRENCY,
                "raw": {"status": True},
            },
        ) as refund_call:
            note = CreditNoteFactory.create(
                return_against=self.invoice,
                rate=rate,
                customer=FOREIGN_CUSTOMER,
                currency=FOREIGN_CURRENCY,
                conversion_rate=CONVERSION_RATE,
                debit_to=USD_RECEIVABLE,
            )
        self.addCleanup(CreditNoteFactory.cleanup, note)
        return note, refund_call

    def only_refund(self) -> object:
        """Return the single Refund Log raised against the fixture payment."""
        names = frappe.get_all("Paystack Refund Log", filters={"payment_log": self.log_name}, pluck="name")
        self.assertEqual(len(names), 1)
        return self.refund_log_of(names[0])

    def test_paystack_is_asked_for_the_charge_currency_amount(self) -> None:
        """A 100 USD credit note asks Paystack to return 6000 NGN."""
        _note, refund_call = self.credit_note(rate=100)

        self.assertAlmostEqual(flt(refund_call.call_args.kwargs["amount"]), 6000.0, places=2)
        self.assertEqual(refund_call.call_args.kwargs["currency"], CHARGE_CURRENCY)

    def test_refund_log_holds_the_charge_currency_amount(self) -> None:
        """The stored amount is what Paystack was told to return."""
        self.credit_note(rate=100)

        refund = self.only_refund()
        self.assertAlmostEqual(flt(refund.refund_amount), 6000.0, places=2)
        self.assertEqual(refund.currency, CHARGE_CURRENCY)

    def test_full_credit_note_marks_the_payment_refunded(self) -> None:
        """Refunding the whole capture marks the payment Refunded."""
        self.credit_note(rate=100)
        self.only_refund()

        self.assertEqual(
            frappe.db.get_value("Paystack Payment Log", self.log_name, "status"),
            "Refunded",
        )

    def test_reversal_clears_the_credit_note(self) -> None:
        """The reversal is allocated to the credit note it was raised for."""
        note, _call = self.credit_note(rate=100)

        refund = self.only_refund()
        self.assert_reversal_ledger(refund.reversal_payment_entry, 6000.0)
        self.assertAlmostEqual(
            flt(frappe.db.get_value("Sales Invoice", note, "outstanding_amount")),
            0.0,
            places=2,
        )


class CompanyCurrencyReceivableTestCase(MultiCurrencyTestCase):
    """A USD invoice posted against a receivable kept in the company currency.

    outstanding_amount is then a rupee figure, and amount_paid is allocated against it untouched.
    """

    def setUp(self) -> None:
        """Let the company's own receivable carry a foreign-currency invoice."""
        super().setUp()
        self.addCleanup(
            restore_single_party_account_setting,
            frappe.db.get_single_value(ACCOUNTS_SETTINGS, SINGLE_PARTY_ACCOUNT_FIELD),
        )
        frappe.db.set_single_value(ACCOUNTS_SETTINGS, SINGLE_PARTY_ACCOUNT_FIELD, 1)

    def make_invoice(self, rate: float = 100) -> str:
        """Raise a USD invoice on the company's own default receivable."""
        invoice = SalesInvoiceFactory.create(
            rate=rate,
            customer=CustomerFactory.create(customer_name=BASE_RECEIVABLE_CUSTOMER),
            currency=FOREIGN_CURRENCY,
            conversion_rate=CONVERSION_RATE,
        )
        self.addCleanup(SalesInvoiceFactory.cleanup, invoice)
        return invoice


class TestCompanyCurrencyReceivable(CompanyCurrencyReceivableTestCase):
    """Settling a foreign invoice whose receivable is in the company currency."""

    def test_the_receivable_is_kept_in_the_company_currency(self) -> None:
        """The fixture invoice is in USD while its receivable is in NGN."""
        doc = frappe.get_doc("Sales Invoice", self.make_invoice())

        self.assertEqual(doc.currency, FOREIGN_CURRENCY)
        self.assertEqual(
            frappe.get_cached_value("Account", doc.debit_to, "account_currency"),
            COMPANY_CURRENCY,
        )
        # outstanding_amount is held in the party account's currency.
        self.assertAlmostEqual(flt(doc.grand_total), 100.0, places=2)
        self.assertAlmostEqual(flt(doc.outstanding_amount), 6000.0, places=2)

    def test_the_allocation_is_the_whole_receivable(self) -> None:
        """6000 NGN captured is allocated as 6000 NGN of receivable."""
        invoice = self.make_invoice()
        log_name = self.pay(invoice, 100)

        create_payment_entry_from_log(log_name)
        pe = self.payment_entry_of(log_name)

        self.assertAlmostEqual(flt(pe.references[0].allocated_amount), 6000.0, places=2)

    def test_the_invoice_is_settled_in_full(self) -> None:
        """Nothing is left outstanding after the full charge is booked."""
        invoice = self.make_invoice()
        log_name = self.pay(invoice, 100)

        create_payment_entry_from_log(log_name)
        self.payment_entry_of(log_name)

        outstanding = frappe.db.get_value("Sales Invoice", invoice, "outstanding_amount")
        self.assertAlmostEqual(flt(outstanding), 0.0, places=2)

    def test_the_receivable_is_not_revalued(self) -> None:
        """A company-currency receivable settles at rate 1, with no deduction."""
        invoice = self.make_invoice()
        log_name = self.pay(invoice, 100)

        create_payment_entry_from_log(log_name)
        pe = self.payment_entry_of(log_name)

        self.assertAlmostEqual(flt(pe.source_exchange_rate), 1.0, places=2)
        self.assertAlmostEqual(flt(pe.paid_amount), 6000.0, places=2)
        self.assertAlmostEqual(flt(pe.base_paid_amount), 6000.0, places=2)
        self.assertAlmostEqual(sum(flt(row.amount) for row in pe.deductions), 0.0, places=2)

    def test_a_partial_charge_allocates_what_arrived(self) -> None:
        """Half the charge clears half the rupee receivable."""
        invoice = self.make_invoice()
        log_name = self.pay(invoice, 50)

        create_payment_entry_from_log(log_name)
        pe = self.payment_entry_of(log_name)

        self.assertAlmostEqual(flt(pe.references[0].allocated_amount), 3000.0, places=2)
        outstanding = frappe.db.get_value("Sales Invoice", invoice, "outstanding_amount")
        self.assertAlmostEqual(flt(outstanding), 3000.0, places=2)


class TestPartyAccountRate(CompanyCurrencyReceivableTestCase):
    """The one rule both the payment and the refund path convert by."""

    def test_a_company_currency_receivable_needs_no_conversion(self) -> None:
        """amount_paid is already in the company currency."""
        doc = frappe.get_doc("Sales Invoice", self.make_invoice())

        self.assertEqual(party_account_for(doc), doc.debit_to)
        self.assertAlmostEqual(party_account_rate(doc, doc.debit_to), 1.0, places=4)

    def test_a_foreign_receivable_converts_at_the_documents_rate(self) -> None:
        """A non-company party account forces the document into its currency."""
        doc = frappe.get_doc("Sales Invoice", self.make_usd_invoice(rate=100))

        self.assertEqual(party_account_for(doc), USD_RECEIVABLE)
        self.assertAlmostEqual(party_account_rate(doc, USD_RECEIVABLE), CONVERSION_RATE, places=4)

    def test_a_missing_rate_falls_back_to_one(self) -> None:
        """A document carrying no rate converts at 1."""
        doc = frappe.get_doc("Sales Invoice", self.make_usd_invoice(rate=100))
        doc.conversion_rate = 0

        self.assertAlmostEqual(party_account_rate(doc, USD_RECEIVABLE), 1.0, places=4)

    def test_a_sales_order_resolves_through_the_customer(self) -> None:
        """A Sales Order resolves its party account through the customer."""
        order = SalesOrderFactory.create(
            customer=CustomerFactory.create(customer_name=BASE_RECEIVABLE_CUSTOMER)
        )
        self.addCleanup(SalesOrderFactory.cleanup, order)
        doc = frappe.get_doc("Sales Order", order)

        account = party_account_for(doc)
        self.assertEqual(
            frappe.get_cached_value("Account", account, "account_currency"),
            COMPANY_CURRENCY,
        )
        self.assertAlmostEqual(party_account_rate(doc, account), 1.0, places=4)


class TestOutstandingRate(CompanyCurrencyReceivableTestCase):
    """What outstanding_amount divides by to reach the document currency."""

    def test_a_single_currency_document_needs_no_party_account(self) -> None:
        """A document billed at par is already in one currency."""
        self.assertAlmostEqual(outstanding_rate(frappe._dict(conversion_rate=1)), 1.0, places=4)

    def test_a_document_without_a_rate_needs_no_party_account(self) -> None:
        """A document carrying no rate divides the outstanding by 1."""
        self.assertAlmostEqual(outstanding_rate(frappe._dict(conversion_rate=0)), 1.0, places=4)

    def test_a_receivable_in_the_document_currency_needs_no_conversion(self) -> None:
        """A dollar receivable already holds the dollar figure."""
        doc = frappe.get_doc("Sales Invoice", self.make_usd_invoice(rate=100))

        self.assertAlmostEqual(outstanding_rate(doc), 1.0, places=4)

    def test_a_company_currency_receivable_converts_at_the_documents_rate(self) -> None:
        """A rupee receivable on a dollar invoice holds the converted figure."""
        doc = frappe.get_doc("Sales Invoice", self.make_invoice())

        self.assertAlmostEqual(outstanding_rate(doc), CONVERSION_RATE, places=4)


class TestPayableAmountConversion(CompanyCurrencyReceivableTestCase):
    """What a document owes, answered in the currency it is billed in."""

    def test_a_company_currency_receivable_is_not_over_reported(self) -> None:
        """The receivable holds 6000 rupees; the invoice owes 100 dollars."""
        doc = frappe.get_doc("Sales Invoice", self.make_invoice())

        self.assertAlmostEqual(flt(doc.outstanding_amount), 6000.0, places=2)
        self.assertAlmostEqual(get_payable_amount(doc), 100.0, places=2)

    def test_a_receivable_in_the_document_currency_is_reported_as_is(self) -> None:
        """A dollar receivable is reported as it stands."""
        doc = frappe.get_doc("Sales Invoice", self.make_usd_invoice(rate=100))

        self.assertAlmostEqual(get_payable_amount(doc), 100.0, places=2)
