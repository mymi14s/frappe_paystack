# Copyright (c) 2025, Anthony Emmanuel and contributors
# For license information, please see license.txt

from typing import Any, Optional

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, add_to_date, cint, flt, get_datetime, getdate, now_datetime, nowdate

from frappe_paystack.api import check_gateway_role, notify_payment_authorized
from frappe_paystack.utils import (
    customer_email,
    discard_draft_payment_entry,
    from_minor_units,
    log_error_for,
    normalize_currency,
    party_account_for,
    party_account_rate,
    record_failure,
    validate_payment,
)
from frappe_paystack.utils.payment_request import resolve_payment_entry
from frappe_paystack.utils.reconciliation import SETTLED_PAYSTACK_STATUSES, ReconciliationEngine
from frappe_paystack.utils.sweep import run_sweep

from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

PAYMENT_LOG = "Paystack Payment Log"
GATEWAY_DOCTYPE = "Paystack Gateway Setting"
SALES_INVOICE = "Sales Invoice"
POS_INVOICE = "POS Invoice"

# A capture the app has stopped trying to book.
NEEDS_ATTENTION = "Needs Attention"

VALID_STATUSES = (
    "Pending",
    "Processed",
    NEEDS_ATTENTION,
    "Completed",
    "Partially Refunded",
    "Refunded",
    "Failed",
)

# Statuses meaning the money has been captured; no further payment is due.
SETTLED_STATUSES_LOG = (
    "Processed",
    NEEDS_ATTENTION,
    "Completed",
    "Partially Refunded",
    "Refunded",
)

# Statuses holding a capture, so the log is the only record of that money.
UNDELETABLE_STATUSES = ("Processed", NEEDS_ATTENTION, "Completed")

# Doctypes that expose outstanding_amount.
OUTSTANDING_DOCTYPES = (SALES_INVOICE,)

# Checkout the gateway falls back to when none is configured.
DEFAULT_CHECKOUT_MODE = "Inline"

CREATE_PAYMENT_ENTRY_JOB = (
    "frappe_paystack.frappe_paystack.doctype.paystack_payment_log"
    ".paystack_payment_log.create_payment_entry_from_log"
)

# Grace period before the sweep re-drives a settlement, in minutes.
SETTLEMENT_RETRY_DELAY_MINUTES = 15

# Age of the oldest log the sweep picks up, in days.
SETTLEMENT_RETRY_LOOKBACK_DAYS = 7

# Logs the sweep re-drives in one scheduler tick.
SETTLEMENT_RETRY_LIMIT = 50

# Names this sweep's Integration Requests, streak keys and Error Logs.
PAYMENT_SWEEP = "payment"

# Wait a log owes after its first settlement attempt, in minutes.
SETTLEMENT_BACKOFF_MINUTES = 10

# Doublings the wait takes before the cap decides it.
SETTLEMENT_BACKOFF_STEPS = 8

# Longest wait between two settlement attempts, in minutes.
SETTLEMENT_BACKOFF_CAP_MINUTES = 24 * 60

# Scheduled attempts a stuck capture is allowed before the app gives up on it.
SETTLEMENT_MAX_ATTEMPTS = 12

# Log statuses holding captured money that no Payment Entry has booked yet.
COMPLETABLE_STATUSES = ("Processed", NEEDS_ATTENTION)

COMPLETE_PAYMENT_JOB = (
    "frappe_paystack.frappe_paystack.doctype.paystack_payment_log"
    ".paystack_payment_log.complete_payment_from_log"
)

# Realtime event carrying a manual completion's outcome to the desk form.
COMPLETE_PAYMENT_EVENT = "paystack_payment_completed"

# Lifetime of the key holding a log's in-flight completion, in seconds.
COMPLETION_LOCK_TTL = 900

# Rounding slack between the amount Paystack reports and the recorded one.
VERIFY_TOLERANCE = 0.01

# Submitted-document statuses that still owe money.
PAYABLE_STATUSES = [
    # Sales Invoice
    "Partly Paid",
    "Partly Paid and Discounted",
    "Unpaid",
    "Unpaid and Discounted",
    "Overdue",
    "Overdue and Discounted",
    # Sales Order
    "To Deliver and Bill",
    "To Bill",
    "To Deliver",
    # version-16 raises an unpaid Sales Order here.
    "To Pay",
    # Dunning
    "Unresolved",
]

# The captures the settlement retry owns. POS books its ledger on Complete Order.
STUCK_CAPTURE_FILTERS = {
    "status": "Processed",
    "payment_entry": ["in", ["", None]],
    "docstatus": ["<", 2],
    "linked_doctype": ["!=", POS_INVOICE],
}


class PaystackPaymentLog(Document):
    def before_insert(self) -> None:
        self.validate_payment()
        self.set_expiry()
        validated = self.validate_record()
        if validated:
            frappe.throw(validated)

    def set_expiry(self) -> None:
        """
        Stamp when this checkout link stops accepting payment.

        The window comes from the gateway's payment_link_validity_hours; zero
        leaves the link open indefinitely.
        """
        if self.expires_at:
            return

        hours = cint(
            frappe.db.get_value(
                GATEWAY_DOCTYPE,
                {"enabled": 1, "company": self.company},
                "payment_link_validity_hours",
            )
        )
        if hours > 0:
            self.expires_at = add_to_date(now_datetime(), hours=hours)

    def is_expired(self) -> bool:
        """
        Report whether this link's validity window has closed.

        Only the checkout closes; money that reaches Paystack still settles.
        """
        if not self.expires_at:
            return False

        return get_datetime(self.expires_at) < now_datetime()

    def validate_record(self) -> str:
        errors = ""
        error_id = f"{self.linked_doctype} - {self.linked_docname}"
        if self.status == "Completed":
            return ""

        if frappe.db.exists(self.linked_doctype, {"name": self.linked_docname}):
            doc = frappe.get_doc(self.linked_doctype, self.linked_docname)
            if doc.doctype == POS_INVOICE:
                # A draft POS Invoice still takes payment.
                if doc.docstatus == 2:
                    errors = _("{0}: Document has been cancelled.").format(error_id)
            elif doc.docstatus == 1 and doc.status not in PAYABLE_STATUSES:
                errors = _("{0}: Document already settled").format(error_id)
            elif doc.docstatus in [0, 2]:
                errors = _("{0}: Document has been cancelled or in draft.").format(error_id)
        else:
            errors = _("{0}: Document not found.").format(error_id)

        return errors

    def record_refusal(self, reason: str) -> None:
        """File the reason this log is refusing a save."""
        log_error_for(
            title=f"Paystack Payment Log {self.name} refused a save",
            message=reason,
            reference_doctype=PAYMENT_LOG,
            reference_name=self.name,
        )

    def validate(self) -> None:
        if self.currency:
            self.currency = normalize_currency(self.currency)

        validated = self.validate_record()
        if validated:
            self.errors = validated
            self.record_refusal(validated)
            frappe.throw(validated)
        if bool(self.linked_doctype) ^ bool(self.linked_docname):
            frappe.throw(_("Linked Doctype and Linked Docname must both be set or both be empty."))

        if flt(self.amount) < 0:
            frappe.throw(_("Amount cannot be negative."))

        if self.status not in VALID_STATUSES:
            frappe.throw(_("Invalid status value."))

    def on_update(self) -> None:
        """
        Enqueue Payment Entry creation for a Processed or Completed log.

        The job is deduplicated on the log.
        """
        if not (self.linked_doctype and self.linked_docname):
            return

        if self.status not in ("Processed", "Completed") or self.docstatus in [1, 2] or self.payment_entry:
            return

        # A Payment Request settles inline through ERPNext before this runs.
        if self.payment_request:
            return

        frappe.enqueue(
            CREATE_PAYMENT_ENTRY_JOB,
            payment_log_name=self.name,
            queue="default",
            timeout=600,
            is_async=not frappe.flags.in_test,
            job_id=f"pe-{self.name}",
            deduplicate=True,
            # The job reads this log, which a worker sees only once the save commits.
            enqueue_after_commit=True,
        )

    def get_payment_link(self) -> str:
        """Return the checkout URL for this log."""
        return f"{frappe.utils.get_url()}/paystack-checkout/{self.name}"

    def get_payment_public_key(self) -> Optional[dict]:
        """Return gateway settings (public key, currency, suspense, mode)."""
        if frappe.db.exists(GATEWAY_DOCTYPE, {"enabled": 1, "company": self.company}):
            doc = frappe.get_doc(GATEWAY_DOCTYPE, {"enabled": 1, "company": self.company})
            return {
                "public_key": doc.get_password("public_key"),
                "currency": doc.currency,
                "suspense_account": doc.suspense_account,
                "mode_of_payment": doc.mode_of_payment,
                "checkout_mode": doc.checkout_mode or DEFAULT_CHECKOUT_MODE,
            }
        return None

    def get_data(self) -> dict:
        """Return the payment data the public checkout page is allowed to see."""
        order = frappe.get_doc(self.linked_doctype, self.linked_docname)
        gateway_settings = self.get_payment_public_key() or {}

        charge_currency = gateway_settings.get("currency") or self.currency or "NGN"
        charge_amount = flt(self.amount) * flt(order.conversion_rate or 1)

        return {
            "reference": self.name,
            "status": self.status,
            "customer": order.customer,
            "email": customer_email(order.customer) or "",
            "order_no": order.name,
            "order_status": order.status,
            "order_docstatus": order.docstatus,
            "order_currency": order.currency,
            "grand_total": flt(self.amount),
            "exchange_rate": flt(order.conversion_rate or 1),
            "currency": charge_currency,
            "payment_amount": flt(charge_amount, 2),
            "reference_doctype": self.linked_doctype,
            "reference_docname": self.linked_docname,
            "public_key": gateway_settings.get("public_key"),
            "checkout_mode": gateway_settings.get("checkout_mode") or DEFAULT_CHECKOUT_MODE,
            "expires_at": str(self.expires_at or ""),
            "is_expired": self.is_expired(),
            "is_payable": self.is_payable(order),
        }

    def is_payable(self, order: Any) -> bool:
        """Return whether this log can still be paid."""
        if self.status in SETTLED_STATUSES_LOG:
            return False

        if self.is_expired():
            return False

        # A POS Invoice is payable while draft; a cancelled one is not.
        if order.doctype == POS_INVOICE:
            return order.docstatus != 2

        return order.docstatus == 1 and order.status in PAYABLE_STATUSES

    def on_trash(self) -> None:
        """Block deletion of a log holding a capture or a Payment Entry."""
        if self.status in UNDELETABLE_STATUSES:
            frappe.throw(
                _("Cannot delete a Payment Log at {0}. Cancel the linked Payment Entry first.").format(
                    self.status
                )
            )
        if self.payment_entry:
            frappe.throw(
                _(
                    "Cannot delete this log because it is linked to Payment Entry {0}. "
                    "Cancel the Payment Entry first."
                ).format(self.payment_entry)
            )

    @frappe.whitelist()
    def validate_payment(self) -> Any:
        """Verify the transaction via the Paystack API."""
        return validate_payment(self)


def create_payment_entry_from_log(payment_log_name: str) -> None:
    """
    Create a Payment Entry from a processed Paystack Payment Log.

    Runs as Administrator; get_payment_entry() reads account balances.
    """
    session_user = frappe.session.user
    booked = None
    try:
        frappe.set_user("Administrator")  # nosemgrep - reading account balances needs a system user
        log = frappe.get_doc("Paystack Payment Log", payment_log_name)

        if not (log.linked_doctype and log.linked_docname):
            return

        if log.payment_entry or log.status not in SETTLED_STATUSES_LOG:
            return

        # POS books the ledger on submit, from the invoice's own payments table.
        if log.linked_doctype == POS_INVOICE:
            settle_pos_invoice(log)
            return

        # A Payment Request settles through ERPNext.
        if log.payment_request:
            settle_payment_request(log)
            return

        inv = frappe.get_doc(log.linked_doctype, log.linked_docname)

        if inv.doctype in OUTSTANDING_DOCTYPES and flt(inv.outstanding_amount) <= 0:
            if log.status != "Completed":
                log.db_set("status", "Completed", update_modified=True)
            return

        # party_amount is allocated against the receivable, in that account's currency.
        party_account = party_account_for(inv)
        rate = party_account_rate(inv, party_account)
        paid_amount = flt(flt(log.amount_paid) / rate, inv.precision("grand_total"))
        if paid_amount <= 0:
            return

        gateway_settings = log.get_payment_public_key()
        if not gateway_settings:
            log_error_for(
                title=f"Paystack gateway settings not found for Payment Log {payment_log_name}",
                message=f"Company: {log.company}",
                reference_doctype="Paystack Payment Log",
                reference_name=payment_log_name,
            )
            return

        pe = get_payment_entry(
            log.linked_doctype,
            log.linked_docname,
            party_amount=paid_amount,
            bank_account=gateway_settings.get("suspense_account"),
        )

        pe.mode_of_payment = gateway_settings.get("mode_of_payment")
        pe.reference_no = log.payment_reference or payment_log_name
        pe.reference_date = log.payment_date or getdate()
        pe.remarks = f"Auto-created from Paystack Payment Log {payment_log_name}"

        # Pin the receivable's rate to the reference document's.
        if rate != 1:
            pe.source_exchange_rate = rate

        pe.flags.ignore_permissions = True
        booked = pe
        pe.save()
        pe.submit()

        log.db_set("payment_entry", pe.name, update_modified=False)
        log.db_set("status", "Completed", update_modified=True)
    except Exception:
        discard_draft_payment_entry(booked)
        frappe.db.set_value(
            "Paystack Payment Log",
            payment_log_name,
            "errors",
            record_failure(
                f"Paystack Payment Entry creation failed for log {payment_log_name}",
                reference_doctype="Paystack Payment Log",
                reference_name=payment_log_name,
            ),
        )
    finally:
        frappe.set_user(session_user)  # nosemgrep - restores the caller the job started as


def settle_payment_request(log: Any) -> None:
    """Settle through ERPNext's Payment Request and record what it booked."""
    notify_payment_authorized(log)
    log.reload()

    if log.payment_entry:
        return

    payment_entry = resolve_payment_entry(log.payment_request)
    if not payment_entry:
        return

    log.db_set("payment_entry", payment_entry, update_modified=False)
    log.db_set("status", "Completed", update_modified=True)


def unsettled_logs(minutes: int = SETTLEMENT_RETRY_DELAY_MINUTES) -> list:
    """
    Return Processed logs that never produced a Payment Entry.

    A log written within the delay is held back, and one nothing has tried
    sorts first.
    """
    return frappe.get_all(
        PAYMENT_LOG,
        filters={
            "status": "Processed",
            "payment_entry": ["in", ["", None]],
            "docstatus": ["<", 2],
            "modified": ["<", add_to_date(now_datetime(), minutes=-minutes)],
            "creation": [">", add_days(nowdate(), -SETTLEMENT_RETRY_LOOKBACK_DAYS)],
        },
        fields=["name", "retry_count", "last_retry"],
        order_by="last_retry asc, creation asc",
        limit=SETTLEMENT_RETRY_LIMIT,
    )


def backoff_minutes(retry_count: int) -> int:
    """
    Return the wait a log owes after the given number of attempts.

    The wait doubles from SETTLEMENT_BACKOFF_MINUTES and flattens at
    SETTLEMENT_BACKOFF_CAP_MINUTES.
    """
    if cint(retry_count) < 1:
        return 0

    steps = min(cint(retry_count) - 1, SETTLEMENT_BACKOFF_STEPS)
    return min(SETTLEMENT_BACKOFF_MINUTES * 2**steps, SETTLEMENT_BACKOFF_CAP_MINUTES)


def retry_due(log: Any) -> bool:
    """Report whether a log has served the wait its attempts bought."""
    if not log.last_retry:
        return True

    return get_datetime(log.last_retry) <= add_to_date(
        now_datetime(), minutes=-backoff_minutes(log.retry_count)
    )


def due_logs() -> list:
    """
    Return the names of the unsettled logs whose wait has run out.

    A log a manual completion holds is left out.
    """
    return [log.name for log in unsettled_logs() if retry_due(log) and not completion_held(log.name)]


def record_retry(payment_log_name: str) -> None:
    """Count one attempt against a log's back-off, committing before the attempt runs."""
    frappe.db.set_value(
        PAYMENT_LOG,
        payment_log_name,
        {
            "retry_count": cint(frappe.db.get_value(PAYMENT_LOG, payment_log_name, "retry_count")) + 1,
            "last_retry": now_datetime(),
        },
        update_modified=False,
    )
    frappe.db.commit()  # nosemgrep - the attempt is counted before the attempt can crash


def clear_retries(payment_log_name: str) -> None:
    """Forget the attempts a booked log took."""
    frappe.db.set_value(
        PAYMENT_LOG,
        payment_log_name,
        {"retry_count": 0, "last_retry": None},
        update_modified=False,
    )
    frappe.db.commit()  # nosemgrep - the cleared back-off outlives a later rollback


def stuck_captures(extra: dict, limit: int = 0) -> list:
    """Return the names of the captures owed a Payment Entry, narrowed by extra."""
    return frappe.get_all(
        PAYMENT_LOG,
        filters={**STUCK_CAPTURE_FILTERS, **extra},
        pluck="name",
        order_by="creation asc",
        limit=limit,
    )


def expired_logs() -> list:
    """
    Return the captures older than the retry lookback.

    One batch at a time; the sweep runs on a 300-second queue.
    """
    return stuck_captures(
        {"creation": ["<=", add_days(nowdate(), -SETTLEMENT_RETRY_LOOKBACK_DAYS)]},
        limit=SETTLEMENT_RETRY_LIMIT,
    )


def attempts_spent(payment_log_name: str) -> bool:
    """Report whether a still-stuck capture has used its scheduled attempts."""
    return bool(stuck_captures({"name": payment_log_name, "retry_count": [">=", SETTLEMENT_MAX_ATTEMPTS]}))


def abandon_settlement(payment_log_name: str, cause: str) -> None:
    """
    Move a capture nothing could book to Needs Attention, and alert.

    The status is committed before the alert, and a log that has left Processed
    is selected no more.
    """
    frappe.db.set_value(
        PAYMENT_LOG,
        payment_log_name,
        "status",
        NEEDS_ATTENTION,
        update_modified=False,
    )
    frappe.clear_document_cache(PAYMENT_LOG, payment_log_name)
    frappe.db.commit()  # nosemgrep - Needs Attention is durable before the alert is raised

    log_error_for(
        title=f"Paystack settlement abandoned for log {payment_log_name}",
        message=_("{0} Last recorded error: {1}").format(
            cause,
            frappe.db.get_value(PAYMENT_LOG, payment_log_name, "errors") or _("none recorded."),
        ),
        reference_doctype=PAYMENT_LOG,
        reference_name=payment_log_name,
    )


def abandon_expired_settlements() -> list:
    """Give up on every capture the retry window has closed on, returning the names moved."""
    cause = _("No Payment Entry was booked within {0} days of this capture.").format(
        SETTLEMENT_RETRY_LOOKBACK_DAYS
    )

    names = expired_logs()
    for name in names:
        abandon_settlement(name, cause)

    return names


def drive_settlement_retry(payment_log_name: str) -> None:
    """
    Count a scheduled attempt against a log and re-drive its settlement.

    A log the attempt books forgets its back-off; one that has spent its
    attempts is given up on.
    """
    record_retry(payment_log_name)
    create_payment_entry_from_log(payment_log_name)

    if frappe.db.get_value(PAYMENT_LOG, payment_log_name, "payment_entry"):
        clear_retries(payment_log_name)
    elif attempts_spent(payment_log_name):
        abandon_settlement(
            payment_log_name,
            _("{0} settlement attempts booked nothing.").format(SETTLEMENT_MAX_ATTEMPTS),
        )


def retry_stuck_settlements() -> None:
    """
    Re-drive settlement for logs stuck at Processed with no Payment Entry.

    Each log serves the wait its retry_count buys. The captures the window has
    closed on are given up on first.
    """
    try:
        abandon_expired_settlements()
        names = due_logs()
    except Exception:
        frappe.log_error(
            title="Paystack payment sweep: lookup failed",
            message=frappe.get_traceback(),
        )
        return

    run_sweep(
        PAYMENT_SWEEP,
        PAYMENT_LOG,
        "payment_entry",
        names,
        drive_settlement_retry,
    )


def settle_pos_invoice(log: Any) -> None:
    """
    Mark a paid POS tender settled, leaving the sale for the cashier.

    POS submits the invoice on Complete Order.
    """
    if log.status != "Completed":
        log.db_set("status", "Completed", update_modified=True)


def is_completable(log: Any) -> bool:
    """Report whether a log holds captured money nothing has booked yet."""
    return log.status in COMPLETABLE_STATUSES and not log.payment_entry


def completion_key(payment_log_name: str) -> str:
    """Return the cache key holding a log's in-flight completion."""
    return frappe.cache.make_key(f"paystack-complete:{payment_log_name}")


def hold_completion(payment_log_name: str) -> bool:
    """Claim a log's single in-flight completion, returning True for the caller that takes it."""
    return bool(
        frappe.cache.set(  # nosemgrep - make_key scopes the key to the site; nx needs raw set
            completion_key(payment_log_name),
            frappe.session.user,
            ex=COMPLETION_LOCK_TTL,
            nx=True,
        )
    )


def completion_held(payment_log_name: str) -> bool:
    """Report whether a completion of this log is already in flight."""
    return bool(frappe.cache.get(completion_key(payment_log_name)))  # nosemgrep - site-scoped by make_key


def release_completion(payment_log_name: str) -> None:
    """Let the next completion of a log through."""
    frappe.cache.delete(completion_key(payment_log_name))


@frappe.whitelist()
def complete_payment(payment_log_name: str) -> str:
    """
    Queue the settlement a capture is owed, and return the log it runs on.

    Held to the roles that move money; the outcome arrives on
    COMPLETE_PAYMENT_EVENT.
    """
    check_gateway_role(_("You are not permitted to complete Paystack payments."))

    log = frappe.get_doc(PAYMENT_LOG, payment_log_name)
    log.check_permission("read")

    if not is_completable(log):
        frappe.throw(_("{0} has no outstanding settlement to complete.").format(payment_log_name))

    if not hold_completion(payment_log_name):
        frappe.throw(_("A completion of {0} is already running.").format(payment_log_name))

    queued = frappe.enqueue(
        COMPLETE_PAYMENT_JOB,
        payment_log_name=payment_log_name,
        user=frappe.session.user,
        queue="long",
        timeout=600,
        is_async=not frappe.flags.in_test,
        job_id=f"paystack-complete-{payment_log_name}",
        deduplicate=True,
    )

    if not queued:
        release_completion(payment_log_name)
        frappe.throw(_("A completion of {0} is already queued.").format(payment_log_name))

    return payment_log_name


def verified_transaction(log: Any) -> Optional[dict]:
    """
    Return what Paystack reports for a capture, or None when it has none.

    The lookup falls back from the transaction id to the log name.
    """
    return ReconciliationEngine(log.company).lookup_transaction(log)


def verification_mismatch(log: Any, transaction: Optional[dict]) -> Optional[str]:
    """
    Report how Paystack's record of a capture differs from the log's.

    Returns None when the amount, currency and capture all match.
    """
    if transaction is None:
        return _("Paystack has no transaction under reference {0}.").format(log.name)

    status = (transaction.get("status") or "").lower()
    if status not in SETTLED_PAYSTACK_STATUSES:
        return _("Paystack reports this transaction as {0}, not a capture.").format(status or _("unknown"))

    # Raw codes: normalize_currency() coerces an unsupported one to NGN.
    currency = (transaction.get("currency") or "").upper().strip()
    recorded = (log.currency_paid or log.currency or "").upper().strip()
    if currency != recorded:
        return _("Paystack settled in {0} but this log records {1}.").format(
            currency or _("no currency"), recorded or _("no currency")
        )

    captured = from_minor_units(transaction.get("amount") or 0, currency)
    if abs(captured - flt(log.amount_paid)) > VERIFY_TOLERANCE:
        return _("Paystack captured {0} but this log records {1}.").format(captured, flt(log.amount_paid))

    return None


def publish_completion(payment_log_name: str, user: str, message: str, payment_entry: Optional[str]) -> None:
    """Tell the user who asked for a completion how it ended."""
    frappe.publish_realtime(
        COMPLETE_PAYMENT_EVENT,
        {
            "log": payment_log_name,
            "booked": bool(payment_entry),
            "payment_entry": payment_entry,
            "message": message,
        },
        user=user,
    )


def refuse_completion(payment_log_name: str, user: str, reason: str) -> None:
    """Record why nothing was booked, commit it and report it."""
    frappe.db.set_value(PAYMENT_LOG, payment_log_name, "errors", reason)
    frappe.db.commit()  # nosemgrep - the refusal reason is durable before it is published
    publish_completion(payment_log_name, user, reason, None)


def settle_verified_log(payment_log_name: str, user: str) -> None:
    """
    Drive one log through the sweep and report what it booked.

    The back-off is skipped and the attempt is not counted.
    """
    run_sweep(
        PAYMENT_SWEEP,
        PAYMENT_LOG,
        "payment_entry",
        [payment_log_name],
        create_payment_entry_from_log,
    )

    payment_entry = frappe.db.get_value(PAYMENT_LOG, payment_log_name, "payment_entry")
    if not payment_entry:
        publish_completion(
            payment_log_name,
            user,
            frappe.db.get_value(PAYMENT_LOG, payment_log_name, "errors")
            or _("Settlement produced no Payment Entry."),
            None,
        )
        return

    publish_completion(
        payment_log_name,
        user,
        _("Payment Entry {0} booked.").format(payment_entry),
        payment_entry,
    )


def complete_payment_from_log(payment_log_name: str, user: str) -> None:
    """
    Verify a capture with Paystack and book the settlement it is owed.

    A transaction Paystack reports as different money is refused. Every outcome
    is written, committed and published.
    """
    try:
        log = frappe.get_doc(PAYMENT_LOG, payment_log_name)

        if not is_completable(log):
            publish_completion(
                payment_log_name,
                user,
                _("{0} has no outstanding settlement to complete.").format(payment_log_name),
                log.payment_entry,
            )
            return

        mismatch = verification_mismatch(log, verified_transaction(log))
        if mismatch:
            refuse_completion(payment_log_name, user, mismatch)
            return

        settle_verified_log(payment_log_name, user)
    except Exception:
        refuse_completion(
            payment_log_name,
            user,
            record_failure(
                f"Paystack manual completion failed for log {payment_log_name}",
                reference_doctype=PAYMENT_LOG,
                reference_name=payment_log_name,
            ),
        )
    finally:
        release_completion(payment_log_name)
