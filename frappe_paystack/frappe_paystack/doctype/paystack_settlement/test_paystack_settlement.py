# Copyright (c) 2026, Anthony Emmanuel and Contributors
# See license.txt

import frappe

from frappe_paystack.tests.factories import SettlementFactory
from frappe_paystack.tests.test_base import PaystackTestCase

SETTLEMENT = "Paystack Settlement"


class TestPaystackSettlement(PaystackTestCase):
    """The Paystack Settlement doctype."""

    def payout(self) -> str:
        """Insert a payout and register its cleanup."""
        name = SettlementFactory.create()
        self.addCleanup(SettlementFactory.cleanup, name)
        return name

    def test_validate_upper_cases_the_currency(self) -> None:
        """validate trims and upper-cases a currency code."""
        payout = frappe.get_doc(SETTLEMENT, self.payout())
        payout.currency = " ngn "
        payout.validate()

        self.assertEqual(payout.currency, "NGN")

    def test_validate_leaves_a_blank_currency_alone(self) -> None:
        """validate accepts a payout carrying no currency."""
        payout = frappe.get_doc(SETTLEMENT, self.payout())
        payout.currency = None
        payout.validate()

        self.assertIsNone(payout.currency)

    def test_on_trash_blocks_a_posted_payout(self) -> None:
        """on_trash refuses to delete a payout carrying a Journal Entry."""
        name = self.payout()
        frappe.db.set_value(SETTLEMENT, name, "journal_entry", "JV-FAKE-001")

        with self.assertRaises(frappe.ValidationError):
            frappe.get_doc(SETTLEMENT, name).on_trash()

    def test_on_trash_allows_an_unposted_payout(self) -> None:
        """on_trash deletes a payout that reached no ledger."""
        name = SettlementFactory.create()

        frappe.delete_doc(SETTLEMENT, name, ignore_permissions=True)

        self.assertFalse(frappe.db.exists(SETTLEMENT, name))

    def test_the_settlement_id_has_a_unique_constraint(self) -> None:
        """settlement_id is unique, so one payout is recorded once."""
        field = frappe.get_meta(SETTLEMENT).get_field("settlement_id")

        self.assertTrue(field.unique)
