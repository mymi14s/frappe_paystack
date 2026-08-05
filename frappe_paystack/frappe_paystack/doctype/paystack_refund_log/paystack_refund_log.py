# Copyright (c) 2026, Anthony Emmanuel and contributors
# For license information, please see license.txt

from typing import Any, Optional

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, fmt_money, getdate

from frappe_paystack.utils import (
    customer_email,
    discard_draft_payment_entry,
    normalize_currency,
    party_account_for,
    party_account_rate,
    record_failure,
)

from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

# refund_amount is held in the charge currency, as Payment Log.amount_paid is.
GATEWAY_DOCTYPE = "Paystack Gateway Setting"
SALES_INVOICE = "Sales Invoice"

REFUND_LOG_DOCTYPE = "Paystack Refund Log"
REFUND_RECEIPT_PRINT_FORMAT = "Paystack Refund Receipt"

# Payment Log statuses that still hold refundable money.
REFUNDABLE_LOG_STATUSES = ("Completed", "Partially Refunded")


class PaystackRefundLog(Document):
    def validate(self) -> None:
        self.validate_draft_only()
        self.validate_payment_log()
        self.validate_refund_amount()
        if self.currency:
            self.currency = normalize_currency(self.currency)

        if self.status not in ("Pending", "Processed", "Completed", "Failed"):
            frappe.throw(_("Invalid status value."))

    def validate_draft_only(self) -> None:
        """Keep the log in draft; the reversal is booked from on_update."""
        if self.docstatus:
            frappe.throw(
                _("Paystack Refund Log is a draft-only record and cannot be submitted or cancelled.")
            )

    def validate_payment_log(self) -> None:
        """Ensure the referenced Payment Log exists and still holds money."""
        if not self.payment_log:
            frappe.throw(_("Payment Log is required to create a Refund Log."))

        if not frappe.db.exists("Paystack Payment Log", self.payment_log):
            frappe.throw(_("Payment Log {0} does not exist.").format(self.payment_log))

        payment_log_status = frappe.db.get_value("Paystack Payment Log", self.payment_log, "status")
        if payment_log_status not in REFUNDABLE_LOG_STATUSES:
            frappe.throw(
                _("Payment Log {0} is {1}. Only completed payments can be refunded.").format(
                    self.payment_log, payment_log_status
                )
            )

    def validate_refund_amount(self) -> None:
        """Ensure the refund amount is positive and within the refundable balance."""
        if flt(self.refund_amount) <= 0:
            frappe.throw(_("Refund amount must be greater than zero."))

        amount_paid = flt(frappe.db.get_value("Paystack Payment Log", self.payment_log, "amount_paid"))
        total_refunded = get_total_refunded(self.payment_log, exclude=self.name)

        available = amount_paid - total_refunded
        if flt(self.refund_amount) > available:
            frappe.throw(
                _(
                    "Refund amount ({0}) exceeds available refundable amount ({1}). "
                    "Amount paid: {2}, Already refunded: {3}."
                ).format(self.refund_amount, available, amount_paid, total_refunded)
            )

    def on_update(self) -> None:
        """Create a reversal Payment Entry when the refund is Processed."""
        if self.status != "Processed" or self.reversal_payment_entry:
            return

        payment_log = frappe.get_doc("Paystack Payment Log", self.payment_log)

        # This refund's own reference wins, with the payment's document as fallback.
        reference_doctype = self.linked_doctype or payment_log.linked_doctype
        reference_docname = self.linked_docname or payment_log.linked_docname

        try:
            inv = frappe.get_doc(reference_doctype, reference_docname)
        except Exception:
            frappe.log_error(
                f"Paystack refund {self.name}: linked document "
                f"{reference_doctype} {reference_docname} could not be fetched"
            )
            return

        booked = None
        try:
            gateway_settings = payment_log.get_payment_public_key()
            if not gateway_settings:
                frappe.throw(_("No enabled Paystack gateway for this company."))

            pe = self.build_reversal_entry(inv, gateway_settings)
            pe.mode_of_payment = gateway_settings.get("mode_of_payment")
            pe.reference_date = getdate()
            pe.reference_no = self.refund_reference or self.name
            pe.remarks = f"Reversal from Paystack Refund Log {self.name}"

            pe.flags.ignore_permissions = True
            booked = pe
            pe.save()
            pe.submit()

            self.db_set("reversal_payment_entry", pe.name, update_modified=False)
            self.db_set("status", "Completed", update_modified=True)
            self.update_payment_log_total_refunded()
            self.send_refund_receipt_email()
        except Exception:
            discard_draft_payment_entry(booked)
            self.db_set(
                "errors",
                record_failure(f"Paystack reversal Payment Entry creation failed for refund {self.name}"),
                update_modified=True,
            )

    def build_reversal_entry(self, inv: Any, gateway_settings: dict) -> Any:
        """
        Build the Payment Entry that returns money to the customer.

        A credit note takes a negative allocation; any other reference builds an
        unallocated reversal off the suspense account.
        """
        suspense_account = gateway_settings.get("suspense_account")
        party_account = party_account_for(inv)
        rate = party_account_rate(inv, party_account)
        party_amount = flt(flt(self.refund_amount) / rate, inv.precision("grand_total"))

        if inv.doctype == SALES_INVOICE and inv.get("is_return"):
            pe = get_payment_entry(
                inv.doctype,
                inv.name,
                party_amount=-party_amount,
                bank_account=suspense_account,
            )
        else:
            pe = frappe.new_doc("Payment Entry")
            pe.payment_type = "Pay"
            pe.company = inv.company
            pe.posting_date = getdate()
            pe.party_type = "Customer"
            pe.party = inv.customer
            pe.paid_from = suspense_account
            pe.paid_to = party_account
            pe.paid_amount = flt(self.refund_amount)
            pe.received_amount = party_amount

        # The reversal carries the document's own exchange rate.
        if rate != 1:
            pe.target_exchange_rate = rate

        return pe

    def on_trash(self) -> None:
        """Refuse deletion of a Processed or Completed log, or one with a reversal."""
        if self.status in ("Processed", "Completed"):
            frappe.throw(
                _("Cannot delete a Processed or Completed Refund Log. Cancel the linked Payment Entry first.")
            )
        if self.reversal_payment_entry:
            frappe.throw(
                _(
                    "Cannot delete this log because it is linked to Payment Entry {0}. "
                    "Cancel the Payment Entry first."
                ).format(self.reversal_payment_entry)
            )

    def update_payment_log_total_refunded(self) -> None:
        """Update total_refunded and the refund status on the linked Payment Log."""
        total = get_total_refunded(self.payment_log)
        amount_paid = flt(frappe.db.get_value("Paystack Payment Log", self.payment_log, "amount_paid"))

        frappe.db.set_value(
            "Paystack Payment Log",
            self.payment_log,
            {"total_refunded": total, "status": refund_status(total, amount_paid)},
            update_modified=False,
        )

    def send_refund_receipt_email(self) -> None:
        """Email the customer a refund receipt when the refund is Completed."""
        if self.status != "Completed":
            return

        # The customer is read off the billed document.
        customer = None
        if self.linked_doctype and self.linked_docname:
            customer = frappe.db.get_value(self.linked_doctype, self.linked_docname, "customer")

        recipient = customer_email(customer) if customer else ""
        if not recipient:
            return

        try:
            frappe.enqueue(
                frappe.sendmail,
                queue="short",
                timeout=300,
                recipients=[recipient],
                subject=_("Refund Receipt - {0}").format(self.name),
                message=_("A refund of {0} has been processed. Your receipt is attached.").format(
                    fmt_money(self.refund_amount, currency=self.currency)
                ),
                reference_doctype=REFUND_LOG_DOCTYPE,
                reference_name=self.name,
                attachments=[
                    frappe.attach_print(
                        REFUND_LOG_DOCTYPE,
                        self.name,
                        print_format=REFUND_RECEIPT_PRINT_FORMAT,
                    )
                ],
            )
        except Exception:
            frappe.log_error(
                f"Paystack refund receipt email failed for {self.name}",
                frappe.get_traceback(),
            )


def refund_status(total_refunded: float, amount_paid: float) -> str:
    """Return the Payment Log status for a refunded amount."""
    if flt(total_refunded) <= 0:
        return "Completed"
    if flt(total_refunded) >= flt(amount_paid):
        return "Refunded"
    return "Partially Refunded"


def get_total_refunded(payment_log_name: str, exclude: Optional[str] = None) -> float:
    """
    Return the total refunded for a Payment Log, optionally excluding one log.

    Processed and Completed refunds count.
    """
    filters = {
        "payment_log": payment_log_name,
        "status": ["in", ["Processed", "Completed"]],
    }
    if exclude:
        filters["name"] = ["!=", exclude]

    result = frappe.get_all(
        "Paystack Refund Log",
        filters=filters,
        fields=["sum(refund_amount) as total"],
    )[0]

    return flt(result.total) if result.total else 0.0


@frappe.whitelist()
def send_refund_receipt(refund_log_name: str) -> bool:
    """Send a refund receipt email to the customer."""
    if not frappe.db.exists(REFUND_LOG_DOCTYPE, refund_log_name):
        return False

    refund_log = frappe.get_doc(REFUND_LOG_DOCTYPE, refund_log_name)
    refund_log.check_permission("read")
    refund_log.send_refund_receipt_email()
    return True
