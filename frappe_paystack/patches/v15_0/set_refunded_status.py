import frappe
from frappe.utils import flt

from frappe_paystack.frappe_paystack.doctype.paystack_refund_log.paystack_refund_log import refund_status

PAYMENT_LOG = "Paystack Payment Log"


def execute() -> None:
    """Stamp the refund status on payment logs that already carry refunds."""
    logs = frappe.get_all(
        PAYMENT_LOG,
        filters={"total_refunded": [">", 0]},
        fields=["name", "amount_paid", "total_refunded"],
    )

    for log in logs:
        frappe.db.set_value(
            PAYMENT_LOG,
            log.name,
            "status",
            refund_status(flt(log.total_refunded), flt(log.amount_paid)),
            update_modified=False,
        )
