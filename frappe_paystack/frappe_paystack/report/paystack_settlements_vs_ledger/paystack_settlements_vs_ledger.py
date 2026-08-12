"""
What Paystack says it paid out, beside what the ledger shows it booked.

Sets each of a payout's four Paystack figures next to the ledger entry carrying
it, and totals the captures that name the payout.
"""

from typing import Any, Optional

import frappe
from frappe import _
from frappe.utils import flt, fmt_money

from frappe_paystack.utils import check_company_permission
from frappe_paystack.utils.settlement import BALANCE_TOLERANCE, SETTLEMENT_DOCTYPE, settlement_gateway

PAYMENT_LOG = "Paystack Payment Log"
JOURNAL_ENTRY = "Journal Entry"

# The ledger role each of the gateway's three accounts plays on a payout entry.
ACCOUNT_FIELDS = {
    "suspense_account": "suspense",
    "settlement_bank_account": "bank",
    "paystack_fee_account": "fee",
}


def get_columns() -> list:
    return [
        {
            "label": _("Settlement"),
            "fieldname": "settlement",
            "fieldtype": "Link",
            "options": SETTLEMENT_DOCTYPE,
            "width": 150,
        },
        {
            "label": _("Payout Date"),
            "fieldname": "settlement_date",
            "fieldtype": "Date",
            "width": 110,
        },
        {"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 90},
        {
            "label": _("Journal Entry"),
            "fieldname": "journal_entry",
            "fieldtype": "Link",
            "options": JOURNAL_ENTRY,
            "width": 160,
        },
        {
            "label": _("Currency"),
            "fieldname": "currency",
            "fieldtype": "Data",
            "width": 90,
        },
        {
            "label": _("Paystack Gross"),
            "fieldname": "gross_amount",
            "fieldtype": "Currency",
            "options": "currency",
            "width": 140,
        },
        {
            "label": _("Suspense Cleared"),
            "fieldname": "booked_gross",
            "fieldtype": "Currency",
            "options": "currency",
            "width": 150,
        },
        {
            "label": _("Paystack Fees"),
            "fieldname": "total_fees",
            "fieldtype": "Currency",
            "options": "currency",
            "width": 130,
        },
        {
            "label": _("Fees Booked"),
            "fieldname": "booked_fee",
            "fieldtype": "Currency",
            "options": "currency",
            "width": 130,
        },
        {
            "label": _("Paystack Deductions"),
            "fieldname": "deductions",
            "fieldtype": "Currency",
            "options": "currency",
            "width": 160,
        },
        {
            "label": _("Deductions Booked"),
            "fieldname": "booked_deductions",
            "fieldtype": "Currency",
            "options": "currency",
            "width": 160,
        },
        {
            "label": _("Paystack Net"),
            "fieldname": "net_amount",
            "fieldtype": "Currency",
            "options": "currency",
            "width": 130,
        },
        {
            "label": _("Bank Debited"),
            "fieldname": "booked_net",
            "fieldtype": "Currency",
            "options": "currency",
            "width": 130,
        },
        {
            "label": _("Bank Difference"),
            "fieldname": "difference",
            "fieldtype": "Currency",
            "options": "currency",
            "width": 140,
        },
        {
            "label": _("Captures"),
            "fieldname": "capture_count",
            "fieldtype": "Int",
            "width": 90,
        },
        {
            "label": _("Captures Cleared"),
            "fieldname": "captured_amount",
            "fieldtype": "Currency",
            "options": "currency",
            "width": 150,
        },
        {
            "label": _("Unlinked Gross"),
            "fieldname": "unlinked_amount",
            "fieldtype": "Currency",
            "options": "currency",
            "width": 140,
        },
        {
            "label": _("Discrepancy"),
            "fieldname": "discrepancy",
            "fieldtype": "Data",
            "width": 400,
        },
    ]


def date_condition(filters: dict) -> Optional[list]:
    """Return the settlement_date filter the date range asks for, if any."""
    from_date = filters.get("from_date")
    to_date = filters.get("to_date")

    if from_date and to_date:
        return ["between", [from_date, to_date]]
    if from_date:
        return [">=", from_date]
    if to_date:
        return ["<=", to_date]
    return None


def settlement_rows(filters: dict) -> list:
    """Return the payouts in range, newest first."""
    conditions = {"company": filters.get("company")}

    dates = date_condition(filters)
    if dates:
        conditions["settlement_date"] = dates

    return frappe.get_all(
        SETTLEMENT_DOCTYPE,
        filters=conditions,
        fields=[
            "name as settlement",
            "company",
            "settlement_date",
            "status",
            "currency",
            "gross_amount",
            "total_fees",
            "deductions",
            "net_amount",
            "journal_entry",
            "errors",
        ],
        order_by="settlement_date desc, creation desc",
    )


def capture_totals(settlements: list, company: Optional[str]) -> dict:
    """Return how many captures name each payout, and what they add up to."""
    totals = {}
    if not settlements:
        return totals

    for log in frappe.get_all(
        PAYMENT_LOG,
        filters={"settlement": ["in", settlements], "company": company},
        fields=["settlement", "amount_paid"],
    ):
        bucket = totals.setdefault(log.settlement, {"count": 0, "amount": 0.0})
        bucket["count"] += 1
        bucket["amount"] = flt(bucket["amount"] + flt(log.amount_paid))

    return totals


def ledger_totals(vouchers: list, gateway: Optional[Any]) -> dict:
    """
    Return what each payout's journal entry still stands for in the ledger.

    Cancelled rows are excluded. A voucher with live rows is keyed even when none
    of them touch the gateway's accounts.
    """
    totals = {}
    if not vouchers or not gateway:
        return totals

    roles = {gateway.get(fieldname): role for fieldname, role in ACCOUNT_FIELDS.items()}

    for entry in frappe.get_all(
        "GL Entry",
        filters={
            "voucher_type": JOURNAL_ENTRY,
            "voucher_no": ["in", vouchers],
            "is_cancelled": 0,
        },
        fields=["voucher_no", "account", "debit", "credit"],
    ):
        bucket = totals.setdefault(entry.voucher_no, {})
        if entry.account not in roles:
            continue

        role = roles[entry.account]
        bucket[f"{role}_debit"] = flt(bucket.get(f"{role}_debit")) + flt(entry.debit)
        bucket[f"{role}_credit"] = flt(bucket.get(f"{role}_credit")) + flt(entry.credit)

    return totals


def ledger_gaps(row: dict) -> list:
    """Return a line for each Paystack figure the ledger disagrees with."""
    currency = row["currency"]
    checks = (
        (_("gross"), row["gross_amount"], row["booked_gross"]),
        (_("fees"), row["total_fees"], row["booked_fee"]),
        (_("deductions"), row["deductions"], row["booked_deductions"]),
        (_("net"), row["net_amount"], row["booked_net"]),
    )

    return [
        _("Paystack {0} {1}, ledger {2}").format(
            label,
            fmt_money(flt(paystack), currency=currency),
            fmt_money(flt(booked), currency=currency),
        )
        for label, paystack, booked in checks
        if abs(flt(paystack) - flt(booked)) > BALANCE_TOLERANCE
    ]


def discrepancy(row: dict, gateway: Optional[Any], ledger: Optional[dict]) -> str:
    """Return one line saying why a payout does not tie out, empty when it does."""
    if not row["journal_entry"]:
        return row["errors"] or _("Not posted: no journal entry cleared this payout.")

    if not gateway:
        return _(
            "Paystack is not enabled for {0}, so the accounts this payout posted to cannot be resolved."
        ).format(row["company"])

    if ledger is None:
        return _("Journal Entry {0} carries no live ledger entry; it has been cancelled.").format(
            row["journal_entry"]
        )

    gaps = ledger_gaps(row)
    if gaps:
        return "; ".join(gaps)

    if abs(flt(row["unlinked_amount"])) > BALANCE_TOLERANCE:
        return _("Captures naming this payout total {0} of its {1} gross.").format(
            fmt_money(flt(row["captured_amount"]), currency=row["currency"]),
            fmt_money(flt(row["gross_amount"]), currency=row["currency"]),
        )

    return ""


def get_data(filters: dict) -> list:
    rows = settlement_rows(filters)
    gateway = settlement_gateway(filters.get("company"))
    captures = capture_totals([row["settlement"] for row in rows], filters.get("company"))
    ledger = ledger_totals([row["journal_entry"] for row in rows if row["journal_entry"]], gateway)

    for row in rows:
        capture = captures.get(row["settlement"]) or {"count": 0, "amount": 0.0}
        booked = ledger.get(row["journal_entry"])
        amounts = booked or {}

        row["booked_gross"] = flt(amounts.get("suspense_credit"))
        row["booked_deductions"] = flt(amounts.get("suspense_debit"))
        row["booked_fee"] = flt(amounts.get("fee_debit"))
        row["booked_net"] = flt(amounts.get("bank_debit"))
        row["difference"] = flt(row["net_amount"]) - row["booked_net"]
        row["capture_count"] = capture["count"]
        row["captured_amount"] = capture["amount"]
        row["unlinked_amount"] = flt(row["gross_amount"]) - capture["amount"]
        row["discrepancy"] = discrepancy(row, gateway, booked)
        row.pop("company")
        row.pop("errors")

    if filters.get("only_discrepancies"):
        return [row for row in rows if row["discrepancy"]]

    return rows


def execute(filters: Optional[dict] = None) -> tuple:
    filters = filters or {}
    check_company_permission(filters.get("company"))
    return get_columns(), get_data(filters)
