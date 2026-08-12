"""What the customer portal offers while a Paystack payment is settling."""

import frappe

from frappe_paystack.tests.factories import (
    CustomerFactory,
    DunningFactory,
    GatewaySettingFactory,
    PaymentLogFactory,
    POSInvoiceFactory,
    SalesInvoiceFactory,
    SalesOrderFactory,
    cleanup_doc,
)
from frappe_paystack.tests.test_base import PaystackTestCase
from frappe_paystack.utils.portal import (
    PAYMENT_STATE_OPEN,
    PAYMENT_STATE_PROCESSING,
    PAYMENT_STATE_SETTLED,
    apply_payment_state,
    owns_payment_log,
    payment_state,
)

PAYMENT_LOG = "Paystack Payment Log"
SALES_INVOICE = "Sales Invoice"
SALES_ORDER = "Sales Order"
POS_INVOICE = "POS Invoice"
DUNNING = "Dunning"

# The hook Frappe calls once a website page has resolved its document.
CONTEXT_HOOK = "frappe_paystack.utils.portal.apply_payment_state"

STATE_CUSTOMER = "_Test Customer Paystack State"
STATE_USER = "portal-state-paystack@example.com"

# The party a Dunning is raised for.
DUNNING_CUSTOMER = "_Test Customer Paystack State Dunning"


def stamp(log: str, status: str, amount_paid: float = 1000) -> None:
    """Put a payment log into a captured state by writing straight to the row."""
    frappe.db.set_value(
        PAYMENT_LOG,
        log,
        {"status": status, "amount_paid": amount_paid, "currency_paid": "NGN"},
    )
    frappe.clear_document_cache(PAYMENT_LOG, log)


class PortalStateTestCase(PaystackTestCase):
    """Documents and logs the portal state is read from."""

    def setUp(self) -> None:
        """Enable Paystack for the test company."""
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

    def invoice(self, rate: float = 1000, customer: str = "_Test Customer") -> str:
        """Raise a submitted, unpaid Sales Invoice."""
        name = SalesInvoiceFactory.create(rate=rate, customer=customer)
        self.addCleanup(SalesInvoiceFactory.cleanup, name)
        return name

    def order(self, rate: float = 1000, customer: str = "_Test Customer") -> str:
        """Raise a submitted Sales Order."""
        name = SalesOrderFactory.create(rate=rate, customer=customer)
        self.addCleanup(SalesOrderFactory.cleanup, name)
        return name

    def log_for(
        self,
        docname: str,
        doctype: str = SALES_INVOICE,
        amount: float = 1000,
    ) -> str:
        """Raise an open payment log against a document."""
        name = PaymentLogFactory.create(linked_doctype=doctype, linked_docname=docname, amount=amount)
        self.addCleanup(PaymentLogFactory.cleanup, name)
        return name

    def settle_invoice(self, invoice: str) -> None:
        """Leave an invoice with nothing outstanding."""
        frappe.db.set_value(SALES_INVOICE, invoice, {"outstanding_amount": 0, "status": "Paid"})
        frappe.clear_document_cache(SALES_INVOICE, invoice)


class TestPaymentStateInFlight(PortalStateTestCase):
    """A capture Paystack holds while the Payment Entry is outstanding."""

    def test_a_captured_payment_awaiting_settlement_is_processing(self) -> None:
        """A Processed log puts the invoice in the processing state."""
        invoice = self.invoice()
        stamp(self.log_for(invoice), "Processed")

        self.assertEqual(payment_state(SALES_INVOICE, invoice), PAYMENT_STATE_PROCESSING)

    def test_an_expired_link_that_captured_money_is_still_processing(self) -> None:
        """A Processed log with a past expires_at reads as processing."""
        invoice = self.invoice()
        log = self.log_for(invoice)
        stamp(log, "Processed")
        frappe.db.set_value(PAYMENT_LOG, log, "expires_at", "2020-01-01 00:00:00")
        frappe.clear_document_cache(PAYMENT_LOG, log)

        self.assertEqual(payment_state(SALES_INVOICE, invoice), PAYMENT_STATE_PROCESSING)

    def test_a_capture_outranks_an_unused_link(self) -> None:
        """A Processed log decides the state while a Pending one also exists."""
        invoice = self.invoice()
        stamp(self.log_for(invoice), "Processed")
        self.log_for(invoice)

        self.assertEqual(payment_state(SALES_INVOICE, invoice), PAYMENT_STATE_PROCESSING)

    def test_a_sales_order_capture_is_processing_too(self) -> None:
        """A Processed log against a Sales Order reads as processing."""
        order = self.order()
        stamp(self.log_for(order, doctype=SALES_ORDER), "Processed")

        self.assertEqual(payment_state(SALES_ORDER, order), PAYMENT_STATE_PROCESSING)


class TestPaymentStateOpen(PortalStateTestCase):
    """Documents the customer still owes money on."""

    def test_a_document_with_no_payment_log_is_open(self) -> None:
        """A document with no payment log is open."""
        self.assertEqual(payment_state(SALES_INVOICE, self.invoice()), PAYMENT_STATE_OPEN)

    def test_an_unused_link_leaves_the_document_open(self) -> None:
        """A Pending log leaves the document open."""
        invoice = self.invoice()
        self.log_for(invoice)

        self.assertEqual(payment_state(SALES_INVOICE, invoice), PAYMENT_STATE_OPEN)

    def test_a_declined_payment_leaves_the_document_open(self) -> None:
        """A Failed log leaves the document open."""
        invoice = self.invoice()
        stamp(self.log_for(invoice), "Failed", amount_paid=0)

        self.assertEqual(payment_state(SALES_INVOICE, invoice), PAYMENT_STATE_OPEN)

    def test_a_part_payment_still_leaves_the_invoice_payable(self) -> None:
        """A Completed log covering part of the bill leaves the invoice open."""
        invoice = self.invoice(rate=1000)
        stamp(self.log_for(invoice, amount=400), "Completed", amount_paid=400)

        self.assertEqual(payment_state(SALES_INVOICE, invoice), PAYMENT_STATE_OPEN)


class TestPaymentStateSettled(PortalStateTestCase):
    """Captures the Payment Entry has booked."""

    def test_a_booked_capture_that_clears_the_bill_is_settled(self) -> None:
        """A Completed log on an invoice with nothing outstanding is settled."""
        invoice = self.invoice()
        stamp(self.log_for(invoice), "Completed")
        self.settle_invoice(invoice)

        self.assertEqual(payment_state(SALES_INVOICE, invoice), PAYMENT_STATE_SETTLED)

    def test_a_partly_refunded_capture_is_settled(self) -> None:
        """A Partially Refunded log on a cleared invoice is settled."""
        invoice = self.invoice()
        stamp(self.log_for(invoice), "Partially Refunded")
        self.settle_invoice(invoice)

        self.assertEqual(payment_state(SALES_INVOICE, invoice), PAYMENT_STATE_SETTLED)

    def test_a_fully_refunded_capture_is_settled(self) -> None:
        """A Refunded log on a cleared invoice is settled."""
        invoice = self.invoice()
        stamp(self.log_for(invoice), "Refunded")
        self.settle_invoice(invoice)

        self.assertEqual(payment_state(SALES_INVOICE, invoice), PAYMENT_STATE_SETTLED)

    def test_a_paid_sales_order_is_settled(self) -> None:
        """A Sales Order settles on advance_paid covering the order total."""
        order = self.order(rate=1000)
        stamp(self.log_for(order, doctype=SALES_ORDER), "Completed")
        frappe.db.set_value(SALES_ORDER, order, "advance_paid", 1000)
        frappe.clear_document_cache(SALES_ORDER, order)

        self.assertEqual(payment_state(SALES_ORDER, order), PAYMENT_STATE_SETTLED)


class TestOrderPageContext(PortalStateTestCase):
    """What the hook leaves on an ERPNext or Webshop portal order page."""

    def context_for(self, doctype: str, docname: str) -> frappe._dict:
        """Build the page context both order templates are rendered from."""
        context = frappe._dict(
            doc=frappe.get_doc(doctype, docname),
            show_pay_button=True,
            enabled_checkout=1,
        )
        apply_payment_state(context)
        return context

    def test_the_hook_is_registered(self) -> None:
        """apply_payment_state is registered on update_website_context."""
        self.assertIn(CONTEXT_HOOK, frappe.get_hooks("update_website_context"))

    def test_the_erpnext_pay_button_is_taken_off(self) -> None:
        """A capture in flight sets show_pay_button, ERPNext's flag, to False."""
        order = self.order()
        stamp(self.log_for(order, doctype=SALES_ORDER), "Processed")

        self.assertIs(self.context_for(SALES_ORDER, order).show_pay_button, False)

    def test_the_webshop_pay_button_is_taken_off(self) -> None:
        """A capture in flight sets enabled_checkout, Webshop's flag, to False."""
        order = self.order()
        stamp(self.log_for(order, doctype=SALES_ORDER), "Processed")

        self.assertIs(self.context_for(SALES_ORDER, order).enabled_checkout, False)

    def test_the_order_page_pill_reads_processing_payment(self) -> None:
        """A capture in flight sets an orange Processing Payment indicator."""
        order = self.order()
        stamp(self.log_for(order, doctype=SALES_ORDER), "Processed")

        context = self.context_for(SALES_ORDER, order)

        self.assertEqual(context.doc.indicator_title, "Processing Payment")
        self.assertEqual(context.doc.indicator_color, "orange")
        self.assertEqual(context.paystack_payment_state, PAYMENT_STATE_PROCESSING)

    def test_an_unpaid_document_keeps_its_pay_button(self) -> None:
        """An unpaid document keeps both pay flags and carries no payment state."""
        order = self.order()

        context = self.context_for(SALES_ORDER, order)

        self.assertIs(context.show_pay_button, True)
        self.assertEqual(context.enabled_checkout, 1)
        self.assertIsNone(context.paystack_payment_state)

    def test_an_unpaid_document_keeps_its_own_indicator(self) -> None:
        """An unpaid Sales Order carries no indicator_title."""
        order = self.order()
        self.log_for(order, doctype=SALES_ORDER)

        self.assertIsNone(self.context_for(SALES_ORDER, order).doc.get("indicator_title"))

    def test_a_settled_order_offers_no_further_payment(self) -> None:
        """A settled Sales Order clears both pay flags and reads as settled."""
        order = self.order(rate=1000)
        stamp(self.log_for(order, doctype=SALES_ORDER), "Completed")
        frappe.db.set_value(SALES_ORDER, order, "advance_paid", 1000)
        frappe.clear_document_cache(SALES_ORDER, order)

        context = self.context_for(SALES_ORDER, order)

        self.assertIs(context.show_pay_button, False)
        self.assertIs(context.enabled_checkout, False)
        self.assertEqual(context.paystack_payment_state, PAYMENT_STATE_SETTLED)

    def test_a_settled_order_keeps_its_own_indicator(self) -> None:
        """A settled Sales Order carries no indicator_title."""
        order = self.order(rate=1000)
        stamp(self.log_for(order, doctype=SALES_ORDER), "Completed")
        frappe.db.set_value(SALES_ORDER, order, "advance_paid", 1000)
        frappe.clear_document_cache(SALES_ORDER, order)

        self.assertIsNone(self.context_for(SALES_ORDER, order).doc.get("indicator_title"))

    def test_a_page_without_a_document_is_left_alone(self) -> None:
        """A context holding no document keeps its pay flag and carries no state."""
        context = frappe._dict(show_pay_button=True)

        apply_payment_state(context)

        self.assertIs(context.show_pay_button, True)
        self.assertIsNone(context.paystack_payment_state)

    def test_the_checkout_pages_payload_is_left_alone(self) -> None:
        """A context holding the checkout payload dict keeps its pay flag."""
        invoice = self.invoice()
        log = self.log_for(invoice)
        stamp(log, "Processed")

        context = frappe._dict(doc=frappe.get_doc(PAYMENT_LOG, log).get_data(), show_pay_button=True)
        apply_payment_state(context)

        self.assertIs(context.show_pay_button, True)
        self.assertIsNone(context.paystack_payment_state)

    def test_a_document_paystack_cannot_collect_is_left_alone(self) -> None:
        """A capture against a Dunning leaves the page context as it was."""
        self.addCleanup(CustomerFactory.cleanup, DUNNING_CUSTOMER)
        invoice = SalesInvoiceFactory.create(
            rate=1000,
            customer=CustomerFactory.create(customer_name=DUNNING_CUSTOMER),
            overdue_days=45,
        )
        self.addCleanup(SalesInvoiceFactory.cleanup, invoice)

        dunning = DunningFactory.create(invoice)
        self.addCleanup(DunningFactory.cleanup, dunning)
        stamp(self.log_for(dunning, doctype=DUNNING, amount=100), "Processed", 100)

        context = frappe._dict(doc=frappe.get_doc(DUNNING, dunning), show_pay_button=True)
        apply_payment_state(context)

        self.assertIs(context.show_pay_button, True)
        self.assertIsNone(context.paystack_payment_state)


class TestMyPaymentsPayNow(PaystackTestCase):
    """The /my-payments outstanding invoice list."""

    def setUp(self) -> None:
        """Give a portal customer an unpaid invoice and remember the session."""
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

        self.session_user = frappe.session.user
        self.addCleanup(self.restore_session)

        self.customer = self.customer_with_contact(STATE_CUSTOMER, STATE_USER)
        self.invoice = SalesInvoiceFactory.create(rate=1000, customer=self.customer)
        self.addCleanup(SalesInvoiceFactory.cleanup, self.invoice)

    def restore_session(self) -> None:
        """Put the session user back."""
        frappe.session.user = self.session_user

    def customer_with_contact(self, customer: str, email: str) -> str:
        """Create a Customer and the Contact that resolves an email to it."""
        CustomerFactory.create(customer_name=customer)
        self.addCleanup(CustomerFactory.cleanup, customer)

        contact = frappe.get_doc(
            {
                "doctype": "Contact",
                "first_name": customer,
                "email_ids": [{"email_id": email, "is_primary": 1}],
                "links": [{"link_doctype": "Customer", "link_name": customer}],
            }
        )
        contact.flags.ignore_permissions = True
        contact.flags.ignore_mandatory = True
        contact.insert()
        self.addCleanup(cleanup_doc, "Contact", contact.name)
        return customer

    def page_context(self) -> frappe._dict:
        """Build the /my-payments context as the portal customer."""
        frappe.session.user = STATE_USER
        try:
            return frappe.get_module("frappe_paystack.www.my-payments.index").get_context(frappe._dict())
        finally:
            frappe.session.user = self.session_user

    def row_for(self, invoice: str) -> frappe._dict:
        """Return the listed row for an invoice."""
        return next(row for row in self.page_context().invoices if row.name == invoice)

    def capture_for(self, invoice: str) -> str:
        """Raise a captured payment against an invoice."""
        log = PaymentLogFactory.create(linked_docname=invoice, amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log)
        stamp(log, "Processed")
        return log

    def test_an_unpaid_invoice_offers_pay_now(self) -> None:
        """An unpaid invoice lists with the open payment state."""
        self.assertEqual(self.row_for(self.invoice).payment_state, PAYMENT_STATE_OPEN)

    def test_an_invoice_being_paid_offers_no_pay_now_button(self) -> None:
        """An invoice with a capture in flight lists with the processing state."""
        self.capture_for(self.invoice)

        self.assertEqual(self.row_for(self.invoice).payment_state, PAYMENT_STATE_PROCESSING)

    def test_the_page_carries_the_label_shown_in_place_of_pay_now(self) -> None:
        """The page context carries the processing label 'Processing Payment'."""
        self.assertEqual(self.page_context().processing_label, "Processing Payment")


class TestOwnershipDoctypeGate(PortalStateTestCase):
    """Ownership is resolved through the documents the portal lists."""

    def test_a_dunning_payment_is_not_owned_by_its_customer(self) -> None:
        """A Dunning log is unowned even by the customer the Dunning names."""
        self.addCleanup(CustomerFactory.cleanup, DUNNING_CUSTOMER)
        customer = CustomerFactory.create(customer_name=DUNNING_CUSTOMER)

        invoice = SalesInvoiceFactory.create(rate=1000, customer=customer, overdue_days=45)
        self.addCleanup(SalesInvoiceFactory.cleanup, invoice)

        dunning = DunningFactory.create(invoice)
        self.addCleanup(DunningFactory.cleanup, dunning)
        log = self.log_for(dunning, doctype=DUNNING, amount=100)

        self.assertEqual(frappe.db.get_value(DUNNING, dunning, "customer"), customer)
        self.assertIs(owns_payment_log(customer, log), False)


class TestPaymentStateDoctypes(PortalStateTestCase):
    """Every document Paystack can be paid against is read the same way."""

    def test_a_pos_invoice_capture_is_processing(self) -> None:
        """A Processed log against a POS Invoice reads as processing."""
        invoice = POSInvoiceFactory.create(mode_of_payment=None)
        self.addCleanup(POSInvoiceFactory.cleanup, invoice)
        stamp(self.log_for(invoice, doctype=POS_INVOICE), "Processed")

        self.assertEqual(payment_state(POS_INVOICE, invoice), PAYMENT_STATE_PROCESSING)
