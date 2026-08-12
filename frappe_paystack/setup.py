"""Registers Paystack as a standard Payment Gateway, on install and from patches."""

import json
from typing import Any, Dict, List

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_field
from payments.utils import create_payment_gateway

GATEWAY_DOCTYPE = "Paystack Gateway Setting"
MODE_OF_PAYMENT = "Paystack"

PAYMENT_LOG = "Paystack Payment Log"
REFUND_LOG = "Paystack Refund Log"
RECONCILIATION_LOG = "Paystack Reconciliation Log"

ERROR_LOG = "Error Log"
INTEGRATION_REQUEST = "Integration Request"
SETTLEMENT = "Paystack Settlement"

NUMBER_CARD = "Number Card"
DASHBOARD_CHART = "Dashboard Chart"

# Error Log title prefix the unreviewed-errors card counts by.
ERROR_TITLE_PREFIX = "Paystack"

# Payment Log statuses that mean the money was captured.
CAPTURED_STATUSES = [
    "Processed",
    "Needs Attention",
    "Completed",
    "Partially Refunded",
    "Refunded",
]

# The workspace's number cards, each counting outstanding work.
NUMBER_CARDS = [
    {
        "name": "Paystack Awaiting Settlement",
        "document_type": PAYMENT_LOG,
        "filters_json": json.dumps(
            [
                [PAYMENT_LOG, "status", "=", "Processed"],
                [PAYMENT_LOG, "payment_entry", "is", "not set"],
            ]
        ),
    },
    {
        "name": "Paystack Failed Payments",
        "document_type": PAYMENT_LOG,
        "filters_json": json.dumps([[PAYMENT_LOG, "status", "=", "Failed"]]),
    },
    {
        "name": "Paystack Reconciliation Mismatches",
        "document_type": RECONCILIATION_LOG,
        "filters_json": json.dumps([[RECONCILIATION_LOG, "status", "=", "Mismatch"]]),
    },
    {
        "name": "Paystack Refunds Pending",
        "document_type": REFUND_LOG,
        "filters_json": json.dumps([[REFUND_LOG, "status", "=", "Pending"]]),
    },
    {
        # Rejected signatures, throttled webhooks and refused calls file one of these.
        "name": "Paystack Failed API Calls",
        "document_type": INTEGRATION_REQUEST,
        "filters_json": json.dumps(
            [
                [INTEGRATION_REQUEST, "integration_request_service", "=", "Paystack"],
                [INTEGRATION_REQUEST, "status", "=", "Failed"],
            ]
        ),
    },
    {
        # Counts unseen Error Logs titled "Paystack ...".
        "name": "Paystack Errors Unreviewed",
        "document_type": ERROR_LOG,
        "filters_json": json.dumps(
            [
                [ERROR_LOG, "method", "like", f"{ERROR_TITLE_PREFIX}%"],
                [ERROR_LOG, "seen", "=", 0],
            ]
        ),
    },
    {
        # A settlement with no journal entry is money still in suspense.
        "name": "Paystack Payouts Not Booked",
        "document_type": SETTLEMENT,
        "filters_json": json.dumps([[SETTLEMENT, "journal_entry", "is", "not set"]]),
    },
    {
        # Captures the settlement retry gave up on.
        "name": "Paystack Needs Attention",
        "document_type": PAYMENT_LOG,
        "filters_json": json.dumps([[PAYMENT_LOG, "status", "=", "Needs Attention"]]),
    },
]

DASHBOARD_CHARTS: List[Dict[str, Any]] = [
    {
        "chart_name": "Paystack Payments Captured",
        "chart_type": "Sum",
        "document_type": PAYMENT_LOG,
        "based_on": "payment_date",
        "value_based_on": "amount_paid",
        "timespan": "Last Month",
        "time_interval": "Daily",
        "timeseries": 1,
        "type": "Line",
        "filters_json": json.dumps([[PAYMENT_LOG, "status", "in", CAPTURED_STATUSES]]),
    },
    {
        "chart_name": "Paystack Payments by Status",
        "chart_type": "Group By",
        "document_type": PAYMENT_LOG,
        "group_by_based_on": "status",
        "group_by_type": "Count",
        "type": "Donut",
        "filters_json": "[]",
    },
]

# POS Settings fields for a phone payment.
POS_INVOICE_FIELDS = [
    {
        "fieldname": "contact_mobile",
        "label": "Mobile No",
        "fieldtype": "Data",
        "read_only": 0,
    },
    {
        "fieldname": "contact_email",
        "label": "Email",
        "fieldtype": "Data",
        "read_only": 0,
    },
    {
        "fieldname": "request_for_payment",
        "label": "Request for Payment",
        "fieldtype": "Button",
        "read_only": 0,
    },
]

PAYMENT_TYPES = "Cash\nBank\nGeneral\nPhone\nEmail"


def ensure_email_payment_type() -> None:
    """Add Email to the Mode of Payment type options, through a Property Setter."""
    frappe.make_property_setter(
        {
            "doctype": "Mode of Payment",
            "fieldname": "type",
            "property": "options",
            "value": PAYMENT_TYPES,
            "property_type": "Text",
        },
        is_system_generated=False,
    )

    # Makes Email available to a save in the same request.
    frappe.clear_cache(doctype="Mode of Payment")


def ensure_pos_contact_email_field() -> None:
    """Add the contact_email field POS Invoice holds a one-off address in."""
    create_custom_field(
        "POS Invoice",
        {
            "fieldname": "contact_email",
            "label": "Email",
            "fieldtype": "Data",
            "options": "Email",
            "insert_after": "contact_mobile",
        },
        ignore_validate=True,
    )


def ensure_mode_of_payment(pos_enabled: bool = False) -> str:
    """
    Create the 'Paystack' Mode of Payment if it does not already exist.

    pos_enabled types the mode "Phone". Returns the mode name.
    """
    if not frappe.db.exists("Mode of Payment", MODE_OF_PAYMENT):
        mop = frappe.get_doc(
            {
                "doctype": "Mode of Payment",
                "mode_of_payment": MODE_OF_PAYMENT,
                "enabled": 1,
                "type": "Phone" if pos_enabled else "General",
            }
        )
        mop.flags.ignore_permissions = True
        mop.insert()
    return MODE_OF_PAYMENT


def setup_pos_payment_mode(company: str, suspense_account: str, channel: str = "Email") -> str:
    """
    Wire the Paystack Mode of Payment up for the POS counter.

    The Mode of Payment Account uses the same account as the gateway. Returns
    the Mode of Payment name.
    """
    if channel not in ("Phone", "Email"):
        frappe.throw(_("Unsupported POS payment channel: {0}").format(channel))

    ensure_email_payment_type()
    ensure_mode_of_payment(pos_enabled=True)

    mop = frappe.get_doc("Mode of Payment", MODE_OF_PAYMENT)
    mop.type = channel

    # POS matches a tender to the gateway by account.
    existing = [row for row in mop.accounts if row.company == company]
    if existing:
        for row in existing:
            row.default_account = suspense_account
    else:
        mop.append("accounts", {"company": company, "default_account": suspense_account})

    mop.flags.ignore_permissions = True
    mop.save()

    ensure_pos_invoice_fields()
    ensure_pos_contact_email_field()
    set_gateway_account_channel(company, channel)
    return mop.name


def set_gateway_account_channel(company: str, channel: str) -> None:
    """Pin the Payment Gateway Account's payment_channel to the POS channel."""
    name = frappe.db.get_value(
        "Payment Gateway Account", {"payment_gateway": "Paystack", "company": company}, "name"
    )
    if name:
        frappe.db.set_value("Payment Gateway Account", name, "payment_channel", channel)


def ensure_pos_invoice_fields() -> None:
    """
    Expose the payment fields on the POS screen, in order.

    POS renders invoice_fields in table order, so the table is rewritten with
    these fields first and the site's own rows after them.
    """
    settings = frappe.get_single("POS Settings")
    ours = [field["fieldname"] for field in POS_INVOICE_FIELDS]

    kept = [row for row in settings.invoice_fields if row.fieldname not in ours]
    settings.invoice_fields = []

    for field in POS_INVOICE_FIELDS:
        settings.append("invoice_fields", field)
    for row in kept:
        # append() keeps an idx a row already carries.
        settings.append("invoice_fields", {**row.as_dict(), "idx": None})

    settings.flags.ignore_permissions = True
    settings.save()


def ensure_number_cards() -> None:
    """
    Create the number cards the Paystack workspace refers to.

    An existing card keeps whatever filters it carries.
    """
    for card in NUMBER_CARDS:
        if frappe.db.exists(NUMBER_CARD, card["name"]):
            continue

        doc = frappe.get_doc(
            {
                "doctype": NUMBER_CARD,
                "type": "Document Type",
                "function": "Count",
                "is_public": 1,
                "show_percentage_stats": 0,
                "label": card["name"],
                **card,
            }
        )
        doc.flags.ignore_permissions = True
        doc.insert()


def ensure_dashboard_charts() -> None:
    """Create the charts the Paystack workspace refers to."""
    for chart in DASHBOARD_CHARTS:
        if frappe.db.exists(DASHBOARD_CHART, chart["chart_name"]):
            continue

        doc = frappe.get_doc({"doctype": DASHBOARD_CHART, "is_public": 1, **chart})
        doc.flags.ignore_permissions = True
        doc.insert()


def create_dashboard_widgets() -> None:
    """Create everything the Paystack workspace renders beyond its links."""
    ensure_number_cards()
    ensure_dashboard_charts()


def after_install() -> None:
    """Register Paystack as a standard Payment Gateway on app install."""
    ensure_mode_of_payment()
    ensure_email_payment_type()
    create_payment_gateway_record()
    create_payment_gateway_accounts_for_existing_settings()
    create_dashboard_widgets()


def create_payment_gateway_record() -> None:
    """Register 'Paystack' as a Payment Gateway via the payments app helper."""
    create_payment_gateway("Paystack", settings=GATEWAY_DOCTYPE)


def update_payment_gateway_controller(setting_name: str) -> None:
    """Point the Payment Gateway controller at a Paystack Gateway Setting."""
    if frappe.db.exists("Payment Gateway", "Paystack"):
        pg = frappe.get_doc("Payment Gateway", "Paystack")
        pg.gateway_controller = setting_name
        pg.flags.ignore_permissions = True
        pg.flags.ignore_links = True
        pg.save()


def create_payment_gateway_account(
    company: str,
    suspense_account: str,
    currency: str = "NGN",
) -> str:
    """Create or update the Paystack Payment Gateway Account for a company."""
    pga_name = frappe.db.get_value(
        "Payment Gateway Account",
        {
            "payment_gateway": "Paystack",
            "company": company,
        },
        "name",
    )

    if pga_name:
        pga = frappe.get_doc("Payment Gateway Account", pga_name)
        pga.payment_account = suspense_account
        pga.currency = currency
        pga.flags.ignore_permissions = True
        pga.save()
        return pga_name

    pga = frappe.get_doc(
        {
            "doctype": "Payment Gateway Account",
            "payment_gateway": "Paystack",
            "payment_account": suspense_account,
            "currency": currency,
            "company": company,
            "is_default": 1,
        }
    )
    pga.flags.ignore_permissions = True
    pga.insert()
    return pga.name


def create_payment_gateway_accounts_for_existing_settings() -> None:
    """Create a Payment Gateway Account per enabled Paystack Gateway Setting."""
    settings = frappe.get_all(
        "Paystack Gateway Setting",
        filters={"enabled": 1},
        fields=["name", "company", "suspense_account", "currency"],
    )

    for s in settings:
        update_payment_gateway_controller(s.name)
        if s.suspense_account:
            create_payment_gateway_account(
                company=s.company,
                suspense_account=s.suspense_account,
                currency=s.currency or "NGN",
            )


def find_replacement_setting(removed_setting: str) -> str:
    """
    Return the setting best placed to take over the Payment Gateway.

    An enabled setting is preferred; a disabled one still resolves.
    """
    return frappe.db.get_value(
        GATEWAY_DOCTYPE,
        {"name": ["!=", removed_setting]},
        "name",
        order_by="enabled desc, modified desc",
    )


def retire_payment_gateway() -> None:
    """Delete the Payment Gateway record once no setting is left."""
    frappe.delete_doc("Payment Gateway", "Paystack", force=True, ignore_permissions=True)
    frappe.clear_document_cache("Payment Gateway", "Paystack")


def repoint_payment_gateway_controller(removed_setting: str) -> None:
    """
    Move the Payment Gateway off a setting that is being deleted.

    It moves to a replacement setting, or the gateway record is retired.
    """
    if not frappe.db.exists("Payment Gateway", "Paystack"):
        return

    current = frappe.db.get_value("Payment Gateway", "Paystack", "gateway_controller")
    if current != removed_setting:
        return

    replacement = find_replacement_setting(removed_setting)
    if not replacement:
        retire_payment_gateway()
        return

    update_payment_gateway_controller(replacement)
