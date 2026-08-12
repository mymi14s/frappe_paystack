# Copyright (c) 2026, Anthony Emmanuel and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class PaystackSettlement(Document):
    def validate(self) -> None:
        """Normalise the currency to trimmed upper case."""
        if self.currency:
            self.currency = self.currency.upper().strip()

    def on_trash(self) -> None:
        """Refuse deletion while the payout's journal entry stands."""
        if self.journal_entry:
            frappe.throw(
                _(
                    "Cannot delete this settlement because it is linked to Journal Entry {0}. "
                    "Cancel the Journal Entry first."
                ).format(self.journal_entry)
            )
