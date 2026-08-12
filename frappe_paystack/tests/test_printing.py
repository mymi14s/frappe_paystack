"""Print formats, and the checkout link and QR code they carry."""

from base64 import b64decode

import frappe
from frappe.utils import add_to_date, now_datetime, random_string

from frappe_paystack.api import payment_link_qr
from frappe_paystack.tests.factories import (
    UNPRIVILEGED_ROLE,
    GatewaySettingFactory,
    PaymentLogFactory,
    SalesInvoiceFactory,
    cleanup_user,
)
from frappe_paystack.tests.test_base import PaystackTestCase
from frappe_paystack.utils.printing import open_payment_log, paystack_payment_link, paystack_payment_qr

PAYMENT_LOG = "Paystack Payment Log"

INVOICE_FORMAT = "Paystack Invoice with Payment Link"
RECEIPT_FORMAT = "Paystack Payment Receipt"


class PrintingTestCase(PaystackTestCase):
    """An invoice a checkout link can be raised against."""

    def setUp(self) -> None:
        """Enable Paystack and raise an unpaid invoice."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

        self.invoice = SalesInvoiceFactory.create(rate=1000)
        self.addCleanup(SalesInvoiceFactory.cleanup, self.invoice)

    def link_for(self, **fields: str) -> str:
        """Raise a Payment Log against the fixture invoice."""
        log = PaymentLogFactory.create(linked_docname=self.invoice, amount=1000, status="Pending")
        self.addCleanup(PaymentLogFactory.cleanup, log)

        if fields:
            frappe.db.set_value(PAYMENT_LOG, log, fields)
            frappe.clear_document_cache(PAYMENT_LOG, log)

        return log

    def invoice_doc(self) -> frappe.model.document.Document:
        """Return the fixture invoice."""
        return frappe.get_doc("Sales Invoice", self.invoice)


class TestOpenPaymentLog(PrintingTestCase):
    """The print format shows the link a document already carries."""

    def test_a_document_without_a_link_has_none(self) -> None:
        """An invoice with no payment log returns None."""
        self.assertIsNone(open_payment_log(self.invoice_doc()))

    def test_an_open_link_is_found(self) -> None:
        """A Pending log is returned as the open link."""
        log = self.link_for()

        self.assertEqual(open_payment_log(self.invoice_doc()).name, log)

    def test_an_expired_link_is_not_printed(self) -> None:
        """A log past its expiry returns None."""
        self.link_for(expires_at=add_to_date(now_datetime(), hours=-1))

        self.assertIsNone(open_payment_log(self.invoice_doc()))

    def test_a_settled_link_is_not_printed(self) -> None:
        """A Processed log returns None."""
        self.link_for(status="Processed")

        self.assertIsNone(open_payment_log(self.invoice_doc()))

    def test_the_newest_open_link_wins(self) -> None:
        """The most recent open log is returned."""
        self.link_for()
        latest = self.link_for()

        self.assertEqual(open_payment_log(self.invoice_doc()).name, latest)


class TestPrintHelpers(PrintingTestCase):
    """The Jinja methods a print format calls."""

    def test_the_link_points_at_the_checkout(self) -> None:
        """The printed URL is the customer-facing checkout page."""
        log = self.link_for()

        self.assertTrue(paystack_payment_link(self.invoice_doc()).endswith(f"/paystack-checkout/{log}"))

    def test_no_link_renders_an_empty_string(self) -> None:
        """A document with no open log renders an empty string."""
        self.assertEqual(paystack_payment_link(self.invoice_doc()), "")

    def test_the_qr_encodes_the_link(self) -> None:
        """The QR renders as an SVG data URI built from the link."""
        self.link_for()

        uri = paystack_payment_qr(self.invoice_doc())

        self.assertTrue(uri.startswith("data:image/svg+xml;base64,"))
        self.assertIn("<svg", b64decode(uri.split(",", 1)[1]).decode())

    def test_no_link_renders_no_qr(self) -> None:
        """A document with no open log renders an empty QR string."""
        self.assertEqual(paystack_payment_qr(self.invoice_doc()), "")


class TestInvoicePrintFormat(PrintingTestCase):
    """The Sales Invoice format carries a pay-online block when there is one."""

    def rendered(self) -> str:
        """Render the invoice through the Paystack print format."""
        return frappe.get_print("Sales Invoice", self.invoice, print_format=INVOICE_FORMAT)

    def test_the_invoice_body_is_rendered(self) -> None:
        """The format renders the invoice body."""
        html = self.rendered()

        self.assertIn(self.invoice, html)
        self.assertIn("Grand Total", html)

    def test_an_open_link_is_printed_with_a_qr(self) -> None:
        """An open link prints as a pay-online block with its checkout URL and QR."""
        log = self.link_for()

        html = self.rendered()

        self.assertIn("Pay online", html)
        self.assertIn(f"/paystack-checkout/{log}", html)
        self.assertIn("data:image/svg+xml;base64,", html)

    def test_no_link_prints_no_payment_block(self) -> None:
        """An invoice with no open link prints no pay-online block."""
        html = self.rendered()

        self.assertNotIn("Pay online", html)
        self.assertNotIn("data:image/svg+xml;base64,", html)


class TestReceiptPrintFormat(PaystackTestCase):
    """The receipt the portal offers once a payment is captured."""

    def setUp(self) -> None:
        """Enable Paystack and settle a payment."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

        self.log = PaymentLogFactory.create_completed(amount=2500, amount_paid=2500)
        self.addCleanup(PaymentLogFactory.cleanup, self.log)
        frappe.db.set_value(PAYMENT_LOG, self.log, "payment_reference", "mrc_receipt_001")
        frappe.clear_document_cache(PAYMENT_LOG, self.log)

    def rendered(self) -> str:
        """Render the receipt for the fixture payment."""
        return frappe.get_print(PAYMENT_LOG, self.log, print_format=RECEIPT_FORMAT)

    def test_the_receipt_names_the_payment(self) -> None:
        """The receipt names the payment reference and the amount."""
        html = self.rendered()

        self.assertIn("Payment Receipt", html)
        self.assertIn("mrc_receipt_001", html)
        self.assertIn("2,500", html)

    def test_a_refund_is_shown_when_there_is_one(self) -> None:
        """A partly refunded payment says so on its receipt."""
        frappe.db.set_value(PAYMENT_LOG, self.log, "total_refunded", 500)
        frappe.clear_document_cache(PAYMENT_LOG, self.log)

        self.assertIn("Refunded", self.rendered())

    def test_an_unrefunded_payment_shows_no_refund_row(self) -> None:
        """A payment with nothing refunded prints no refund row."""
        self.assertNotIn("Refunded", self.rendered())


class TestPaymentLinkQrEndpoint(PrintingTestCase):
    """The desk asks for a QR code for a link it just raised."""

    def test_the_code_is_returned_for_a_real_link(self) -> None:
        """A real link returns an SVG data URI."""
        log = self.link_for()

        self.assertTrue(payment_link_qr(log).startswith("data:image/svg+xml;base64,"))

    def test_an_unknown_reference_is_refused(self) -> None:
        """An unknown reference raises a ValidationError."""
        with self.assertRaises(frappe.ValidationError):
            payment_link_qr("PSLOG-DOES-NOT-EXIST")

    def test_a_caller_without_the_document_is_refused(self) -> None:
        """A caller without read access to the invoice raises PermissionError."""
        log = self.link_for()

        user = f"paystack-qr-{random_string(8).lower()}@example.com"
        account = frappe.get_doc(
            {
                "doctype": "User",
                "email": user,
                "first_name": "Paystack QR",
                "send_welcome_email": 0,
                "user_type": "System User",
                "roles": [{"role": UNPRIVILEGED_ROLE}],
            }
        )
        account.flags.ignore_permissions = True
        account.insert()
        self.addCleanup(cleanup_user, user)
        self.addCleanup(frappe.set_user, "Administrator")
        frappe.set_user(user)

        with self.assertRaises(frappe.PermissionError):
            payment_link_qr(log)
