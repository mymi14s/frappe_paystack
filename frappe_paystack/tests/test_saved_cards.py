"""Tests for saved cards: storing a Paystack authorization and charging it again."""

from typing import Any, Optional
from unittest.mock import patch

import frappe
from frappe.utils import getdate, random_string, today

from frappe_paystack.api import charge_saved_card, process_charge_webhook_event, saved_cards
from frappe_paystack.frappe_paystack.doctype.paystack_customer_authorization import (
    paystack_customer_authorization as authorizations,
)
from frappe_paystack.tests.factories import (
    TEST_COMPANY,
    ChargeableInvoiceFactory,
    CustomerAuthorizationFactory,
    CustomerFactory,
    GatewaySettingFactory,
    PaymentLogFactory,
    cleanup_doc,
    cleanup_user,
)
from frappe_paystack.tests.test_base import PaystackTestCase

PAYMENT_LOG = "Paystack Payment Log"
AUTHORIZATION_DOCTYPE = authorizations.AUTHORIZATION_DOCTYPE

CARD_CUSTOMER = "_Test Paystack Card Customer"
OTHER_CARD_CUSTOMER = "_Test Paystack Other Card Customer"

# The customer ChargeableInvoiceFactory bills.
CHARGE_CUSTOMER = "_Test Paystack Saved Card Customer"

CHARGE_AUTHORIZATION_PATCH = "frappe_paystack.api.charge_authorization"


def card_authorization(**overrides: Any) -> dict:
    """Return an authorization object shaped the way Paystack sends one."""
    authorization = {
        "authorization_code": "AUTH_webhook_1",
        "signature": f"SIG_{random_string(8)}",
        "last4": "4081",
        "exp_month": "12",
        "exp_year": str(getdate(today()).year + 3),
        "card_type": "visa DEBIT",
        "brand": "visa",
        "bank": "Test Bank",
        "channel": "card",
        "reusable": True,
    }
    authorization.update(overrides)
    return authorization


class SavedCardTestCase(PaystackTestCase):
    """Shared gateway fixture and authorization cleanup."""

    def setUp(self) -> None:
        """Enable a Paystack gateway for the test company."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

    def track_authorization(self, name: Optional[str]) -> Optional[str]:
        """Register a stored authorization for deletion."""
        if name:
            self.addCleanup(CustomerAuthorizationFactory.cleanup, name)
        return name

    def store(self, customer: str, **overrides: Any) -> Optional[str]:
        """Store an authorization for a customer and track it."""
        CustomerFactory.create(customer_name=customer)
        self.addCleanup(CustomerFactory.cleanup, customer)

        return self.track_authorization(
            authorizations.store_authorization(
                customer=customer,
                company=TEST_COMPANY,
                email="saved.card@example.com",
                authorization=card_authorization(**overrides),
                customer_code="CUS_test",
            )
        )


class TestStorableAuthorizations(PaystackTestCase):
    """An authorization is storable when it can be charged again."""

    def test_a_reusable_card_is_stored(self) -> None:
        """A reusable card authorization is storable."""
        self.assertTrue(authorizations.is_storable(card_authorization()))

    def test_a_one_off_authorization_is_not_stored(self) -> None:
        """An authorization flagged single-use is not storable."""
        self.assertFalse(authorizations.is_storable(card_authorization(reusable=False)))

    def test_a_non_card_channel_is_not_stored(self) -> None:
        """An authorization on a non-card channel is not storable."""
        self.assertFalse(authorizations.is_storable(card_authorization(channel="bank")))

    def test_an_authorization_without_a_code_is_not_stored(self) -> None:
        """Without the code there is nothing to charge."""
        self.assertFalse(authorizations.is_storable(card_authorization(authorization_code=None)))

    def test_an_authorization_without_a_signature_is_not_stored(self) -> None:
        """Without the signature the same card cannot be recognised again."""
        self.assertFalse(authorizations.is_storable(card_authorization(signature=None)))


class TestStoringAuthorizations(SavedCardTestCase):
    """store_authorization keeps one row per customer and card."""

    def test_a_card_is_stored_against_its_customer(self) -> None:
        """The stored row carries the customer, the last4 and the card label."""
        name = self.store(CARD_CUSTOMER)

        doc = frappe.get_doc(AUTHORIZATION_DOCTYPE, name)
        self.assertEqual(doc.customer, CARD_CUSTOMER)
        self.assertEqual(doc.last4, "4081")
        self.assertEqual(doc.card_label, "Visa •••• 4081")
        self.assertTrue(doc.reusable)

    def test_the_authorization_code_is_encrypted(self) -> None:
        """The stored code is encrypted and read back through the document."""
        name = self.store(CARD_CUSTOMER)

        stored = frappe.db.get_value(AUTHORIZATION_DOCTYPE, name, "authorization_code")
        self.assertNotEqual(stored, "AUTH_webhook_1")
        self.assertEqual(
            frappe.get_doc(AUTHORIZATION_DOCTYPE, name).get_authorization_code(),
            "AUTH_webhook_1",
        )

    def test_the_same_card_updates_one_row(self) -> None:
        """The same signature updates one row, carrying the reissued code."""
        signature = f"SIG_{random_string(8)}"
        first = self.store(CARD_CUSTOMER, signature=signature)
        second = self.store(CARD_CUSTOMER, signature=signature, authorization_code="AUTH_webhook_2")

        self.assertEqual(first, second)
        self.assertEqual(
            frappe.get_doc(AUTHORIZATION_DOCTYPE, first).get_authorization_code(),
            "AUTH_webhook_2",
        )

    def test_a_different_card_is_a_new_row(self) -> None:
        """Two cards for one customer are two instruments."""
        first = self.store(CARD_CUSTOMER)
        second = self.store(CARD_CUSTOMER)

        self.assertNotEqual(first, second)

    def test_an_unstorable_authorization_is_ignored(self) -> None:
        """Nothing is written for an instrument that cannot be charged again."""
        self.assertIsNone(self.store(CARD_CUSTOMER, reusable=False))

    def test_no_customer_stores_nothing(self) -> None:
        """A payment that names no customer leaves no orphan row."""
        self.assertIsNone(
            authorizations.store_authorization(
                customer=None,
                company=TEST_COMPANY,
                email="x@example.com",
                authorization=card_authorization(),
            )
        )

    def test_a_label_without_a_last4_still_names_the_brand(self) -> None:
        """An authorization with no last4 is labelled by its brand."""
        name = self.store(CARD_CUSTOMER, last4=None)

        self.assertEqual(frappe.db.get_value(AUTHORIZATION_DOCTYPE, name, "card_label"), "Visa")

    def test_a_label_falls_back_through_the_names_paystack_sends(self) -> None:
        """With no brand the card type names the instrument."""
        name = self.store(CARD_CUSTOMER, brand=None, last4=None)

        self.assertEqual(
            frappe.db.get_value(AUTHORIZATION_DOCTYPE, name, "card_label"),
            "Visa Debit",
        )


class TestAuthorizationUsability(SavedCardTestCase):
    """When a stored instrument is still offered."""

    def test_a_current_card_is_usable(self) -> None:
        """A card within its expiry is usable."""
        name = self.store(CARD_CUSTOMER)

        self.assertTrue(frappe.get_doc(AUTHORIZATION_DOCTYPE, name).is_usable())

    def test_an_expired_card_is_not_usable(self) -> None:
        """A card whose expiry has passed is no longer offered."""
        name = self.store(CARD_CUSTOMER, exp_month="1", exp_year="2020")

        doc = frappe.get_doc(AUTHORIZATION_DOCTYPE, name)
        self.assertTrue(doc.has_expired())
        self.assertFalse(doc.is_usable())

    def test_a_card_without_an_expiry_is_not_treated_as_expired(self) -> None:
        """A card carrying no expiry date counts as unexpired."""
        name = self.store(CARD_CUSTOMER, exp_month=None, exp_year=None)

        self.assertFalse(frappe.get_doc(AUTHORIZATION_DOCTYPE, name).has_expired())

    def test_a_deactivated_card_is_not_usable(self) -> None:
        """A row switched inactive is unusable."""
        name = self.store(CARD_CUSTOMER)
        frappe.db.set_value(AUTHORIZATION_DOCTYPE, name, "active", 0)

        self.assertFalse(frappe.get_doc(AUTHORIZATION_DOCTYPE, name).is_usable())

    def test_a_non_reusable_row_is_not_usable(self) -> None:
        """A row without the reusable flag is unusable."""
        name = self.store(CARD_CUSTOMER)
        frappe.db.set_value(AUTHORIZATION_DOCTYPE, name, "reusable", 0)

        self.assertFalse(frappe.get_doc(AUTHORIZATION_DOCTYPE, name).is_usable())


class TestUsableAuthorizations(SavedCardTestCase):
    """The picker lists only what can be charged, and only the right customer's."""

    def setUp(self) -> None:
        """Give one customer a current card and another customer their own."""
        super().setUp()
        self.card = self.store(CARD_CUSTOMER)
        self.other_card = self.store(OTHER_CARD_CUSTOMER)

    def names_for(self, customer: str, company: Optional[str] = None) -> list:
        """Return the authorization names offered for a customer."""
        return [row.name for row in authorizations.usable_authorizations(customer, company)]

    def test_a_customers_own_card_is_listed(self) -> None:
        """A customer's own card is listed for them."""
        self.assertIn(self.card, self.names_for(CARD_CUSTOMER))

    def test_another_customers_card_is_never_listed(self) -> None:
        """Another customer's saved card is left out of the list."""
        self.assertNotIn(self.other_card, self.names_for(CARD_CUSTOMER))

    def test_an_expired_card_is_filtered_out(self) -> None:
        """A row still marked active is dropped once its expiry passes."""
        expired = self.store(CARD_CUSTOMER, exp_month="1", exp_year="2020")

        self.assertNotIn(expired, self.names_for(CARD_CUSTOMER))

    def test_a_deactivated_card_is_filtered_out(self) -> None:
        """Deactivating a row takes it off the list."""
        frappe.db.set_value(AUTHORIZATION_DOCTYPE, self.card, "active", 0)

        self.assertNotIn(self.card, self.names_for(CARD_CUSTOMER))

    def test_the_company_filter_narrows_the_list(self) -> None:
        """A card stored for one company is not offered on another's document."""
        self.assertIn(self.card, self.names_for(CARD_CUSTOMER, TEST_COMPANY))
        self.assertNotIn(self.card, self.names_for(CARD_CUSTOMER, "_Test Company 2"))

    def test_no_authorization_code_is_ever_listed(self) -> None:
        """The picker names the card; the token stays on the server."""
        for row in authorizations.usable_authorizations(CARD_CUSTOMER):
            self.assertNotIn("authorization_code", row)


class TestWebhookCapturesAuthorizations(SavedCardTestCase):
    """A settled charge leaves the instrument behind for next time."""

    def setUp(self) -> None:
        """Raise a payment log the webhook can settle."""
        super().setUp()
        self.log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, self.log_name)
        self.customer = frappe.db.get_value(
            "Sales Invoice",
            frappe.db.get_value(PAYMENT_LOG, self.log_name, "linked_docname"),
            "customer",
        )
        self.addCleanup(self.remove_captured)

    def remove_captured(self) -> None:
        """Delete every authorization the webhook stored for the fixture."""
        for name in frappe.get_all(AUTHORIZATION_DOCTYPE, filters={"customer": self.customer}, pluck="name"):
            cleanup_doc(AUTHORIZATION_DOCTYPE, name)

    def settle(self, authorization: Any) -> None:
        """Run a successful charge webhook carrying an authorization object."""
        process_charge_webhook_event(
            {
                "event": "charge.success",
                "data": {
                    "id": frappe.utils.random_string(8),
                    "status": "success",
                    "amount": 100000,
                    "currency": "NGN",
                    "reference": f"mrc_{self.log_name}",
                    "authorization": authorization,
                    "customer": {
                        "email": "buyer@example.com",
                        "customer_code": "CUS_webhook",
                    },
                    "metadata": {"reference": self.log_name},
                },
            }
        )

    def stored_signatures(self) -> list:
        """Return the signatures stored for the fixture customer."""
        return frappe.get_all(
            AUTHORIZATION_DOCTYPE,
            filters={"customer": self.customer},
            pluck="signature",
        )

    def test_a_reusable_card_is_captured(self) -> None:
        """The card that paid is stored against the customer that paid with it."""
        authorization = card_authorization()

        self.settle(authorization)

        self.assertIn(authorization["signature"], self.stored_signatures())

    def test_a_one_off_authorization_is_not_captured(self) -> None:
        """A non-reusable charge stores nothing."""
        authorization = card_authorization(reusable=False)

        self.settle(authorization)

        self.assertNotIn(authorization["signature"], self.stored_signatures())

    def test_a_charge_without_an_authorization_stores_nothing(self) -> None:
        """A charge carrying no authorization object stores nothing."""
        self.settle(None)

        self.assertEqual(self.stored_signatures(), [])

    def test_the_webhook_payload_is_filed_without_the_code(self) -> None:
        """The filed Integration Request payload carries no authorization code."""
        self.settle(card_authorization())

        filed = frappe.get_all(
            "Integration Request",
            filters={"reference_docname": self.log_name},
            fields=["data"],
        )

        self.assertTrue(filed)
        for row in filed:
            self.assertNotIn("AUTH_webhook_1", row.data)

    def test_a_malformed_authorization_is_ignored(self) -> None:
        """A malformed authorization returns None."""
        self.assertIsNone(
            authorizations.capture_authorization(
                frappe.get_doc(PAYMENT_LOG, self.log_name), {"authorization": "nope"}
            )
        )


class TestSavedCardsEndpoint(SavedCardTestCase):
    """The desk picker reads a customer's cards, and only theirs."""

    def setUp(self) -> None:
        """Give one customer a card."""
        super().setUp()
        self.card = self.store(CARD_CUSTOMER)

    def test_a_customers_cards_are_returned(self) -> None:
        """The endpoint returns the customer's stored cards."""
        names = [row.name for row in saved_cards(CARD_CUSTOMER)]

        self.assertIn(self.card, names)

    def test_a_caller_without_customer_read_is_refused(self) -> None:
        """A caller without Customer read permission is refused."""
        user = f"paystack-cards-{random_string(8).lower()}@example.com"
        account = frappe.get_doc(
            {
                "doctype": "User",
                "email": user,
                "first_name": "Paystack Cards",
                "send_welcome_email": 0,
                "user_type": "System User",
                "roles": [{"role": "Blogger"}],
            }
        )
        account.flags.ignore_permissions = True
        account.insert()
        self.addCleanup(cleanup_user, user)
        self.addCleanup(frappe.set_user, "Administrator")
        frappe.set_user(user)

        with self.assertRaises(frappe.PermissionError):
            saved_cards(CARD_CUSTOMER)


class TestChargeSavedCard(SavedCardTestCase):
    """Charging a stored card raises a log the webhook settles."""

    def setUp(self) -> None:
        """Store a card, then raise the invoice it will be charged for."""
        super().setUp()
        self.card = self.store(CHARGE_CUSTOMER)
        self.other_card = self.store(OTHER_CARD_CUSTOMER)

        self.invoice = ChargeableInvoiceFactory.create(customer=CHARGE_CUSTOMER, rate=5000)
        self.addCleanup(ChargeableInvoiceFactory.cleanup, self.invoice)

    def charge(self, result: dict, **kwargs: Any) -> str:
        """Charge the stored card against a stubbed Paystack answer."""
        with patch(CHARGE_AUTHORIZATION_PATCH, return_value=result) as mock_charge:
            self.mock_charge = mock_charge
            log = charge_saved_card("Sales Invoice", self.invoice, self.card, **kwargs)

        self.addCleanup(PaymentLogFactory.cleanup, log)
        return log

    def test_a_successful_charge_raises_a_payment_log(self) -> None:
        """The charge runs against a log the webhook can settle as usual."""
        log = self.charge({"status": "success", "id": 1234})

        doc = frappe.get_doc(PAYMENT_LOG, log)
        self.assertEqual(doc.linked_docname, self.invoice)
        self.assertEqual(doc.customer_authorization, self.card)
        self.assertTrue(doc.payment_request)

    def test_the_charge_carries_the_webhook_metadata(self) -> None:
        """The charge carries the log and its document in the metadata."""
        log = self.charge({"status": "success"})

        metadata = self.mock_charge.call_args.kwargs["metadata"]
        self.assertEqual(metadata["reference"], log)
        self.assertEqual(metadata["reference_doctype"], "Sales Invoice")
        self.assertEqual(metadata["reference_docname"], self.invoice)

    def test_a_partial_amount_is_billed(self) -> None:
        """A caller-supplied amount collects less than the whole invoice."""
        log = self.charge({"status": "success"}, amount=1000)

        self.assertAlmostEqual(frappe.db.get_value(PAYMENT_LOG, log, "amount"), 1000, places=2)

    def test_a_declined_card_fails_the_log(self) -> None:
        """A decline raises with the gateway message and leaves the log Failed."""
        with patch(
            CHARGE_AUTHORIZATION_PATCH,
            return_value={"status": "failed", "gateway_response": "Declined"},
        ):
            with self.assertRaises(frappe.ValidationError) as caught:
                charge_saved_card("Sales Invoice", self.invoice, self.card)

        log = self.latest_log()
        self.addCleanup(PaymentLogFactory.cleanup, log)

        self.assertIn("Declined", str(caught.exception))
        self.assertEqual(frappe.db.get_value(PAYMENT_LOG, log, "status"), "Failed")

    def test_a_card_needing_the_customer_is_reported(self) -> None:
        """A card asking for an OTP raises, pointing at the checkout link."""
        with patch(
            CHARGE_AUTHORIZATION_PATCH,
            return_value={"status": "send_otp", "message": "OTP required"},
        ):
            with self.assertRaises(frappe.ValidationError) as caught:
                charge_saved_card("Sales Invoice", self.invoice, self.card)

        self.addCleanup(PaymentLogFactory.cleanup, self.latest_log())
        self.assertIn("checkout link", str(caught.exception))

    def test_a_rejected_call_records_the_failure_on_the_log(self) -> None:
        """A transport failure leaves a trail on the log it was raised for."""
        with patch(CHARGE_AUTHORIZATION_PATCH, side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                charge_saved_card("Sales Invoice", self.invoice, self.card)

        log = self.latest_log()
        self.addCleanup(PaymentLogFactory.cleanup, log)
        self.assertIn("Error Log", frappe.db.get_value(PAYMENT_LOG, log, "errors"))

    def test_another_customers_card_cannot_be_charged(self) -> None:
        """The instrument has to belong to the customer being billed."""
        with self.assertRaises(frappe.PermissionError):
            charge_saved_card("Sales Invoice", self.invoice, self.other_card)

    def test_an_expired_card_cannot_be_charged(self) -> None:
        """An expired card raises before Paystack is called."""
        frappe.db.set_value(AUTHORIZATION_DOCTYPE, self.card, "exp_year", "2020")
        frappe.clear_document_cache(AUTHORIZATION_DOCTYPE, self.card)

        with patch(CHARGE_AUTHORIZATION_PATCH) as mock_charge:
            with self.assertRaises(frappe.ValidationError) as caught:
                charge_saved_card("Sales Invoice", self.invoice, self.card)

        self.assertIn("can no longer be charged", str(caught.exception))
        mock_charge.assert_not_called()

    def signed_in_user(self, role: str) -> str:
        """Create a user holding one role and sign the session in as them."""
        user = f"paystack-charge-{random_string(8).lower()}@example.com"
        account = frappe.get_doc(
            {
                "doctype": "User",
                "email": user,
                "first_name": "Paystack Charge",
                "send_welcome_email": 0,
                "user_type": "System User",
                "roles": [{"role": role}],
            }
        )
        account.flags.ignore_permissions = True
        account.insert()
        self.addCleanup(cleanup_user, user)
        self.addCleanup(frappe.set_user, "Administrator")
        frappe.set_user(user)
        return user

    def restrict_to_customer(self, user: str, customer: str) -> None:
        """Leave a user able to read only the documents of one customer."""
        frappe.set_user("Administrator")
        permission = frappe.get_doc(
            {
                "doctype": "User Permission",
                "user": user,
                "allow": "Customer",
                "for_value": customer,
            }
        )
        permission.flags.ignore_permissions = True
        permission.insert()
        self.addCleanup(cleanup_doc, "User Permission", permission.name)
        frappe.clear_cache(user=user)
        frappe.set_user(user)

    def test_a_user_without_the_role_cannot_charge(self) -> None:
        """A user without the required role cannot charge a saved card."""
        self.signed_in_user("Accounts User")

        with self.assertRaises(frappe.PermissionError):
            charge_saved_card("Sales Invoice", self.invoice, self.card)

    def test_a_user_who_cannot_read_the_document_is_refused(self) -> None:
        """The caller has to be able to read the document being billed."""
        user = self.signed_in_user("Accounts Manager")
        self.restrict_to_customer(user, OTHER_CARD_CUSTOMER)

        with (
            patch(CHARGE_AUTHORIZATION_PATCH),
            patch("frappe_paystack.api.create_payment_link") as create_link,
        ):
            with self.assertRaises(frappe.PermissionError):
                charge_saved_card("Sales Invoice", self.invoice, self.card)

        create_link.assert_not_called()

    def test_the_same_user_passes_the_guards_for_a_readable_document(self) -> None:
        """A caller who can read the document reaches create_payment_link."""
        user = self.signed_in_user("Accounts Manager")
        self.restrict_to_customer(user, CHARGE_CUSTOMER)

        with patch(
            "frappe_paystack.api.create_payment_link",
            side_effect=RuntimeError("past the guards"),
        ):
            with self.assertRaises(RuntimeError):
                charge_saved_card("Sales Invoice", self.invoice, self.card)

    def charge_through_a_request(self, expected: type, **stub: Any) -> str:
        """Charge, then roll back the way a failed POST request does."""
        frappe.db.commit()

        with patch(CHARGE_AUTHORIZATION_PATCH, **stub):
            with self.assertRaises(expected):
                charge_saved_card("Sales Invoice", self.invoice, self.card)

        log = self.latest_log()
        self.addCleanup(PaymentLogFactory.cleanup, log)
        frappe.db.rollback()
        return log

    def test_a_declined_card_records_the_failure_durably(self) -> None:
        """A declined charge leaves the log Failed through the rollback."""
        log = self.charge_through_a_request(
            frappe.ValidationError,
            return_value={"status": "failed", "gateway_response": "Declined"},
        )

        self.assertEqual(frappe.db.get_value(PAYMENT_LOG, log, "status"), "Failed")

    def test_a_rejected_call_keeps_its_error_log(self) -> None:
        """The log's error trail and the Error Log it names survive the rollback."""
        log = self.charge_through_a_request(RuntimeError, side_effect=RuntimeError("boom"))

        self.assertIn("Error Log", frappe.db.get_value(PAYMENT_LOG, log, "errors") or "")
        self.assertTrue(frappe.db.exists("Error Log", {"reference_name": log}))

    def test_a_card_needing_the_customer_keeps_its_link(self) -> None:
        """The log an OTP refusal points at survives the rollback."""
        log = self.charge_through_a_request(
            frappe.ValidationError,
            return_value={"status": "send_otp", "message": "OTP required"},
        )

        self.assertTrue(frappe.db.exists(PAYMENT_LOG, log))

    def latest_log(self) -> str:
        """Return the newest Payment Log raised against the fixture invoice."""
        return frappe.get_all(
            PAYMENT_LOG,
            filters={"linked_docname": self.invoice},
            order_by="creation desc",
            limit=1,
            pluck="name",
        )[0]
