"""Tests for the app's web pages: the checkout page and the /my-payments portal."""

import json
from unittest.mock import patch

import frappe
from frappe.utils import flt

from frappe_paystack.frappe_paystack.doctype.paystack_payment_log.paystack_payment_log import (
    PaystackPaymentLog,
)
from frappe_paystack.tests.factories import (
    CustomerFactory,
    GatewaySettingFactory,
    PaymentLogFactory,
    PortalCustomerFactory,
    POSInvoiceFactory,
    RefundLogFactory,
    SalesInvoiceFactory,
    SalesOrderFactory,
    cleanup_doc,
    cleanup_user,
)
from frappe_paystack.tests.test_base import PaystackTestCase
from frappe_paystack.utils.portal import customer_for, download_payment_receipt, owns_payment_log

# Internal accounting configuration and secrets.
LEAKY_FIELDS = ("suspense_account", "mode_of_payment", "secret_key", "webhook_secret")

# A rendered field carrying markup.
MARKUP_NAME = '</script><img src=x onerror="alert(1)"> & Co'

# The dotted path Frappe's website router uses for this controller.
CHECKOUT_MODULE_NAME = "frappe_paystack.www.paystack-checkout.index"

checkout_page = frappe.get_module(CHECKOUT_MODULE_NAME)
checkout_context = checkout_page.get_context

my_payments_context = frappe.get_module("frappe_paystack.www.my-payments.index").get_context

PORTAL_USER = "portal-customer@example.com"
PORTAL_CUSTOMER = "_Test Customer Paystack Portal"
OTHER_CUSTOMER = "_Test Customer Paystack Other"
OTHER_USER = "other-paystack@example.com"
UNKNOWN_USER = "nobody-paystack@example.com"

POS_INVOICE = "POS Invoice"


class TestCheckoutContext(PaystackTestCase):
    """get_context() builds the checkout payload."""

    def setUp(self) -> None:
        """Enable Paystack for the test company."""
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

    def build_context(self, reference) -> frappe._dict:
        """Render the page context for a reference."""
        frappe.form_dict.reference = reference
        self.addCleanup(frappe.form_dict.pop, "reference", None)
        return checkout_context(frappe._dict())

    def test_unknown_reference_yields_no_doc(self) -> None:
        """An unknown reference renders the not-found state."""
        context = self.build_context("NONEXISTENT-LOG")

        self.assertIsNone(context.reference)
        self.assertIsNone(context.doc)
        self.assertEqual(context.payload, "null")

    def test_missing_reference_yields_no_doc(self) -> None:
        """A missing reference renders the not-found state."""
        context = self.build_context(None)

        self.assertIsNone(context.reference)
        self.assertIsNone(context.doc)

    def test_valid_reference_builds_payload(self) -> None:
        """A real payment log produces a JSON payload for the Vue app."""
        log_name = PaymentLogFactory.create(status="Pending", amount=2500)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        context = self.build_context(log_name)

        self.assertEqual(context.reference, log_name)
        payload = json.loads(context.payload)
        self.assertEqual(payload["reference"], log_name)
        self.assertEqual(payload["customer"], "_Test Customer")
        self.assertEqual(payload["public_key"], "pk_test_123")
        self.assertTrue(payload["is_payable"])

    def test_payload_is_valid_json(self) -> None:
        """The payload is a JSON object string."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        payload = self.build_context(log_name).payload

        self.assertNotIn("'", payload.replace("\\'", ""))
        self.assertIsInstance(json.loads(payload), dict)

    def test_payload_does_not_leak_internal_config(self) -> None:
        """Internal accounting and secret fields stay off the public page."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        payload = json.loads(self.build_context(log_name).payload)

        for field in LEAKY_FIELDS:
            with self.subTest(field=field):
                self.assertNotIn(field, payload)

    def test_page_is_not_cached(self) -> None:
        """The checkout page is rendered with no_cache set."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        self.assertEqual(self.build_context(log_name).no_cache, 1)

    def markup_payload(self) -> str:
        """Render the payload of a log whose customer name carries markup."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        with patch.object(PaystackPaymentLog, "get_data", return_value={"customer": MARKUP_NAME}):
            return self.build_context(log_name).payload

    def test_the_payload_cannot_close_its_script_tag(self) -> None:
        """Markup in a rendered field carries no angle bracket into the page."""
        payload = self.markup_payload()

        self.assertNotIn("<", payload)
        self.assertNotIn(">", payload)

    def test_the_escaped_payload_still_parses(self) -> None:
        """The escapes read back as the characters they stand for."""
        self.assertEqual(json.loads(self.markup_payload())["customer"], MARKUP_NAME)


class TestCheckoutPayability(PaystackTestCase):
    """is_payable drives whether the pay button renders."""

    def setUp(self) -> None:
        """Enable Paystack for the test company."""
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

    def test_pending_log_is_payable(self) -> None:
        """An open payment log against an unpaid invoice is payable."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        doc = frappe.get_doc("Paystack Payment Log", log_name)
        self.assertTrue(doc.get_data()["is_payable"])

    def test_completed_log_is_not_payable(self) -> None:
        """An already-paid log cannot be paid again."""
        log_name = PaymentLogFactory.create_completed(amount=1000, amount_paid=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        doc = frappe.get_doc("Paystack Payment Log", log_name)
        self.assertFalse(doc.get_data()["is_payable"])

    def test_settled_invoice_is_not_payable(self) -> None:
        """A settled invoice closes the checkout."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        doc = frappe.get_doc("Paystack Payment Log", log_name)
        frappe.db.set_value("Sales Invoice", doc.linked_docname, "status", "Paid")
        frappe.clear_document_cache("Sales Invoice", doc.linked_docname)

        self.assertFalse(doc.get_data()["is_payable"])

    def test_charge_amount_uses_conversion_rate(self) -> None:
        """The charged amount is the order total at the conversion rate."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        data = frappe.get_doc("Paystack Payment Log", log_name).get_data()
        self.assertAlmostEqual(
            data["payment_amount"],
            data["grand_total"] * data["exchange_rate"],
            places=2,
        )


class MyPaymentsTestCase(PaystackTestCase):
    """Shared fixtures for the portal page."""

    def setUp(self) -> None:
        """Enable Paystack and remember the session to restore."""
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

        self.session_user = frappe.session.user
        self.addCleanup(self.restore_session)

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

    def context_for(self, user: str) -> frappe._dict:
        """Build the page context as a given user."""
        frappe.session.user = user
        try:
            return my_payments_context(frappe._dict())
        finally:
            frappe.session.user = self.session_user


class TestMyPaymentsAccess(MyPaymentsTestCase):
    """Who the page answers."""

    def test_guest_is_refused(self) -> None:
        """A signed-out visitor cannot read anyone's payments."""
        with self.assertRaises(frappe.PermissionError):
            self.context_for("Guest")

    def test_user_without_a_contact_sees_nothing(self) -> None:
        """A user no Contact points at gets no customer and no rows."""
        context = self.context_for(UNKNOWN_USER)

        self.assertIsNone(context.customer)
        self.assertEqual(context.invoices, [])
        self.assertEqual(context.payments, [])

    def test_contact_resolves_the_customer(self) -> None:
        """The session email is matched to a Contact's customer."""
        customer = self.customer_with_contact(PORTAL_CUSTOMER, PORTAL_USER)

        self.assertEqual(self.context_for(PORTAL_USER).customer, customer)


class TestMyPaymentsContent(MyPaymentsTestCase):
    """What the page lists."""

    def setUp(self) -> None:
        """Give the portal user a customer, an invoice and a payment."""
        super().setUp()
        self.customer = self.customer_with_contact(PORTAL_CUSTOMER, PORTAL_USER)

        self.invoice = self.invoice_for(self.customer, rate=1000)
        self.log = self.payment_for(self.invoice, amount=1000)
        frappe.db.set_value("Paystack Payment Log", self.log, "payment_reference", "mrc_portal_001")

    def invoice_for(self, customer: str, rate: float) -> str:
        """Raise a submitted, unpaid invoice for a customer."""
        invoice = SalesInvoiceFactory.create(rate=rate, customer=customer)
        self.addCleanup(SalesInvoiceFactory.cleanup, invoice)
        return invoice

    def payment_for(self, invoice: str, amount: float) -> str:
        """Create a Payment Log against an invoice."""
        log = PaymentLogFactory.create(linked_docname=invoice, amount=amount, status="Pending")
        self.addCleanup(PaymentLogFactory.cleanup, log)
        return log

    def test_unpaid_invoice_is_listed(self) -> None:
        """The customer's outstanding invoice is offered for payment."""
        invoices = self.context_for(PORTAL_USER).invoices

        row = next(row for row in invoices if row.name == self.invoice)
        self.assertEqual(flt(row.outstanding_amount), 1000.0)

    def test_payment_history_carries_the_paystack_reference(self) -> None:
        """A history row carries the Paystack reference and its linked document."""
        payments = self.context_for(PORTAL_USER).payments

        row = next(row for row in payments if row.name == self.log)
        self.assertEqual(row.reference, "mrc_portal_001")
        self.assertEqual(row.linked_docname, self.invoice)

    def test_another_customers_payments_are_not_listed(self) -> None:
        """A customer's history lists only their own payments and invoices."""
        other = self.customer_with_contact(OTHER_CUSTOMER, OTHER_USER)
        invoice = self.invoice_for(other, rate=2000)
        log = self.payment_for(invoice, amount=2000)

        context = self.context_for(PORTAL_USER)

        self.assertNotIn(log, [row.name for row in context.payments])
        self.assertNotIn(invoice, [row.name for row in context.invoices])


class TestMyPaymentsDoctypes(MyPaymentsTestCase):
    """The history covers every document Paystack can be paid against."""

    def setUp(self) -> None:
        """Give the portal user a customer."""
        super().setUp()
        self.customer = self.customer_with_contact(PORTAL_CUSTOMER, PORTAL_USER)

    def order_for(self, customer: str) -> str:
        """Raise a submitted Sales Order for a customer."""
        order = SalesOrderFactory.create(rate=1000, customer=customer)
        self.addCleanup(SalesOrderFactory.cleanup, order)
        return order

    def pos_invoice_for(self, customer: str) -> str:
        """Raise a draft POS Invoice for a customer."""
        invoice = POSInvoiceFactory.create(customer=customer, mode_of_payment=None)
        self.addCleanup(POSInvoiceFactory.cleanup, invoice)
        return invoice

    def payment_for(self, doctype: str, docname: str) -> str:
        """Create a Payment Log against a document."""
        log = PaymentLogFactory.create(linked_doctype=doctype, linked_docname=docname, amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log)
        return log

    def payments_of(self, user: str) -> list:
        """Return the payment log names the page lists for a user."""
        return [row.name for row in self.context_for(user).payments]

    def test_a_sales_order_payment_is_listed(self) -> None:
        """A Sales Order payment reaches the customer's history."""
        log = self.payment_for("Sales Order", self.order_for(self.customer))

        self.assertIn(log, self.payments_of(PORTAL_USER))

    def test_a_pos_invoice_payment_is_listed(self) -> None:
        """A POS Invoice payment reaches the customer's history."""
        log = self.payment_for(POS_INVOICE, self.pos_invoice_for(self.customer))

        self.assertIn(log, self.payments_of(PORTAL_USER))

    def test_each_row_links_to_its_own_portal_page(self) -> None:
        """Each row links to ERPNext's customer route for its document."""
        order = self.order_for(self.customer)
        log = self.payment_for("Sales Order", order)

        row = next(row for row in self.context_for(PORTAL_USER).payments if row.name == log)
        self.assertEqual(row.route, "orders")

    def test_a_document_without_a_portal_page_is_not_linked(self) -> None:
        """A POS Invoice has no customer-facing page, so its row carries no link."""
        log = self.payment_for(POS_INVOICE, self.pos_invoice_for(self.customer))

        row = next(row for row in self.context_for(PORTAL_USER).payments if row.name == log)
        self.assertIsNone(row.route)

    def test_another_customers_orders_are_still_hidden(self) -> None:
        """Another customer's Sales Order and POS Invoice payments stay hidden."""
        other = self.customer_with_contact(OTHER_CUSTOMER, OTHER_USER)
        order_log = self.payment_for("Sales Order", self.order_for(other))
        pos_log = self.payment_for(POS_INVOICE, self.pos_invoice_for(other))

        listed = self.payments_of(PORTAL_USER)

        self.assertNotIn(order_log, listed)
        self.assertNotIn(pos_log, listed)

    def test_the_owning_customer_still_sees_them(self) -> None:
        """The other customer's own Sales Order payment is listed for them."""
        other = self.customer_with_contact(OTHER_CUSTOMER, OTHER_USER)
        order_log = self.payment_for("Sales Order", self.order_for(other))

        self.assertIn(order_log, self.payments_of(OTHER_USER))


class TestMyPaymentsRefunds(MyPaymentsTestCase):
    """The portal shows what came back as well as what went out."""

    def setUp(self) -> None:
        """Give the portal user a settled payment."""
        super().setUp()
        self.customer = self.customer_with_contact(PORTAL_CUSTOMER, PORTAL_USER)
        self.log = self.settled_payment(self.customer)

    def settled_payment(self, customer: str) -> str:
        """Raise a Completed payment against a customer's invoice."""
        invoice = SalesInvoiceFactory.create(rate=1000, customer=customer)
        self.addCleanup(SalesInvoiceFactory.cleanup, invoice)

        log = PaymentLogFactory.create(linked_docname=invoice, amount=1000, status="Pending")
        self.addCleanup(PaymentLogFactory.cleanup, log)
        frappe.db.set_value(
            "Paystack Payment Log",
            log,
            {"status": "Completed", "amount_paid": 1000, "currency_paid": "NGN"},
        )
        frappe.clear_document_cache("Paystack Payment Log", log)
        return log

    def refund_for(self, log: str, amount: float = 400) -> str:
        """Raise a refund against a payment."""
        refund = RefundLogFactory.create(payment_log_name=log, refund_amount=amount)
        self.addCleanup(RefundLogFactory.cleanup, refund)
        return refund

    def test_a_refund_against_a_listed_payment_is_shown(self) -> None:
        """A customer can see money that came back to their card."""
        refund = self.refund_for(self.log)

        refunds = self.context_for(PORTAL_USER).refunds

        self.assertIn(refund, [row.name for row in refunds])

    def test_no_refunds_leaves_the_section_empty(self) -> None:
        """A customer with no refunds gets an empty refund list."""
        self.assertEqual(self.context_for(PORTAL_USER).refunds, [])

    def test_a_customer_who_has_never_paid_is_asked_for_no_refunds(self) -> None:
        """A customer with no payments is listed no refunds."""
        self.customer_with_contact(OTHER_CUSTOMER, OTHER_USER)

        context = self.context_for(OTHER_USER)

        self.assertEqual(context.payments, [])
        self.assertEqual(context.refunds, [])

    def test_another_customers_refund_is_not_shown(self) -> None:
        """Refunds are scoped to the payments the page already lists."""
        other = self.customer_with_contact(OTHER_CUSTOMER, OTHER_USER)
        other_refund = self.refund_for(self.settled_payment(other))

        listed = [row.name for row in self.context_for(PORTAL_USER).refunds]

        self.assertNotIn(other_refund, listed)

    def test_the_owning_customer_still_sees_it(self) -> None:
        """The other customer's own refund is listed for them."""
        other = self.customer_with_contact(OTHER_CUSTOMER, OTHER_USER)
        other_refund = self.refund_for(self.settled_payment(other))

        self.assertIn(other_refund, [row.name for row in self.context_for(OTHER_USER).refunds])

    def test_a_settled_payment_offers_a_receipt(self) -> None:
        """Only a captured payment gets a download button."""
        row = next(row for row in self.context_for(PORTAL_USER).payments if row.name == self.log)

        self.assertTrue(row.has_receipt)

    def test_an_open_payment_offers_no_receipt(self) -> None:
        """A pending payment offers no receipt."""
        pending = PaymentLogFactory.create(
            linked_docname=SalesInvoiceFactory.create(rate=500, customer=self.customer),
            amount=500,
            status="Pending",
        )
        self.addCleanup(PaymentLogFactory.cleanup, pending)

        row = next(row for row in self.context_for(PORTAL_USER).payments if row.name == pending)

        self.assertFalse(row.has_receipt)


class TestPaymentReceiptDownload(MyPaymentsTestCase):
    """The receipt endpoint answers a customer only for their own payments."""

    def setUp(self) -> None:
        """Give two customers a settled payment each."""
        super().setUp()
        self.customer = self.customer_with_contact(PORTAL_CUSTOMER, PORTAL_USER)
        self.other = self.customer_with_contact(OTHER_CUSTOMER, OTHER_USER)

        for email in (PORTAL_USER, OTHER_USER, UNKNOWN_USER):
            self.website_user(email)

        self.log = self.settled_payment(self.customer)
        self.other_log = self.settled_payment(self.other)

    def website_user(self, email: str) -> None:
        """Create the portal login a receipt is rendered for."""
        if frappe.db.exists("User", email):
            return

        user = frappe.get_doc(
            {
                "doctype": "User",
                "email": email,
                "first_name": "Paystack Portal",
                "send_welcome_email": 0,
                "user_type": "Website User",
            }
        )
        user.flags.ignore_permissions = True
        user.insert()
        self.addCleanup(cleanup_user, email)

    def settled_payment(self, customer: str, status: str = "Completed") -> str:
        """Raise a payment against a customer's invoice in a given status."""
        invoice = SalesInvoiceFactory.create(rate=1000, customer=customer)
        self.addCleanup(SalesInvoiceFactory.cleanup, invoice)

        log = PaymentLogFactory.create(linked_docname=invoice, amount=1000, status="Pending")
        self.addCleanup(PaymentLogFactory.cleanup, log)
        frappe.db.set_value(
            "Paystack Payment Log",
            log,
            {"status": status, "amount_paid": 1000, "currency_paid": "NGN"},
        )
        frappe.clear_document_cache("Paystack Payment Log", log)
        return log

    def download_as(self, user: str, reference: str) -> None:
        """Call the receipt endpoint as a given user, with frappe.get_print stubbed."""
        frappe.session.user = user
        try:
            with patch("frappe.get_print", return_value=b"%PDF-1.4") as printed:
                self.printed = printed
                download_payment_receipt(reference)
        finally:
            frappe.session.user = self.session_user

    def test_a_customer_downloads_their_own_receipt(self) -> None:
        """A customer's own receipt is returned as a PDF response."""
        self.download_as(PORTAL_USER, self.log)

        self.assertEqual(frappe.local.response.type, "pdf")
        self.assertEqual(frappe.local.response.filename, f"{self.log}.pdf")
        self.assertEqual(frappe.local.response.filecontent, b"%PDF-1.4")

    def test_the_receipt_format_is_the_one_rendered(self) -> None:
        """The receipt renders through the Paystack Payment Receipt format."""
        self.download_as(PORTAL_USER, self.log)

        self.assertEqual(self.printed.call_args.kwargs["print_format"], "Paystack Payment Receipt")

    def test_the_owner_may_read_the_log_through_the_website(self) -> None:
        """Website permission on a Payment Log is granted to its owning customer."""
        frappe.session.user = PORTAL_USER
        try:
            self.assertTrue(frappe.has_website_permission(frappe.get_doc("Paystack Payment Log", self.log)))
            self.assertFalse(
                frappe.has_website_permission(frappe.get_doc("Paystack Payment Log", self.other_log))
            )
        finally:
            frappe.session.user = self.session_user

    def test_another_customers_receipt_is_refused(self) -> None:
        """Another customer's receipt raises a PermissionError."""
        with self.assertRaises(frappe.PermissionError):
            self.download_as(PORTAL_USER, self.other_log)

    def test_a_user_with_no_customer_is_refused(self) -> None:
        """A signed-in visitor who is nobody's contact gets no receipts."""
        with self.assertRaises(frappe.PermissionError):
            self.download_as(UNKNOWN_USER, self.log)

    def test_an_unpaid_payment_has_no_receipt(self) -> None:
        """A receipt is only issued once money has been captured."""
        pending = self.settled_payment(self.customer, status="Pending")

        with self.assertRaises(frappe.ValidationError):
            self.download_as(PORTAL_USER, pending)

    def test_an_unknown_reference_is_refused(self) -> None:
        """Nothing is rendered for a payment that does not exist."""
        with self.assertRaises(frappe.DoesNotExistError):
            self.download_as(PORTAL_USER, "PSLOG-DOES-NOT-EXIST")

    def test_a_payment_on_a_doctype_the_portal_does_not_show(self) -> None:
        """Ownership is resolved only through the doctypes the portal lists."""
        frappe.db.set_value("Paystack Payment Log", self.log, "linked_doctype", "Dunning")

        self.assertFalse(owns_payment_log(self.customer, self.log))


# The shopper PortalCustomerFactory builds.
SHOPPER_CUSTOMER = "_Test Customer Paystack Shopper"
SHOPPER_USER = "portal-shopper-paystack@example.com"
SHOPPER_CONTACT = "Paystack Portal Shopper"


class TestPortalCustomerFactory(PaystackTestCase):
    """The chain a webshop test signs in through."""

    def setUp(self) -> None:
        """Build the shopper and register its cleanup."""
        super().setUp()
        self.customer = PortalCustomerFactory.create(SHOPPER_CUSTOMER, SHOPPER_USER, SHOPPER_CONTACT)
        self.addCleanup(
            PortalCustomerFactory.cleanup,
            SHOPPER_CUSTOMER,
            SHOPPER_USER,
            SHOPPER_CONTACT,
        )

    def test_the_signed_in_user_resolves_to_the_customer(self) -> None:
        """The signed-in shopper resolves to the customer the factory built."""
        self.assertEqual(customer_for(SHOPPER_USER), self.customer)

    def test_the_login_can_sign_in(self) -> None:
        """The shopper's login is enabled and holds the Customer role."""
        user = frappe.get_doc("User", SHOPPER_USER)

        self.assertTrue(user.enabled)
        self.assertIn("Customer", [row.role for row in user.roles])

    def test_the_customer_has_the_address_the_cart_needs(self) -> None:
        """The shopper's customer carries the address a cart order needs."""
        self.assertTrue(
            frappe.db.exists(
                "Dynamic Link",
                {
                    "parenttype": "Address",
                    "link_doctype": "Customer",
                    "link_name": self.customer,
                },
            )
        )

    def test_a_second_call_reuses_the_records(self) -> None:
        """A second create() reuses the records it already built."""
        PortalCustomerFactory.create(SHOPPER_CUSTOMER, SHOPPER_USER, SHOPPER_CONTACT)

        self.assertEqual(frappe.db.count("Contact Email", {"email_id": SHOPPER_USER}), 1)

    def test_cleanup_leaves_no_contact_behind(self) -> None:
        """Cleanup removes the User, the Customer and the Contact email."""
        PortalCustomerFactory.cleanup(SHOPPER_CUSTOMER, SHOPPER_USER, SHOPPER_CONTACT)

        self.assertFalse(frappe.db.exists("User", SHOPPER_USER))
        self.assertFalse(frappe.db.exists("Customer", SHOPPER_CUSTOMER))
        self.assertEqual(frappe.db.count("Contact Email", {"email_id": SHOPPER_USER}), 0)
