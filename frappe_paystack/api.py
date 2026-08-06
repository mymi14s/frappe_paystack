import json
import time
from functools import wraps
from typing import Any, Optional

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import cint, flt

from frappe_paystack.frappe_paystack.doctype.paystack_customer_authorization import (
    paystack_customer_authorization as authorizations,
)
from frappe_paystack.frappe_paystack.doctype.paystack_refund_log.paystack_refund_log import (
    REFUNDABLE_LOG_STATUSES,
)
from frappe_paystack.utils import (
    charge_authorization,
    charge_currency,
    coalesce_currency,
    customer_email,
    ensure_supported_currency,
    from_minor_units,
    initialize_transaction,
    initiate_refund,
    is_ip_allowed,
    is_paystack_enabled,
    log_error_for,
    log_integration_request,
    outstanding_rate,
    record_failure,
    redact_authorization,
    resolve_paystack_settings,
    resolve_settings_for_signature,
)
from frappe_paystack.utils.payment_request import (
    can_bill_through_payment_request,
    payment_request_checkout_url,
    resolve_payment_entry,
)
from frappe_paystack.utils.pos_payment import notify_pos_link_paid
from frappe_paystack.utils.qr import qr_data_uri
from frappe_paystack.utils.settlement import SETTLEMENT_EVENTS, process_settlement_webhook_event

LOG_DOCTYPE = "Paystack Payment Log"
REFUND_LOG_DOCTYPE = "Paystack Refund Log"
AUTHORIZATION_DOCTYPE = "Paystack Customer Authorization"

# Paystack transaction states a saved-card charge can answer with.
CHARGE_SUCCEEDED = "success"
CHARGE_REJECTED = ("failed", "abandoned", "reversed")

# Hosted checkouts a single IP may open per minute.
HOSTED_CHECKOUT_LIMIT = 30
HOSTED_CHECKOUT_WINDOW = 60

# Per-source-IP webhook throttling.
WEBHOOK_IP_LIMIT = 100

# Ceiling across every source IP within one window.
WEBHOOK_GLOBAL_LIMIT = 1000

WEBHOOK_RATE_WINDOW = 60

# Throttled requests within one window before an Error Log alert is raised.
WEBHOOK_VIOLATION_ALERT = 10

# Forged webhooks within one window before an Error Log alert is raised.
WEBHOOK_SIGNATURE_ALERT = 10

# Roles allowed to move money on a customer's card.
REFUND_ROLES = ("System Manager", "Accounts Manager")

# Rounding slack between the capture and what a Payment Request bills.
SETTLEMENT_TOLERANCE = 0.01

# Log statuses that mean the money is already captured.
SETTLED_LOG_STATUSES = (
    "Processed",
    "Needs Attention",
    "Completed",
    "Partially Refunded",
    "Refunded",
)

# Refund Log statuses that are already final.
SETTLED_REFUND_STATUSES = ("Processed", "Completed")

# Integration Request statuses set once the POS payment screen is released.
POS_NOTIFIED_STATUSES = ("Authorized", "Failed")


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


def get_webhook_request_data() -> tuple:
    """Return the parsed body, raw payload, signature and source IP of the request."""
    data = frappe.request.get_json() or {}
    payload = frappe.request.data or b""
    signature = frappe.get_request_header("x-paystack-signature")
    request_ip = getattr(frappe.local, "request_ip", None)
    return data, payload, signature, request_ip


def check_gateway_role(message: str) -> None:
    """Abort unless the session user holds a role allowed to move money."""
    if frappe.session.user == "Administrator":
        return

    if not set(REFUND_ROLES) & set(frappe.get_roles()):
        frappe.throw(message, frappe.PermissionError)


def check_refund_permission() -> None:
    """Abort unless the session user may return money to a customer."""
    check_gateway_role(_("You are not permitted to refund Paystack payments."))


def check_saved_card_permission() -> None:
    """Abort unless the session user may charge a card the customer saved."""
    check_gateway_role(_("You are not permitted to charge saved Paystack cards."))


def settlement_mismatch(log: Any, request: Any) -> Optional[str]:
    """Return a message when the capture and the outstanding amount differ beyond
    SETTLEMENT_TOLERANCE, else None."""
    if not flt(log.amount_paid):
        return None

    # amount_paid is in the charge currency; the request bills in the document's.
    rate = flt(frappe.db.get_value(request.reference_doctype, request.reference_name, "conversion_rate")) or 1
    captured = flt(flt(log.amount_paid) / rate, request.precision("grand_total"))
    outstanding = flt(request.outstanding_amount)

    if abs(captured - outstanding) <= SETTLEMENT_TOLERANCE:
        return None

    return _("Paystack captured {0} but Payment Request {1} bills {2}. Settle this payment manually.").format(
        captured, request.name, outstanding
    )


def settle_request(log: Any, request: Any) -> None:
    """Book a Payment Request, recording a settlement that stopped part-way."""
    try:
        request.set_as_paid()
    except Exception:
        frappe.db.set_value(
            LOG_DOCTYPE,
            log.name,
            "errors",
            record_failure(
                f"Paystack settlement stopped part-way for log {log.name}",
                reference_doctype=LOG_DOCTYPE,
                reference_name=log.name,
            ),
        )


def notify_payment_authorized(log: Any) -> None:
    """Settle the payment through ERPNext's Payment Request, as Administrator."""
    if not log.payment_request:
        return

    session_user = frappe.session.user
    try:
        frappe.set_user("Administrator")  # nosemgrep - the guest webhook needs a system user here

        if not frappe.db.exists("Payment Request", log.payment_request):
            return

        request = frappe.get_doc("Payment Request", log.payment_request)
        if request.docstatus != 1 or request.status == "Paid":
            return

        mismatch = settlement_mismatch(log, request)
        if mismatch:
            frappe.db.set_value("Paystack Payment Log", log.name, "errors", mismatch)
            return

        request.flags.ignore_permissions = True

        # Webshop defines this hook to redirect the customer.
        if hasattr(request, "on_payment_authorized"):
            request.run_method("on_payment_authorized", "Completed")
            request.reload()

        if request.status != "Paid" and not resolve_payment_entry(request.name):
            settle_request(log, request)

        payment_entry = resolve_payment_entry(request.name)
        if payment_entry:
            frappe.db.set_value(
                "Paystack Payment Log",
                log.name,
                {"payment_entry": payment_entry, "status": "Completed"},
            )
    except Exception:
        log_error_for(
            title=f"Paystack on_payment_authorized failed: {log.name}",
            message=frappe.get_traceback(),
            reference_doctype=LOG_DOCTYPE,
            reference_name=log.name,
        )
    finally:
        frappe.set_user(session_user)  # nosemgrep - restores the caller the webhook arrived as


def notify_pos_payment(reference: str, amount: float, success: bool, message: str = "") -> None:
    """
    Release the POS payment screen once a phone charge settles.

    The reference names the Integration Request, which names the Payment
    Request, which names the POS Invoice.
    """
    if not frappe.db.exists("Integration Request", reference):
        return

    integration_request = frappe.get_doc("Integration Request", reference)
    if integration_request.reference_doctype != "Payment Request":
        return

    # The first webhook moves the row on; every later one stops here.
    if integration_request.status in POS_NOTIFIED_STATUSES:
        return

    pos_invoice = frappe.db.get_value(
        "Payment Request", integration_request.reference_docname, "reference_name"
    )
    if not pos_invoice:
        return

    frappe.publish_realtime(
        event="process_phone_payment",
        doctype="POS Invoice",
        docname=pos_invoice,
        user=integration_request.owner,
        message={
            "amount": flt(amount),
            "success": success,
            "failure_message": "" if success else message,
        },
    )

    frappe.db.set_value(
        "Integration Request",
        reference,
        "status",
        "Authorized" if success else "Failed",
        update_modified=False,
    )


def webhook_rate_key(scope: str) -> str:
    """Return the counter key for the current fixed window."""
    window = int(time.time()) // WEBHOOK_RATE_WINDOW
    return frappe.cache.make_key(f"paystack-webhook-{scope}:{window}")


def count_in_window(scope: str) -> int:
    """Increment and return this window's counter for a scope."""
    key = webhook_rate_key(scope)
    count = cint(frappe.cache.incrby(key, 1))
    if count == 1:
        frappe.cache.expire(key, WEBHOOK_RATE_WINDOW)
    return count


def global_rate_limit_exceeded() -> bool:
    """Count this webhook against the app-wide ceiling."""
    return count_in_window("global") > WEBHOOK_GLOBAL_LIMIT


def record_rate_limit_violation(scope: str, request_ip: Optional[str]) -> None:
    """File a throttled webhook and alert once violations become sustained."""
    log_integration_request(
        status="Failed",
        url="webhook",
        request_data={"scope": scope, "request_ip": request_ip},
        error=f"Webhook rate limit exceeded ({scope}) for IP {request_ip or 'unknown'}",
    )

    if count_in_window("violations") == WEBHOOK_VIOLATION_ALERT:
        log_error_for(
            title="Paystack webhook rate limit: sustained violations",
            message=(
                f"{WEBHOOK_VIOLATION_ALERT} webhook requests were throttled within "
                f"{WEBHOOK_RATE_WINDOW}s. Latest: {scope} limit, IP {request_ip or 'unknown'}."
            ),
        )


def record_signature_rejection(data: dict, request_ip: Optional[str]) -> None:
    """File a forged webhook and alert once the attempts become sustained."""
    log_integration_request(
        status="Failed",
        url="webhook",
        request_data=data,
        error="Invalid Paystack signature",
    )

    if count_in_window("signatures") == WEBHOOK_SIGNATURE_ALERT:
        log_error_for(
            title="Paystack webhook: sustained signature rejections",
            message=(
                f"{WEBHOOK_SIGNATURE_ALERT} webhook requests failed signature "
                f"verification within {WEBHOOK_RATE_WINDOW}s. "
                f"Latest source IP: {request_ip or 'unknown'}."
            ),
        )


def rate_limited_webhook(fn):
    """Apply the app-wide webhook ceiling and log every throttled request."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        request_ip = getattr(frappe.local, "request_ip", None)

        if global_rate_limit_exceeded():
            record_rate_limit_violation("global", request_ip)
            frappe.throw(
                _("Too many Paystack webhooks. Retry shortly."),
                frappe.RateLimitExceededError,
            )

        try:
            return fn(*args, **kwargs)
        except frappe.RateLimitExceededError:
            record_rate_limit_violation("ip", request_ip)
            raise

    return wrapper


@frappe.whitelist(allow_guest=True)  # nosemgrep - guest by design; signature and IP allowlist guard it
@rate_limited_webhook
@rate_limit(limit=WEBHOOK_IP_LIMIT, seconds=WEBHOOK_RATE_WINDOW)
def paystack_webhook() -> None:
    """Receive and process Paystack webhook events."""
    data, payload, signature, request_ip = get_webhook_request_data()

    # The signature selects the gateway whose secret verified the payload.
    settings = resolve_settings_for_signature(payload, signature)
    if not settings:
        record_signature_rejection(data, request_ip)
        frappe.throw(_("Invalid Paystack signature"), frappe.PermissionError)

    if not is_ip_allowed(settings.get("allowed_webhook_ips"), request_ip):
        log_integration_request(
            status="Failed",
            url="webhook",
            request_data=data,
            error=f"IP {request_ip} not in allowlist",
        )
        frappe.throw(_("IP not allowed"), frappe.PermissionError)

    process_webhook_event(data, settings)

    frappe.local.response["http_status_code"] = 200


def process_webhook_event(data: dict, settings: Optional[dict] = None) -> None:
    """Route a webhook event to its handler, held to the company that signed it."""
    event = data.get("event", "")
    company = (settings or {}).get("company")

    if event in SETTLEMENT_EVENTS:
        # A payout names no document of its own.
        process_settlement_webhook_event(data, company)
    elif event in ("refund.processed", "refund.failed"):
        process_refund_webhook_event(data, company)
    else:
        process_charge_webhook_event(data, company)


def signed_for(doctype: str, name: str, company: Optional[str]) -> bool:
    """
    Report whether a record belongs to the company that signed a webhook.

    A gateway carrying no company answers True.
    """
    if not company:
        return True

    return frappe.db.get_value(doctype, name, "company") == company


def refund_webhook_references(tx: dict) -> tuple:
    """
    Return the (refund reference, transaction reference) a payload carries.

    Paystack sends either a nested transaction object or the flat
    transaction_reference/refund_reference pair.
    """
    transaction = tx.get("transaction")
    if not isinstance(transaction, dict):
        transaction = {}

    refund_reference = tx.get("refund_reference") or tx.get("reference")
    transaction_reference = transaction.get("reference") or tx.get("transaction_reference")
    return refund_reference, transaction_reference


def find_refund_log(refund_reference: str, transaction_reference: str) -> Optional[str]:
    """
    Return the Refund Log a refund webhook belongs to, or None.

    The transaction fallback matches payment_reference and answers with the
    newest unsettled Refund Log on that payment.
    """
    if refund_reference:
        name = frappe.db.get_value(REFUND_LOG_DOCTYPE, {"refund_reference": refund_reference}, "name")
        if name:
            return name

    if not transaction_reference:
        return None

    payment_log = frappe.db.get_value(LOG_DOCTYPE, {"payment_reference": transaction_reference}, "name")
    if not payment_log:
        return None

    return frappe.db.get_value(
        REFUND_LOG_DOCTYPE,
        {"payment_log": payment_log, "status": ["not in", SETTLED_REFUND_STATUSES]},
        "name",
        order_by="creation desc",
    )


def process_refund_webhook_event(data: dict, company: Optional[str] = None) -> None:
    """Handle refund.processed and refund.failed webhook events."""
    refund_log_name = None
    try:
        tx = frappe._dict(data.get("data") or {})
        event = data.get("event", "")
        refund_reference, transaction_reference = refund_webhook_references(tx)

        refund_log_name = find_refund_log(refund_reference, transaction_reference)
        if not refund_log_name:
            log_integration_request(
                status="Failed",
                url="webhook",
                request_data=data,
                error=f"No Paystack Refund Log for refund {refund_reference}",
            )
            return

        if not signed_for(REFUND_LOG_DOCTYPE, refund_log_name, company):
            log_integration_request(
                status="Failed",
                url="webhook",
                request_data=data,
                error=f"Refund Log {refund_log_name} belongs to another company",
            )
            return

        existing_status = frappe.db.get_value(REFUND_LOG_DOCTYPE, refund_log_name, "status")
        if existing_status in SETTLED_REFUND_STATUSES:
            log_integration_request(
                status="Completed",
                url="webhook",
                request_data=data,
                response_data={"skipped": f"already {existing_status}"},
                reference_doctype=REFUND_LOG_DOCTYPE,
                reference_docname=refund_log_name,
            )
            return

        new_status = "Processed" if event == "refund.processed" else "Failed"
        ir_name = log_integration_request(
            status="Completed",
            url="webhook",
            request_data=data,
            response_data={"refund_reference": refund_reference, "status": new_status},
            reference_doctype=REFUND_LOG_DOCTYPE,
            reference_docname=refund_log_name,
        )

        updates = {
            "status": new_status,
            "raw_response": json.dumps(redact_authorization(tx)),
            "integration_request": ir_name,
        }
        # Keeps the recorded reference when the payload carries none.
        if refund_reference:
            updates["refund_reference"] = refund_reference

        frappe.db.set_value(REFUND_LOG_DOCTYPE, refund_log_name, updates)
        frappe.db.commit()

        if new_status == "Processed":
            refund_log = frappe.get_doc(REFUND_LOG_DOCTYPE, refund_log_name)
            refund_log.reload()
            refund_log.run_method("on_update")
    except Exception as e:
        log_error_for(
            title=f"Paystack refund webhook failed: {refund_log_name or 'unmatched'}",
            message=str(e),
            reference_doctype=REFUND_LOG_DOCTYPE,
            reference_name=refund_log_name,
        )


def process_charge_webhook_event(data: dict, company: Optional[str] = None) -> None:
    """Handle charge.success and charge.failed webhook events."""
    # metadata.reference is the Payment Log name.
    ref = None
    try:
        tx = frappe._dict(data.get("data") or {})
        metadata = frappe._dict(tx.get("metadata") or {})
        ref = metadata.get("reference")
        amount = (tx.get("amount") or 0) / 100
        currency = (tx.get("currency") or "NGN").upper()
        succeeded = tx.get("status") == "success"

        # Dedup on the Paystack transaction id, which a retry carries.
        event_id = str(tx.get("id") or "")
        already_seen = event_id and frappe.db.get_value(LOG_DOCTYPE, {"webhook_event_id": event_id}, "name")
        if already_seen:
            log_integration_request(
                status="Completed",
                url="webhook",
                request_data=data,
                response_data={"skipped": f"duplicate event {event_id}"},
                reference_doctype=LOG_DOCTYPE,
                reference_docname=already_seen,
            )
            return

        # A POS charge has no Payment Log; its reference is an Integration Request.
        notify_pos_payment(
            tx.get("reference"),
            amount,
            succeeded,
            tx.get("gateway_response") or "",
        )

        name = ref if ref and frappe.db.exists(LOG_DOCTYPE, ref) else None
        if not name:
            log_integration_request(
                status="Failed",
                url="webhook",
                request_data=data,
                error=f"No Paystack Payment Log for reference {ref or ''}",
            )
            return

        if not signed_for(LOG_DOCTYPE, name, company):
            log_integration_request(
                status="Failed",
                url="webhook",
                request_data=data,
                error=f"Paystack Payment Log {name} belongs to another company",
            )
            return

        existing_status = frappe.db.get_value(LOG_DOCTYPE, name, "status")
        if existing_status in SETTLED_LOG_STATUSES:
            log_integration_request(
                status="Completed",
                url="webhook",
                request_data=data,
                response_data={"skipped": f"already {existing_status}"},
                reference_doctype=LOG_DOCTYPE,
                reference_docname=name,
            )
            return

        new_status = "Processed" if succeeded else "Failed"
        ir_name = log_integration_request(
            status="Completed",
            url="webhook",
            request_data=data,
            response_data={"reference": ref, "status": new_status},
            reference_doctype=LOG_DOCTYPE,
            reference_docname=name,
        )

        updates = {
            "status": new_status,
            "amount_paid": amount,
            "currency_paid": currency,
            # What Paystack kept out of this capture.
            "paystack_fee": from_minor_units(tx.get("fees") or 0, currency),
            "payment_reference": tx.get("reference"),
            "transaction_id": tx.get("id"),
            "idempotency_key": tx.get("id"),
            "webhook_event_id": event_id or None,
            "payment_date": (tx.get("paid_at") or "").split("T")[0] or None,
            "integration_request": ir_name,
        }
        frappe.db.set_value("Paystack Payment Log", name, updates)
        frappe.db.commit()

        if new_status == "Processed":
            log = frappe.get_doc("Paystack Payment Log", name)
            notify_pos_link_paid(log)
            # Settles before on_update enqueues its fallback.
            notify_payment_authorized(log)
            log.reload()
            log.run_method("on_update")
            authorizations.capture_authorization(log, tx)
    except Exception as e:
        log_error_for(
            title=f"Paystack charge webhook failed: {ref or 'unmatched'}",
            message=str(e),
            reference_doctype=LOG_DOCTYPE,
            reference_name=ref,
        )


def get_payable_amount(doc: Any) -> float:
    """
    Return the amount still payable on a reference document, in its currency.

    outstanding_amount when present, converted from the party account's
    currency, else grand_total less advance_paid.
    """
    outstanding = flt(doc.get("outstanding_amount"))
    if outstanding:
        return flt(outstanding / outstanding_rate(doc))

    return max(0.0, flt(doc.get("grand_total")) - flt(doc.get("advance_paid")))


@frappe.whitelist()
def create_payment_link(
    doctype: str,
    docname: str,
    amount: Optional[float] = None,
    currency: Optional[str] = None,
) -> str:
    """
    Return a Paystack checkout URL for a document.

    A Sales Order or Sales Invoice bills through a Payment Request; any other
    document gets a Paystack Payment Log of its own.
    """
    doc = frappe.get_doc(doctype, docname)
    # The caller must hold read on the document being billed.
    doc.check_permission("read")

    settings = resolve_paystack_settings(getattr(doc, "company", None))
    if not settings:
        frappe.throw(_("Paystack not enabled for {0}").format(getattr(doc, "company", "")))

    # A caller-supplied amount is capped at the payable figure.
    payable = get_payable_amount(doc)
    if payable <= 0:
        frappe.throw(_("There is nothing left to pay on {0} {1}.").format(doctype, docname))

    if amount is None:
        amount = payable
    elif flt(amount) <= 0:
        frappe.throw(_("A payment link amount must be greater than zero."))

    amount = min(flt(amount), payable)

    if currency:
        ensure_supported_currency(currency)

    # A Payment Request bills in the reference document's currency.
    if can_bill_through_payment_request(doctype):
        ensure_supported_currency(doc.get("currency"))
        return payment_request_checkout_url(doc, amount, customer_email(doc.get("customer")))

    currency = coalesce_currency(currency, getattr(doc, "company", None), settings)

    reference = log_pending_payment(doc, amount, currency)
    return reference.get_payment_link()


@frappe.whitelist(allow_guest=True)  # nosemgrep - guest by design; the hash-named log is the only reference
def validate_payment_link(docname: str) -> dict:
    """Return payment data for the checkout page."""
    if frappe.db.exists(LOG_DOCTYPE, docname):
        doc = frappe.get_doc(LOG_DOCTYPE, docname)
        return doc.get_data()
    return {}


def checkout_reference(url: str) -> str:
    """Return the Payment Log name a checkout URL points at."""
    return url.rstrip("/").rsplit("/", 1)[-1]


def payable_log(reference: str) -> Any:
    """Return an open Payment Log, refusing one that can no longer be paid."""
    if not frappe.db.exists(LOG_DOCTYPE, reference):
        frappe.throw(_("This payment link is no longer available."))

    log = frappe.get_doc(LOG_DOCTYPE, reference)
    if not log.get_data().get("is_payable"):
        frappe.throw(_("This payment link can no longer be paid."))

    return log


@frappe.whitelist(allow_guest=True, methods=["POST"])  # nosemgrep - guest by design; rate limited, POST only
@rate_limit(limit=HOSTED_CHECKOUT_LIMIT, seconds=HOSTED_CHECKOUT_WINDOW)
def start_hosted_checkout(reference: str, email: Optional[str] = None) -> str:
    """
    Return the Paystack-hosted checkout URL for a payment log.

    The URL is stored on the log and handed back on every later call. POST
    only, which keeps the stored URL through Frappe's GET rollback.
    """
    log = payable_log(reference)
    if log.authorization_url:
        return log.authorization_url

    data = log.get_data()
    url = initialize_transaction(
        company=log.company,
        email=email or data.get("email"),
        amount=data.get("payment_amount"),
        currency=data.get("currency"),
        reference=log.name,
        metadata=checkout_metadata(data, email or data.get("email")),
        callback_url=log.get_payment_link(),
        payment_log=log.name,
    )

    log.db_set("authorization_url", url, update_modified=False)
    return url


def checkout_metadata(data: dict, email: Optional[str]) -> dict:
    """
    Return the metadata every Paystack charge carries.

    The webhook resolves the payment through metadata.reference.
    """
    return {
        "reference": data.get("reference"),
        "reference_doctype": data.get("reference_doctype"),
        "reference_docname": data.get("reference_docname"),
        "customer": data.get("customer"),
        "email": email or "",
    }


@frappe.whitelist()
def payment_link_qr(reference: str) -> str:
    """Return a scannable QR code for a checkout link, as an SVG data URI."""
    if not frappe.db.exists(LOG_DOCTYPE, reference):
        frappe.throw(_("Payment link {0} does not exist.").format(reference))

    log = frappe.get_doc(LOG_DOCTYPE, reference)
    # The caller is judged on read of the document being collected.
    frappe.get_doc(log.linked_doctype, log.linked_docname).check_permission("read")

    return qr_data_uri(log.get_payment_link())


@frappe.whitelist()
def saved_cards(customer: str, company: Optional[str] = None) -> list:
    """
    Return the instruments a customer can be charged on again.

    Each entry names the card; the authorization code stays on the server.
    """
    if not frappe.has_permission("Customer", "read", doc=customer):
        frappe.throw(
            _("Not permitted to read Customer {0}.").format(customer),
            frappe.PermissionError,
        )

    return authorizations.usable_authorizations(customer, company)


def check_saved_card(authorization: Any, doc: Any) -> None:
    """Refuse an instrument that is not this customer's, or not chargeable."""
    if authorization.customer != doc.get("customer"):
        frappe.throw(
            _("That saved card belongs to another customer."),
            frappe.PermissionError,
        )

    if not authorization.is_usable():
        frappe.throw(_("That saved card can no longer be charged."))


@frappe.whitelist()
def charge_saved_card(
    doctype: str,
    docname: str,
    authorization: str,
    amount: Optional[float] = None,
) -> str:
    """Charge a stored instrument for a document and return its Payment Log."""
    check_saved_card_permission()

    doc = frappe.get_doc(doctype, docname)
    doc.check_permission("read")

    saved_card = frappe.get_doc(AUTHORIZATION_DOCTYPE, authorization)
    check_saved_card(saved_card, doc)

    log = frappe.get_doc(
        LOG_DOCTYPE,
        checkout_reference(create_payment_link(doctype, docname, amount, doc.get("currency"))),
    )
    data = log.get_data()

    try:
        result = charge_authorization(
            company=log.company,
            email=saved_card.email or customer_email(doc.get("customer")),
            amount=data.get("payment_amount"),
            currency=data.get("currency"),
            reference=log.name,
            authorization_code=saved_card.get_authorization_code(),
            metadata=checkout_metadata(data, saved_card.email),
            payment_log=log.name,
        )
    except Exception:
        log.db_set(
            "errors",
            record_failure(
                f"Paystack saved-card charge failed for log {log.name}",
                reference_doctype=LOG_DOCTYPE,
                reference_name=log.name,
            ),
        )
        keep_failure_state()
        raise

    log.db_set("customer_authorization", saved_card.name, update_modified=False)
    report_charge_outcome(log, result)

    return log.name


def keep_failure_state() -> None:
    """Keep what a failure path recorded through Frappe's rollback."""
    frappe.db.commit()  # nosemgrep - the failure record is written before a throw that rolls back


def report_charge_outcome(log: Any, result: dict) -> None:
    """
    Throw unless Paystack captured the money outright.

    A rejected charge marks the log Failed; any other state throws with a
    message pointing at the checkout link.
    """
    status = (result.get("status") or "").lower()
    if status == CHARGE_SUCCEEDED:
        return

    message = result.get("gateway_response") or result.get("message") or status

    if status in CHARGE_REJECTED:
        log.db_set("status", "Failed", update_modified=True)
        keep_failure_state()
        frappe.throw(_("The saved card was declined: {0}").format(message))

    keep_failure_state()
    frappe.throw(
        _(
            "This card needs the customer to authorise the payment ({0}). "
            "Send them the checkout link instead."
        ).format(message)
    )


@frappe.whitelist()
def initiate_refund_from_log(
    payment_log_name: str,
    amount: float,
    reason: Optional[str] = None,
) -> str:
    """
    Create a Paystack Refund Log and initiate the refund, returning the log name.

    The amount is in the charge currency, in major units.
    """
    check_refund_permission()

    payment_log = frappe.get_doc(LOG_DOCTYPE, payment_log_name)
    payment_log.check_permission("read")

    if payment_log.status not in REFUNDABLE_LOG_STATUSES:
        frappe.throw(_("Only completed payments can be refunded."))

    if not payment_log.transaction_id:
        frappe.throw(_("Payment Log has no transaction ID to refund."))

    # The amount is in the currency that was charged.
    currency = charge_currency(payment_log.company, payment_log.currency_paid)

    refund_log = frappe.get_doc(
        {
            "doctype": "Paystack Refund Log",
            "payment_log": payment_log_name,
            "company": payment_log.company,
            "linked_doctype": payment_log.linked_doctype,
            "linked_docname": payment_log.linked_docname,
            "transaction_id": payment_log.transaction_id,
            "refund_amount": amount,
            "currency": currency,
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
            currency=currency,
            company=payment_log.company,
            merchant_note=reason,
            refund_log=refund_log.name,
        )
        refund_log.db_set("status", "Processed", update_modified=True)
        refund_log.db_set("refund_reference", result.get("reference"), update_modified=False)
        refund_log.db_set("raw_response", frappe.as_json(result.get("raw")), update_modified=False)
        refund_log.reload()
        refund_log.run_method("on_update")
    except Exception:
        refund_log.db_set("status", "Failed", update_modified=True)
        refund_log.db_set(
            "errors",
            record_failure(
                f"Paystack refund failed for Payment Log {payment_log_name}",
                reference_doctype=LOG_DOCTYPE,
                reference_name=payment_log_name,
            ),
            update_modified=True,
        )

    return refund_log.name
