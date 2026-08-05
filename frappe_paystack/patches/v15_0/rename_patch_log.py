"""
Point the Patch Log rows of the moved patches at their current paths.

Runs pre_model_sync.
"""

from frappe_paystack.migration import rename_patch_log_entries


def execute() -> None:
    rename_patch_log_entries()
