"""Tests for the remaining Payment Log and Gateway Setting guard paths."""

from typing import Any
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime, today

from frappe_paystack.api import process_charge_webhook_event
from frappe_paystack.frappe_paystack.doctype.paystack_payment_log.paystack_payment_log import (
    create_payment_entry_from_log,
    due_logs,
    retry_stuck_settlements,
)
from frappe_paystack.tests.factories import (
    TEST_COMPANY,
    VALIDATE_PAYMENT_PATCH_TARGET,
    GatewaySettingFactory,
    PaymentLogFactory,
    POSInvoiceFactory,
    SalesInvoiceFactory,
    cleanup_doc,
)
from frappe_paystack.tests.test_base import PaystackTestCase
from frappe_paystack.utils.pos_payment import build_payment_log

PAYMENT_LOG = "Paystack Payment Log"
GATEWAY_DOCTYPE = "Paystack Gateway Setting"
PAYMENT_GATEWAY = "Payment Gateway"
POS_INVOICE = "POS Invoice"

LOG_MODULE = "frappe_paystack.frappe_paystack.doctype.paystack_payment_log.paystack_payment_log"

# Where the gateway controller binds the setup helpers.
GATEWAY_MODULE = "frappe_paystack.frappe_paystack.doctype.paystack_gateway_setting.paystack_gateway_setting"

VERIFIED_TRANSACTION = {"status": True, "data": {"status": "success"}}


class PaymentLogPathTestCase(PaystackTestCase):
    """Shared gateway fixture for Payment Log guard tests."""

    def setUp(self) -> None:
        """Enable a Paystack gateway for the test company."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

    def insert_log(self, **fields: Any) -> Any:
        """Insert a Payment Log with the Paystack verification stubbed out."""
        values = {
            "doctype": PAYMENT_LOG,
            "company": TEST_COMPANY,
            "amount": 1000,
            "status": "Completed",
        }
        values.update(fields)

        with patch(VALIDATE_PAYMENT_PATCH_TARGET, return_value=VERIFIED_TRANSACTION):
            doc = frappe.get_doc(values)
            doc.flags.ignore_permissions = True
            doc.insert()
            frappe.db.commit()

        self.addCleanup(PaymentLogFactory.cleanup, doc.name)
        return doc


class TestPaymentLogValidation(PaymentLogPathTestCase):
    """validate refuses logs that cannot describe a payment."""

    def test_missing_currency_is_left_alone(self) -> None:
        """A log raised without a currency keeps an empty currency."""
        doc = self.insert_log(currency=None)

        self.assertFalse(doc.currency)

    def test_half_a_reference_is_refused(self) -> None:
        """A linked doctype without a docname is refused."""
        with self.assertRaises(frappe.ValidationError):
            self.insert_log(linked_doctype="Sales Invoice", linked_docname=None)

    def test_unreferenced_log_books_no_payment_entry(self) -> None:
        """A log that bills no document is left with no Payment Entry."""
        doc = self.insert_log(currency="NGN")

        self.assertIsNone(doc.payment_entry)

    def test_unreferenced_log_is_skipped_by_the_entry_job(self) -> None:
        """The Payment Entry job leaves a log that bills nothing unbooked."""
        doc = self.insert_log(currency="NGN")

        create_payment_entry_from_log(doc.name)

        self.assertIsNone(frappe.db.get_value(PAYMENT_LOG, doc.name, "payment_entry"))


class TestSettledInvoiceShortCircuit(PaymentLogPathTestCase):
    """A log against a settled invoice is left as it stands."""

    def test_completed_log_on_a_settled_invoice_is_left_alone(self) -> None:
        """A Completed log on a settled invoice keeps its fields and timestamp."""
        invoice = SalesInvoiceFactory.create(rate=1000)
        self.addCleanup(SalesInvoiceFactory.cleanup, invoice)
        log_name = PaymentLogFactory.create(
            linked_docname=invoice, amount=1000, amount_paid=1000, status="Completed"
        )
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        self.assertEqual(frappe.db.get_value("Sales Invoice", invoice, "outstanding_amount"), 0)
        frappe.db.set_value(PAYMENT_LOG, log_name, "payment_entry", None)
        frappe.clear_document_cache(PAYMENT_LOG, log_name)
        modified = frappe.db.get_value(PAYMENT_LOG, log_name, "modified")

        create_payment_entry_from_log(log_name)

        self.assertIsNone(frappe.db.get_value(PAYMENT_LOG, log_name, "payment_entry"))
        self.assertEqual(frappe.db.get_value(PAYMENT_LOG, log_name, "modified"), modified)


class TestPosInvoiceLogPaths(PaymentLogPathTestCase):
    """A POS Invoice is charged while it is still a draft."""

    def setUp(self) -> None:
        """Open a till sale to charge against."""
        super().setUp()
        self.invoice = frappe.get_doc(POS_INVOICE, POSInvoiceFactory.create())
        self.addCleanup(POSInvoiceFactory.cleanup, self.invoice.name)

    def raise_log(self, amount: float = 1000) -> Any:
        """Return a Pending Payment Log against the open sale."""
        log = build_payment_log(self.invoice, amount)
        self.addCleanup(PaymentLogFactory.cleanup, log.name)
        return log

    def test_a_draft_sale_can_still_be_paid(self) -> None:
        """A draft POS Invoice is payable."""
        log = self.raise_log()

        self.assertTrue(log.is_payable(self.invoice))
        self.assertTrue(log.get_data()["is_payable"])

    def test_a_cancelled_sale_cannot_be_paid(self) -> None:
        """A cancelled POS Invoice is unpayable."""
        log = self.raise_log()
        POSInvoiceFactory.cancel(self.invoice.name)

        self.assertFalse(log.is_payable(frappe.get_doc(POS_INVOICE, self.invoice.name)))

    def test_a_cancelled_sale_cannot_raise_a_link(self) -> None:
        """A link against a cancelled sale raises ValidationError naming it."""
        POSInvoiceFactory.cancel(self.invoice.name)

        with self.assertRaises(frappe.ValidationError) as raised:
            build_payment_log(self.invoice, 1000)

        self.assertIn("cancelled", str(raised.exception))

    def test_settlement_completes_the_log_without_a_payment_entry(self) -> None:
        """Settling a POS log completes it and leaves its payment entry empty."""
        log = self.raise_log()
        frappe.db.set_value(PAYMENT_LOG, log.name, {"status": "Processed", "amount_paid": 1000})
        frappe.clear_document_cache(PAYMENT_LOG, log.name)

        create_payment_entry_from_log(log.name)

        self.assertEqual(frappe.db.get_value(PAYMENT_LOG, log.name, "status"), "Completed")
        self.assertFalse(frappe.db.get_value(PAYMENT_LOG, log.name, "payment_entry"))


class TestPaymentRequestGuard(PaystackTestCase):
    """on_update leaves a Payment Request log to ERPNext."""

    def build_log(self, payment_request: str = "") -> Any:
        """Return an unsaved Processed log billing a POS Invoice."""
        return frappe.get_doc(
            {
                "doctype": PAYMENT_LOG,
                "company": TEST_COMPANY,
                "linked_doctype": POS_INVOICE,
                "linked_docname": "POS-TEST-GUARD",
                "amount": 1000,
                "status": "Processed",
                "payment_request": payment_request or None,
            }
        )

    def test_a_payment_request_log_is_not_queued(self) -> None:
        """A log carrying a payment request enqueues nothing."""
        with patch("frappe.enqueue") as enqueue:
            self.build_log(payment_request="PREQ-TEST-0001").on_update()

        enqueue.assert_not_called()

    def test_a_log_without_one_is_queued(self) -> None:
        """A log with no payment request enqueues the fallback job."""
        with patch("frappe.enqueue") as enqueue:
            self.build_log().on_update()

        self.assertTrue(enqueue.called)

    def test_the_job_is_named_by_the_supported_keyword(self) -> None:
        """The settlement fallback identifies its job with job_id."""
        with patch("frappe.enqueue") as enqueue:
            self.build_log().on_update()

        self.assertNotIn("job_name", enqueue.call_args.kwargs)
        self.assertTrue(enqueue.call_args.kwargs["job_id"].startswith("pe-"))

    def test_the_job_is_deduplicated_on_the_log(self) -> None:
        """The settlement fallback lets the queue turn away a job it already holds."""
        with patch("frappe.enqueue") as enqueue:
            self.build_log().on_update()

        self.assertTrue(enqueue.call_args.kwargs["deduplicate"])


class TestSettlementRetry(PaymentLogPathTestCase):
    """The sweep re-drives a settlement that fell over."""

    def stall(self, log_name: str) -> None:
        """Age a Processed log past the retry grace period."""
        frappe.db.set_value(
            PAYMENT_LOG,
            log_name,
            {
                "status": "Processed",
                "amount_paid": 1000,
                "payment_reference": f"ref-{log_name}",
                "payment_date": today(),
            },
            update_modified=False,
        )
        frappe.db.set_value(
            PAYMENT_LOG,
            log_name,
            "modified",
            add_to_date(now_datetime(), minutes=-60),
            update_modified=False,
        )
        frappe.clear_document_cache(PAYMENT_LOG, log_name)

    def stalled_log(self) -> str:
        """Return a log stuck at Processed with no Payment Entry."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        self.stall(log_name)
        return log_name

    def sweep(self, log_name: str) -> None:
        """Run the sweep with its selection pinned to one log."""
        with patch(f"{LOG_MODULE}.due_logs", return_value=[log_name]):
            retry_stuck_settlements()

    def test_a_stalled_log_is_selected(self) -> None:
        """A Processed log with no Payment Entry past the grace period is selected."""
        self.assertIn(self.stalled_log(), due_logs())

    def test_a_fresh_log_is_left_to_settle_itself(self) -> None:
        """A log inside the grace period is left out of the selection."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        frappe.db.set_value(PAYMENT_LOG, log_name, {"status": "Processed"})

        self.assertNotIn(log_name, due_logs())

    def test_a_settled_log_is_not_selected(self) -> None:
        """A log that has booked its Payment Entry is left out of the selection."""
        log_name = self.stalled_log()
        self.sweep(log_name)
        payment_entry = frappe.db.get_value(PAYMENT_LOG, log_name, "payment_entry")
        self.addCleanup(cleanup_doc, "Payment Entry", payment_entry)

        self.assertNotIn(log_name, due_logs())

    def test_the_sweep_books_the_missing_payment_entry(self) -> None:
        """The sweep books the Payment Entry and completes the log."""
        log_name = self.stalled_log()

        self.sweep(log_name)

        payment_entry = frappe.db.get_value(PAYMENT_LOG, log_name, "payment_entry")
        self.addCleanup(cleanup_doc, "Payment Entry", payment_entry)
        self.assertTrue(payment_entry)
        self.assertEqual(frappe.db.get_value(PAYMENT_LOG, log_name, "status"), "Completed")

    def test_running_it_twice_books_one_payment_entry(self) -> None:
        """Two sweeps over one log leave a single Payment Entry."""
        log_name = self.stalled_log()

        self.sweep(log_name)
        payment_entry = frappe.db.get_value(PAYMENT_LOG, log_name, "payment_entry")
        self.addCleanup(cleanup_doc, "Payment Entry", payment_entry)
        self.sweep(log_name)

        self.assertEqual(frappe.db.get_value(PAYMENT_LOG, log_name, "payment_entry"), payment_entry)
        self.assertEqual(frappe.db.count("Payment Entry", {"reference_no": f"ref-{log_name}"}), 1)

    def test_a_lookup_failure_is_logged_not_raised(self) -> None:
        """A lookup failure inside the sweep goes to frappe.log_error."""
        with patch(f"{LOG_MODULE}.due_logs", side_effect=Exception("database is away")):
            with patch("frappe.log_error") as log_error:
                retry_stuck_settlements()

        self.assertTrue(log_error.called)


class TestGatewayRegistration(PaystackTestCase):
    """register_as_payment_gateway points the gateway at the saved setting."""

    def setUp(self) -> None:
        """Snapshot the Payment Gateway controller so each test restores it."""
        super().setUp()
        self.addCleanup(
            self.restore_controller,
            frappe.db.get_value(PAYMENT_GATEWAY, "Paystack", "gateway_controller"),
        )
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

    def restore_controller(self, controller: str) -> None:
        """Point the Payment Gateway back at the controller it named before."""
        if frappe.db.exists(PAYMENT_GATEWAY, "Paystack"):
            frappe.db.set_value(
                PAYMENT_GATEWAY,
                "Paystack",
                "gateway_controller",
                controller,
                update_modified=False,
            )
            frappe.db.commit()

    def test_setting_without_a_suspense_account_creates_no_account(self) -> None:
        """A setting with no suspense account is registered and creates no account."""
        frappe.db.set_value(GATEWAY_DOCTYPE, self.gateway, "suspense_account", None)
        frappe.clear_document_cache(GATEWAY_DOCTYPE, self.gateway)
        doc = frappe.get_doc(GATEWAY_DOCTYPE, self.gateway)

        with patch(f"{GATEWAY_MODULE}.create_payment_gateway_account") as mock_create:
            doc.register_as_payment_gateway()

        mock_create.assert_not_called()
        self.assertEqual(
            frappe.db.get_value(PAYMENT_GATEWAY, "Paystack", "gateway_controller"),
            self.gateway,
        )

    def test_registration_failure_is_logged_not_raised(self) -> None:
        """A failure inside registration is logged and returns None."""
        doc = frappe.get_doc(GATEWAY_DOCTYPE, self.gateway)

        with patch(
            f"{GATEWAY_MODULE}.create_payment_gateway_record",
            side_effect=Exception("the gateway account setup is broken"),
        ):
            self.assertIsNone(doc.register_as_payment_gateway())


class TestPaymentLinkExpiry(PaymentLogPathTestCase):
    """A gateway can put a clock on the checkout links it raises."""

    def set_validity(self, hours: int) -> None:
        """Set how long this company's checkout links stay payable."""
        frappe.db.set_value(GATEWAY_DOCTYPE, self.gateway, "payment_link_validity_hours", hours)
        frappe.clear_document_cache(GATEWAY_DOCTYPE, self.gateway)

    def new_log(self, **fields: Any) -> Any:
        """Raise a Pending log against a fresh unpaid invoice."""
        invoice = SalesInvoiceFactory.create(rate=1000)
        self.addCleanup(SalesInvoiceFactory.cleanup, invoice)
        return self.insert_log(
            status="Pending",
            linked_doctype="Sales Invoice",
            linked_docname=invoice,
            **fields,
        )

    def test_no_validity_leaves_the_link_open(self) -> None:
        """The default of zero hours keeps a link valid indefinitely."""
        self.set_validity(0)

        log = self.new_log()

        self.assertIsNone(log.expires_at)
        self.assertFalse(log.is_expired())

    def test_validity_stamps_an_expiry(self) -> None:
        """A configured window is measured from when the link was raised."""
        self.set_validity(2)

        log = self.new_log()

        self.assertIsNotNone(log.expires_at)
        self.assertGreater(get_datetime(log.expires_at), now_datetime())
        self.assertLess(get_datetime(log.expires_at), add_to_date(now_datetime(), hours=3))

    def test_an_expiry_already_set_is_kept(self) -> None:
        """A caller-supplied expiry is kept as given."""
        self.set_validity(2)
        chosen = add_to_date(now_datetime(), hours=48)

        log = self.new_log(expires_at=chosen)

        self.assertEqual(get_datetime(log.expires_at), chosen)

    def test_a_past_expiry_closes_the_checkout(self) -> None:
        """A link past its window can no longer be paid."""
        log = self.new_log(expires_at=add_to_date(now_datetime(), hours=-1))

        self.assertTrue(log.is_expired())
        self.assertFalse(log.get_data()["is_payable"])

    def test_a_future_expiry_leaves_the_checkout_open(self) -> None:
        """A log inside its expiry window is payable."""
        log = self.new_log(expires_at=add_to_date(now_datetime(), hours=1))

        self.assertFalse(log.is_expired())
        self.assertTrue(log.get_data()["is_payable"])

    def test_the_checkout_payload_reports_the_expiry(self) -> None:
        """The checkout payload carries is_expired and the expiry timestamp."""
        expiry = add_to_date(now_datetime(), hours=-1)
        log = self.new_log(expires_at=expiry)

        data = log.get_data()

        self.assertTrue(data["is_expired"])
        self.assertEqual(data["expires_at"], str(expiry))

    def test_an_open_link_reports_no_expiry(self) -> None:
        """A link with no window carries an empty expiry string."""
        self.set_validity(0)

        data = self.new_log().get_data()

        self.assertFalse(data["is_expired"])
        self.assertEqual(data["expires_at"], "")

    def test_expiry_does_not_stop_a_webhook_settling(self) -> None:
        """A charge webhook settles a log whose link has expired."""
        log = self.new_log(expires_at=add_to_date(now_datetime(), hours=-1))

        process_charge_webhook_event(
            {
                "event": "charge.success",
                "data": {
                    "id": 987654321,
                    "status": "success",
                    "amount": 100000,
                    "currency": "NGN",
                    "reference": f"mrc_{log.name}",
                    "metadata": {"reference": log.name},
                },
            }
        )

        self.assertIn(
            frappe.db.get_value(PAYMENT_LOG, log.name, "status"),
            ("Processed", "Completed"),
        )


class TestCheckoutMode(PaymentLogPathTestCase):
    """The gateway decides whether checkout runs inline or on Paystack."""

    def set_mode(self, mode: str) -> None:
        """Point this company's gateway at a checkout mode."""
        frappe.db.set_value(GATEWAY_DOCTYPE, self.gateway, "checkout_mode", mode)
        frappe.clear_document_cache(GATEWAY_DOCTYPE, self.gateway)

    def payload(self) -> dict:
        """Return the checkout payload for a fresh log."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        return frappe.get_doc(PAYMENT_LOG, log_name).get_data()

    def test_hosted_mode_reaches_the_page(self) -> None:
        """A Hosted gateway reports Hosted in the checkout payload."""
        self.set_mode("Hosted")

        self.assertEqual(self.payload()["checkout_mode"], "Hosted")

    def test_inline_is_the_default(self) -> None:
        """An Inline gateway reports Inline in the checkout payload."""
        self.set_mode("Inline")

        self.assertEqual(self.payload()["checkout_mode"], "Inline")

    def test_an_empty_mode_falls_back_to_inline(self) -> None:
        """An empty checkout mode reports Inline."""
        self.set_mode("")

        self.assertEqual(self.payload()["checkout_mode"], "Inline")

    def test_a_log_without_a_gateway_falls_back_to_inline(self) -> None:
        """A log with no enabled gateway reports Inline."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        log = frappe.get_doc(PAYMENT_LOG, log_name)
        self.disable_company_gateways()
        frappe.clear_document_cache(GATEWAY_DOCTYPE, self.gateway)

        self.assertEqual(log.get_data()["checkout_mode"], "Inline")
