from unittest.mock import patch

import frappe
from frappe.utils import flt
from payments.utils import get_payment_gateway_controller

from frappe_paystack.api import notify_payment_authorized
from frappe_paystack.frappe_paystack.doctype.paystack_payment_log.paystack_payment_log import (
    create_payment_entry_from_log,
)
from frappe_paystack.setup import (
    create_payment_gateway_account,
    create_payment_gateway_record,
    update_payment_gateway_controller,
)
from frappe_paystack.tests.factories import (
    CustomerFactory,
    GatewaySettingFactory,
    PaymentLogFactory,
    SalesInvoiceFactory,
    SalesOrderFactory,
    cleanup_doc,
    cleanup_linked_payment_entries,
)
from frappe_paystack.tests.test_base import PaystackTestCase
from frappe_paystack.utils import SUPPORTED_CURRENCIES, resolve_paystack_settings

VALIDATE_PAYMENT_PATCH = (
    "frappe_paystack.frappe_paystack.doctype.paystack_payment_log."
    "paystack_payment_log.PaystackPaymentLog.validate_payment"
)

# Currencies the acceptance check is offered, supported ones first.
PROBE_CURRENCIES = SUPPORTED_CURRENCIES + ["EUR", "GBP", "JPY"]

ERROR_LOG = "Error Log"


def accepts_currency(setting, currency: str) -> bool:
    """Report whether the controller lets a charge in this currency through."""
    try:
        setting.validate_transaction_currency(currency)
    except frappe.ValidationError:
        return False

    return True


class TestPaymentGatewayRegistration(PaystackTestCase):
    """Paystack registration as a standard Payment Gateway."""

    def setUp(self):
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

    def test_payment_gateway_record_created(self):
        """create_payment_gateway_record creates a Payment Gateway record with the right settings doctype."""
        create_payment_gateway_record()

        self.assertTrue(frappe.db.exists("Payment Gateway", "Paystack"))
        pg = frappe.get_doc("Payment Gateway", "Paystack")
        self.assertEqual(pg.gateway_settings, "Paystack Gateway Setting")

    def test_update_payment_gateway_controller(self):
        """update_payment_gateway_controller sets the controller to the gateway setting record."""
        create_payment_gateway_record()
        update_payment_gateway_controller(self.gateway_name)

        pg = frappe.get_doc("Payment Gateway", "Paystack")
        self.assertEqual(pg.gateway_controller, self.gateway_name)

    def test_get_payment_gateway_controller_returns_setting(self):
        """get_payment_gateway_controller('Paystack') returns the PaystackGatewaySetting document."""

        create_payment_gateway_record()
        update_payment_gateway_controller(self.gateway_name)

        controller = get_payment_gateway_controller("Paystack")
        self.assertEqual(controller.doctype, "Paystack Gateway Setting")
        self.assertEqual(controller.name, self.gateway_name)


class TestPaymentGatewayAccount(PaystackTestCase):
    """Auto-creation of Payment Gateway Account."""

    def setUp(self):
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)
        create_payment_gateway_record()

    def test_create_payment_gateway_account(self):
        """create_payment_gateway_account creates a Payment Gateway Account linked to Paystack."""
        settings = resolve_paystack_settings("_Test Company")

        pga_name = create_payment_gateway_account(
            company="_Test Company",
            suspense_account=settings.get("suspense_account")
            or frappe.db.get_value(
                "Account",
                {"company": "_Test Company", "is_group": 0},
                "name",
            ),
            currency="NGN",
        )

        self.assertTrue(frappe.db.exists("Payment Gateway Account", pga_name))
        pga = frappe.get_doc("Payment Gateway Account", pga_name)
        self.assertEqual(pga.payment_gateway, "Paystack")
        self.assertEqual(pga.company, "_Test Company")

        frappe.delete_doc(
            "Payment Gateway Account",
            pga_name,
            force=True,
            ignore_permissions=True,
        )

    def test_create_payment_gateway_account_updates_existing(self):
        """Calling create_payment_gateway_account twice returns the same account."""
        settings = resolve_paystack_settings("_Test Company")
        suspense = settings.get("suspense_account") or frappe.db.get_value(
            "Account",
            {"company": "_Test Company", "is_group": 0},
            "name",
        )

        pga_name1 = create_payment_gateway_account(
            company="_Test Company",
            suspense_account=suspense,
            currency="NGN",
        )
        self.addCleanup(
            lambda: (
                frappe.delete_doc(
                    "Payment Gateway Account",
                    pga_name1,
                    force=True,
                    ignore_permissions=True,
                )
                if frappe.db.exists("Payment Gateway Account", pga_name1)
                else None
            )
        )

        pga_name2 = create_payment_gateway_account(
            company="_Test Company",
            suspense_account=suspense,
            currency="NGN",
        )

        self.assertEqual(pga_name1, pga_name2)


class TestPaystackControllerInterface(PaystackTestCase):
    """The PaystackGatewaySetting controller interface used by the payments app."""

    def setUp(self):
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

    def test_validate_transaction_currency_accepts_supported(self):
        """The controller accepts the supported currencies and no others."""
        setting = frappe.get_doc("Paystack Gateway Setting", self.gateway_name)

        accepted = [currency for currency in PROBE_CURRENCIES if accepts_currency(setting, currency)]

        self.assertEqual(accepted, SUPPORTED_CURRENCIES)

    def test_validate_transaction_currency_throws_for_unsupported(self):
        """The controller throws for unsupported currencies."""
        setting = frappe.get_doc("Paystack Gateway Setting", self.gateway_name)
        with self.assertRaises(frappe.ValidationError):
            setting.validate_transaction_currency("EUR")

    def test_on_payment_request_submission_admits_a_supported_currency(self):
        """on_payment_request_submission returns True for a currency Paystack charges."""
        setting = frappe.get_doc("Paystack Gateway Setting", self.gateway_name)
        request = frappe._dict(currency=setting.supported_currencies[0])
        self.assertTrue(setting.on_payment_request_submission(request))

    def test_on_payment_request_submission_refuses_an_unsupported_currency(self):
        """on_payment_request_submission returns False for a currency Paystack cannot charge."""
        setting = frappe.get_doc("Paystack Gateway Setting", self.gateway_name)
        request = frappe._dict(currency="EUR")
        self.assertFalse(setting.on_payment_request_submission(request))

    @patch(VALIDATE_PAYMENT_PATCH)
    def test_get_payment_url_creates_payment_log(self, mock_vp):
        """get_payment_url creates a Paystack Payment Log and returns a checkout URL."""

        mock_vp.return_value = {"status": True, "data": {"status": "success"}}

        sinv_name = SalesInvoiceFactory.create(rate=5000)
        self.addCleanup(SalesInvoiceFactory.cleanup, sinv_name)

        setting = frappe.get_doc("Paystack Gateway Setting", self.gateway_name)

        url = setting.get_payment_url(
            amount=5000,
            title="_Test Company",
            description="Payment for invoice",
            reference_doctype="Sales Invoice",
            reference_docname=sinv_name,
            payer_email="test@example.com",
            payer_name="Test Customer",
            order_id="PR-001",
            currency="NGN",
            payment_gateway="Paystack",
        )

        self.assertIn("/paystack-checkout/", url)

        log_name = url.split("/paystack-checkout/")[-1]
        self.assertTrue(frappe.db.exists("Paystack Payment Log", log_name))

        log = frappe.get_doc("Paystack Payment Log", log_name)
        self.assertEqual(log.linked_doctype, "Sales Invoice")
        self.assertEqual(log.linked_docname, sinv_name)
        self.assertEqual(log.amount, 5000)
        self.assertEqual(log.status, "Pending")

        PaymentLogFactory.cleanup(log_name)


class TestPaymentRequestBridge(PaystackTestCase):
    """The bridge that drives Payment Request status on a successful payment."""

    def setUp(self):
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

    @patch(VALIDATE_PAYMENT_PATCH)
    def test_payment_entry_settles_the_payment_request(self, mock_vp):
        """Submitting the Payment Entry drives the Payment Request to Paid."""

        mock_vp.return_value = {"status": True, "data": {"status": "success"}}

        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        pr_name = self.create_test_payment_request(log_name)

        frappe.db.set_value(
            "Paystack Payment Log",
            log_name,
            {
                "status": "Processed",
                "amount_paid": 1000,
                "payment_reference": f"ref-{log_name}",
                "payment_date": frappe.utils.today(),
            },
        )
        frappe.clear_document_cache("Paystack Payment Log", log_name)
        create_payment_entry_from_log(log_name)

        pe_name = frappe.db.get_value("Paystack Payment Log", log_name, "payment_entry")
        self.assertTrue(pe_name, "no Payment Entry was created")
        self.addCleanup(cleanup_doc, "Payment Entry", pe_name)

        self.assertEqual(frappe.db.get_value("Payment Request", pr_name, "status"), "Paid")

    @patch(VALIDATE_PAYMENT_PATCH)
    def test_shopping_cart_order_is_billed(self, mock_vp):
        """A paid cart order is invoiced and reads as fully billed."""
        mock_vp.return_value = {"status": True, "data": {"status": "success"}}

        customer = "_Test Cart Customer"
        CustomerFactory.create(customer_name=customer)
        self.addCleanup(CustomerFactory.cleanup, customer)

        order = SalesOrderFactory.create(rate=1000, order_type="Shopping Cart", customer=customer)
        self.addCleanup(SalesOrderFactory.cleanup, order)

        log_name = PaymentLogFactory.create(
            status="Pending",
            amount=1000,
            linked_doctype="Sales Order",
            linked_docname=order,
        )
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        pr_name = self.create_test_payment_request(log_name)

        frappe.db.set_value(
            "Paystack Payment Log",
            log_name,
            {"status": "Processed", "amount_paid": 1000},
        )
        frappe.clear_document_cache("Paystack Payment Log", log_name)
        create_payment_entry_from_log(log_name)

        invoice = frappe.db.get_value("Sales Invoice Item", {"sales_order": order, "docstatus": 1}, "parent")
        self.assertTrue(invoice, "Sales Order was paid but never invoiced")
        # Payment Entries are torn down ahead of the invoice and the order.
        self.addCleanup(cleanup_doc, "Sales Invoice", invoice)
        self.addCleanup(cleanup_linked_payment_entries, "Sales Invoice", invoice)
        self.addCleanup(cleanup_linked_payment_entries, "Sales Order", order)
        self.assertEqual(flt(frappe.db.get_value("Sales Order", order, "per_billed")), 100.0)
        self.assertEqual(frappe.db.get_value("Payment Request", pr_name, "status"), "Paid")

    def test_notify_payment_authorized_is_safe_for_missing_reference(self):
        """A log naming a Payment Request that is gone books nothing and alerts nobody."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        frappe.db.set_value("Paystack Payment Log", log_name, "payment_request", "ACC-PRQ-GONE-999")
        frappe.clear_document_cache("Paystack Payment Log", log_name)

        alerts = frappe.db.count(ERROR_LOG)

        notify_payment_authorized(frappe.get_doc("Paystack Payment Log", log_name))

        record = frappe.db.get_value(
            "Paystack Payment Log", log_name, ["payment_entry", "errors", "status"], as_dict=True
        )
        self.assertFalse(record.payment_entry)
        self.assertFalse(record.errors)
        self.assertEqual(record.status, "Pending")
        self.assertEqual(frappe.db.count(ERROR_LOG), alerts)
        self.assertEqual(frappe.session.user, "Administrator")

    def create_test_payment_request(self, log_name: str) -> str:
        """Create a minimal Payment Request for testing."""

        create_payment_gateway_record()

        # Party validation reads the gateway account as a Bank or Cash account.
        suspense = frappe.db.get_value(
            "Account",
            {
                "company": "_Test Company",
                "is_group": 0,
                "account_type": ["in", ["Bank", "Cash"]],
            },
            "name",
        )

        pga_name = create_payment_gateway_account(
            company="_Test Company",
            suspense_account=suspense,
            currency="NGN",
        )
        self.addCleanup(
            lambda: (
                frappe.delete_doc(
                    "Payment Gateway Account",
                    pga_name,
                    force=True,
                    ignore_permissions=True,
                )
                if frappe.db.exists("Payment Gateway Account", pga_name)
                else None
            )
        )

        linked_doctype = frappe.db.get_value("Paystack Payment Log", log_name, "linked_doctype")
        linked_docname = frappe.db.get_value("Paystack Payment Log", log_name, "linked_docname")

        pr = frappe.get_doc(
            {
                "doctype": "Payment Request",
                "payment_gateway_account": pga_name,
                "payment_gateway": "Paystack",
                "reference_doctype": linked_doctype,
                "reference_name": linked_docname,
                "payment_request_type": "Inward",
                "grand_total": 1000,
                "currency": "NGN",
                "email_to": "test@example.com",
                # on_submit files a Communication, whose subject is mandatory.
                "subject": "Test payment request",
                "message": "Please pay via Paystack.",
                "mute_email": 1,
            }
        )
        pr.flags.ignore_permissions = True
        pr.flags.ignore_mandatory = True
        pr.insert()
        # Torn down ahead of the invoice the Payment Request links to.
        self.addCleanup(cleanup_doc, "Payment Request", pr.name)

        # on_submit emails the request, which renders a PDF over the network.
        with patch("erpnext.accounts.doctype.payment_request.payment_request.PaymentRequest.send_email"):
            pr.submit()

        # Submitting creates a Payment Log of its own through the gateway controller.
        for extra in frappe.get_all(
            "Paystack Payment Log", filters={"payment_request": pr.name}, pluck="name"
        ):
            self.addCleanup(cleanup_doc, "Paystack Payment Log", extra)

        log = frappe.get_doc("Paystack Payment Log", log_name)
        log.db_set("payment_request", pr.name)

        return pr.name


class TestPaymentUrlIsIdempotent(PaystackTestCase):
    """ERPNext asks for the URL twice per checkout and gets one link."""

    def setUp(self) -> None:
        """Enable Paystack for the test company."""
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

    def build_payment_request(self, invoice: str) -> str:
        """Create a Payment Request against an invoice."""
        pr = frappe.get_doc(
            {
                "doctype": "Payment Request",
                "payment_gateway": "Paystack",
                "reference_doctype": "Sales Invoice",
                "reference_name": invoice,
                "payment_request_type": "Inward",
                "grand_total": 1000,
                "currency": "NGN",
                "email_to": "test@example.com",
            }
        )
        pr.flags.ignore_permissions = True
        pr.flags.ignore_mandatory = True
        pr.flags.ignore_validate = True
        pr.insert()
        self.addCleanup(cleanup_doc, "Payment Request", pr.name)
        return pr.name

    def request_url(self, pr_name: str, invoice: str) -> str:
        """Ask the controller for a checkout URL the way ERPNext does."""
        setting = frappe.get_doc("Paystack Gateway Setting", self.gateway_name)
        return setting.get_payment_url(
            amount=1000,
            currency="NGN",
            reference_doctype="Payment Request",
            reference_docname=pr_name,
        )

    def test_repeat_calls_reuse_one_payment_log(self) -> None:
        """A second call returns the same link, over a single Payment Log."""
        invoice = SalesInvoiceFactory.create(rate=1000)
        self.addCleanup(SalesInvoiceFactory.cleanup, invoice)
        pr_name = self.build_payment_request(invoice)

        first = self.request_url(pr_name, invoice)
        second = self.request_url(pr_name, invoice)

        self.assertEqual(first, second)

        logs = frappe.get_all("Paystack Payment Log", filters={"payment_request": pr_name}, pluck="name")
        for log in logs:
            self.addCleanup(cleanup_doc, "Paystack Payment Log", log)
        self.assertEqual(len(logs), 1)

    def test_the_link_resolves_to_a_real_log(self) -> None:
        """The redirect URL names a Payment Log that exists."""
        invoice = SalesInvoiceFactory.create(rate=1000)
        self.addCleanup(SalesInvoiceFactory.cleanup, invoice)
        pr_name = self.build_payment_request(invoice)

        url = self.request_url(pr_name, invoice)
        reference = url.rstrip("/").split("/")[-1]
        self.addCleanup(cleanup_doc, "Paystack Payment Log", reference)

        self.assertTrue(frappe.db.exists("Paystack Payment Log", reference))


class TestPaymentUrlSurvivesGetRequest(PaystackTestCase):
    """Shopping Cart asks for the URL over GET, which Frappe rolls back."""

    def setUp(self) -> None:
        """Enable Paystack and clear the commit flag."""
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)
        frappe.local.flags.commit = False

    def test_new_log_marks_the_request_for_commit(self) -> None:
        """Creating a log sets frappe.local.flags.commit on the request."""
        invoice = SalesInvoiceFactory.create(rate=1000)
        self.addCleanup(SalesInvoiceFactory.cleanup, invoice)

        setting = frappe.get_doc("Paystack Gateway Setting", self.gateway_name)
        url = setting.get_payment_url(
            amount=1000,
            currency="NGN",
            reference_doctype="Sales Invoice",
            reference_docname=invoice,
        )
        self.addCleanup(cleanup_doc, "Paystack Payment Log", url.rsplit("/", 1)[-1])

        self.assertTrue(frappe.local.flags.commit)
