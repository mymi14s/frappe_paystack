import hashlib
import hmac
import json
from typing import Any, Optional

import frappe

from frappe_paystack.utils import (
	coalesce_currency,
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
	"""Update the Paystack Payment Log from a webhook event payload."""
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