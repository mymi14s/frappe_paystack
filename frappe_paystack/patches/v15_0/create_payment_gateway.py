"""Register Paystack as a Payment Gateway on an existing site."""

import frappe

from frappe_paystack.setup import (
    create_payment_gateway_accounts_for_existing_settings,
    create_payment_gateway_record,
    ensure_email_payment_type,
    ensure_mode_of_payment,
)


def execute() -> None:
    ensure_mode_of_payment()
    ensure_email_payment_type()
    create_payment_gateway_record()
    create_payment_gateway_accounts_for_existing_settings()
    frappe.db.commit()
