"""Payment Entry creation from a Paystack Payment Log: the wiring and the resulting allocation."""

import random
from typing import Any
from unittest.mock import patch

import frappe
from frappe.utils import flt, today

from frappe_paystack.api import create_payment_link, process_charge_webhook_event
from frappe_paystack.frappe_paystack.doctype.paystack_payment_log.paystack_payment_log import (
    create_payment_entry_from_log,
)
from frappe_paystack.tests.factories import (
    SELLING_PRICE_LIST,
    CustomerFactory,
    GatewaySettingFactory,
    ItemFactory,
    PaymentLogFactory,
    SalesInvoiceFactory,
    SalesOrderFactory,
    cleanup_doc,
    cleanup_linked_payment_entries,
)
from frappe_paystack.tests.test_base import PaystackTestCase
from frappe_paystack.utils.payment_request import (
    build_payment_request,
    can_bill_through_payment_request,
    gateway_account,
    open_payment_request,
    payment_request_checkout_url,
)

TEST_COMPANY = "_Test Company"
PAYMENT_LOG = "Paystack Payment Log"
PAYMENT_REQUEST = "Payment Request"

ACCOUNTS_SETTINGS = "Accounts Settings"

# Whether ERPNext saves a Payment Request itself before handing it back.
DRAFT_REQUEST_FIELD = "create_pr_in_draft_status"

# A company the site has no Paystack Payment Gateway Account for.
COMPANY_WITHOUT_GATEWAY = "Paystack Test Company Without A Gateway"

LOG_MODULE = "frappe_paystack.frappe_paystack.doctype.paystack_payment_log.paystack_payment_log"

# The step of set_as_paid() that bills a Shopping Cart order.
MAKE_INVOICE = "erpnext.accounts.doctype.payment_request.payment_request.PaymentRequest.make_invoice"

# A submission-time hook on the Payment Entry, run once the row is inserted.
SUBMIT_PAYMENT_ENTRY = "erpnext.accounts.doctype.payment_entry.payment_entry.PaymentEntry.before_submit"


def restore_draft_request_setting(value) -> None:
    """Put the Accounts Settings draft-Payment-Request flag back."""
    frappe.db.set_single_value(ACCOUNTS_SETTINGS, DRAFT_REQUEST_FIELD, value)
    frappe.db.commit()


class PaymentEntryTestCase(PaystackTestCase):
    """Shared setup for Payment Entry creation."""

    def setUp(self) -> None:
        """Enable Paystack for the test company."""
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

    def mark_paid(self, log_name: str, amount: float) -> None:
        """Set a log to the state the webhook leaves it in."""
        frappe.db.set_value(
            "Paystack Payment Log",
            log_name,
            {
                "status": "Processed",
                "amount_paid": amount,
                "payment_reference": f"ref-{log_name}",
                "payment_date": frappe.utils.today(),
            },
        )
        frappe.clear_document_cache("Paystack Payment Log", log_name)

    def payment_entry_of(self, log_name: str) -> object:
        """Return the Payment Entry created for a log."""
        pe_name = frappe.db.get_value("Paystack Payment Log", log_name, "payment_entry")
        self.assertTrue(pe_name, "no Payment Entry was created")
        self.addCleanup(cleanup_doc, "Payment Entry", pe_name)
        return frappe.get_doc("Payment Entry", pe_name)


class TestSalesInvoicePaymentEntry(PaymentEntryTestCase):
    """Payment Entry against a Sales Invoice."""

    def test_payment_entry_is_created_and_submitted(self) -> None:
        """A processed log produces a submitted Payment Entry."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        self.mark_paid(log_name, 1000)

        create_payment_entry_from_log(log_name)

        pe = self.payment_entry_of(log_name)
        self.assertEqual(pe.docstatus, 1)
        self.assertEqual(pe.payment_type, "Receive")
        self.assertEqual(pe.party_type, "Customer")
        self.assertEqual(pe.party, "_Test Customer")

    def test_payment_entry_references_the_invoice(self) -> None:
        """ERPNext allocates the payment against the source invoice."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        invoice = frappe.db.get_value("Paystack Payment Log", log_name, "linked_docname")
        self.mark_paid(log_name, 1000)

        create_payment_entry_from_log(log_name)

        pe = self.payment_entry_of(log_name)
        self.assertEqual(len(pe.references), 1)
        self.assertEqual(pe.references[0].reference_doctype, "Sales Invoice")
        self.assertEqual(pe.references[0].reference_name, invoice)
        self.assertAlmostEqual(flt(pe.references[0].allocated_amount), 1000.0, places=2)

    def test_payment_entry_settles_the_invoice(self) -> None:
        """A full payment clears the invoice outstanding."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        invoice = frappe.db.get_value("Paystack Payment Log", log_name, "linked_docname")
        self.mark_paid(log_name, 1000)

        create_payment_entry_from_log(log_name)
        self.payment_entry_of(log_name)

        outstanding = frappe.db.get_value("Sales Invoice", invoice, "outstanding_amount")
        self.assertAlmostEqual(flt(outstanding), 0.0, places=2)

    def test_partial_payment_leaves_balance(self) -> None:
        """A partial payment leaves the remaining balance outstanding."""
        invoice = SalesInvoiceFactory.create(rate=1000)
        self.addCleanup(SalesInvoiceFactory.cleanup, invoice)

        log_name = PaymentLogFactory.create(linked_docname=invoice, status="Pending", amount=400)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        self.mark_paid(log_name, 400)

        create_payment_entry_from_log(log_name)
        pe = self.payment_entry_of(log_name)

        self.assertAlmostEqual(flt(pe.references[0].allocated_amount), 400.0, places=2)
        outstanding = frappe.db.get_value("Sales Invoice", invoice, "outstanding_amount")
        self.assertAlmostEqual(flt(outstanding), 600.0, places=2)

    def test_log_is_completed_and_linked(self) -> None:
        """The log records the Payment Entry and moves to Completed."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        self.mark_paid(log_name, 1000)

        create_payment_entry_from_log(log_name)
        self.payment_entry_of(log_name)

        self.assertEqual(frappe.db.get_value("Paystack Payment Log", log_name, "status"), "Completed")

    def test_creation_is_idempotent(self) -> None:
        """Running twice leaves the one Payment Entry linked to the log."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        self.mark_paid(log_name, 1000)

        create_payment_entry_from_log(log_name)
        first = self.payment_entry_of(log_name)

        create_payment_entry_from_log(log_name)

        self.assertEqual(
            frappe.db.get_value("Paystack Payment Log", log_name, "payment_entry"),
            first.name,
        )
        self.assertEqual(frappe.db.count("Payment Entry", {"reference_no": f"ref-{log_name}"}), 1)

    def test_gateway_details_are_applied(self) -> None:
        """Mode of payment and Paystack reference land on the Payment Entry."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        self.mark_paid(log_name, 1000)

        create_payment_entry_from_log(log_name)

        pe = self.payment_entry_of(log_name)
        self.assertEqual(pe.mode_of_payment, "Paystack")
        self.assertEqual(pe.reference_no, f"ref-{log_name}")
        self.assertIn(log_name, pe.remarks)


class TestSalesOrderPaymentEntry(PaymentEntryTestCase):
    """Payment Entry against a Sales Order."""

    def test_payment_entry_references_the_order(self) -> None:
        """A Sales Order payment is allocated against the order."""
        order = SalesOrderFactory.create(rate=1000)
        self.addCleanup(SalesOrderFactory.cleanup, order)

        log_name = PaymentLogFactory.create(linked_doctype="Sales Order", linked_docname=order, amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        self.mark_paid(log_name, 1000)

        create_payment_entry_from_log(log_name)

        pe = self.payment_entry_of(log_name)
        self.assertEqual(pe.references[0].reference_doctype, "Sales Order")
        self.assertEqual(pe.references[0].reference_name, order)

    def test_order_advance_paid_is_updated(self) -> None:
        """ERPNext records the payment as advance against the order."""
        order = SalesOrderFactory.create(rate=1000)
        self.addCleanup(SalesOrderFactory.cleanup, order)

        log_name = PaymentLogFactory.create(linked_doctype="Sales Order", linked_docname=order, amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        self.mark_paid(log_name, 1000)

        create_payment_entry_from_log(log_name)
        self.payment_entry_of(log_name)

        advance = frappe.db.get_value("Sales Order", order, "advance_paid")
        self.assertAlmostEqual(flt(advance), 1000.0, places=2)


class TestPaymentEntryGuards(PaymentEntryTestCase):
    """Cases that create no Payment Entry."""

    def test_pending_log_creates_nothing(self) -> None:
        """A log that has not been paid produces no Payment Entry."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        create_payment_entry_from_log(log_name)

        self.assertFalse(frappe.db.get_value("Paystack Payment Log", log_name, "payment_entry"))

    def test_settled_invoice_short_circuits(self) -> None:
        """An invoice with nothing outstanding is marked Completed."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        invoice = frappe.db.get_value("Paystack Payment Log", log_name, "linked_docname")
        self.mark_paid(log_name, 1000)
        frappe.db.set_value("Sales Invoice", invoice, "outstanding_amount", 0)

        create_payment_entry_from_log(log_name)

        self.assertFalse(frappe.db.get_value("Paystack Payment Log", log_name, "payment_entry"))
        self.assertEqual(frappe.db.get_value("Paystack Payment Log", log_name, "status"), "Completed")

    def test_zero_amount_creates_nothing(self) -> None:
        """A zero paid amount produces no Payment Entry."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        self.mark_paid(log_name, 0)

        create_payment_entry_from_log(log_name)

        self.assertFalse(frappe.db.get_value("Paystack Payment Log", log_name, "payment_entry"))


class TestSettlementThatCannotBeSubmitted(PaymentEntryTestCase):
    """A Payment Entry that is inserted and then refused leaves nothing behind."""

    def entries_for(self, invoice: str) -> list:
        """Return every Payment Entry allocated against a document, with its state."""
        return frappe.get_all(
            "Payment Entry Reference",
            filters={"reference_doctype": "Sales Invoice", "reference_name": invoice},
            fields=["parent", "docstatus"],
        )

    def refused_settlement(self) -> tuple:
        """Drive a settlement whose submission is refused, and return log and invoice."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        invoice = frappe.db.get_value(PAYMENT_LOG, log_name, "linked_docname")
        self.mark_paid(log_name, 1000)

        with patch(SUBMIT_PAYMENT_ENTRY, create=True, side_effect=frappe.ValidationError("refused")):
            create_payment_entry_from_log(log_name)

        return log_name, invoice

    def test_a_refused_settlement_leaves_no_payment_entry(self) -> None:
        """The Payment Entry the attempt inserted is discarded."""
        _, invoice = self.refused_settlement()

        self.assertEqual(self.entries_for(invoice), [])

    def test_a_refused_settlement_is_recorded_on_the_log(self) -> None:
        """The log carries the failure and no Payment Entry."""
        log_name, _ = self.refused_settlement()

        record = frappe.db.get_value(PAYMENT_LOG, log_name, ["payment_entry", "errors"], as_dict=True)
        self.assertFalse(record.payment_entry)
        self.assertTrue(record.errors)

    def test_repeated_attempts_leave_nothing_behind(self) -> None:
        """A second refused attempt adds no second draft."""
        log_name, invoice = self.refused_settlement()

        with patch(SUBMIT_PAYMENT_ENTRY, create=True, side_effect=frappe.ValidationError("refused")):
            create_payment_entry_from_log(log_name)

        self.assertEqual(self.entries_for(invoice), [])


class PaymentRequestTestCase(PaymentEntryTestCase):
    """A desk payment link is billed through ERPNext's Payment Request."""

    def build_order(self, order_type: str = "Shopping Cart", rate: float = 1000) -> Any:
        """Return a submitted Sales Order in a currency Paystack can charge."""
        CustomerFactory.create()
        ItemFactory.create()

        order = frappe.get_doc(
            {
                "doctype": "Sales Order",
                "customer": "_Test Customer",
                "company": TEST_COMPANY,
                "order_type": order_type,
                "delivery_date": today(),
                "transaction_date": today(),
                "currency": "NGN",
                "conversion_rate": 1,
                "selling_price_list": SELLING_PRICE_LIST,
                "items": [{"item_code": "_Test Item Home Products 100", "qty": 1, "rate": rate}],
            }
        )
        order.flags.ignore_permissions = True
        order.flags.ignore_mandatory = True
        order.insert()
        order.submit()

        self.addCleanup(self.cleanup_order, order.name)
        return order

    def cleanup_order(self, order: str) -> None:
        """Unwind everything the settlement raised against a Sales Order."""
        invoices = set(frappe.get_all("Sales Invoice Item", filters={"sales_order": order}, pluck="parent"))
        for invoice in invoices:
            cleanup_linked_payment_entries("Sales Invoice", invoice)
            cleanup_doc("Sales Invoice", invoice)

        cleanup_linked_payment_entries("Sales Order", order)

        for request in frappe.get_all(
            "Payment Request",
            filters={"reference_doctype": "Sales Order", "reference_name": order},
            pluck="name",
        ):
            cleanup_doc(PAYMENT_REQUEST, request)

        cleanup_doc("Sales Order", order)

    def cleanup_log(self, log: str) -> None:
        """Delete a Payment Log, leaving its linked documents to cleanup_order."""
        if not frappe.db.exists(PAYMENT_LOG, log):
            return

        frappe.db.set_value(PAYMENT_LOG, log, {"status": "Pending", "payment_entry": None})
        frappe.delete_doc(PAYMENT_LOG, log, force=True, ignore_permissions=True)
        frappe.db.commit()

    def raise_link(self, order: Any, amount: float = 1000) -> str:
        """Return the Payment Log backing a checkout URL for an order."""
        url = payment_request_checkout_url(order, amount, "buyer@example.com")
        log = url.rsplit("/", 1)[-1]
        self.addCleanup(self.cleanup_log, log)
        return log

    def raise_desk_link(self, order: Any, amount: float = 1000) -> str:
        """Return the Payment Log behind the desk "Pay now" button."""
        url = create_payment_link("Sales Order", order.name, amount=amount, currency=order.currency)
        log = url.rsplit("/", 1)[-1]
        self.addCleanup(self.cleanup_log, log)
        return log

    def submitted_entries_for(self, request: str) -> list:
        """Return the submitted Payment Entries booked for a Payment Request."""
        by_reference = frappe.get_all(
            "Payment Entry", filters={"reference_no": request, "docstatus": 1}, pluck="name"
        )
        by_allocation = frappe.get_all(
            "Payment Entry Reference",
            filters={"payment_request": request, "docstatus": 1},
            pluck="parent",
        )
        return sorted(set(by_reference) | set(by_allocation))


class TestPaymentRequestRouting(PaymentRequestTestCase):
    """The desk "Pay now" button routes through a Payment Request."""

    def test_the_desk_button_raises_a_payment_request(self) -> None:
        """A desk link carries the Payment Request it was raised through."""
        order = self.build_order()

        log = self.raise_desk_link(order)

        self.assertTrue(frappe.db.get_value(PAYMENT_LOG, log, "payment_request"))

    def test_the_desk_button_bills_the_order(self) -> None:
        """A Sales Order paid from the desk is fully billed and advances to To Deliver."""
        order = self.build_order()
        log = self.raise_desk_link(order)
        self.mark_paid(log, 1000)

        create_payment_entry_from_log(log)

        order.reload()
        self.assertEqual(flt(order.per_billed), 100.0)
        self.assertEqual(order.status, "To Deliver")

    def test_the_desk_payment_is_allocated_not_left_as_advance(self) -> None:
        """The invoice the settlement raises is settled by the same money."""
        order = self.build_order()
        log = self.raise_desk_link(order)
        self.mark_paid(log, 1000)

        create_payment_entry_from_log(log)

        invoices = set(
            frappe.get_all("Sales Invoice Item", filters={"sales_order": order.name}, pluck="parent")
        )
        self.assertEqual(len(invoices), 1)
        outstanding = frappe.db.get_value("Sales Invoice", invoices.pop(), "outstanding_amount")
        self.assertAlmostEqual(flt(outstanding), 0.0, places=2)

    def test_a_pos_invoice_cannot_bill_through_a_payment_request(self) -> None:
        """A POS Invoice does not bill through a Payment Request."""
        self.assertFalse(can_bill_through_payment_request("POS Invoice"))

    def test_sales_documents_bill_through_a_payment_request(self) -> None:
        """Sales Order and Sales Invoice bill through a Payment Request."""
        self.assertTrue(can_bill_through_payment_request("Sales Order"))
        self.assertTrue(can_bill_through_payment_request("Sales Invoice"))

    def test_a_document_without_a_company_resolves_no_gateway_account(self) -> None:
        """A document carrying no company resolves no gateway account."""
        self.assertIsNone(gateway_account(None))

    def test_a_request_that_booked_nothing_leaves_the_log_stuck(self) -> None:
        """A settlement booking no entry leaves the log Processed and unlinked."""
        order = self.build_order()
        log = self.raise_link(order)
        self.mark_paid(log, 1000)

        with patch(f"{LOG_MODULE}.notify_payment_authorized"):
            with patch(f"{LOG_MODULE}.resolve_payment_entry", return_value=None):
                create_payment_entry_from_log(log)

        self.assertEqual(frappe.db.get_value(PAYMENT_LOG, log, "status"), "Processed")
        self.assertFalse(frappe.db.get_value(PAYMENT_LOG, log, "payment_entry"))

    def test_the_log_carries_the_payment_request(self) -> None:
        """The log carries the Payment Request and the order it was raised for."""
        order = self.build_order()

        log = self.raise_link(order)

        record = frappe.db.get_value(PAYMENT_LOG, log, ["payment_request", "linked_docname"], as_dict=True)
        self.assertTrue(record.payment_request)
        self.assertEqual(record.linked_docname, order.name)

    def test_the_request_is_submitted_on_the_email_channel(self) -> None:
        """The Payment Request is submitted on the Email channel."""
        order = self.build_order()

        log = self.raise_link(order)

        request = frappe.db.get_value(PAYMENT_LOG, log, "payment_request")
        record = frappe.db.get_value(PAYMENT_REQUEST, request, ["docstatus", "payment_channel"], as_dict=True)
        self.assertEqual(record.docstatus, 1)
        self.assertEqual(record.payment_channel, "Email")

    def test_a_partial_link_asks_for_less(self) -> None:
        """A part payment raises a request for the part amount."""
        order = self.build_order()

        log = self.raise_link(order, 400)

        request = frappe.db.get_value(PAYMENT_LOG, log, "payment_request")
        self.assertEqual(flt(frappe.db.get_value(PAYMENT_REQUEST, request, "grand_total")), 400.0)

    def test_a_repeat_link_reuses_the_open_request(self) -> None:
        """A second link reuses the order's one open Payment Request."""
        order = self.build_order()
        first = self.raise_link(order)

        second = self.raise_link(order)

        self.assertEqual(
            frappe.db.get_value(PAYMENT_LOG, first, "payment_request"),
            frappe.db.get_value(PAYMENT_LOG, second, "payment_request"),
        )
        self.assertEqual(
            frappe.db.count(
                PAYMENT_REQUEST,
                {"reference_doctype": "Sales Order", "reference_name": order.name},
            ),
            1,
        )

    def test_an_unpaid_request_is_found(self) -> None:
        """The reuse lookup matches a request billing the same amount."""
        order = self.build_order()
        self.raise_link(order)

        self.assertTrue(open_payment_request(order, 1000))
        self.assertFalse(open_payment_request(order, 250))

    def test_settlement_bills_the_shopping_cart_order(self) -> None:
        """A paid cart order is fully billed and reaches To Deliver."""
        order = self.build_order()
        log = self.raise_link(order)
        self.mark_paid(log, 1000)

        create_payment_entry_from_log(log)

        order.reload()
        self.assertEqual(flt(order.per_billed), 100.0)
        self.assertEqual(order.status, "To Deliver")

    def test_settlement_links_the_payment_entry_to_the_log(self) -> None:
        """Settlement completes the log and links the Payment Entry it booked."""
        order = self.build_order()
        log = self.raise_link(order)
        self.mark_paid(log, 1000)

        create_payment_entry_from_log(log)

        record = frappe.db.get_value(PAYMENT_LOG, log, ["status", "payment_entry"], as_dict=True)
        self.assertEqual(record.status, "Completed")
        self.assertTrue(record.payment_entry)

    def test_settlement_is_idempotent(self) -> None:
        """A second settlement run books no further Payment Entry."""
        order = self.build_order()
        log = self.raise_link(order)
        self.mark_paid(log, 1000)

        request = frappe.db.get_value(PAYMENT_LOG, log, "payment_request")

        create_payment_entry_from_log(log)
        booked = self.submitted_entries_for(request)
        self.assertEqual(len(booked), 1)

        frappe.db.set_value(PAYMENT_LOG, log, {"payment_entry": None, "status": "Processed"})
        frappe.clear_document_cache(PAYMENT_LOG, log)
        create_payment_entry_from_log(log)

        self.assertEqual(self.submitted_entries_for(request), booked)
        self.assertEqual(frappe.db.get_value(PAYMENT_LOG, log, "payment_entry"), booked[0])
        order.reload()
        self.assertEqual(flt(order.per_billed), 100.0)

    def test_a_short_capture_is_not_booked_as_the_full_request(self) -> None:
        """A short capture leaves the log Processed with the shortfall recorded."""
        order = self.build_order()
        log = self.raise_link(order)
        self.mark_paid(log, 400)

        create_payment_entry_from_log(log)

        record = frappe.db.get_value(PAYMENT_LOG, log, ["status", "payment_entry", "errors"], as_dict=True)
        self.assertEqual(record.status, "Processed")
        self.assertFalse(record.payment_entry)
        self.assertTrue(record.errors, "the shortfall was not recorded on the log")
        order.reload()
        self.assertEqual(flt(order.per_billed), 0.0)


class TestPaymentRequestGuards(PaymentRequestTestCase):
    """A checkout link is handed out only with a gateway behind it."""

    def draft_request(self, order: Any, amount: float = 1000) -> str:
        """Insert a draft Payment Request that names no gateway."""
        request = frappe.get_doc(
            {
                "doctype": PAYMENT_REQUEST,
                "payment_request_type": "Inward",
                "reference_doctype": order.doctype,
                "reference_name": order.name,
                "party_type": "Customer",
                "party": order.customer,
                "company": order.company,
                "currency": order.currency,
                "grand_total": amount,
            }
        )
        request.flags.ignore_permissions = True
        request.insert()

        self.addCleanup(cleanup_doc, PAYMENT_REQUEST, request.name)
        return request.name

    def test_a_request_without_a_gateway_is_refused(self) -> None:
        """A request whose company has no gateway account is refused."""
        order = self.build_order()
        self.draft_request(order)

        order.company = COMPANY_WITHOUT_GATEWAY

        with self.assertRaises(frappe.ValidationError) as refused:
            build_payment_request(order, 1000)

        self.assertIn(COMPANY_WITHOUT_GATEWAY, str(refused.exception))

    def test_an_unsaved_request_is_inserted_before_it_is_submitted(self) -> None:
        """With draft requests switched off, the request is inserted and submitted."""
        self.addCleanup(
            restore_draft_request_setting,
            frappe.db.get_single_value(ACCOUNTS_SETTINGS, DRAFT_REQUEST_FIELD),
        )
        frappe.db.set_single_value(ACCOUNTS_SETTINGS, DRAFT_REQUEST_FIELD, 0)

        order = self.build_order()
        request = build_payment_request(order, 1000, "buyer@example.com")

        self.assertTrue(frappe.db.exists(PAYMENT_REQUEST, request.name))
        self.assertEqual(frappe.db.get_value(PAYMENT_REQUEST, request.name, "docstatus"), 1)


class TestWebhookSettlement(PaymentRequestTestCase):
    """Settlement driven by the charge webhook."""

    def charge_event(self, log: str, amount: float = 1000) -> dict:
        """Build the charge.success payload Paystack sends for a log."""
        return {
            "event": "charge.success",
            "data": {
                "id": random.randint(200_000_000, 999_999_999),
                "reference": f"mrc-{log}",
                "status": "success",
                "amount": int(amount * 100),
                "currency": "NGN",
                "paid_at": f"{today()}T10:00:00.000Z",
                "metadata": {"reference": log},
            },
        }

    def test_a_paid_cart_order_reaches_completed_with_its_entry(self) -> None:
        """The webhook leaves the log Completed and linked to its Payment Entry."""
        order = self.build_order()
        log = self.raise_link(order)

        process_charge_webhook_event(self.charge_event(log))

        record = frappe.db.get_value(PAYMENT_LOG, log, ["status", "payment_entry"], as_dict=True)
        self.assertTrue(record.payment_entry, "the webhook booked nothing it could link")
        self.assertEqual(record.status, "Completed")

    def test_a_paid_cart_order_is_billed_by_the_webhook(self) -> None:
        """The webhook bills the order and marks the Payment Request Paid."""
        order = self.build_order()
        log = self.raise_link(order)

        process_charge_webhook_event(self.charge_event(log))

        order.reload()
        self.assertEqual(flt(order.per_billed), 100.0)
        self.assertEqual(
            frappe.db.get_value(
                PAYMENT_REQUEST,
                frappe.db.get_value(PAYMENT_LOG, log, "payment_request"),
                "status",
            ),
            "Paid",
        )

    def test_a_settlement_that_stopped_part_way_still_links_what_it_booked(self) -> None:
        """A settlement that booked money and then failed leaves both on the log."""
        order = self.build_order()
        log = self.raise_link(order)
        request = frappe.db.get_value(PAYMENT_LOG, log, "payment_request")

        with patch(MAKE_INVOICE, side_effect=frappe.ValidationError("the invoice could not be raised")):
            process_charge_webhook_event(self.charge_event(log))

        booked = self.submitted_entries_for(request)
        self.assertEqual(len(booked), 1, "the settlement booked no Payment Entry")

        record = frappe.db.get_value(PAYMENT_LOG, log, ["payment_entry", "errors"], as_dict=True)
        self.assertEqual(record.payment_entry, booked[0], "the booked Payment Entry was not linked")
        self.assertTrue(record.errors, "the settlement failure was not recorded on the log")

    def test_a_replayed_webhook_books_nothing_further(self) -> None:
        """A replayed charge event leaves the same single Payment Entry."""
        order = self.build_order()
        log = self.raise_link(order)
        request = frappe.db.get_value(PAYMENT_LOG, log, "payment_request")

        process_charge_webhook_event(self.charge_event(log))
        booked = self.submitted_entries_for(request)

        process_charge_webhook_event(self.charge_event(log))

        self.assertEqual(len(booked), 1)
        self.assertEqual(self.submitted_entries_for(request), booked)


class TestPaymentEntryAsGuest(PaymentEntryTestCase):
    """The settlement job run under the Guest session the webhook leaves."""

    def test_payment_entry_is_created_when_running_as_guest(self) -> None:
        """A submitted Payment Entry is created when the job runs as Guest."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        self.mark_paid(log_name, 1000)

        self.addCleanup(frappe.set_user, "Administrator")
        frappe.set_user("Guest")

        create_payment_entry_from_log(log_name)

        frappe.set_user("Administrator")
        pe = self.payment_entry_of(log_name)
        self.assertEqual(pe.docstatus, 1)
        self.assertFalse(frappe.db.get_value("Paystack Payment Log", log_name, "errors"))

    def test_session_user_is_restored(self) -> None:
        """The job leaves the caller's session user as it found it."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        self.mark_paid(log_name, 1000)

        self.addCleanup(frappe.set_user, "Administrator")
        frappe.set_user("Guest")

        create_payment_entry_from_log(log_name)

        self.assertEqual(frappe.session.user, "Guest")
        frappe.set_user("Administrator")
        self.addCleanup(
            cleanup_doc,
            "Payment Entry",
            frappe.db.get_value("Paystack Payment Log", log_name, "payment_entry"),
        )
