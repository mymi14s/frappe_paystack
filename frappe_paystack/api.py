
import frappe, hmac, hashlib, json
from frappe_paystack.utils import (
    resolve_paystack_settings, is_paystack_enabled, coalesce_currency, resolve_paystack_settings
)

LOG_DOCTYPE = "Paystack Payment Log"

@frappe.whitelist()
def is_enabled_for_company(company):
    return is_paystack_enabled(company)


def log_pending_payment(doc, amount, currency):
    log = frappe.new_doc("Paystack Payment Log")
    log.company = doc.company
    log.linked_doctype = doc.doctype
    log.linked_docname = doc.name
    log.amount = amount
    log.currency = currency or "NGN"
    log.status = "Pending"
    log.insert(ignore_permissions=True)
    return log

def _company_from_reference(reference):
    try: 
        return frappe.db.get_value("Paystack Payment Log", reference, "company")
    except Exception: return None

def verify_paystack_signature(payload, signature, secret):
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature or "")

@frappe.whitelist(allow_guest=True)
def paystack_webhook():
    data = frappe.request.get_json() or {}
    if frappe.local.conf.developer_mode:
        process_webhook_event(data)
        frappe.local.response["http_status_code"] = 201
        return
    signature = frappe.get_request_header("x-paystack-signature")
    payload = frappe.request.data or b""
    metadata = frappe._dict(dict(data.get("data")).get("metadata"))
    ref = metadata.get("reference") 
    if not ref: frappe.throw("Invalid webhook payload")
    company = _company_from_reference(ref)
    settings = resolve_paystack_settings(company) if company else None
    if not settings: frappe.throw("No Paystack settings for company", frappe.PermissionError)
    if not verify_paystack_signature(payload, signature, settings["secret_key"]):
        frappe.throw("Invalid Paystack signature", frappe.PermissionError)
    process_webhook_event(data)

    frappe.local.response["http_status_code"] = 201

def process_webhook_event(data):
    try:
        tx = frappe._dict(data.get("data")) or {}
        metadata = frappe._dict(tx.get("metadata"))
        ref = metadata.get("reference")
        amount = (tx.get("amount") or 0)/100
        currency = (tx.get("currency") or "NGN").upper()
        if not ref: return
        name = ref if frappe.db.exists("Paystack Payment Log", ref) else None
        if not name:
            return
        log = frappe.get_doc("Paystack Payment Log", name)
        log.status = "Processed" if tx.get("status") == "success" else "Failed"
        log.amount_paid = amount
        log.currency_paid = currency
        log.payment_reference = tx.get("reference")
        log.transaction_id = tx.get("reference")
        log.payment_date = tx.get("paid_at").split("T")[0]
        log.raw_response = json.dumps(tx)
        log.save(ignore_permissions=True)
        frappe.db.commit()
        if not frappe.db.get_value("Customer", metadata.get("customer"), "email_id"):
            frappe.db.set_value("Customer", metadata.get("customer"), "email_id", metadata.get("email"))
    except Exception as e:
        frappe.log_error(str(e), "Paystack payment")


@frappe.whitelist()
def create_payment_link(doctype, docname, amount: float=None, currency: str=None):
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
def validate_payment_link(docname):
    if frappe.db.exists(LOG_DOCTYPE, docname):
        doc = frappe.get_doc(LOG_DOCTYPE, docname).get_data()
        return doc
    return {}