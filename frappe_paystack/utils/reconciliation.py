"""Payment reconciliation engine for Paystack payments."""

from typing import Any, Optional

import frappe
import requests
from frappe import _
from frappe.utils import flt, now, today

from frappe_paystack.utils import log_integration_request, parse_paystack_response, resolve_paystack_settings

RECONCILIATION_LOG = "Paystack Reconciliation Log"
PAYMENT_LOG = "Paystack Payment Log"

PAYSTACK_API = "https://api.paystack.co"

# Paystack answers a reference it has never seen with 404.
TRANSACTION_NOT_FOUND = 404

# Amounts closer than this are treated as equal.
AMOUNT_TOLERANCE = 0.01

# Paystack transaction states that represent a genuinely captured payment.
SETTLED_PAYSTACK_STATUSES = ("success",)

# Payment Log states that mean money was taken.
SETTLED_LOG_STATUSES = (
    "Processed",
    "Needs Attention",
    "Completed",
    "Partially Refunded",
    "Refunded",
)

# Log statuses the reconciliation pass reads.
RECONCILABLE_STATUSES = ("Pending", "Processed", "Needs Attention", "Completed")

# The reconciliation status a human sets by hand.
MANUAL_OVERRIDE = "Manual Override"

# Statuses a human has set; the automated pass skips them.
PROTECTED_STATUSES = (MANUAL_OVERRIDE,)


class ReconciliationEngine:
    """Reconciles Paystack payments with ERPNext records."""

    def __init__(self, company: Optional[str] = None) -> None:
        """Initialize reconciliation engine for a company."""
        self.company = company or frappe.defaults.get_user_default("company")
        if not self.company:
            frappe.throw(
                _("No company supplied and the current user has no default company."),
                frappe.ValidationError,
            )

        self.settings = resolve_paystack_settings(self.company)
        if not self.settings:
            frappe.throw(
                _("Paystack is not enabled for {0}.").format(self.company),
                frappe.ValidationError,
            )

    def run_full_reconciliation(self, date_from: str, date_to: str) -> dict:
        """Reconcile every eligible payment log in a date range."""
        payment_logs = frappe.db.get_list(
            PAYMENT_LOG,
            filters={
                "company": self.company,
                "status": ["in", RECONCILABLE_STATUSES],
                "creation": ["between", [date_from, date_to]],
            },
            fields=["name"],
            order_by="creation asc",
        )

        tally = {"reconciled": 0, "mismatched": 0, "pending": 0, "errored": 0}

        for log in payment_logs:
            try:
                result = self.reconcile_payment(log.name)
            except Exception:
                frappe.log_error(
                    title=f"Paystack reconciliation aborted for {log.name}",
                    message=frappe.get_traceback(),
                )
                tally["errored"] += 1
                continue

            if result["status"] == "Reconciled":
                tally["reconciled"] += 1
            elif result["status"] == "Mismatch":
                tally["mismatched"] += 1
            elif result["status"] == "Skipped":
                continue
            else:
                tally["pending"] += 1

            frappe.db.commit()  # nosemgrep - each reconciled log is kept when a later log fails

        return {"total": len(payment_logs), **tally}

    def reconcile_payment(self, payment_log_name: str) -> dict:
        """Reconcile a single payment log against the Paystack transaction."""
        if not frappe.db.exists(PAYMENT_LOG, payment_log_name):
            frappe.throw(
                _("Payment Log {0} not found.").format(payment_log_name),
                frappe.DoesNotExistError,
            )

        log = frappe.get_doc(PAYMENT_LOG, payment_log_name)

        existing_status = frappe.db.get_value(RECONCILIATION_LOG, payment_log_name, "status")
        if existing_status in PROTECTED_STATUSES:
            return {"payment_log": payment_log_name, "status": "Skipped"}

        try:
            transaction = self.lookup_transaction(log)
        except Exception as e:
            frappe.log_error(
                title=f"Paystack reconciliation lookup failed: {payment_log_name}",
                message=frappe.get_traceback(),
            )
            return self.write_reconciliation_log(
                payment_log_name, None, flt(log.amount_paid), "Pending", str(e)
            )

        if transaction is None:
            return self.write_reconciliation_log(
                payment_log_name,
                0,
                flt(log.amount_paid) - flt(log.total_refunded),
                "Pending",
                "Paystack has no transaction under this reference; nothing was paid",
            )

        status, reason, paystack_amount, recorded_amount = self.compare(log, transaction)
        return self.write_reconciliation_log(
            payment_log_name, paystack_amount, recorded_amount, status, reason
        )

    def compare(self, log: Any, transaction: dict) -> tuple:
        """
        Compare a payment log against a Paystack transaction.

        Returns (status, reason, paystack_amount, recorded_amount), the recorded
        amount net of refunds.
        """
        paystack_amount = flt(transaction.get("amount") or 0) / 100
        paystack_status = (transaction.get("status") or "").lower()
        # The compared figure is net of refunds.
        recorded_amount = flt(log.amount_paid) - flt(log.total_refunded)
        # Raw codes; normalize_currency() coerces unsupported ones to NGN.
        paystack_currency = (transaction.get("currency") or "").upper().strip()
        frappe_currency = (log.currency_paid or log.currency or "").upper().strip()

        if paystack_status not in SETTLED_PAYSTACK_STATUSES:
            status = "Mismatch" if log.status in SETTLED_LOG_STATUSES else "Pending"
            return (
                status,
                f"Paystack reports transaction status '{paystack_status or 'unknown'}'",
                paystack_amount,
                recorded_amount,
            )

        if paystack_currency and frappe_currency and paystack_currency != frappe_currency:
            return (
                "Mismatch",
                f"Currency differs: Paystack {paystack_currency} vs ERPNext {frappe_currency}",
                paystack_amount,
                recorded_amount,
            )

        if log.status != "Completed":
            return (
                "Mismatch",
                f"Paystack captured this payment but the log is still '{log.status}'",
                paystack_amount,
                recorded_amount,
            )

        if abs(paystack_amount - recorded_amount) >= AMOUNT_TOLERANCE:
            return (
                "Mismatch",
                f"Amount differs: Paystack {paystack_amount} vs ERPNext {recorded_amount}",
                paystack_amount,
                recorded_amount,
            )

        return ("Reconciled", None, paystack_amount, recorded_amount)

    def lookup_transaction(self, log: Any) -> Optional[dict]:
        """
        Return the Paystack transaction behind a payment log, or None.

        A log carrying no transaction_id is resolved through verify on its name.
        """
        if log.transaction_id:
            return self.fetch_transaction(log.transaction_id, log.name)

        return self.verify_reference(log.name)

    def fetch_transaction(self, transaction_id: str, payment_log: Optional[str] = None) -> Optional[dict]:
        """Fetch a transaction from the Paystack API by its Paystack id."""
        return self.get_transaction(
            f"{PAYSTACK_API}/transaction/{transaction_id}",
            {"transaction_id": transaction_id},
            payment_log,
        )

    def verify_reference(self, reference: str) -> Optional[dict]:
        """Fetch a transaction by the merchant reference the checkout sent."""
        return self.get_transaction(
            f"{PAYSTACK_API}/transaction/verify/{reference}",
            {"reference": reference},
            reference,
        )

    def get_transaction(self, url: str, request_data: dict, payment_log: Optional[str]) -> Optional[dict]:
        """
        GET a Paystack transaction endpoint, or None when it has no such record.

        Files an Integration Request for the call either way.
        """
        headers = {
            "Authorization": f"Bearer {self.settings.get('secret_key')}",
            "Content-Type": "application/json",
        }

        try:
            response = requests.get(url, headers=headers, timeout=10)
            data = parse_paystack_response(response)
        except Exception as e:
            log_integration_request(
                status="Failed",
                url=url,
                request_data=request_data,
                error=str(e),
                reference_doctype=PAYMENT_LOG,
                reference_docname=payment_log,
            )
            raise

        log_integration_request(
            status="Completed" if response.ok else "Failed",
            url=url,
            request_data=request_data,
            response_data=data,
            reference_doctype=PAYMENT_LOG,
            reference_docname=payment_log,
        )

        if response.status_code == TRANSACTION_NOT_FOUND:
            return None

        response.raise_for_status()

        if not data.get("status"):
            raise ValueError(data.get("message") or "Paystack returned an unsuccessful response")

        return data.get("data") or {}

    def write_reconciliation_log(
        self,
        payment_log_name: str,
        paystack_amount: Optional[float],
        frappe_amount: float,
        status: str,
        reason: Optional[str],
    ) -> dict:
        """Create or update the reconciliation log for a payment."""
        if frappe.db.exists(RECONCILIATION_LOG, payment_log_name):
            log = frappe.get_doc(RECONCILIATION_LOG, payment_log_name)
        else:
            log = frappe.new_doc(RECONCILIATION_LOG)
            log.payment_log = payment_log_name

        log.company = self.company
        log.reconciliation_date = today()
        log.paystack_amount = flt(paystack_amount)
        log.frappe_amount = flt(frappe_amount)
        log.status = status
        log.mismatch_reason = reason or ""
        log.reconciled_by = frappe.session.user
        log.reconciled_at = now()

        log.flags.ignore_permissions = True
        log.save()

        return {"payment_log": payment_log_name, "status": status, "reason": reason}
