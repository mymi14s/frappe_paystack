"""
Hold each company's Paystack collection to its own gateway setting.

Creates the missing Payment Gateway Accounts, repairs the shared controller and
moves an open checkout onto the company its Payment Request bills.
"""

import frappe

from frappe_paystack.setup import (
    GATEWAY_DOCTYPE,
    create_payment_gateway_accounts_for_existing_settings,
    find_replacement_setting,
    update_payment_gateway_controller,
)

PAYMENT_GATEWAY = "Payment Gateway"
PAYMENT_LOG = "Paystack Payment Log"
PAYMENT_REQUEST = "Payment Request"
PAYSTACK = "Paystack"


def repair_gateway_controller() -> None:
    """
    Point the shared controller at a setting that exists.

    The gateway record is left standing when no setting is available.
    """
    current = frappe.db.get_value(PAYMENT_GATEWAY, PAYSTACK, "gateway_controller")
    if frappe.db.exists(GATEWAY_DOCTYPE, current or ""):
        return

    replacement = find_replacement_setting(current or "")
    if replacement:
        update_payment_gateway_controller(replacement)


def restamp_open_checkouts() -> list:
    """
    Move a pending checkout onto the company its Payment Request bills.

    Returns the logs that moved.
    """
    moved = []

    for log in frappe.get_all(
        PAYMENT_LOG,
        filters={"status": "Pending", "payment_request": ["is", "set"]},
        fields=["name", "company", "payment_request"],
    ):
        company = frappe.db.get_value(PAYMENT_REQUEST, log.payment_request, "company")
        if not company or company == log.company:
            continue

        frappe.db.set_value(PAYMENT_LOG, log.name, "company", company, update_modified=False)
        frappe.clear_document_cache(PAYMENT_LOG, log.name)
        moved.append(log.name)

    return moved


def execute() -> None:
    create_payment_gateway_accounts_for_existing_settings()
    repair_gateway_controller()
    restamp_open_checkouts()
    frappe.db.commit()
