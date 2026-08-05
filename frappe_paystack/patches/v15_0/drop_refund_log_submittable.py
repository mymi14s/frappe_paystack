import frappe

REFUND_LOG = "Paystack Refund Log"

UNREVERSED_NOTE = (
    "Refund was sent while this log was submitted, so no reversal Payment "
    "Entry was booked. Book the reversal manually."
)


def execute() -> None:
    """
    Drop the submittable columns from Paystack Refund Log.

    Submitted and cancelled rows are returned to draft, and rows that moved
    money without a reversal Payment Entry are flagged in errors.
    """
    unreversed = frappe.get_all(
        REFUND_LOG,
        filters={
            "docstatus": [">", 0],
            "status": ["in", ["Processed", "Completed"]],
            "reversal_payment_entry": ["is", "not set"],
        },
        pluck="name",
    )
    for name in unreversed:
        frappe.db.set_value(REFUND_LOG, name, "errors", UNREVERSED_NOTE, update_modified=False)

    frappe.db.set_value(REFUND_LOG, {"docstatus": [">", 0]}, "docstatus", 0, update_modified=False)

    if frappe.db.has_column(REFUND_LOG, "amended_from"):
        frappe.db.sql_ddl(f"ALTER TABLE `tab{REFUND_LOG}` DROP COLUMN `amended_from`")
