"""Move every human-set reconciliation onto the current override status."""

from frappe_paystack.migration import rename_manual_overrides


def execute() -> None:
    rename_manual_overrides()
