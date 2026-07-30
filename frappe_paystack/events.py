import frappe
from frappe.utils import flt

from frappe_paystack.utils import initiate_refund, resolve_paystack_settings


def sales_invoice_on_submit(doc, method: str = None) -> None:
	"""Auto-initiate a Paystack refund when a credit note is submitted.

	If the Sales Invoice is a return (is_return=1) with a return_against,
	and the original invoice was paid via Paystack, and the gateway setting
	has auto_refund_on_credit_note enabled, create a Refund Log and call
	the Paystack refund API.
	"""
	if not getattr(doc, "is_return", False):
		return

	if not getattr(doc, "return_against", None):
		return

	settings = resolve_paystack_settings(getattr(doc, "company", None))
	if not settings or not settings.get("auto_refund_on_credit_note"):
		return

	payment_log_name = frappe.db.get_value(
		"Paystack Payment Log",
		{
			"linked_docname": doc.return_against,
			"status": "Completed",
		},
		"name",
	)
	if not payment_log_name:
		return

	payment_log = frappe.get_doc("Paystack Payment Log", payment_log_name)
	amount_paid = flt(payment_log.amount_paid)

	if amount_paid <= 0:
		return

	credit_note_total = flt(getattr(doc, "grand_total", 0) or 0)
	refund_amount = min(credit_note_total, amount_paid)

	if refund_amount <= 0:
		return

	transaction_id = payment_log.transaction_id
	currency = payment_log.currency or "NGN"

	refund_log = frappe.get_doc(
		{
			"doctype": "Paystack Refund Log",
			"payment_log": payment_log_name,
			"company": doc.company,
			"linked_doctype": doc.doctype,
			"linked_docname": doc.name,
			"transaction_id": transaction_id,
			"refund_amount": refund_amount,
			"currency": currency,
			"status": "Pending",
			"refund_reason": f"Auto-refund from credit note {doc.name}",
		}
	)
	refund_log.flags.ignore_permissions = True
	refund_log.insert()

	try:
		result = initiate_refund(
			transaction_id=transaction_id,
			amount=refund_amount,
			currency=currency,
			company=doc.company,
			merchant_note=f"Credit note {doc.name}",
		)
		refund_log.db_set("status", "Processed", update_modified=True)
		refund_log.db_set("refund_reference", result.get("reference"), update_modified=False)
		refund_log.db_set("raw_response", frappe.as_json(result.get("raw")), update_modified=False)
		refund_log.reload()
		refund_log.run_method("on_update")
	except Exception:
		frappe.log_error(
			f"Auto-refund failed for credit note {doc.name}",
			frappe.get_traceback(),
		)
		refund_log.db_set("status", "Failed", update_modified=True)
		refund_log.db_set(
			"errors",
			"Auto-refund initiation failed. See Error Log for details.",
			update_modified=True,
		)


def sales_invoice_on_cancel(doc, method: str = None) -> None:
	"""No-op placeholder for Sales Invoice cancel event.

	Canceling a credit note does not automatically reverse the Paystack refund.
	The reversal Payment Entry must be cancelled manually.
	"""
	pass