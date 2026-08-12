# Copyright (c) 2026, Anthony Emmanuel and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

# Amounts within this margin are treated as equal.
AMOUNT_TOLERANCE = 0.01


class PaystackReconciliationLog(Document):
    """Holds the reconciliation status of a Paystack payment against ERPNext."""

    def validate(self) -> None:
        """Check the amounts and set the difference from them."""
        self.validate_amounts()
        self.difference = flt(self.paystack_amount) - flt(self.frappe_amount)
        self.validate_status()

    def validate_amounts(self) -> None:
        """Reject negative amounts on either side."""
        if flt(self.paystack_amount) < 0 or flt(self.frappe_amount) < 0:
            frappe.throw(_("Reconciliation amounts cannot be negative."), frappe.ValidationError)

    def validate_status(self) -> None:
        """Ensure a Reconciled record balances within the tolerance."""
        if self.status == "Reconciled" and abs(flt(self.difference)) >= AMOUNT_TOLERANCE:
            frappe.throw(
                _("Cannot mark as Reconciled: amounts differ by {0}.").format(self.difference),
                frappe.ValidationError,
            )
