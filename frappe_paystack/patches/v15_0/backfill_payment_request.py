"""Link Paystack Payment Logs to the Payment Request they collected."""

import frappe

from frappe_paystack.migration import backfill_payment_requests, linkage_report


def execute() -> None:
    """Backfill the Payment Request link and record what changed."""
    before = linkage_report()
    result = backfill_payment_requests()
    after = linkage_report()

    # The linked names go to the log file.
    frappe.logger("frappe_paystack").info(
        {
            "patch": "v15_0.backfill_payment_request",
            "before": before,
            "after": after,
            **result,
        }
    )

    frappe.db.commit()
