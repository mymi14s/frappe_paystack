# Copyright (c) 2026, Anthony Emmanuel and Contributors
# See license.txt

import frappe
from frappe.utils import getdate, today

from frappe_paystack.tests.factories import CustomerAuthorizationFactory
from frappe_paystack.tests.test_base import PaystackTestCase

AUTHORIZATION = "Paystack Customer Authorization"


class TestPaystackCustomerAuthorization(PaystackTestCase):
    """The Paystack Customer Authorization doctype."""

    def card(self, **kwargs) -> str:
        """Store a card authorization and register its cleanup."""
        name = CustomerAuthorizationFactory.create(**kwargs)
        self.addCleanup(CustomerAuthorizationFactory.cleanup, name)
        return name

    def test_validate_labels_the_card_by_brand_and_last_four(self) -> None:
        """validate builds a label a person can pick the card out by."""
        card = frappe.get_doc(AUTHORIZATION, self.card())
        card.validate()

        self.assertEqual(card.card_label, "Visa •••• 4081")

    def test_a_card_without_last_four_is_labelled_by_brand(self) -> None:
        """A card carrying no digits is labelled by its brand alone."""
        card = frappe.get_doc(AUTHORIZATION, self.card())
        card.last4 = None
        card.validate()

        self.assertEqual(card.card_label, "Visa")

    def test_a_future_expiry_leaves_the_card_usable(self) -> None:
        """A card whose expiry is ahead can still be charged."""
        card = frappe.get_doc(AUTHORIZATION, self.card())

        self.assertFalse(card.has_expired())
        self.assertTrue(card.is_usable())

    def test_a_past_expiry_retires_the_card(self) -> None:
        """A card whose expiry has passed is no longer chargeable."""
        card = frappe.get_doc(AUTHORIZATION, self.card(exp_year=str(getdate(today()).year - 1)))

        self.assertTrue(card.has_expired())
        self.assertFalse(card.is_usable())

    def test_an_absent_expiry_reads_as_unexpired(self) -> None:
        """A card carrying no expiry is not treated as retired."""
        card = frappe.get_doc(AUTHORIZATION, self.card())
        card.exp_month = None

        self.assertFalse(card.has_expired())

    def test_a_deactivated_card_is_not_usable(self) -> None:
        """A card a customer withdrew is refused even before it expires."""
        card = frappe.get_doc(AUTHORIZATION, self.card(active=False))

        self.assertFalse(card.is_usable())

    def test_a_single_use_card_is_not_usable(self) -> None:
        """A card Paystack marked non-reusable cannot be charged again."""
        card = frappe.get_doc(AUTHORIZATION, self.card(reusable=False))

        self.assertFalse(card.is_usable())

    def test_the_authorization_code_is_held_out_of_the_row(self) -> None:
        """The code that charges the card reads back through the password store."""
        name = self.card(authorization_code="AUTH_secret_9999")

        self.assertEqual(frappe.get_doc(AUTHORIZATION, name).get_authorization_code(), "AUTH_secret_9999")
