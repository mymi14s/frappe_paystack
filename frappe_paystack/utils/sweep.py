"""
Durable reporting for the retry sweeps.

A sweep with work leaves one Integration Request naming what it attempted and
what is still stuck, and an Error Log once a row resists SWEEP_ESCALATION sweeps.
"""

from typing import Callable, Optional

import frappe
from frappe import _
from frappe.utils import cint

from frappe_paystack.utils import log_error_for, log_integration_request

# Consecutive failed sweeps a row is allowed before it is escalated.
SWEEP_ESCALATION = 6

# Streak lifetime, in seconds.
SWEEP_STREAK_TTL = 2 * 24 * 60 * 60

# Field every sweepable doctype carries the last failure in.
ERRORS_FIELD = "errors"


def streak_key(sweep: str, name: str) -> str:
    """Return the cache key holding a row's consecutive failure count."""
    return frappe.cache.make_key(f"paystack-sweep-{sweep}:{name}")


def clear_streak(sweep: str, name: str) -> None:
    """Forget a row the sweep has just resolved."""
    frappe.cache.delete(streak_key(sweep, name))


def record_streak(sweep: str, name: str) -> int:
    """Increment and return how many sweeps in a row have failed on this name."""
    key = streak_key(sweep, name)
    streak = cint(frappe.cache.incrby(key, 1))
    if streak == 1:
        frappe.cache.expire(key, SWEEP_STREAK_TTL)
    return streak


def escalate(sweep: str, doctype: str, name: str, reason: Optional[str]) -> None:
    """Alert once, on the sweep where a row's streak reaches SWEEP_ESCALATION."""
    if record_streak(sweep, name) != SWEEP_ESCALATION:
        return

    log_error_for(
        title=_("Paystack {0} sweep: {1} still unresolved after {2} attempts").format(
            sweep, name, SWEEP_ESCALATION
        ),
        message=reason
        or _("{0} {1} reports no error of its own, so the cause is elsewhere.").format(doctype, name),
        reference_doctype=doctype,
        reference_name=name,
    )


def report_sweep(sweep: str, attempted: list, unresolved: list) -> None:
    """File one Integration Request for a sweep that had work to do."""
    error = None
    if unresolved:
        error = _("{0} of {1} still unresolved: {2}").format(
            len(unresolved), len(attempted), ", ".join(unresolved)
        )

    log_integration_request(
        status="Failed" if unresolved else "Completed",
        url=f"sweep:{sweep}",
        request_data={"sweep": sweep, "attempted": attempted},
        response_data={
            "resolved": [name for name in attempted if name not in unresolved],
            "unresolved": unresolved,
        },
        error=error,
    )


def run_sweep(sweep: str, doctype: str, field: str, names: list, drive: Callable) -> None:
    """
    Drive each stuck row, then leave one trace of what is still stuck.

    Nothing is written when there is nothing to sweep. `field` is the accounting
    document the row waits for, read back after each attempt.
    """
    if not names:
        return

    unresolved = []
    for name in names:
        drive(name)

        if frappe.db.get_value(doctype, name, field):
            clear_streak(sweep, name)
            continue

        unresolved.append(name)
        escalate(sweep, doctype, name, frappe.db.get_value(doctype, name, ERRORS_FIELD))

    report_sweep(sweep, names, unresolved)
