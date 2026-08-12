# Copyright (c) 2026, Anthony Emmanuel and Contributors
# See license.txt

import frappe

from frappe_paystack.tests.factories import (
    TEST_COMPANY,
    GatewaySettingFactory,
    PaymentLogFactory,
    cleanup_doc,
)
from frappe_paystack.tests.test_base import PaystackTestCase
from frappe_paystack.utils.reconciliation import MANUAL_OVERRIDE

RECONCILIATION_LOG = "Paystack Reconciliation Log"


class TestPaystackReconciliationLog(PaystackTestCase):
    """The Paystack Reconciliation Log doctype."""

    def setUp(self) -> None:
        """Reconcile against one captured payment."""
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

        self.payment_log = PaymentLogFactory.create(status="Processed", amount=1000, amount_paid=1000)
        self.addCleanup(PaymentLogFactory.cleanup, self.payment_log)

    def build(self, paystack_amount: float, frappe_amount: float, status: str = "Pending"):
        """Return an unsaved reconciliation row for the captured payment."""
        return frappe.get_doc(
            {
                "doctype": RECONCILIATION_LOG,
                "reconciliation_date": frappe.utils.today(),
                "payment_log": self.payment_log,
                "company": TEST_COMPANY,
                "status": status,
                "paystack_amount": paystack_amount,
                "frappe_amount": frappe_amount,
            }
        )

    def insert(self, row) -> str:
        """Insert a reconciliation row and register its cleanup."""
        row.flags.ignore_permissions = True
        row.insert()
        self.addCleanup(cleanup_doc, RECONCILIATION_LOG, row.name)
        return row.name

    def test_validate_sets_the_difference_from_the_two_amounts(self) -> None:
        """validate records what Paystack reports over what ERPNext holds."""
        row = self.build(paystack_amount=1000, frappe_amount=900)
        row.validate()

        self.assertAlmostEqual(row.difference, 100.0, places=2)

    def test_validate_refuses_a_negative_paystack_amount(self) -> None:
        """validate rejects a negative amount on the Paystack side."""
        with self.assertRaises(frappe.ValidationError):
            self.build(paystack_amount=-1, frappe_amount=0).validate()

    def test_validate_refuses_a_negative_frappe_amount(self) -> None:
        """validate rejects a negative amount on the ERPNext side."""
        with self.assertRaises(frappe.ValidationError):
            self.build(paystack_amount=0, frappe_amount=-1).validate()

    def test_a_reconciled_row_has_to_balance(self) -> None:
        """A row marked Reconciled while the amounts differ is refused."""
        with self.assertRaises(frappe.ValidationError):
            self.build(paystack_amount=1000, frappe_amount=900, status="Reconciled").validate()

    def test_a_balanced_row_reconciles(self) -> None:
        """A row whose amounts agree is accepted as Reconciled."""
        name = self.insert(self.build(paystack_amount=1000, frappe_amount=1000, status="Reconciled"))

        self.assertEqual(frappe.db.get_value(RECONCILIATION_LOG, name, "status"), "Reconciled")

    def test_the_status_field_offers_the_manual_override(self) -> None:
        """The Select field lists the status a human sets by hand."""
        options = frappe.get_meta(RECONCILIATION_LOG).get_field("status").options

        self.assertIn(MANUAL_OVERRIDE, options.split("\n"))

    def test_one_payment_reconciles_once(self) -> None:
        """payment_log is unique, so a payment carries a single reconciliation."""
        field = frappe.get_meta(RECONCILIATION_LOG).get_field("payment_log")

        self.assertTrue(field.unique)
