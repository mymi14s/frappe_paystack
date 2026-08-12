"""
Put the payment logs already stuck at Processed onto the settlement back-off.

Runs post_model_sync.
"""

from frappe_paystack.migration import seed_payment_log_retries


def execute() -> None:
    seed_payment_log_retries()
