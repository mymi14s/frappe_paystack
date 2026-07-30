from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Dict, Optional

import frappe
import requests
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

SUPPORTED_CURRENCIES = ["NGN", "USD", "GHS", "ZAR", "KES"]

MINOR_FACTORS = {
	"NGN": 100,
	"USD": 100,
	"GHS": 100,
	"ZAR": 100,
	"KES": 100,
}


def log_integration_request(
	status: str,
	url: str,
	request_data: Any,
	response_data: Any = None,
	error: Optional[str] = None,
	reference_doctype: Optional[str] = None,
	reference_docname: Optional[str] = None,
) -> Optional[str]:
	"""Create an Integration Request record for audit/debugging."""
	try:
		integration_request = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_type": "Remote",
				"method": url,
				"status": status,
				"reference_doctype": reference_doctype,
				"reference_docname": reference_docname,
				"data": safe_json_dumps(request_data)
				if not isinstance(request_data, str)
				else request_data,
				"output": safe_json_dumps(response_data)
				if response_data and not isinstance(response_data, str)
				else (response_data or ""),
				"error": error or "",
			}
		)
		integration_request.flags.ignore_permissions = True
		integration_request.insert()
		return integration_request.name
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Failed to log Integration Request")
		return None


@frappe.whitelist()
def get_customer_contact(customer: str) -> Any:
	"""Get customer emails and phone numbers for the sales invoice frontend."""
	contact_data = frappe._dict({})
	if frappe.db.exists("Customer", {"name": customer}):
		customer_doc = frappe.get_doc("Customer", customer)
		if customer_doc.customer_primary_contact:
			contact = frappe.get_doc("Contact", customer_doc.customer_primary_contact)
			if contact.email_ids:
				contact_data.emails = [i.email_id for i in contact.email_ids]
			if contact.phone_nos:
				contact_data.phone_nos = [i.phone for i in contact.phone_nos]
	if not (contact_data.phone_nos or contact_data.email_id):
		return False
	return contact_data


@frappe.whitelist()
def get_customer_email(customer: str) -> str:
	"""Return the customer's email_id or empty string."""
	return frappe.db.get_value("Customer", customer, "email_id") or ""


def get_company_row_settings(company: Optional[str]) -> Optional[Dict[str, Any]]:
	"""Load the enabled Paystack Gateway Setting for a company."""
	DOCTYPE = "Paystack Gateway Setting"
	try:
		meta = frappe.get_meta(DOCTYPE)
	except Exception:
		return None

	row = {}
	filters = {"enabled": 1}
	if company and meta.has_field("company"):
		filters["company"] = company
	if frappe.db.exists(DOCTYPE, filters):
		row = frappe.get_doc(DOCTYPE, filters)

	if not row or not row.secret_key:
		return None

	default_currency = "NGN"
	if company:
		try:
			cur = frappe.db.get_value("Company", company, "default_currency")
			if cur:
				default_currency = (cur or "NGN").upper()
		except Exception:
			pass

	webhook_secret = None
	try:
		if hasattr(row, "get_password") and row.get("webhook_secret"):
			webhook_secret = row.get_password("webhook_secret")
	except Exception:
		webhook_secret = None
	if not webhook_secret:
		webhook_secret = row.get_password("secret_key")

	allowed_ips = None
	try:
		allowed_ips = row.get("allowed_webhook_ips") or None
	except Exception:
		allowed_ips = None

	return {
		"public_key": row.get("public_key"),
		"secret_key": row.get_password("secret_key"),
		"webhook_secret": webhook_secret,
		"test_mode": bool(row.get("test_mode")),
		"allowed_webhook_ips": allowed_ips,
		"callback_url": row.get("callback_url"),
		"webhook_url": row.get("webhook_url"),
		"default_currency": default_currency,
		"enable_auto_conversion": False,
		"enable_fee_accounting": False,
		"paystack_fee_account": None,
		"wallet_clearing_account": None,
		"default_bank_account": None,
	}


def resolve_paystack_settings(company: Optional[str]) -> Optional[Dict[str, Any]]:
	"""Return the settings dict for a company or None."""
	row = get_company_row_settings(company)
	if row and row.get("secret_key"):
		return row
	return None


def is_paystack_enabled(company: Optional[str]) -> bool:
	"""Check whether Paystack is enabled for a company."""
	return bool(resolve_paystack_settings(company))


def hmac_sha512(payload: bytes, secret: str) -> str:
	"""Compute HMAC-SHA512 hex digest."""
	return hmac.new(secret.encode("utf-8"), payload, hashlib.sha512).hexdigest()


def verify_signature(payload: bytes, signature: Optional[str], secret: str) -> bool:
	"""Compare Paystack x-paystack-signature header with computed HMAC-SHA512."""
	if not signature:
		return False
	expected = hmac_sha512(payload, secret)
	return hmac.compare_digest(expected, signature)


def is_ip_allowed(allowed_ips: Optional[str], request_ip: Optional[str]) -> bool:
	"""Check if the incoming request IP is in the allowed list."""
	if not allowed_ips:
		return True
	if not request_ip:
		return False
	allowed = [ip.strip() for ip in allowed_ips.split("\n") if ip.strip()]
	return request_ip in allowed


def safe_json_dumps(obj: Any) -> str:
	"""Safely serialize obj to JSON, returning '{}' on failure."""
	try:
		return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
	except Exception:
		return "{}"


def normalize_currency(cur: Optional[str]) -> str:
	"""Normalize a currency code to a supported value, defaulting to NGN."""
	cur = (cur or "NGN").upper().strip()
	return cur if cur in SUPPORTED_CURRENCIES else "NGN"


def to_minor_units(amount: float, currency: str) -> int:
	"""Convert a decimal amount to integer minor units for Paystack requests."""
	currency = normalize_currency(currency)
	factor = MINOR_FACTORS.get(currency, 100)
	return int(round(flt(amount) * factor))


def from_minor_units(amount_minor: int, currency: str) -> float:
	"""Convert integer minor units back to a decimal amount."""
	currency = normalize_currency(currency)
	factor = MINOR_FACTORS.get(currency, 100)
	return flt(amount_minor) / factor


def format_money(amount: float, currency: Optional[str]) -> str:
	"""Format an amount as a money string in the given currency."""
	return frappe.utils.fmt_money(flt(amount), currency=normalize_currency(currency))


def sanitize_reference(doctype: str, name: str, company: Optional[str]) -> str:
	"""Build a reference that encodes the doctype, docname, company."""
	company = (company or "").replace(" ", "")
	return f"{doctype}-{name}-{company}"


def parse_reference_company(reference: str) -> Optional[str]:
	"""Extract company from a reference created by sanitize_reference."""
	try:
		return reference.split("-", 2)[2]
	except Exception:
		return None


def get_company_currency(company: str) -> str:
	"""Return the normalized default currency for a company."""
	cur = frappe.db.get_value("Company", company, "default_currency") or "NGN"
	return normalize_currency(cur)


def coalesce_currency(
	currency: Optional[str],
	company: Optional[str],
	settings: Optional[Dict[str, Any]] = None,
) -> str:
	"""Choose the best currency: explicit -> company -> settings -> NGN."""
	if currency:
		return normalize_currency(currency)
	if company:
		try:
			return get_company_currency(company)
		except Exception:
			pass
	if settings and settings.get("default_currency"):
		return normalize_currency(settings["default_currency"])
	return "NGN"


def ensure_supported_currency(currency: str) -> None:
	"""Throw if currency is not supported by this integration."""
	cur = normalize_currency(currency)
	if cur not in SUPPORTED_CURRENCIES:
		frappe.throw(f"Currency {currency} is not supported for Paystack in this app.")


def clamp_amount_to_positive(amount: float) -> float:
	"""Force amounts to be >= 0 with proper rounding."""
	return max(0.0, round(flt(amount), 2))


def pick_company_for_doc(doc: Document) -> Optional[str]:
	"""Try to pull company from a document by most common attributes."""
	for fn in ("company", "party_company", "owning_company"):
		if hasattr(doc, fn) and getattr(doc, fn):
			return getattr(doc, fn)
	if doc.doctype == "Sales Invoice":
		return getattr(doc, "company", None)
	return None


def get_paid_to_account(customer: str, company: str) -> Optional[str]:
	"""Return the customer's receivable account for the given company."""
	accounts = [
		i.account
		for i in frappe.get_doc("Customer", customer).accounts
		if i.company == company
	]
	if accounts:
		return accounts[0]
	return None


def get_gateway_secret(gateway: str) -> str:
	"""Return the secret key for an enabled Paystack gateway."""
	return frappe.get_doc(
		"Paystack Gateway Setting", {"name": gateway, "enabled": 1}
	).get_password("secret_key")


def validate_payment(doc: Document) -> Any:
	"""Verify a Paystack transaction via the Paystack API."""
	if is_paystack_enabled(doc.company):
		secret = resolve_paystack_settings(doc.company)
		if not secret:
			frappe.throw(f"Paystack is not enabled for company {doc.company}")
		url = f"https://api.paystack.co/transaction/verify/{doc.transaction_id}"
		try:
			req = requests.get(
				url,
				headers={"Authorization": f"Bearer {secret.get('secret_key')}"},
				timeout=15,
			)
			data = req.json()
			log_integration_request(
				status="Completed" if req.ok else "Failed",
				url=url,
				request_data={"transaction_id": doc.transaction_id},
				response_data=data,
				reference_doctype="Paystack Payment Log",
				reference_docname=doc.name,
			)
		except Exception as e:
			log_integration_request(
				status="Failed",
				url=url,
				request_data={"transaction_id": doc.transaction_id},
				response_data=None,
				error=str(e),
				reference_doctype="Paystack Payment Log",
				reference_docname=doc.name,
			)
			frappe.throw(f"Failed to verify Paystack transaction: {e}")
	else:
		frappe.throw(f"Paystack is not enabled for company {doc.company}")

	return data