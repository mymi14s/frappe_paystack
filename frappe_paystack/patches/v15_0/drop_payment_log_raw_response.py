import frappe

PAYMENT_LOG = "Paystack Payment Log"


def execute() -> None:
    """
    Drop raw_response and the submittable columns from Paystack Payment Log.

    Submitted rows are returned to draft.
    """
    for column in ("raw_response", "amended_from"):
        if frappe.db.has_column(PAYMENT_LOG, column):
            frappe.db.sql_ddl(f"ALTER TABLE `tab{PAYMENT_LOG}` DROP COLUMN `{column}`")

    frappe.db.set_value(PAYMENT_LOG, {"docstatus": 1}, "docstatus", 0, update_modified=False)
