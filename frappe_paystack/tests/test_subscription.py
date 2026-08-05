"""Tests for subscription collection: ERPNext raises the invoice, a saved card collects it."""

from typing import Any, Optional
from unittest.mock import patch

import frappe
from frappe.utils import add_days, today

from frappe_paystack.api import process_webhook_event
from frappe_paystack.tests.factories import (
    TEST_COMPANY,
    ChargeableInvoiceFactory,
    CustomerAuthorizationFactory,
    CustomerFactory,
    GatewaySettingFactory,
    PaymentLogFactory,
    SubscriptionFactory,
    cleanup_user,
)
from frappe_paystack.tests.test_base import PaystackTestCase
from frappe_paystack.utils.subscription import (
    auto_charge_companies,
    collect_invoice,
    collect_subscription_payments,
    collectable_invoices,
    collection_queue,
    has_open_collection,
)

PAYMENT_LOG = "Paystack Payment Log"
SALES_INVOICE = "Sales Invoice"

CHARGE_AUTHORIZATION_PATCH = "frappe_paystack.api.charge_authorization"

SUBSCRIBER = "_Test Paystack Subscriber"
SUBSCRIBER_EMAIL = "subscriber@example.com"

COLLECTION_CALLER = "paystack-collection-caller@example.com"


def captured_charge() -> dict:
    """Return the Paystack answer to a saved-card charge that went through."""
    return {"status": "success", "reference": "sub_charge_1"}


class SubscriptionTestCase(PaystackTestCase):
    """Base case with an opted-in gateway, a subscriber and a card on file."""

    def setUp(self) -> None:
        """Raise the opted-in gateway, the subscriber and the subscription."""
        super().setUp()

        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)
        GatewaySettingFactory.set_auto_charge_subscriptions(self.gateway, True)

        CustomerFactory.create(customer_name=SUBSCRIBER)
        self.addCleanup(CustomerFactory.cleanup, SUBSCRIBER)

        self.subscription = SubscriptionFactory.create(customer=SUBSCRIBER)
        self.addCleanup(SubscriptionFactory.cleanup, self.subscription)

    def subscription_invoice(self, **kwargs: Any) -> str:
        """Raise a submitted invoice the way a Subscription period close does."""
        invoice = ChargeableInvoiceFactory.create(
            customer=SUBSCRIBER, subscription=self.subscription, **kwargs
        )
        self.addCleanup(ChargeableInvoiceFactory.cleanup, invoice)
        return invoice

    def saved_card(self) -> str:
        """Store a chargeable card for the subscriber."""
        card = CustomerAuthorizationFactory.create(
            customer=SUBSCRIBER, company=TEST_COMPANY, email=SUBSCRIBER_EMAIL
        )
        self.addCleanup(CustomerAuthorizationFactory.cleanup, card)
        return card

    def logs_for(self, invoice: str) -> list:
        """Return the Paystack Payment Logs raised against an invoice."""
        return frappe.get_all(PAYMENT_LOG, filters={"linked_docname": invoice}, pluck="name")

    def track_logs(self, invoice: str) -> list:
        """Register every log raised against an invoice for cleanup."""
        names = self.logs_for(invoice)
        for name in names:
            self.addCleanup(PaymentLogFactory.cleanup, name)
        return names

    def collect(self, invoice: str, **stub: Any) -> Optional[str]:
        """Collect an invoice against a stubbed Paystack charge."""
        with patch(CHARGE_AUTHORIZATION_PATCH, return_value=captured_charge(), **stub):
            log = collect_invoice(invoice)

        self.track_logs(invoice)
        return log


class TestAutoChargeOptIn(SubscriptionTestCase):
    """Automatic collection only runs where the merchant asked for it."""

    def test_an_opted_in_company_is_collected(self) -> None:
        """A company whose gateway opted in is listed for collection."""
        self.assertIn(TEST_COMPANY, auto_charge_companies())

    def test_an_opted_out_company_is_never_collected(self) -> None:
        """A company with the flag cleared is left off the list."""
        GatewaySettingFactory.set_auto_charge_subscriptions(self.gateway, False)

        self.assertNotIn(TEST_COMPANY, auto_charge_companies())

    def test_a_disabled_gateway_is_never_collected(self) -> None:
        """A company whose gateway is disabled is left off the list."""
        self.disable_company_gateways()

        self.assertNotIn(TEST_COMPANY, auto_charge_companies())


class TestCollectableInvoices(SubscriptionTestCase):
    """Which invoices the job is allowed to touch."""

    def test_a_subscription_invoice_is_collectable(self) -> None:
        """A submitted subscription invoice with a balance is collectable."""
        invoice = self.subscription_invoice()

        self.assertIn(invoice, collectable_invoices(TEST_COMPANY))

    def test_an_invoice_no_subscription_raised_is_never_collected(self) -> None:
        """An invoice no subscription raised is left out."""
        invoice = ChargeableInvoiceFactory.create(customer=SUBSCRIBER)
        self.addCleanup(ChargeableInvoiceFactory.cleanup, invoice)

        self.assertNotIn(invoice, collectable_invoices(TEST_COMPANY))

    def test_a_settled_invoice_is_not_collected(self) -> None:
        """A settled invoice is left out."""
        invoice = self.subscription_invoice()
        frappe.db.set_value(SALES_INVOICE, invoice, {"outstanding_amount": 0, "status": "Paid"})

        self.assertNotIn(invoice, collectable_invoices(TEST_COMPANY))

    def test_an_invoice_erpnext_moved_out_of_the_payable_statuses_is_dropped(
        self,
    ) -> None:
        """An invoice in a non-payable status is left out."""
        invoice = self.subscription_invoice()
        frappe.db.set_value(SALES_INVOICE, invoice, "status", "Credit Note Issued")

        self.assertNotIn(invoice, collectable_invoices(TEST_COMPANY))

    def test_an_invoice_with_nothing_outstanding_is_not_collected(self) -> None:
        """An invoice with a zero outstanding is left out."""
        invoice = self.subscription_invoice()
        frappe.db.set_value(SALES_INVOICE, invoice, "outstanding_amount", 0)

        self.assertNotIn(invoice, collectable_invoices(TEST_COMPANY))

    def test_a_draft_invoice_is_never_collected(self) -> None:
        """A draft invoice is left out."""
        invoice = self.subscription_invoice()
        frappe.db.set_value(SALES_INVOICE, invoice, "docstatus", 0)

        self.assertNotIn(invoice, collectable_invoices(TEST_COMPANY))

    def test_an_old_invoice_is_left_to_dunning(self) -> None:
        """An invoice older than the lookback is left out."""
        invoice = self.subscription_invoice(overdue_days=90)

        self.assertNotIn(invoice, collectable_invoices(TEST_COMPANY))

    def test_another_companys_invoice_is_not_collected(self) -> None:
        """An invoice belonging to another company is left out."""
        invoice = self.subscription_invoice()

        self.assertNotIn(invoice, collectable_invoices("_Test Company 2"))


class TestCollectInvoice(SubscriptionTestCase):
    """Charging one subscription invoice."""

    def test_a_subscription_invoice_is_charged_to_the_saved_card(self) -> None:
        """A collection raises a Payment Log linked to the invoice."""
        self.saved_card()
        invoice = self.subscription_invoice()

        log = self.collect(invoice)

        self.assertTrue(log)
        self.assertEqual(frappe.db.get_value(PAYMENT_LOG, log, "linked_docname"), invoice)

    def test_the_collection_settles_through_a_payment_request(self) -> None:
        """The collection's log carries a Payment Request."""
        self.saved_card()
        invoice = self.subscription_invoice()

        log = self.collect(invoice)

        self.assertTrue(frappe.db.get_value(PAYMENT_LOG, log, "payment_request"))

    def test_an_invoice_already_being_collected_is_not_charged_again(self) -> None:
        """A second collection of the same invoice raises no second log."""
        self.saved_card()
        invoice = self.subscription_invoice()
        self.collect(invoice)

        self.assertIsNone(self.collect(invoice))
        self.assertEqual(len(self.logs_for(invoice)), 1)

    def test_an_invoice_already_paid_through_paystack_is_not_charged(self) -> None:
        """An invoice carrying a Processed log raises no second log."""
        self.saved_card()
        invoice = self.subscription_invoice()
        log = PaymentLogFactory.create(
            linked_doctype=SALES_INVOICE, linked_docname=invoice, status="Processed"
        )
        self.addCleanup(PaymentLogFactory.cleanup, log)

        self.assertTrue(has_open_collection(invoice))
        self.assertIsNone(self.collect(invoice))
        self.assertEqual(self.logs_for(invoice), [log])

    def test_a_customer_with_no_card_is_skipped(self) -> None:
        """A customer with no saved card raises no log."""
        invoice = self.subscription_invoice()

        self.assertIsNone(self.collect(invoice))
        self.assertEqual(self.logs_for(invoice), [])

    def test_an_expired_card_is_not_charged(self) -> None:
        """An expired card is skipped."""
        card = CustomerAuthorizationFactory.create(
            customer=SUBSCRIBER,
            company=TEST_COMPANY,
            exp_month="1",
            exp_year="2020",
        )
        self.addCleanup(CustomerAuthorizationFactory.cleanup, card)
        invoice = self.subscription_invoice()

        self.assertIsNone(self.collect(invoice))


class TestCollectionJob(SubscriptionTestCase):
    """The daily job that drives the collection."""

    def test_an_opted_in_invoice_is_collected(self) -> None:
        """The job raises one log for an opted-in subscription invoice."""
        self.saved_card()
        invoice = self.subscription_invoice()

        with patch(CHARGE_AUTHORIZATION_PATCH, return_value=captured_charge()):
            collect_subscription_payments()

        self.assertEqual(len(self.track_logs(invoice)), 1)

    def test_an_opted_out_company_is_left_alone(self) -> None:
        """The job raises no log for an opted-out company."""
        self.saved_card()
        invoice = self.subscription_invoice()
        GatewaySettingFactory.set_auto_charge_subscriptions(self.gateway, False)

        with patch(CHARGE_AUTHORIZATION_PATCH, return_value=captured_charge()):
            collect_subscription_payments()

        self.assertEqual(self.track_logs(invoice), [])

    def test_a_declined_card_is_recorded_and_the_batch_carries_on(self) -> None:
        """A declined collection files an Error Log and the job returns None."""
        invoice = self.subscription_invoice()

        with patch(
            "frappe_paystack.utils.subscription.collect_invoice",
            side_effect=RuntimeError("declined"),
        ):
            self.assertIsNone(collect_subscription_payments())

        self.assertTrue(frappe.db.exists("Error Log", {"reference_name": invoice}))

    def test_the_session_user_is_restored(self) -> None:
        """The job hands the session back to the user it was called as."""
        user = frappe.get_doc(
            {
                "doctype": "User",
                "email": COLLECTION_CALLER,
                "first_name": "Paystack Collection",
                "send_welcome_email": 0,
            }
        )
        user.flags.ignore_permissions = True
        user.insert()
        self.addCleanup(cleanup_user, COLLECTION_CALLER)
        frappe.set_user(COLLECTION_CALLER)

        collect_subscription_payments()

        self.assertEqual(frappe.session.user, COLLECTION_CALLER)

    def test_a_lookup_failure_is_logged_not_raised(self) -> None:
        """A failing company lookup returns an empty queue."""
        with patch(
            "frappe_paystack.utils.subscription.auto_charge_companies",
            side_effect=RuntimeError("db down"),
        ):
            self.assertEqual(collection_queue(), [])


class TestRecurringChargeIdempotency(SubscriptionTestCase):
    """A repeated recurring charge bills the customer once."""

    def charge_event(self, log: str, transaction_id: str, plan: Optional[str] = None) -> dict:
        """Return a charge.success the way Paystack sends a recurring one."""
        transaction = {
            "id": transaction_id,
            "reference": log,
            "status": "success",
            "amount": 100000,
            "currency": "NGN",
            "paid_at": "2026-07-30T10:00:00Z",
            "metadata": {"reference": log},
        }
        if plan:
            transaction["plan"] = {"plan_code": plan, "name": plan}
        return {"event": "charge.success", "data": transaction}

    def test_the_same_recurring_charge_settles_once(self) -> None:
        """A charge.success delivered twice books one Payment Entry reference."""
        invoice = self.subscription_invoice()
        log = PaymentLogFactory.create(linked_doctype=SALES_INVOICE, linked_docname=invoice, status="Pending")
        self.addCleanup(PaymentLogFactory.cleanup, log)

        process_webhook_event(self.charge_event(log, "tx_recurring_1"))
        process_webhook_event(self.charge_event(log, "tx_recurring_1"))

        self.assertEqual(
            frappe.db.count(
                "Payment Entry Reference",
                {"reference_name": invoice, "docstatus": 1},
            ),
            1,
        )

    def test_a_paystack_side_plan_charge_books_nothing(self) -> None:
        """A charge carrying a Paystack plan books no log and is filed failed."""
        payload = self.charge_event("", "tx_plan_1", plan="PLN_foreign")
        payload["data"]["metadata"] = {}

        process_webhook_event(payload)

        self.assertEqual(frappe.db.count(PAYMENT_LOG, {"transaction_id": "tx_plan_1"}), 0)
        self.assertTrue(
            frappe.db.exists(
                "Integration Request",
                {
                    "url": "webhook",
                    "status": "Failed",
                    "creation": [">=", self.started_at],
                },
            )
        )


class TestSubscriptionInvoiceLink(SubscriptionTestCase):
    """The subscription field the collection reads off an invoice."""

    def test_the_invoice_names_the_subscription_that_raised_it(self) -> None:
        """A subscription invoice names the subscription that raised it."""
        invoice = self.subscription_invoice()

        self.assertEqual(
            frappe.db.get_value(SALES_INVOICE, invoice, "subscription"),
            self.subscription,
        )

    def test_an_invoice_dated_today_is_inside_the_lookback(self) -> None:
        """An invoice posted today is inside the lookback."""
        invoice = self.subscription_invoice()
        frappe.db.set_value(SALES_INVOICE, invoice, "posting_date", today())

        self.assertIn(invoice, collectable_invoices(TEST_COMPANY))

    def test_an_invoice_on_the_lookback_edge_is_dropped(self) -> None:
        """An invoice posted 30 days back falls outside the lookback."""
        invoice = self.subscription_invoice()
        frappe.db.set_value(SALES_INVOICE, invoice, "posting_date", add_days(today(), -30))

        self.assertNotIn(invoice, collectable_invoices(TEST_COMPANY))
