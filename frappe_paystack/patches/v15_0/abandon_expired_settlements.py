"""
Move the captures the retry window has closed on into Needs Attention.

Runs post_model_sync. Each row is committed as it is moved.
"""

from frappe_paystack.frappe_paystack.doctype.paystack_payment_log.paystack_payment_log import (
    abandon_expired_settlements,
)


def execute() -> None:
    abandon_expired_settlements()
