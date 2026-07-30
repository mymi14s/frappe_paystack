import frappe
from frappe import log_error
from frappe.model.document import Document
from frappe.utils import flt, getdate

from frappe_paystack.utils import (
	get_customer_email,
	get_paid_to_account,
	normalize_currency,
	validate_payment,
)

GATEWAY_DOCTYPE = "Paystack Gateway Setting"
SALES_ORDER = "Sales Order"
SALES_INVOICE = "Sales Invoice"

SETTLED_STATUSES = [
	"Partly Paid",
	"Unpaid",
	"Overdue",
	"To Deliver and Bill",
	"To Bill",
	"To Deliver",
]


class PaystackPaymentLog(Document):
	def before_insert(self) -> None:
		self.validate_payment()
		validated = self.validate_record()
		if validated:
			frappe.throw(validated)

	def validate_record(self) -> str:
		errors = ""
		error_id = f"{self.linked_doctype} - {self.linked_docname}"
		if self.status == "Completed":
			return ""
		elif frappe.db.exists(self.linked_doctype, {"name": self.linked_docname}):
			doc = frappe.get_doc(self.linked_doctype, self.linked_docname)
			if doc.docstatus == 1 and doc.status not in SETTLED_STATUSES:
				errors = f"{error_id}: Document already settled"
			elif doc.docstatus in [0, 2]:
				errors = f"{error_id}: Document has been cancelled or in draft."
		else:
			errors = f"{error_id}: Document not found."

		return errors

	def validate(self) -> None:
		if self.currency:
			self.currency = normalize_currency(self.currency)

		validated = self.validate_record()
		if validated:
			self.db_set("errors", validated)
			self.reload()
		if validated:
			frappe.throw(validated)
		if bool(self.linked_doctype) ^ bool(self.linked_docname):
			frappe.throw(
				"Linked Doctype and Linked Docname must both be set or both be empty."
			)

		if flt(self.amount) < 0:
			frappe.throw("Amount cannot be negative.")

		if self.status not in ("Pending", "Processed", "Completed", "Failed"):
			frappe.throw("Invalid status value.")

	def on_update(self) -> None:
		"""Create a Payment Entry when the log is Processed/Completed."""
		if not (self.linked_doctype and self.linked_docname):
			return

		if (
			self.status not in ("Processed", "Completed")
			or self.docstatus in [1, 2]
			or self.payment_entry
		):
			return

		try:
			inv = frappe.get_doc(self.linked_doctype, self.linked_docname)
		except Exception:
			log_error(
				msg="Invoice fetch failed in PaystackPaymentLog.on_update",
				data={"log": self.name},
			)
			return

		if inv.doctype != SALES_ORDER:
			if flt(inv.outstanding_amount) <= 0:
				if self.status != "Completed":
					self.db_set("status", "Completed", update_modified=True)
				return

		paid_amount = round(self.amount_paid / inv.conversion_rate, 2)
		if paid_amount <= 0:
			return
		try:
			gateway_settings = self.get_payment_public_key()
			pe = frappe.new_doc("Payment Entry")
			pe.payment_type = "Receive"
			pe.company = inv.company
			pe.posting_date = getdate()
			pe.mode_of_payment = gateway_settings.get("mode_of_payment")
			pe.party_type = "Customer"
			pe.party = inv.customer
			if inv.doctype == SALES_INVOICE:
				pe.paid_from = inv.debit_to
			elif inv.doctype == SALES_ORDER:
				account = get_paid_to_account(inv.customer, inv.company)
				pe.paid_from = account
			pe.paid_to = gateway_settings.get("suspense_account")
			pe.paid_amount = paid_amount
			pe.received_amount = self.amount_paid
			pe.source_exchange_rate = inv.conversion_rate
			pe.reference_date = getdate()
			pe.target_exchange_rate = 1
			pe.reference_date = self.payment_date
			pe.reference_no = self.payment_reference
			pe.remarks = f"Auto-created from Paystack Payment Log {self.name}"

			pe.append(
				"references",
				{
					"reference_doctype": inv.doctype,
					"reference_name": inv.name,
					"allocated_amount": paid_amount,
				},
			)
			pe.flags.ignore_permissions = True
			pe.save()
			pe.submit()
			self.db_set("payment_entry", pe.name, update_modified=False)
			self.db_set("status", "Completed", update_modified=True)
			self.reload()
			self.submit()
		except Exception:
			frappe.log_error(
				"Failed to create Payment Entry from Paystack log",
				f"{self.name} - {frappe.get_traceback()}",
			)

	def get_payment_link(self) -> str:
		"""Return the checkout URL for this log."""
		return f"{frappe.utils.get_url()}/paystack-checkout/{self.name}"

	def get_payment_public_key(self) -> Optional[dict]:
		"""Return gateway settings (public key, currency, suspense, mode)."""
		if frappe.db.exists(GATEWAY_DOCTYPE, {"enabled": 1, "company": self.company}):
			doc = frappe.get_doc(
				GATEWAY_DOCTYPE, {"enabled": 1, "company": self.company}
			)
			return {
				"public_key": doc.get_password("public_key"),
				"currency": doc.currency,
				"suspense_account": doc.suspense_account,
				"mode_of_payment": doc.mode_of_payment,
			}
		return None

	def get_data(self) -> dict:
		"""Return payment data for the checkout page."""
		order = frappe.get_doc(self.linked_doctype, self.linked_docname)
		gateway_settings = self.get_payment_public_key()
		data = {
			"customer": order.customer,
			"exchange_rate": order.conversion_rate,
			"order_currency": order.currency,
			"grand_total": self.amount,
			"payment_amount": round(self.amount * order.conversion_rate, 2),
			"status": self.status,
			"order_status": order.status,
			"order_docstatus": order.docstatus,
			"order_no": order.name,
			"reference_doctype": self.linked_doctype,
			"reference_docname": self.linked_docname,
			"email": get_customer_email(order.customer) or "",
			"reference": self.name,
		}
		if gateway_settings:
			data.update(gateway_settings)

		return data

	def on_trash(self) -> None:
		"""Prevent deletion of processed logs or logs with a linked Payment Entry."""
		if self.status in ("Processed", "Completed"):
			frappe.throw(
				"Cannot delete a Processed or Completed Payment Log. "
				"Cancel the linked Payment Entry first."
			)
		if self.payment_entry:
			frappe.throw(
				f"Cannot delete this log because it is linked to Payment Entry "
				f"{self.payment_entry}. Cancel the Payment Entry first."
			)

	@frappe.whitelist()
	def validate_payment(self) -> Any:
		"""Verify the transaction via the Paystack API."""
		return validate_payment(self)