"""Tests for the Paystack POS channels: the phone charge and the email link."""

from unittest.mock import patch

import frappe
from frappe.integrations.utils import create_request_log

from frappe_paystack.api import notify_pos_payment
from frappe_paystack.frappe_paystack.doctype.paystack_payment_log.paystack_payment_log import (
    settle_pos_invoice,
)
from frappe_paystack.setup import (
    MODE_OF_PAYMENT,
    PAYMENT_TYPES,
    ensure_email_payment_type,
    ensure_mode_of_payment,
    ensure_pos_invoice_fields,
    set_gateway_account_channel,
    setup_pos_payment_mode,
)
from frappe_paystack.tests.factories import (
    GatewaySettingFactory,
    PaymentLogFactory,
    POSInvoiceFactory,
    cleanup_user,
)
from frappe_paystack.tests.test_base import PaystackTestCase
from frappe_paystack.utils import SUPPORTED_CURRENCIES
from frappe_paystack.utils.pos_payment import (
    build_payment_log,
    notify_pos_link_paid,
    paystack_tender_amount,
    pos_payment_status,
    render_email,
    resolve_email,
    send_pos_payment_link,
)

TEST_COMPANY = "_Test Company"
TEST_CUSTOMER = "_Test Customer"
TEST_ITEM = "_Test Item Home Products 100"
PAYMENT_GATEWAY_ACCOUNT = "Payment Gateway Account"
POS_INVOICE = "POS Invoice"
PAYMENT_LOG = "Paystack Payment Log"

# A scratch Mode of Payment carrying no account rows.
SCRATCH_MODE_OF_PAYMENT = "Paystack POS Test Mode"

# A company the site has no Paystack Payment Gateway Account for.
COMPANY_WITHOUT_GATEWAY = "Paystack Test Company Without A Gateway"

# The stand-in POS Invoice every phone-charge Payment Request points at.
POS_INVOICE_REFERENCE = "POS-TEST-001"

CHECKOUT_URL = "https://checkout.paystack.com/abc123"


class PosTestCase(PaystackTestCase):
    """Shared setup for POS tests."""

    def setUp(self) -> None:
        """Enable Paystack for the test company."""
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)


class TestPosPaymentMode(PosTestCase):
    """setup_pos_payment_mode wires the counter up."""

    def test_mode_of_payment_is_typed_phone(self) -> None:
        """A Phone channel types the Mode of Payment as Phone."""
        setup_pos_payment_mode(TEST_COMPANY, self.suspense_account(), channel="Phone")

        self.assertEqual(frappe.db.get_value("Mode of Payment", MODE_OF_PAYMENT, "type"), "Phone")

    def test_mode_of_payment_can_be_typed_email(self) -> None:
        """An Email channel types the Mode of Payment as Email."""
        setup_pos_payment_mode(TEST_COMPANY, self.suspense_account(), channel="Email")

        self.assertEqual(frappe.db.get_value("Mode of Payment", MODE_OF_PAYMENT, "type"), "Email")

    def test_gateway_account_channel_follows_the_mode(self) -> None:
        """The gateway account's payment_channel follows the mode's channel."""
        setup_pos_payment_mode(TEST_COMPANY, self.suspense_account(), channel="Email")

        self.assertEqual(
            frappe.db.get_value(
                "Payment Gateway Account",
                {"payment_gateway": "Paystack", "company": TEST_COMPANY},
                "payment_channel",
            ),
            "Email",
        )

    def test_unsupported_channel_is_rejected(self) -> None:
        """A channel outside Phone and Email is refused."""
        with self.assertRaises(frappe.ValidationError):
            setup_pos_payment_mode(TEST_COMPANY, self.suspense_account(), channel="Carrier Pigeon")

    def test_account_matches_the_gateway_account(self) -> None:
        """The mode's account row for the company holds the suspense account."""
        suspense = self.suspense_account()
        setup_pos_payment_mode(TEST_COMPANY, suspense)

        mop = frappe.get_doc("Mode of Payment", MODE_OF_PAYMENT)
        accounts = {row.company: row.default_account for row in mop.accounts}
        self.assertEqual(accounts.get(TEST_COMPANY), suspense)

    def delete_scratch_mode(self) -> None:
        """Remove the Mode of Payment this test created."""
        frappe.delete_doc(
            "Mode of Payment",
            SCRATCH_MODE_OF_PAYMENT,
            force=True,
            ignore_permissions=True,
        )
        frappe.clear_document_cache("Mode of Payment", SCRATCH_MODE_OF_PAYMENT)
        frappe.db.commit()

    def test_a_mode_with_no_row_for_the_company_gets_one(self) -> None:
        """A mode with no account row for the company gains one."""
        self.addCleanup(self.delete_scratch_mode)
        suspense = self.suspense_account()

        with patch("frappe_paystack.setup.MODE_OF_PAYMENT", SCRATCH_MODE_OF_PAYMENT):
            setup_pos_payment_mode(TEST_COMPANY, suspense)

        mop = frappe.get_doc("Mode of Payment", SCRATCH_MODE_OF_PAYMENT)
        accounts = {row.company: row.default_account for row in mop.accounts}
        self.assertEqual(accounts, {TEST_COMPANY: suspense})

    def test_a_company_without_a_gateway_account_is_left_alone(self) -> None:
        """A channel set for a company with no gateway account changes no rows."""
        before = frappe.get_all(PAYMENT_GATEWAY_ACCOUNT, fields=["name", "payment_channel"], order_by="name")

        set_gateway_account_channel(COMPANY_WITHOUT_GATEWAY, "Phone")

        self.assertEqual(
            frappe.get_all(
                PAYMENT_GATEWAY_ACCOUNT,
                fields=["name", "payment_channel"],
                order_by="name",
            ),
            before,
        )

    def test_setup_is_idempotent(self) -> None:
        """Running setup twice does not duplicate the account row."""
        suspense = self.suspense_account()
        setup_pos_payment_mode(TEST_COMPANY, suspense)
        setup_pos_payment_mode(TEST_COMPANY, suspense)

        mop = frappe.get_doc("Mode of Payment", MODE_OF_PAYMENT)
        rows = [row for row in mop.accounts if row.company == TEST_COMPANY]
        self.assertEqual(len(rows), 1)

    def test_default_mode_of_payment_is_not_phone(self) -> None:
        """ensure_mode_of_payment types the mode as something other than Phone."""
        self.restore_mode_of_payment()
        ensure_mode_of_payment()

        self.assertNotEqual(frappe.db.get_value("Mode of Payment", MODE_OF_PAYMENT, "type"), "Phone")

    def test_pos_invoice_fields_are_registered(self) -> None:
        """POS Settings carries contact_mobile and request_for_payment fields."""
        ensure_pos_invoice_fields()

        fields = {row.fieldname for row in frappe.get_single("POS Settings").invoice_fields}
        self.assertIn("contact_mobile", fields)
        self.assertIn("request_for_payment", fields)


class TestRequestForPayment(PosTestCase):
    """The controller hook ERPNext calls for a phone payment."""

    def build_payment_request(self) -> str:
        """Create a minimal Payment Request to charge against."""
        pr = frappe.get_doc(
            {
                "doctype": "Payment Request",
                "payment_gateway": "Paystack",
                "reference_doctype": "POS Invoice",
                "reference_name": POS_INVOICE_REFERENCE,
                "payment_request_type": "Inward",
                "company": TEST_COMPANY,
                "grand_total": 500,
                "currency": "NGN",
                "email_to": "customer@example.com",
            }
        )
        pr.flags.ignore_permissions = True
        pr.flags.ignore_mandatory = True
        pr.flags.ignore_links = True
        # The reference POS Invoice is a stand-in.
        pr.flags.ignore_validate = True
        pr.insert()
        self.addCleanup(frappe.delete_doc, "Payment Request", pr.name, force=True)
        return pr.name

    def test_charge_is_requested_and_logged(self) -> None:
        """A successful charge records a completed Integration Request."""
        pr_name = self.build_payment_request()
        setting = frappe.get_doc("Paystack Gateway Setting", self.gateway_name)

        with patch(
            "frappe_paystack.frappe_paystack.doctype.paystack_gateway_setting"
            ".paystack_gateway_setting.request_pos_charge"
        ) as mock_charge:
            mock_charge.return_value = {"status": True, "data": {"reference": "x"}}
            setting.request_for_payment(
                reference_doctype="Payment Request",
                reference_docname=pr_name,
                request_amount=500,
                currency="NGN",
            )

        self.assertTrue(mock_charge.called)
        self.assertTrue(
            frappe.db.exists(
                "Integration Request",
                {"reference_docname": pr_name, "status": "Completed"},
            )
        )

    def charge(self, response: dict) -> list:
        """Run a charge with a stubbed response, returning what was published."""
        pr_name = self.build_payment_request()
        setting = frappe.get_doc("Paystack Gateway Setting", self.gateway_name)

        with (
            patch(
                "frappe_paystack.frappe_paystack.doctype.paystack_gateway_setting"
                ".paystack_gateway_setting.request_pos_charge"
            ) as mock_charge,
            patch("frappe.publish_realtime") as mock_publish,
        ):
            mock_charge.return_value = response
            setting.request_for_payment(
                reference_doctype="Payment Request",
                reference_docname=pr_name,
                request_amount=500,
                currency="NGN",
            )

        self.charged_request = pr_name
        return [
            call
            for call in mock_publish.call_args_list
            if call.args and call.args[0] == "paystack_pos_charge_url"
        ]

    def test_the_checkout_url_reaches_the_cashier(self) -> None:
        """The checkout URL is published on the paystack_pos_charge_url channel."""
        published = self.charge(
            {
                "status": True,
                "data": {
                    "reference": "x",
                    "authorization_url": CHECKOUT_URL,
                },
            }
        )

        self.assertEqual(len(published), 1)
        message = published[0].args[1]
        self.assertEqual(message["url"], CHECKOUT_URL)
        self.assertEqual(message["pos_invoice"], POS_INVOICE_REFERENCE)
        self.assertEqual(message["payment_request"], self.charged_request)

    def test_a_response_carrying_no_url_publishes_nothing(self) -> None:
        """A response carrying no URL publishes nothing."""
        self.assertEqual(self.charge({"status": True, "data": {"reference": "x"}}), [])

    def test_a_response_carrying_no_data_publishes_nothing(self) -> None:
        """A response carrying no data object publishes nothing."""
        self.assertEqual(self.charge({"status": True}), [])

    def test_failed_charge_is_recorded_and_raised(self) -> None:
        """A gateway failure marks the Integration Request and propagates."""
        pr_name = self.build_payment_request()
        setting = frappe.get_doc("Paystack Gateway Setting", self.gateway_name)

        with patch(
            "frappe_paystack.frappe_paystack.doctype.paystack_gateway_setting"
            ".paystack_gateway_setting.request_pos_charge"
        ) as mock_charge:
            mock_charge.side_effect = Exception("gateway down")

            with self.assertRaises(Exception):
                setting.request_for_payment(
                    reference_doctype="Payment Request",
                    reference_docname=pr_name,
                    request_amount=500,
                    currency="NGN",
                )

        self.assertTrue(
            frappe.db.exists(
                "Integration Request",
                {"reference_docname": pr_name, "status": "Failed"},
            )
        )


class TestNotifyPosPayment(PaystackTestCase):
    """The webhook releases the POS screen via realtime."""

    def build_integration_request(self, reference_docname: str) -> str:
        """Create an Integration Request standing in for a POS charge."""
        ir = create_request_log(
            {},
            service_name="Paystack",
            status="Queued",
            reference_doctype="Payment Request",
            reference_docname=reference_docname,
        )
        self.addCleanup(frappe.delete_doc, "Integration Request", ir.name, force=True)
        return ir.name

    def build_payment_request(self, pos_invoice: str) -> str:
        """Create a Payment Request pointing at a POS Invoice."""
        pr = frappe.get_doc(
            {
                "doctype": "Payment Request",
                "payment_gateway": "Paystack",
                "reference_doctype": "POS Invoice",
                "reference_name": pos_invoice,
                "payment_request_type": "Inward",
                "grand_total": 500,
                "currency": "NGN",
                "email_to": "customer@example.com",
            }
        )
        pr.flags.ignore_permissions = True
        pr.flags.ignore_mandatory = True
        pr.flags.ignore_links = True
        # The reference POS Invoice is a stand-in.
        pr.flags.ignore_validate = True
        pr.insert()
        self.addCleanup(frappe.delete_doc, "Payment Request", pr.name, force=True)
        return pr.name

    def test_success_publishes_to_the_pos_invoice(self) -> None:
        """A settled charge tells POS which invoice succeeded."""
        pr_name = self.build_payment_request("POS-TEST-100")
        reference = self.build_integration_request(pr_name)

        with patch("frappe.publish_realtime") as mock_publish:
            notify_pos_payment(reference, 500, True)

        self.assertTrue(mock_publish.called)
        kwargs = mock_publish.call_args.kwargs
        self.assertEqual(kwargs["event"], "process_phone_payment")
        self.assertEqual(kwargs["docname"], "POS-TEST-100")
        self.assertTrue(kwargs["message"]["success"])

    def test_failure_carries_the_reason(self) -> None:
        """A declined charge passes the gateway message to the cashier."""
        pr_name = self.build_payment_request("POS-TEST-101")
        reference = self.build_integration_request(pr_name)

        with patch("frappe.publish_realtime") as mock_publish:
            notify_pos_payment(reference, 500, False, "Insufficient funds")

        message = mock_publish.call_args.kwargs["message"]
        self.assertFalse(message["success"])
        self.assertEqual(message["failure_message"], "Insufficient funds")

    def test_unknown_reference_is_ignored(self) -> None:
        """A reference with no Integration Request publishes nothing."""
        with patch("frappe.publish_realtime") as mock_publish:
            notify_pos_payment("NOT-AN-INTEGRATION-REQUEST", 500, True)

        mock_publish.assert_not_called()

    def test_non_payment_request_reference_is_ignored(self) -> None:
        """An Integration Request that is not a POS charge is skipped."""
        ir = create_request_log({}, service_name="Paystack", status="Queued")
        self.addCleanup(frappe.delete_doc, "Integration Request", ir.name, force=True)

        with patch("frappe.publish_realtime") as mock_publish:
            notify_pos_payment(ir.name, 500, True)

        mock_publish.assert_not_called()


class TestEmailPaymentType(PaystackTestCase):
    """Mode of Payment offers Email alongside the core types."""

    def test_property_setter_adds_email(self) -> None:
        """The Email option is added to the core Select."""
        ensure_email_payment_type()

        options = frappe.get_meta("Mode of Payment", cached=False).get_field("type").options
        self.assertIn("Email", options.split("\n"))

    def test_core_types_are_preserved(self) -> None:
        """The built-in types remain among the options."""
        ensure_email_payment_type()

        options = frappe.get_meta("Mode of Payment", cached=False).get_field("type").options
        for kind in ("Cash", "Bank", "General", "Phone"):
            self.assertIn(kind, options.split("\n"))

    def test_is_idempotent(self) -> None:
        """Running it twice leaves the options equal to PAYMENT_TYPES."""
        ensure_email_payment_type()
        ensure_email_payment_type()

        options = frappe.get_meta("Mode of Payment", cached=False).get_field("type").options
        self.assertEqual(options, PAYMENT_TYPES)


class TestTenderAmount(PaystackTestCase):
    """paystack_tender_amount reads what Paystack is collecting."""

    def build_invoice(self, rows) -> object:
        """Return an unsaved POS Invoice carrying tender rows."""
        invoice = frappe.new_doc("POS Invoice")
        invoice.customer = TEST_CUSTOMER
        for mode, kind, amount in rows:
            invoice.append("payments", {"mode_of_payment": mode, "type": kind, "amount": amount})
        return invoice

    def test_sums_the_paystack_row(self) -> None:
        """The amount on the Paystack tender row is returned."""
        invoice = self.build_invoice([("Cash", "Cash", 30), ("Paystack", "Email", 70)])

        self.assertEqual(paystack_tender_amount(invoice), 70)

    def test_returns_zero_without_a_paystack_row(self) -> None:
        """An invoice with no Paystack tender row returns zero."""
        invoice = self.build_invoice([("Cash", "Cash", 100)])

        self.assertEqual(paystack_tender_amount(invoice), 0)


class TestEmailResolution(PaystackTestCase):
    """resolve_email decides where the link is sent."""

    def build_invoice(self, contact_email: str = "") -> object:
        """Return an unsaved POS Invoice carrying a customer."""
        invoice = frappe.new_doc("POS Invoice")
        invoice.customer = TEST_CUSTOMER
        invoice.contact_email = contact_email
        return invoice

    def set_customer_email(self, value) -> None:
        """Point the test customer at an address for the duration."""
        self.addCleanup(
            self.restore_customer_email,
            frappe.db.get_value("Customer", TEST_CUSTOMER, "email_id"),
        )
        frappe.db.set_value("Customer", TEST_CUSTOMER, "email_id", value)

    def restore_customer_email(self, value) -> None:
        """Give the customer back the address it carried before the test."""
        if not frappe.db.exists("Customer", TEST_CUSTOMER):
            return

        frappe.db.set_value("Customer", TEST_CUSTOMER, "email_id", value)
        frappe.db.commit()

    def test_explicit_argument_wins(self) -> None:
        """An explicitly passed address takes precedence."""
        self.set_customer_email("default@example.com")
        invoice = self.build_invoice(contact_email="field@example.com")

        self.assertEqual(resolve_email(invoice, "typed@example.com"), "typed@example.com")

    def test_invoice_field_beats_the_customer_default(self) -> None:
        """The invoice's contact_email takes precedence over the customer's."""
        self.set_customer_email("default@example.com")
        invoice = self.build_invoice(contact_email="field@example.com")

        self.assertEqual(resolve_email(invoice), "field@example.com")

    def test_falls_back_to_the_customer(self) -> None:
        """An empty invoice field uses the customer's address."""
        self.set_customer_email("default@example.com")

        self.assertEqual(resolve_email(self.build_invoice()), "default@example.com")

    def test_no_address_anywhere_throws(self) -> None:
        """No address on the invoice or the customer throws."""
        self.set_customer_email(None)

        with self.assertRaises(frappe.ValidationError):
            resolve_email(self.build_invoice())


class PosInvoiceTestCase(PaystackTestCase):
    """A live Paystack gateway and a draft POS Invoice to charge against."""

    def setUp(self) -> None:
        """Enable Paystack for the test company and open a till sale."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)
        self.invoice = frappe.get_doc(POS_INVOICE, POSInvoiceFactory.create())
        self.addCleanup(POSInvoiceFactory.cleanup, self.invoice.name)

    def track_log(self, name: str) -> str:
        """Register a Payment Log for cleanup and return its name."""
        self.addCleanup(PaymentLogFactory.cleanup, name)
        return name


class TestBuildPaymentLog(PosInvoiceTestCase):
    """build_payment_log raises the log backing a POS charge."""

    def test_log_points_at_the_draft_invoice(self) -> None:
        """The log is linked to the draft invoice and opens Pending."""
        log = build_payment_log(self.invoice, 1000)
        self.track_log(log.name)

        self.assertEqual(log.linked_doctype, POS_INVOICE)
        self.assertEqual(log.linked_docname, self.invoice.name)
        self.assertEqual(log.status, "Pending")
        self.assertEqual(frappe.db.get_value(POS_INVOICE, self.invoice.name, "docstatus"), 0)

    def test_the_log_charges_what_the_tender_asks_for(self) -> None:
        """The log carries the tendered amount and the invoice's company."""
        log = build_payment_log(self.invoice, 400)
        self.track_log(log.name)

        self.assertEqual(log.amount, 400)
        self.assertEqual(log.company, TEST_COMPANY)

    def test_the_currency_is_one_paystack_can_charge(self) -> None:
        """The sale's currency is passed through normalize_currency."""
        log = build_payment_log(self.invoice, 1000)
        self.track_log(log.name)

        self.assertIn(log.currency, SUPPORTED_CURRENCIES)

    def test_disabled_gateway_is_rejected(self) -> None:
        """A company without Paystack cannot raise a payment link."""
        self.disable_company_gateways(TEST_COMPANY)

        with self.assertRaises(frappe.ValidationError):
            build_payment_log(self.invoice, 1000)


class TestRenderEmail(PosInvoiceTestCase):
    """render_email shows the customer what they are paying for."""

    def test_amount_and_link_are_shown(self) -> None:
        """The email carries the formatted amount and the checkout URL."""
        html = render_email(self.invoice, 1000, "https://example.com/pay/LOG-1")

        self.assertIn("https://example.com/pay/LOG-1", html)
        self.assertIn("1,000", html)

    def test_items_are_listed(self) -> None:
        """Each item line and the invoice name appear in the email."""
        item = self.invoice.items[0]
        html = render_email(self.invoice, 1000, "https://example.com/pay/LOG-1")

        self.assertIn(item.item_name or item.item_code, html)
        self.assertIn(self.invoice.name, html)


class TestSendPosPaymentLink(PosInvoiceTestCase):
    """send_pos_payment_link creates the log and emails the link."""

    def send(self, email: str = "till@example.com") -> tuple:
        """Send a link with the mailer and the realtime channel stubbed out."""
        with patch("frappe.sendmail") as sendmail:
            with patch("frappe.publish_realtime") as publish:
                result = send_pos_payment_link(self.invoice.name, email)

        self.track_log(result["log"])
        return result, sendmail, publish

    def test_zero_tender_is_rejected(self) -> None:
        """An invoice with a zero Paystack tender is refused."""
        invoice = frappe.new_doc(POS_INVOICE)
        invoice.customer = TEST_CUSTOMER
        invoice.name = "POS-TEST-EMPTY"

        with patch("frappe.get_doc", return_value=invoice):
            with self.assertRaises(frappe.ValidationError):
                send_pos_payment_link("POS-TEST-EMPTY")

    def test_a_pending_log_is_raised_for_the_tender(self) -> None:
        """The link collects exactly what the Paystack tender asks for."""
        result, _sendmail, _publish = self.send()

        self.assertEqual(result["amount"], 1000)
        self.assertEqual(
            frappe.db.get_value(PAYMENT_LOG, result["log"], "linked_docname"),
            self.invoice.name,
        )

    def test_the_link_is_emailed_to_the_customer(self) -> None:
        """The checkout URL goes out against the POS Invoice."""
        result, sendmail, _publish = self.send()

        kwargs = sendmail.call_args.kwargs
        self.assertEqual(kwargs["recipients"], ["till@example.com"])
        self.assertEqual(kwargs["reference_doctype"], POS_INVOICE)
        self.assertEqual(kwargs["reference_name"], self.invoice.name)
        self.assertIn(result["log"], kwargs["message"])

    def test_the_url_points_at_the_checkout_page(self) -> None:
        """The URL ends at the checkout page for the log."""
        result, _sendmail, _publish = self.send()

        self.assertTrue(result["url"].endswith(f"/paystack-checkout/{result['log']}"))

    def test_the_till_is_told_to_start_waiting(self) -> None:
        """The awaiting event carries the log, the POS Invoice and the email."""
        result, _sendmail, publish = self.send()

        event, payload = publish.call_args.args
        self.assertEqual(event, "paystack_pos_awaiting")
        self.assertEqual(payload["log"], result["log"])
        self.assertEqual(payload["pos_invoice"], self.invoice.name)
        self.assertEqual(payload["email"], "till@example.com")


class TestSendPosPaymentLinkPermission(PosInvoiceTestCase):
    """The link is only sent by someone allowed to read the sale."""

    def test_a_user_without_the_sale_is_refused(self) -> None:
        """A caller with no read on the POS Invoice raises PermissionError."""
        email = "paystack-till-outsider@example.com"
        if not frappe.db.exists("User", email):
            user = frappe.get_doc(
                {
                    "doctype": "User",
                    "email": email,
                    "first_name": "Paystack Till Outsider",
                    "send_welcome_email": 0,
                    "user_type": "System User",
                    "roles": [{"role": "Blogger"}],
                }
            )
            user.flags.ignore_permissions = True
            user.insert()
            frappe.db.commit()
        self.addCleanup(cleanup_user, email)
        self.addCleanup(frappe.set_user, "Administrator")
        frappe.set_user(email)

        with patch("frappe.sendmail") as sendmail:
            with self.assertRaises(frappe.PermissionError):
                send_pos_payment_link(self.invoice.name)

        sendmail.assert_not_called()


class TestPosPaymentStatus(PosInvoiceTestCase):
    """pos_payment_status reports what a payment link has collected."""

    def raise_log(self, amount: float = 1000) -> str:
        """Return a Pending Payment Log against the open sale."""
        return self.track_log(build_payment_log(self.invoice, amount).name)

    def mark_paid(self, log: str, paid: float, status: str = "Processed") -> None:
        """Set a log's status and amount paid to the state the webhook leaves."""
        frappe.db.set_value(PAYMENT_LOG, log, {"status": status, "amount_paid": paid})
        frappe.clear_document_cache(PAYMENT_LOG, log)

    def test_unknown_log_is_reported(self) -> None:
        """An unknown payment log throws."""
        with self.assertRaises(frappe.ValidationError):
            pos_payment_status("PAYSTACK-NO-SUCH-LOG")

    def test_an_unpaid_link_owes_the_whole_tender(self) -> None:
        """An unpaid link reports the full amount outstanding."""
        status = pos_payment_status(self.raise_log())

        self.assertEqual(status["status"], "Pending")
        self.assertEqual(status["amount_paid"], 0)
        self.assertEqual(status["outstanding"], 1000)
        self.assertFalse(status["fully_paid"])

    def test_a_settled_link_reads_as_fully_paid(self) -> None:
        """A settled link reports zero outstanding and fully_paid."""
        log = self.raise_log()
        self.mark_paid(log, 1000)

        status = pos_payment_status(log)

        self.assertEqual(status["pos_invoice"], self.invoice.name)
        self.assertEqual(status["amount_paid"], 1000)
        self.assertEqual(status["outstanding"], 0)
        self.assertTrue(status["fully_paid"])

    def test_a_part_payment_leaves_a_balance(self) -> None:
        """A part payment reports the remaining balance."""
        log = self.raise_log()
        self.mark_paid(log, 300)

        status = pos_payment_status(log)

        self.assertEqual(status["amount_paid"], 300)
        self.assertEqual(status["outstanding"], 700)
        self.assertFalse(status["fully_paid"])

    def test_an_overpaid_link_never_reports_a_negative(self) -> None:
        """An overpaid link reports zero outstanding and fully_paid."""
        log = self.raise_log()
        self.mark_paid(log, 1200)

        status = pos_payment_status(log)

        self.assertEqual(status["outstanding"], 0)
        self.assertTrue(status["fully_paid"])


class TestNotifyPosLinkPaid(PaystackTestCase):
    """notify_pos_link_paid reports progress back to the till."""

    def build_log(self, doctype: str, amount: float, paid: float):
        """Return a stand-in Payment Log."""
        return frappe._dict(
            name="LOG-1",
            linked_doctype=doctype,
            linked_docname="POS-1",
            amount=amount,
            amount_paid=paid,
            owner="Administrator",
        )

    def test_ignores_non_pos_logs(self) -> None:
        """A log linked to a doctype other than POS Invoice publishes nothing."""
        with patch("frappe.publish_realtime") as publish:
            notify_pos_link_paid(self.build_log("Sales Invoice", 100, 100))

        publish.assert_not_called()

    def test_full_payment_is_flagged(self) -> None:
        """A fully paid log publishes fully_paid with zero outstanding."""
        with patch("frappe.publish_realtime") as publish:
            notify_pos_link_paid(self.build_log("POS Invoice", 100, 100))

        payload = publish.call_args.args[1]
        self.assertTrue(payload["fully_paid"])
        self.assertEqual(payload["outstanding"], 0)

    def test_partial_payment_reports_the_balance(self) -> None:
        """A part payment publishes the remaining balance."""
        with patch("frappe.publish_realtime") as publish:
            notify_pos_link_paid(self.build_log("POS Invoice", 100, 40))

        payload = publish.call_args.args[1]
        self.assertFalse(payload["fully_paid"])
        self.assertEqual(payload["outstanding"], 60)

    def test_overpayment_does_not_report_a_negative(self) -> None:
        """An overpaid log publishes fully_paid with zero outstanding."""
        with patch("frappe.publish_realtime") as publish:
            notify_pos_link_paid(self.build_log("POS Invoice", 100, 130))

        payload = publish.call_args.args[1]
        self.assertTrue(payload["fully_paid"])
        self.assertEqual(payload["outstanding"], 0)


class TestSettlePosInvoice(PaystackTestCase):
    """settle_pos_invoice completes the payment log and leaves the invoice."""

    def test_log_is_marked_completed(self) -> None:
        """Settling records the payment against the log."""
        written = {}
        log = frappe._dict(
            name="LOG-POS",
            linked_docname="POS-1",
            status="Processed",
            db_set=lambda field, value, **kwargs: written.update({field: value}),
        )

        settle_pos_invoice(log)

        self.assertEqual(written.get("status"), "Completed")

    def test_already_completed_log_is_left_alone(self) -> None:
        """A log already Completed is left unwritten."""
        written = {}
        log = frappe._dict(
            name="LOG-POS",
            linked_docname="POS-1",
            status="Completed",
            db_set=lambda field, value, **kwargs: written.update({field: value}),
        )

        settle_pos_invoice(log)

        self.assertEqual(written, {})

    def test_the_invoice_is_not_submitted(self) -> None:
        """Settling loads no document and leaves the POS Invoice as it is."""
        with patch("frappe.get_doc") as get_doc:
            settle_pos_invoice(
                frappe._dict(
                    name="LOG-POS",
                    linked_docname="POS-1",
                    status="Processed",
                    db_set=lambda *a, **k: None,
                )
            )

        get_doc.assert_not_called()
