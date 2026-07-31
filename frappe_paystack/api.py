import hashlib
import hmac
import json
from typing import Any, Optional

import frappe

from frappe_paystack.utils import (
	coalesce_currency,
	initiate_refund,
	is_ip_allowed,
	is_paystack_enabled,
	log_integration_request,
	resolve_paystack_settings,
	verify_signature,
)

LOG_DOCTYPE = "Paystack Payment Log"


@frappe.whitelist()
def is_enabled_for_company(company: str) -> bool:
	"""Check whether Paystack is enabled for a company."""
	return is_paystack_enabled(company)


def log_pending_payment(doc: Any, amount: float, currency: str) -> Any:
	"""Insert a Pending Paystack Payment Log and return the document."""
	log = frappe.new_doc("Paystack Payment Log")
	log.company = doc.company
	log.linked_doctype = doc.doctype
	log.linked_docname = doc.name
	log.amount = amount
	log.currency = currency or "NGN"
	log.status = "Pending"
	log.insert(ignore_permissions=True)
	return log


def company_from_reference(reference: str) -> Optional[str]:
	"""Return the company stored on a Paystack Payment Log."""
	try:
		return frappe.db.get_value("Paystack Payment Log", reference, "company")
	except Exception:
		return None


def verify_paystack_signature(payload: bytes, signature: str, secret: str) -> bool:
	"""Verify the Paystack webhook signature using HMAC-SHA512."""
	expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha512).hexdigest()
	return hmac.compare_digest(expected, signature or "")


@frappe.whitelist(allow_guest=True)
def paystack_webhook() -> None:
	"""Receive and process Paystack webhook events."""
	data = frappe.request.get_json() or {}
	payload = frappe.request.data or b""
	signature = frappe.get_request_header("x-paystack-signature")
	request_ip = getattr(frappe.local, "request_ip", None)

	metadata = frappe._dict(dict(data.get("data")).get("metadata"))
	ref = metadata.get("reference")

	company = company_from_reference(ref) if ref else None
	settings = resolve_paystack_settings(company) if company else None

	if settings and not is_ip_allowed(settings.get("allowed_webhook_ips"), request_ip):
		log_integration_request(
			status="Failed",
			url="webhook",
			request_data=data,
			error=f"IP {request_ip} not in allowlist",
			reference_doctype=LOG_DOCTYPE,
			reference_docname=ref,
		)
		frappe.throw("IP not allowed", frappe.PermissionError)

	if not settings:
		log_integration_request(
			status="Failed",
			url="webhook",
			request_data=data,
			error="No Paystack settings for company",
			reference_doctype=LOG_DOCTYPE,
			reference_docname=ref,
		)
		frappe.throw("No Paystack settings for company", frappe.PermissionError)

	webhook_secret = settings.get("webhook_secret") or settings.get("secret_key")
	if not verify_signature(payload, signature, webhook_secret):
		log_integration_request(
			status="Failed",
			url="webhook",
			request_data=data,
			error="Invalid Paystack signature",
			reference_doctype=LOG_DOCTYPE,
			reference_docname=ref,
		)
		frappe.throw("Invalid Paystack signature", frappe.PermissionError)

	process_webhook_event(data)

	frappe.local.response["http_status_code"] = 201


def process_webhook_event(data: dict) -> None:
	"""Route webhook events to the appropriate handler based on event type."""
	event = data.get("event", "")

	if event in ("refund.processed", "refund.failed"):
		process_refund_webhook_event(data)
	else:
		process_charge_webhook_event(data)


def process_refund_webhook_event(data: dict) -> None:
	"""Handle refund.processed and refund.failed webhook events."""
	try:
		tx = frappe._dict(data.get("data")) or {}
		event = data.get("event", "")
		transaction_id = tx.get("transaction", {}).get("reference") or tx.get(
			"transaction_reference"
		)
		refund_reference = tx.get("reference")
		amount = (tx.get("amount") or 0) / 100
		currency = (tx.get("currency") or "NGN").upper()

		if not transaction_id:
			return

		refund_log_name = frappe.db.get_value(
			"Paystack Refund Log",
			{"transaction_id": transaction_id, "refund_reference": refund_reference},
			"name",
		)
		if not refund_log_name:
			return

		existing_status = frappe.db.get_value(
			"Paystack Refund Log", refund_log_name, "status"
		)
		if existing_status in ("Processed", "Completed"):
			return

		refund_log = frappe.get_doc("Paystack Refund Log", refund_log_name)
		if event == "refund.processed":
			refund_log.status = "Processed"
		else:
			refund_log.status = "Failed"

		refund_log.refund_reference = refund_reference
		refund_log.raw_response = json.dumps(tx)
		refund_log.integration_request = log_integration_request(
			status="Completed",
			url="webhook",
			request_data=data,
			response_data={"refund_reference": refund_reference, "status": refund_log.status},
			reference_doctype="Paystack Refund Log",
			reference_docname=refund_log_name,
		)
		refund_log.save(ignore_permissions=True)
		frappe.db.commit()
	except Exception as e:
		frappe.log_error(str(e), "Paystack refund webhook")


def process_charge_webhook_event(data: dict) -> None:
	"""Handle charge.success and charge.failed webhook events."""
	try:
		tx = frappe._dict(data.get("data")) or {}
		metadata = frappe._dict(tx.get("metadata"))
		ref = metadata.get("reference")
		amount = (tx.get("amount") or 0) / 100
		currency = (tx.get("currency") or "NGN").upper()
		if not ref:
			return
		name = ref if frappe.db.exists("Paystack Payment Log", ref) else None
		if not name:
			return

		existing_status = frappe.db.get_value("Paystack Payment Log", name, "status")
		if existing_status in ("Processed", "Completed"):
			return

		log = frappe.get_doc("Paystack Payment Log", name)
		log.status = "Processed" if tx.get("status") == "success" else "Failed"
		log.amount_paid = amount
		log.currency_paid = currency
		log.payment_reference = tx.get("reference")
		log.transaction_id = tx.get("reference")
		log.idempotency_key = tx.get("reference")
		log.payment_date = tx.get("paid_at").split("T")[0]
		log.raw_response = json.dumps(tx)
		log.integration_request = log_integration_request(
			status="Completed",
			url="webhook",
			request_data=data,
			response_data={"reference": ref, "status": log.status},
			reference_doctype=LOG_DOCTYPE,
			reference_docname=name,
		)
		log.save(ignore_permissions=True)
		frappe.db.commit()
		if not frappe.db.get_value("Customer", metadata.get("customer"), "email_id"):
			frappe.db.set_value(
				"Customer", metadata.get("customer"), "email_id", metadata.get("email")
			)
	except Exception as e:
		frappe.log_error(str(e), "Paystack payment")


@frappe.whitelist()
def create_payment_link(
	doctype: str,
	docname: str,
	amount: Optional[float] = None,
	currency: Optional[str] = None,
) -> str:
	"""Create a Paystack Payment Log and return the checkout URL."""
	doc = frappe.get_doc(doctype, docname)
	settings = resolve_paystack_settings(getattr(doc, "company", None))
	if not settings:
		frappe.throw(f"Paystack not enabled for {getattr(doc, 'company', '')}")

	if amount is None:
		if doctype == "Sales Order":
			total = float(getattr(doc, "grand_total", 0) or 0)
			adv = float(getattr(doc, "advance_paid", 0) or 0)
			amount = max(0.0, total - adv)
		else:
			amount = float(getattr(doc, "outstanding_amount", 0) or 0)
	currency = coalesce_currency(currency, getattr(doc, "company", None), settings)

	reference = log_pending_payment(doc, amount, currency)
	return reference.get_payment_link()


@frappe.whitelist(allow_guest=True)
def validate_payment_link(docname: str) -> dict:
	"""Return payment data for the checkout page."""
	if frappe.db.exists(LOG_DOCTYPE, docname):
		doc = frappe.get_doc(LOG_DOCTYPE, docname)
		return doc.get_data()
	return {}


@frappe.whitelist()
def initiate_refund_from_log(
	payment_log_name: str,
	amount: float,
	reason: Optional[str] = None,
) -> str:
	"""Create a Paystack Refund Log and initiate the refund via the Paystack API.

	Args:
		payment_log_name: The name of the completed Paystack Payment Log.
		amount: The refund amount in major units.
		reason: Optional reason for the refund.

	Returns:
		The name of the created Paystack Refund Log.
	"""
	payment_log = frappe.get_doc("Paystack Payment Log", payment_log_name)

	if payment_log.status != "Completed":
		frappe.throw("Only completed payments can be refunded.")

	if not payment_log.transaction_id:
		frappe.throw("Payment Log has no transaction ID to refund.")

	refund_log = frappe.get_doc(
		{
			"doctype": "Paystack Refund Log",
			"payment_log": payment_log_name,
			"company": payment_log.company,
			"linked_doctype": payment_log.linked_doctype,
			"linked_docname": payment_log.linked_docname,
			"transaction_id": payment_log.transaction_id,
			"refund_amount": amount,
			"currency": payment_log.currency or "NGN",
			"status": "Pending",
			"refund_reason": reason or "Manual refund",
		}
	)
	refund_log.flags.ignore_permissions = True
	refund_log.insert()

	try:
		result = initiate_refund(
			transaction_id=payment_log.transaction_id,
			amount=amount,
			currency=payment_log.currency or "NGN",
			company=payment_log.company,
			merchant_note=reason,
		)
		refund_log.db_set("status", "Processed", update_modified=True)
		refund_log.db_set("refund_reference", result.get("reference"), update_modified=False)
		refund_log.db_set("raw_response", frappe.as_json(result.get("raw")), update_modified=False)
		refund_log.reload()
		refund_log.run_method("on_update")
	except Exception:
		frappe.log_error(
			f"Manual refund failed for Payment Log {payment_log_name}",
			frappe.get_traceback(),
		)
		refund_log.db_set("status", "Failed", update_modified=True)
		refund_log.db_set(
			"errors",
			"Refund initiation failed. See Error Log for details.",
			update_modified=True,
		)

	return refund_log.name