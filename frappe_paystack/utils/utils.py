from __future__ import annotations

import hashlib
import hmac
import ipaddress
from typing import Any, Dict, Optional

import frappe
import requests
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.model.document import Document
from frappe.utils import flt

from erpnext.accounts.party import get_party_account

# Gateway name recorded on Integration Request rows.
PAYSTACK_SERVICE = "Paystack"

SUPPORTED_CURRENCIES = ["NGN", "USD", "GHS", "ZAR", "KES"]

INITIALIZE_URL = "https://api.paystack.co/transaction/initialize"
CHARGE_AUTHORIZATION_URL = "https://api.paystack.co/transaction/charge_authorization"

# The one Paystack field that charges a card on its own.
AUTHORIZATION_CODE_KEY = "authorization_code"

REDACTED = "***"

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
    """
    Create an Integration Request record and return its name.

    The service name is always "Paystack". Returns None when the write fails.
    """
    try:
        # create_request_log validates links; drop one that does not resolve.
        if reference_docname and not frappe.db.exists(reference_doctype, reference_docname):
            reference_doctype = reference_docname = None

        integration_request = create_request_log(
            redact_authorization(request_data),
            service_name=PAYSTACK_SERVICE,
            is_remote_request=1,
            status=status,
            url=url,
            output=redact_authorization(response_data),
            error=error,
            reference_doctype=reference_doctype,
            reference_docname=reference_docname,
        )
        return integration_request.name
    except Exception:
        frappe.log_error(
            title="Paystack: failed to log Integration Request",
            message=frappe.get_traceback(),
        )
        return None


def customer_email(customer: str) -> str:
    """Return the customer's email_id or empty string."""
    return frappe.db.get_value("Customer", customer, "email_id") or ""


@frappe.whitelist()
def get_customer_email(customer: str) -> str:
    """
    Return a customer's email to a caller allowed to read the record.

    Throws PermissionError when the caller holds no read on the Customer.
    """
    if not frappe.has_permission("Customer", "read", doc=customer):
        frappe.throw(
            _("Not permitted to read Customer {0}.").format(customer),
            frappe.PermissionError,
        )

    return customer_email(customer)


def check_company_permission(company: Optional[str]) -> None:
    """Abort unless the session user may read a company's records."""
    if not frappe.has_permission("Company", "read", doc=company):
        frappe.throw(
            _("You are not permitted to access Paystack data for {0}.").format(company),
            frappe.PermissionError,
        )


def get_company_row_settings(company: Optional[str]) -> Optional[Dict[str, Any]]:
    """Load the enabled Paystack Gateway Setting for a company."""
    DOCTYPE = "Paystack Gateway Setting"
    try:
        meta = frappe.get_meta(DOCTYPE)
    except Exception:
        return None

    is_company_scoped = meta.has_field("company")
    if is_company_scoped and not company:
        # A company-scoped gateway resolves only with a company named.
        return None

    row = {}
    filters: Dict[str, Any] = {"enabled": 1}
    if is_company_scoped:
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
                default_currency = cur.upper()
        except Exception:
            pass

    webhook_secret = None
    try:
        if row.get("webhook_secret"):
            webhook_secret = row.get_password("webhook_secret")
    except Exception:
        webhook_secret = None
    if not webhook_secret:
        webhook_secret = row.get_password("secret_key")

    allowed_ips = row.get("allowed_webhook_ips") or None

    return {
        # The company travels with the settings.
        "company": row.get("company"),
        "public_key": row.get("public_key"),
        "secret_key": row.get_password("secret_key"),
        "webhook_secret": webhook_secret,
        "test_mode": bool(row.get("test_mode")),
        "allowed_webhook_ips": allowed_ips,
        "auto_refund_on_credit_note": bool(row.get("auto_refund_on_credit_note")),
        "default_currency": default_currency,
    }


def resolve_settings_for_signature(payload: bytes, signature: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Return the enabled gateway whose secret signs this payload, else None.

    A payload more than one gateway verifies returns None and files an Error Log.
    """
    if not signature:
        return None

    companies = frappe.get_all(
        "Paystack Gateway Setting",
        filters={"enabled": 1},
        pluck="company",
        order_by=None,
    )

    matched = []
    for company in sorted({c for c in companies if c}):
        settings = resolve_paystack_settings(company)
        if not settings:
            continue

        secret = settings.get("webhook_secret") or settings.get("secret_key")
        if secret and verify_signature(payload, signature, secret):
            matched.append(settings)

    if len(matched) > 1:
        frappe.log_error(
            title="Paystack webhook: one signing secret serves several companies",
            message=", ".join(str(row.get("company")) for row in matched),
        )
        return None

    return matched[0] if matched else None


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
    """
    Return True when the incoming request IP is in the allowed list.

    Each entry is a single address or a CIDR range, separated by commas or
    newlines. An empty list allows every IP.
    """
    if not allowed_ips:
        return True
    if not request_ip:
        return False

    try:
        address = ipaddress.ip_address(request_ip.strip())
    except ValueError:
        return False

    for entry in allowed_ips.replace(",", "\n").split("\n"):
        entry = entry.strip()
        if not entry:
            continue
        try:
            if address in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue

    return False


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


def get_company_currency(company: str) -> str:
    """Return the normalized default currency for a company."""
    cur = frappe.db.get_value("Company", company, "default_currency") or "NGN"
    return normalize_currency(cur)


def discard_draft_payment_entry(entry: Any) -> None:
    """Remove a Payment Entry that was inserted but never submitted."""
    name = entry.name if entry else None
    if not name or frappe.db.get_value("Payment Entry", name, "docstatus") != 0:
        return

    frappe.delete_doc("Payment Entry", name, force=True, ignore_permissions=True)


def party_account_for(inv: Any) -> str:
    """
    Return the receivable a document's settlement posts against.

    A Sales Invoice carries its own; everything else resolves through the customer.
    """
    if inv.doctype == "Sales Invoice":
        return inv.debit_to

    return get_party_account("Customer", inv.customer, inv.company)


def party_account_rate(inv: Any, party_account: str) -> float:
    """
    Return the rate between the charge currency and a party account's.

    A company-currency party account takes 1.0; any other takes the document's
    conversion rate.
    """
    company_currency = frappe.get_cached_value("Company", inv.company, "default_currency")
    account_currency = frappe.get_cached_value("Account", party_account, "account_currency")
    if account_currency == company_currency:
        return 1.0

    return flt(inv.conversion_rate) or 1.0


def outstanding_rate(inv: Any) -> float:
    """
    Return what outstanding_amount divides by to reach the document currency.

    A party account in the document's own currency, or a conversion rate of 0
    or 1, takes 1.0.
    """
    rate = flt(inv.get("conversion_rate"))
    if rate in (0.0, 1.0):
        return 1.0

    account_currency = frappe.get_cached_value("Account", party_account_for(inv), "account_currency")
    return 1.0 if account_currency == inv.get("currency") else rate


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


def charge_currency(company: Optional[str], currency_paid: Optional[str] = None) -> str:
    """
    Return the currency Paystack settled a payment in.

    amount_paid and every refund amount are held in this currency.
    """
    if currency_paid:
        return normalize_currency(currency_paid)

    return coalesce_currency(None, company, None)


def ensure_supported_currency(currency: str) -> None:
    """Throw if currency is not supported by this integration."""
    cur = (currency or "").upper().strip()
    if cur not in SUPPORTED_CURRENCIES:
        frappe.throw(_("Currency {0} is not supported for Paystack in this app.").format(currency))


def get_gateway_secret(gateway: Optional[str]) -> str:
    """Return the secret key for an enabled Paystack gateway."""
    return frappe.get_doc("Paystack Gateway Setting", {"name": gateway, "enabled": 1}).get_password(
        "secret_key"
    )


def validate_payment(doc: Document) -> Any:
    """
    Verify a Paystack transaction via the Paystack API.

    A log carrying no payment reference returns None.
    """
    if not doc.get("payment_reference"):
        return None

    settings = resolve_paystack_settings(doc.company)
    if not settings:
        frappe.throw(_("Paystack is not enabled for company {0}").format(doc.company))

    url = f"https://api.paystack.co/transaction/verify/{doc.payment_reference}"
    try:
        req = requests.get(
            url,
            headers={"Authorization": f"Bearer {settings.get('secret_key')}"},
            timeout=15,
        )
        data = parse_paystack_response(req)
        log_integration_request(
            status="Completed" if req.ok else "Failed",
            url=url,
            request_data={"payment_reference": doc.payment_reference},
            response_data=data,
            reference_doctype="Paystack Payment Log",
            reference_docname=doc.name,
        )
    except Exception as e:
        log_integration_request(
            status="Failed",
            url=url,
            request_data={"payment_reference": doc.payment_reference},
            response_data=None,
            error=str(e),
            reference_doctype="Paystack Payment Log",
            reference_docname=doc.name,
        )
        frappe.throw(_("Failed to verify Paystack transaction: {0}").format(str(e)))

    return data


def post_paystack_charge(
    company: str,
    email: str,
    amount: float,
    currency: str,
    url: str,
    body_extra: Dict[str, Any],
    reference_doctype: str,
    reference_docname: Optional[str],
) -> Dict[str, Any]:
    """
    POST a charge to Paystack and return the parsed response.

    The caller supplies the URL and the body_extra keys. Throws when Paystack
    rejects the charge.
    """
    settings = resolve_paystack_settings(company)
    if not settings:
        frappe.throw(_("Paystack is not enabled for company {0}").format(company))

    if not email:
        frappe.throw(_("A customer email is required to start a Paystack charge."))

    ensure_supported_currency(currency)

    body = {
        "email": email,
        "amount": to_minor_units(amount, currency),
        "currency": normalize_currency(currency),
        **body_extra,
    }

    try:
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {settings.get('secret_key')}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=30,
        )
        data = parse_paystack_response(response)
        log_integration_request(
            status="Completed" if response.ok else "Failed",
            url=url,
            request_data=redact_authorization(body),
            response_data=data,
            reference_doctype=reference_doctype,
            reference_docname=reference_docname,
        )
    except Exception as e:
        log_integration_request(
            status="Failed",
            url=url,
            request_data=redact_authorization(body),
            response_data=None,
            error=str(e),
            reference_doctype=reference_doctype,
            reference_docname=reference_docname,
        )
        raise

    if not response.ok or not data.get("status"):
        frappe.throw(_("Paystack rejected the charge: {0}").format(data.get("message") or response.text))

    return data


def redact_authorization(payload: Any) -> Any:
    """
    Return a payload with every authorization code replaced by REDACTED.

    Dicts and lists are walked to any depth.
    """
    if isinstance(payload, list):
        return [redact_authorization(item) for item in payload]

    if not isinstance(payload, dict):
        return payload

    return {
        key: REDACTED if key == AUTHORIZATION_CODE_KEY else redact_authorization(value)
        for key, value in payload.items()
    }


def request_pos_charge(
    company: str,
    email: str,
    amount: float,
    currency: str,
    reference: str,
) -> Dict[str, Any]:
    """
    Initialise a Paystack transaction for a POS phone payment.

    The response data carries the authorization_url the cashier shows.
    """
    return post_paystack_charge(
        company=company,
        email=email,
        amount=amount,
        currency=currency,
        url=INITIALIZE_URL,
        body_extra={"reference": reference},
        reference_doctype="Integration Request",
        reference_docname=reference,
    )


def initialize_transaction(
    company: str,
    email: str,
    amount: float,
    currency: str,
    reference: str,
    metadata: Optional[Dict[str, Any]] = None,
    callback_url: Optional[str] = None,
    payment_log: Optional[str] = None,
) -> str:
    """
    Open a Paystack-hosted checkout and return its authorization_url.

    Throws when Paystack returns no checkout URL.
    """
    body_extra: Dict[str, Any] = {"reference": reference}
    if metadata:
        body_extra["metadata"] = metadata
    if callback_url:
        body_extra["callback_url"] = callback_url

    data = post_paystack_charge(
        company=company,
        email=email,
        amount=amount,
        currency=currency,
        url=INITIALIZE_URL,
        body_extra=body_extra,
        reference_doctype="Paystack Payment Log",
        reference_docname=payment_log,
    )

    url = (data.get("data") or {}).get("authorization_url")
    if not url:
        frappe.throw(_("Paystack did not return a checkout URL."))

    return str(url)


def charge_authorization(
    company: str,
    email: str,
    amount: float,
    currency: str,
    reference: str,
    authorization_code: str,
    metadata: Optional[Dict[str, Any]] = None,
    payment_log: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Charge a stored authorization and return the transaction data.

    Paystack answers synchronously; the webhook settles the payment.
    """
    body_extra: Dict[str, Any] = {
        "reference": reference,
        "authorization_code": authorization_code,
    }
    if metadata:
        body_extra["metadata"] = metadata

    data = post_paystack_charge(
        company=company,
        email=email,
        amount=amount,
        currency=currency,
        url=CHARGE_AUTHORIZATION_URL,
        body_extra=body_extra,
        reference_doctype="Paystack Payment Log",
        reference_docname=payment_log,
    )

    return data.get("data") or {}


def initiate_refund(
    transaction_id: str,
    amount: float,
    currency: str,
    company: str,
    merchant_note: Optional[str] = None,
    refund_log: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Initiate a refund via the Paystack API.

    The amount is in major units. Returns a dict of status, reference, amount,
    currency and raw.
    """
    settings = resolve_paystack_settings(company)
    if not settings:
        frappe.throw(_("Paystack is not enabled for company {0}").format(company))

    minor_amount = to_minor_units(amount, currency)
    url = "https://api.paystack.co/refund"
    body = {"transaction": transaction_id, "amount": minor_amount}
    if merchant_note:
        body["merchant_note"] = merchant_note

    try:
        req = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {settings.get('secret_key')}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=30,
        )
        data = parse_paystack_response(req)
        log_integration_request(
            status="Completed" if req.ok else "Failed",
            url=url,
            request_data={**body, "transaction_id": transaction_id},
            response_data=data,
            reference_doctype="Paystack Refund Log",
            reference_docname=refund_log,
        )
    except Exception as e:
        log_integration_request(
            status="Failed",
            url=url,
            request_data={**body, "transaction_id": transaction_id},
            response_data=None,
            error=str(e),
            reference_doctype="Paystack Refund Log",
            reference_docname=refund_log,
        )
        frappe.throw(_("Failed to initiate Paystack refund: {0}").format(str(e)))

    if not data.get("status"):
        frappe.throw(_("Paystack refund failed: {0}").format(data.get("message", _("Unknown error"))))

    refund_data = data.get("data", {})
    return {
        "status": refund_data.get("status", "pending"),
        "reference": refund_data.get("reference"),
        "amount": from_minor_units(refund_data.get("amount", 0), currency),
        "currency": refund_data.get("currency", currency),
        # The echoed transaction carries the card's token.
        "raw": redact_authorization(data),
    }


def error_log_link(error_log_name: Optional[str]) -> str:
    """Return a desk link to an Error Log record."""
    if not error_log_name:
        return ""
    return f'<a href="/app/error-log/{error_log_name}">{error_log_name}</a>'


def log_error_for(
    title: str,
    message: Optional[str] = None,
    reference_doctype: Optional[str] = None,
    reference_name: Optional[str] = None,
) -> Optional[str]:
    """
    Write an Error Log filed against the record it traces.

    A reference that does not resolve is dropped.
    """
    if reference_name and not frappe.db.exists(reference_doctype, reference_name):
        reference_doctype = reference_name = None

    error_log = frappe.log_error(
        title=title,
        message=message or frappe.get_traceback(),
        reference_doctype=reference_doctype,
        reference_name=reference_name,
    )
    return getattr(error_log, "name", None)


def record_failure(
    title: str,
    message: Optional[str] = None,
    reference_doctype: Optional[str] = None,
    reference_name: Optional[str] = None,
) -> str:
    """Log an error and return a message naming the Error Log record."""
    name = log_error_for(title, message, reference_doctype, reference_name)
    if not name:
        return _("{0}. See the Error Log for details.").format(title)
    return _("{0}. See Error Log {1}.").format(title, error_log_link(name))


def parse_paystack_response(response: Any) -> Dict[str, Any]:
    """
    Return the JSON body of a Paystack response.

    A body that does not decode to a JSON object throws with the HTTP status and
    its first 200 characters.
    """
    try:
        data = response.json()
    except ValueError:
        data = None

    if not isinstance(data, dict):
        body = (response.text or "").strip()[:200]
        frappe.throw(
            _("Paystack returned a non-JSON response (HTTP {0}): {1}").format(
                response.status_code, body or _("empty body")
            )
        )

    return data
