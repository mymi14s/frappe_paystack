"""Delete the Scheduled Job Type row of the weekly reconciliation report."""

from frappe_paystack.migration import drop_removed_scheduled_jobs


def execute() -> None:
    drop_removed_scheduled_jobs()
