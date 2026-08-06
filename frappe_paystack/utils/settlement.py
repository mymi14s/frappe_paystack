"""
Paystack payouts: the leg that clears the suspense account and books the fee.

One journal entry per payout takes the net to the bank, the fee to the fee
account and the whole gross out of suspense.
"""

from typing import Any, Optional

import frappe
import requests
from frappe import _
from frappe.utils import add_days, flt, getdate, nowdate

from frappe_paystack.utils import (
    from_minor_units,
    log_error_for,
    log_integration_request,
    parse_paystack_response,
    record_failure,
    resolve_paystack_settings,
)
from frappe_paystack.utils.sweep import run_sweep

SETTLEMENT_DOCTYPE = "Paystack Settlement"
PAYMENT_LOG = "Paystack Payment Log"
GATEWAY_DOCTYPE = "Paystack Gateway Setting"

# Paystack sends the payout event under both names.
SETTLEMENT_EVENTS = ("settlement.success", "settlement.processed")

SETTLEMENT_TRANSACTIONS_URL = "https://api.paystack.co/settlement/{0}/transaction"

# Transactions read per page while walking a payout.
TRANSACTION_PAGE_SIZE = 100

# Pages walked before the linkage gives up.
MAX_TRANSACTION_PAGES = 50

# Rounding slack between the gross, the fee and the net Paystack reports.
BALANCE_TOLERANCE = 0.01

# Oldest payout the retry sweep will pick up.
RETRY_LOOKBACK_DAYS = 30

# Payouts one retry sweep attempts.
RETRY_LIMIT = 50

LINK_TRANSACTIONS_JOB = "frappe_paystack.utils.settlement.link_settled_payments"

# Names this sweep's Integration Requests, streak keys and Error Logs.
SETTLEMENT_SWEEP = "settlement"


def process_settlement_webhook_event(data: dict, company: Optional[str]) -> None:
    """Record a Paystack payout and clear the suspense account it emptied."""
    settlement_id = None
    try:
        payout = frappe._dict(data.get("data") or {})
        settlement_id = str(payout.get("id") or "")

        if not settlement_id:
            log_integration_request(
                status="Failed",
                url="webhook",
                request_data=data,
                error="Paystack settlement carries no id",
            )
            return

        if not company:
            log_integration_request(
                status="Failed",
                url="webhook",
                request_data=data,
                error=f"No Paystack gateway company for settlement {settlement_id}",
            )
            return

        if frappe.db.exists(SETTLEMENT_DOCTYPE, settlement_id):
            log_integration_request(
                status="Completed",
                url="webhook",
                request_data=data,
                response_data={"skipped": f"settlement {settlement_id} already recorded"},
                reference_doctype=SETTLEMENT_DOCTYPE,
                reference_docname=settlement_id,
            )
            return

        settlement = record_settlement(payout, company, settlement_id)
        settlement.db_set(
            "integration_request",
            log_integration_request(
                status="Completed",
                url="webhook",
                request_data=data,
                response_data={"settlement": settlement_id},
                reference_doctype=SETTLEMENT_DOCTYPE,
                reference_docname=settlement.name,
            ),
            update_modified=False,
        )
        frappe.db.commit()

        # Only a posted payout claims its captures.
        if post_settlement_entry(settlement):
            enqueue_transaction_linkage(settlement.name)
    except Exception as e:
        log_error_for(
            title=f"Paystack settlement webhook failed: {settlement_id or 'unmatched'}",
            message=str(e),
            reference_doctype=SETTLEMENT_DOCTYPE,
            reference_name=settlement_id,
        )


def settlement_date(payout: dict) -> Any:
    """
    Return the date a payout landed, defaulting to today.

    The webhook names the field settlement_date, the endpoint settled_at.
    """
    return getdate(payout.get("settlement_date") or payout.get("settled_at"))


def record_settlement(payout: dict, company: str, settlement_id: str) -> Any:
    """
    Insert the Paystack Settlement a payout payload describes.

    The currency is stored exactly as Paystack sent it.
    """
    currency = (payout.get("currency") or "").upper().strip()

    settlement = frappe.new_doc(SETTLEMENT_DOCTYPE)
    settlement.settlement_id = settlement_id
    settlement.company = company
    settlement.status = "Pending"
    settlement.settlement_date = settlement_date(payout)
    settlement.currency = currency
    settlement.gross_amount = from_minor_units(payout.get("total_amount") or 0, currency)
    settlement.total_fees = from_minor_units(payout.get("total_fees") or 0, currency)
    settlement.deductions = from_minor_units(payout.get("deductions") or 0, currency)
    settlement.net_amount = from_minor_units(payout.get("effective_amount") or 0, currency)
    settlement.flags.ignore_permissions = True
    settlement.insert()

    return settlement


def settlement_gateway(company: Optional[str]) -> Optional[Any]:
    """Return the enabled gateway setting a payout is booked through."""
    name = frappe.db.get_value(GATEWAY_DOCTYPE, {"enabled": 1, "company": company}, "name")
    return frappe.get_doc(GATEWAY_DOCTYPE, name) if name else None


def missing_settlement_accounts(gateway: Any) -> list:
    """Return the labels of the accounts a payout entry cannot be built without."""
    return [
        _(gateway.meta.get_label(fieldname))
        for fieldname in (
            "suspense_account",
            "settlement_bank_account",
            "paystack_fee_account",
        )
        if not gateway.get(fieldname)
    ]


def unpostable_reason(settlement: Any, gateway: Optional[Any]) -> Optional[str]:
    """
    Report why a payout cannot be booked, or None when it can.

    Refuses a currency the company does not book in, a gateway missing any of
    the three accounts, and totals that do not add up.
    """
    if not gateway:
        return _("Paystack is not enabled for {0}.").format(settlement.company)

    company_currency = frappe.get_cached_value("Company", settlement.company, "default_currency")
    if settlement.currency != company_currency:
        return _("Paystack paid out in {0} but {1} books in {2}. Clear this payout by hand.").format(
            settlement.currency, settlement.company, company_currency
        )

    missing = missing_settlement_accounts(gateway)
    if missing:
        return _("Set {0} on the Paystack Gateway Setting before this payout can be booked.").format(
            ", ".join(missing)
        )

    if flt(settlement.gross_amount) <= 0:
        return _("Paystack reports nothing settled on this payout.")

    shortfall = (
        flt(settlement.gross_amount)
        - flt(settlement.total_fees)
        - flt(settlement.deductions)
        - flt(settlement.net_amount)
    )
    if abs(shortfall) > BALANCE_TOLERANCE:
        return _(
            "Paystack reports {0} settled less {1} in fees and {2} in deductions, "
            "which is not the {3} paid out. Clear this payout by hand."
        ).format(
            settlement.gross_amount,
            settlement.total_fees,
            settlement.deductions,
            settlement.net_amount,
        )

    return None


def build_settlement_entry(settlement: Any, gateway: Any) -> Any:
    """
    Return the journal entry that empties suspense into the bank and the fee.

    A zero fee gets no row. A deduction debits suspense alone.
    """
    cost_center = frappe.get_cached_value("Company", settlement.company, "cost_center")

    entry = frappe.new_doc("Journal Entry")
    entry.voucher_type = "Journal Entry"
    entry.company = settlement.company
    entry.posting_date = settlement.settlement_date
    entry.cheque_no = settlement.settlement_id
    entry.cheque_date = settlement.settlement_date
    entry.user_remark = f"Paystack settlement {settlement.settlement_id}"

    entry.append(
        "accounts",
        {
            "account": gateway.settlement_bank_account,
            "debit_in_account_currency": flt(settlement.net_amount),
            "cost_center": cost_center,
        },
    )

    if flt(settlement.total_fees):
        entry.append(
            "accounts",
            {
                "account": gateway.paystack_fee_account,
                "debit_in_account_currency": flt(settlement.total_fees),
                "cost_center": cost_center,
            },
        )

    if flt(settlement.deductions):
        entry.append(
            "accounts",
            {
                "account": gateway.suspense_account,
                "debit_in_account_currency": flt(settlement.deductions),
                "cost_center": cost_center,
            },
        )

    entry.append(
        "accounts",
        {
            "account": gateway.suspense_account,
            "credit_in_account_currency": flt(settlement.gross_amount),
            "cost_center": cost_center,
        },
    )

    return entry


def discard_draft_entry(entry: Optional[Any]) -> None:
    """Remove a payout's journal entry when it was inserted but never submitted."""
    name = entry.name if entry else None
    if not name or frappe.db.get_value("Journal Entry", name, "docstatus") != 0:
        return

    frappe.delete_doc("Journal Entry", name, force=True, ignore_permissions=True)


def post_settlement_entry(settlement: Any) -> Optional[str]:
    """
    Book a payout, returning the journal entry that cleared it.

    A payout that already carries an entry returns it. Whatever blocks a posting
    is written to the payout.
    """
    if settlement.journal_entry:
        return settlement.journal_entry

    gateway = settlement_gateway(settlement.company)
    reason = unpostable_reason(settlement, gateway)
    if reason:
        settlement.db_set("errors", reason, update_modified=False)
        frappe.db.commit()  # nosemgrep - the unpostable reason is written before the early return
        return None

    entry = None
    try:
        entry = build_settlement_entry(settlement, gateway)
        entry.flags.ignore_permissions = True
        entry.insert()
        entry.submit()
    except Exception:
        discard_draft_entry(entry)
        settlement.db_set("status", "Failed", update_modified=False)
        settlement.db_set(
            "errors",
            record_failure(
                f"Paystack settlement journal entry failed for payout {settlement.name}",
                reference_doctype=SETTLEMENT_DOCTYPE,
                reference_name=settlement.name,
            ),
            update_modified=False,
        )
        frappe.db.commit()
        return None

    settlement.db_set("journal_entry", entry.name, update_modified=False)
    settlement.db_set("status", "Processed", update_modified=False)
    settlement.db_set("errors", None, update_modified=False)
    frappe.db.commit()  # nosemgrep - the booked journal entry outlives a later rollback

    return entry.name


def enqueue_transaction_linkage(settlement_name: str) -> None:
    """
    Queue the walk that names which captures a payout paid out.

    One Paystack call per page, deduplicated on the payout.
    """
    frappe.enqueue(
        LINK_TRANSACTIONS_JOB,
        settlement_name=settlement_name,
        queue="long",
        timeout=600,
        is_async=not frappe.flags.in_test,
        job_id=f"paystack-settlement-{settlement_name}",
        deduplicate=True,
    )


def settlement_transactions(settings: dict, settlement: Any) -> list:
    """
    Return every transaction Paystack paid out in a settlement.

    An unreadable page ends the walk.
    """
    url = SETTLEMENT_TRANSACTIONS_URL.format(settlement.settlement_id)
    headers = {"Authorization": f"Bearer {settings.get('secret_key')}"}
    transactions = []

    for page in range(1, MAX_TRANSACTION_PAGES + 1):
        params = {"perPage": TRANSACTION_PAGE_SIZE, "page": page}

        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
            data = parse_paystack_response(response)
        except Exception as e:
            log_integration_request(
                status="Failed",
                url=url,
                request_data=params,
                error=str(e),
                reference_doctype=SETTLEMENT_DOCTYPE,
                reference_docname=settlement.name,
            )
            break

        log_integration_request(
            status="Completed" if response.ok else "Failed",
            url=url,
            request_data=params,
            response_data=data,
            reference_doctype=SETTLEMENT_DOCTYPE,
            reference_docname=settlement.name,
        )

        rows = data.get("data") or []
        transactions.extend(rows)

        if len(rows) < TRANSACTION_PAGE_SIZE:
            break

    return transactions


def link_settled_payments(settlement_name: str) -> int:
    """
    Stamp each capture in a payout with the payout that cleared it.

    The fee comes from the transaction. Matching is scoped to the payout's company.
    """
    settlement = frappe.get_doc(SETTLEMENT_DOCTYPE, settlement_name)
    settings = resolve_paystack_settings(settlement.company)
    if not settings:
        return 0

    linked = 0
    for transaction in settlement_transactions(settings, settlement):
        transaction_id = str(transaction.get("id") or "")
        if not transaction_id:
            continue

        name = frappe.db.get_value(
            PAYMENT_LOG,
            {"transaction_id": transaction_id, "company": settlement.company},
            "name",
        )
        if not name:
            continue

        frappe.db.set_value(
            PAYMENT_LOG,
            name,
            {
                "settlement": settlement.name,
                "paystack_fee": from_minor_units(transaction.get("fees") or 0, settlement.currency),
            },
            update_modified=False,
        )
        linked += 1

    frappe.db.commit()  # nosemgrep - the linked captures are kept when a later page fails
    return linked


def unposted_settlements() -> list:
    """Return recent payouts that never produced a journal entry."""
    return frappe.get_all(
        SETTLEMENT_DOCTYPE,
        filters={
            "journal_entry": ["in", ["", None]],
            "creation": [">", add_days(nowdate(), -RETRY_LOOKBACK_DAYS)],
        },
        pluck="name",
        order_by="creation asc",
        limit=RETRY_LIMIT,
    )


def retry_unposted_settlements() -> None:
    """Book payouts whose journal entry could not be raised when they arrived."""
    try:
        names = unposted_settlements()
    except Exception:
        frappe.log_error(
            title="Paystack settlement sweep: lookup failed",
            message=frappe.get_traceback(),
        )
        return

    run_sweep(
        SETTLEMENT_SWEEP,
        SETTLEMENT_DOCTYPE,
        "journal_entry",
        names,
        post_unposted_settlement,
    )


def post_unposted_settlement(name: str) -> None:
    """Re-drive one payout's journal entry by name."""
    post_settlement_entry(frappe.get_doc(SETTLEMENT_DOCTYPE, name))
