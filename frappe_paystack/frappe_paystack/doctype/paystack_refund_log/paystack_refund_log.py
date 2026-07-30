import frappe
from frappe.model.document import Document
from frappe.utils import flt, getdate

from frappe_paystack.utils import (
	get_customer_email,
	get_paid_to_account,
	log_integration_request,
	normalize_currency,
)

GATEWAY_DOCTYPE = "Paystack Gateway Setting"
SALES_INVOICE = "Sales Invoice"
SALES_ORDER = "Sales Order"

REFUND_LOG_DOCTYPE = "Paystack Refund Log"


class PaystackRefundLog(Document):
	def validate(self) -> None:
		self.validate_payment_log()
		self.validate_refund_amount()
		if self.currency:
			self.currency = normalize_currency(self.currency)

		if self.status not in ("Pending", "Processed", "Completed", "Failed"):
			frappe.throw("Invalid status value.")

	def validate_payment_log(self) -> None:
		"""Ensure the referenced Payment Log exists and is Completed."""
		if not self.payment_log:
			frappe.throw("Payment Log is required to create a Refund Log.")

		if not frappe.db.exists("Paystack Payment Log", self.payment_log):
			frappe.throw(f"Payment Log {self.payment_log} does not exist.")

		payment_log_status = frappe.db.get_value(
			"Paystack Payment Log", self.payment_log, "status"
		)
		if payment_log_status != "Completed":
			frappe.throw(
				f"Payment Log {self.payment_log} is not Completed. "
				f"Only completed payments can be refunded."
			)

	def validate_refund_amount(self) -> None:
		"""Ensure refund amount is positive and does not exceed available refundable amount."""
		if flt(self.refund_amount) <= 0:
			frappe.throw("Refund amount must be greater than zero.")

		amount_paid = flt(
			frappe.db.get_value(
				"Paystack Payment Log", self.payment_log, "amount_paid"
			)
		)
		total_refunded = get_total_refunded(self.payment_log, exclude=self.name)

		available = amount_paid - total_refunded
		if flt(self.refund_amount) > available:
			frappe.throw(
				f"Refund amount ({self.refund_amount}) exceeds available refundable "
				f"amount ({available}). Amount paid: {amount_paid}, "
				f"Already refunded: {total_refunded}."
			)

	def on_update(self) -> None:
		"""Create a reversal Payment Entry when the refund is Processed."""
		if self.status != "Processed" or self.reversal_payment_entry:
			return

		if self.docstatus in [1, 2]:
			return

		payment_log = frappe.get_doc("Paystack Payment Log", self.payment_log)

		try:
			inv = frappe.get_doc(payment_log.linked_doctype, payment_log.linked_docname)
		except Exception:
			frappe.log_error(
				f"Could not fetch linked document {payment_log.linked_doctype} "
				f"{payment_log.linked_docname} for refund {self.name}"
			)
			return

		try:
			gateway_settings = payment_log.get_payment_public_key()
			if not gateway_settings:
				frappe.throw("No enabled Paystack gateway for this company.")

			pe = frappe.new_doc("Payment Entry")
			pe.payment_type = "Pay"
			pe.company = inv.company
			pe.posting_date = getdate()
			pe.mode_of_payment = gateway_settings.get("mode_of_payment")
			pe.party_type = "Customer"
			pe.party = inv.customer

			if inv.doctype == SALES_INVOICE:
				pe.paid_from = gateway_settings.get("suspense_account")
				pe.paid_to = inv.debit_to
			elif inv.doctype == SALES_ORDER:
				pe.paid_from = gateway_settings.get("suspense_account")
				account = get_paid_to_account(inv.customer, inv.company)
				pe.paid_to = account

			pe.paid_amount = flt(self.refund_amount)
			pe.received_amount = flt(self.refund_amount)
			pe.reference_date = getdate()
			pe.reference_no = self.refund_reference or self.name
			pe.remarks = f"Reversal from Paystack Refund Log {self.name}"

			if inv.doctype == SALES_INVOICE and hasattr(inv, "is_return") and inv.is_return:
				pe.append(
					"references",
					{
						"reference_doctype": inv.doctype,
						"reference_name": inv.name,
						"allocated_amount": flt(self.refund_amount),
					},
				)

			pe.flags.ignore_permissions = True
			pe.save()
			pe.submit()

			self.db_set(
				"reversal_payment_entry", pe.name, update_modified=False
			)
			self.db_set("status", "Completed", update_modified=True)
			self.reload()
			self.submit()
		except Exception:
			frappe.log_error(
				"Failed to create reversal Payment Entry from Paystack Refund Log",
				f"{self.name} - {frappe.get_traceback()}",
			)
			self.db_set("status", "Failed", update_modified=True)
			self.db_set(
				"errors",
				"Reversal Payment Entry creation failed. See Error Log for details.",
				update_modified=True,
			)
		finally:
			self.reload()

	def on_trash(self) -> None:
		"""Prevent deletion of processed refund logs or logs with a reversal PE."""
		if self.status in ("Processed", "Completed"):
			frappe.throw(
				"Cannot delete a Processed or Completed Refund Log. "
				"Cancel the linked Payment Entry first."
			)
		if self.reversal_payment_entry:
			frappe.throw(
				f"Cannot delete this log because it is linked to Payment Entry "
				f"{self.reversal_payment_entry}. Cancel the Payment Entry first."
			)

	def send_refund_receipt_email(self) -> None:
		"""Email the customer a refund receipt when the refund is Completed."""
		if self.status != "Completed":
			return

		payment_log = frappe.get_doc("Paystack Payment Log", self.payment_log)
		customer_email = get_customer_email(payment_log.get("customer", ""))

		if not customer_email:
			return

		try:
			email_subject = f"Refund Receipt - {self.name}"
			email_body = f"""
				<h3>Refund Receipt</h3>
				<p>Dear Customer,</p>
				<p>A refund of {self.currency} {self.refund_amount} has been processed.</p>
				<p>Refund Reference: {self.refund_reference or self.name}</p>
				<p>Reason: {self.refund_reason or 'N/A'}</p>
				<p>Thank you.</p>
			"""

			frappe.sendmail(
				recipients=[customer_email],
				subject=email_subject,
				message=email_body,
				reference_doctype=REFUND_LOG_DOCTYPE,
				reference_name=self.name,
				delayed=False,
			)
		except Exception:
			frappe.log_error(
				f"Failed to send refund receipt email for {self.name}",
				frappe.get_traceback(),
			)


def get_total_refunded(payment_log_name: str, exclude: str = None) -> float:
	"""Return the total refunded amount for a Payment Log, excluding a specific Refund Log."""
	filters = {
		"payment_log": payment_log_name,
		"status": ["in", ["Processed", "Completed"]],
	}
	if exclude:
		filters["name"] = ["!=", exclude]

	result = frappe.db.get_all(
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
	refund_log.send_refund_receipt_email()
	return True