"""Site fixtures for the Paystack Cypress specs.

setup_pos_fixtures raises the POS till, setup_shop_fixtures the Webshop
storefront. Each returns a "restore" its teardown puts back.
"""

from typing import Callable, Optional

import frappe
from frappe.installer import update_site_config
from frappe.tests.ui_test_helpers import whitelist_for_tests as frappe_whitelist_for_tests
from frappe.utils import add_to_date, cint, flt, now_datetime, nowdate

from frappe_paystack.setup import MODE_OF_PAYMENT, create_payment_gateway_account, setup_pos_payment_mode
from frappe_paystack.tests.factories import PortalCustomerFactory, SuspenseAccountFactory
from frappe_paystack.utils import hmac_sha512
from frappe_paystack.utils.settlement import post_settlement_entry

COMPANY = "_Test Company"
POS_USER = "test.paystack.pos@example.com"
POS_PASSWORD = "paystack-ui-test-passphrase"
POS_ROLES = (
    "Accounts Manager",
    "Accounts User",
    "Sales Manager",
    "Sales User",
    "Stock User",
    "System Manager",
)
CUSTOMER = "_Test Paystack POS Customer"
CUSTOMER_EMAIL = "test.paystack.shopper@example.com"
CONTACT_FIRST_NAME = "Paystack POS Shopper"
ITEM = "_Test Paystack POS Item"
ITEM_RATE = 100.0
# The item is priced in a list carrying the company's own currency.
PRICE_LIST = "_Test Paystack POS Price List"
POS_PROFILE = "_Test Paystack POS Profile"
GATEWAY = "_Test Paystack POS Gateway"
CASH_MODE = "Cash"

# version-16 turned whitelist_for_tests into a decorator factory.
FRAPPE_MAJOR_VERSION = int(frappe.__version__.split(".")[0])


def whitelist_for_tests(fn: Callable) -> Callable:
    """Whitelist a test endpoint on either frappe branch."""
    if FRAPPE_MAJOR_VERSION < 16:
        return frappe_whitelist_for_tests(fn)
    return frappe_whitelist_for_tests()(fn)


@whitelist_for_tests
def setup_pos_fixtures() -> dict:
    """Create everything the POS specs need and return their names.

    The returned "restore" block is handed straight back to teardown_pos_fixtures.
    """
    restore = {
        "mode_of_payment_type": frappe.db.get_value("Mode of Payment", MODE_OF_PAYMENT, "type"),
        "mute_emails": cint(frappe.conf.get("mute_emails")),
    }

    ensure_company()
    suspense = ensure_gateway_setting()
    ensure_mode_of_payment_account(CASH_MODE)
    # Points the tender and the gateway at one account.
    setup_pos_payment_mode(COMPANY, suspense, channel="Email")

    create_pos_test_user()
    ensure_customer()
    ensure_item()
    profile = ensure_pos_profile()
    opening = ensure_opening_entry(profile)

    # Muting keeps the Email Queue record and leaves delivery off.
    update_site_config("mute_emails", 1)

    frappe.db.commit()

    return {
        "company": COMPANY,
        "customer": CUSTOMER,
        "customer_email": CUSTOMER_EMAIL,
        "item": ITEM,
        "rate": ITEM_RATE,
        "pos_profile": profile,
        "pos_opening_entry": opening,
        "mode_of_payment": MODE_OF_PAYMENT,
        "user": POS_USER,
        "restore": restore,
    }


@whitelist_for_tests
def teardown_pos_fixtures(restore=None) -> None:
    """Remove the fixture records and put the site state back."""
    try:
        remove_pos_invoices()
        remove_opening_entries()

        suspense = frappe.db.get_value("Paystack Gateway Setting", GATEWAY, "suspense_account")

        delete_doc("POS Profile", POS_PROFILE)
        remove_item_prices()
        delete_doc("Price List", PRICE_LIST)
        delete_doc("Item", ITEM)
        remove_customer()
        delete_doc("Paystack Gateway Setting", GATEWAY)
        SuspenseAccountFactory.cleanup(suspense)
    finally:
        restore_site_state(frappe.parse_json(restore) if restore else {})
        frappe.db.commit()


def restore_site_state(restore: dict) -> None:
    """Put back the two settings the fixtures changed."""
    previous_type = restore.get("mode_of_payment_type")
    if previous_type and frappe.db.exists("Mode of Payment", MODE_OF_PAYMENT):
        frappe.db.set_value("Mode of Payment", MODE_OF_PAYMENT, "type", previous_type)

    update_site_config("mute_emails", cint(restore.get("mute_emails")))


@whitelist_for_tests
def pos_payment_logs(pos_invoice: str) -> list:
    """Return the Paystack Payment Logs raised against a POS Invoice."""
    return frappe.get_all(
        "Paystack Payment Log",
        filters={"linked_doctype": "POS Invoice", "linked_docname": pos_invoice},
        fields=["name", "status", "amount", "amount_paid", "payment_reference"],
    )


def create_pos_test_user() -> str:
    """Create the cashier the specs log in as, with a known password."""
    if not frappe.db.exists("User", POS_USER):
        user = frappe.new_doc("User")
        user.email = POS_USER
        user.first_name = "Paystack POS"
        user.send_welcome_email = 0
        user.flags.ignore_password_policy = True
        user.new_password = POS_PASSWORD
        user.insert(ignore_permissions=True)
    else:
        user = frappe.get_doc("User", POS_USER)
        user.flags.ignore_password_policy = True
        user.new_password = POS_PASSWORD

    held = {row.role for row in user.roles}
    for role in POS_ROLES:
        if role not in held and frappe.db.exists("Role", role):
            user.append("roles", {"role": role})

    user.enabled = 1
    user.flags.ignore_permissions = True
    user.save()
    return POS_USER


def ensure_company() -> str:
    """Create the company the till sells for if the site has none."""
    if frappe.db.exists("Company", COMPANY):
        return COMPANY

    company = frappe.get_doc(
        {
            "doctype": "Company",
            "company_name": COMPANY,
            "abbr": "_TC",
            "default_currency": frappe.db.get_single_value("System Settings", "currency") or "NGN",
            "country": frappe.db.get_single_value("System Settings", "country") or "India",
        }
    )
    company.flags.ignore_permissions = True
    company.insert()
    return COMPANY


def ensure_gateway_setting() -> str:
    """Enable Paystack for the test company, returning its suspense account.

    An already enabled setting is used as it stands.
    """
    enabled = frappe.db.get_value(
        "Paystack Gateway Setting",
        {"enabled": 1, "company": COMPANY},
        ["name", "suspense_account"],
        as_dict=True,
    )
    if enabled and enabled.suspense_account:
        return enabled.suspense_account

    suspense_account = SuspenseAccountFactory.create(COMPANY)

    if frappe.db.exists("Paystack Gateway Setting", GATEWAY):
        setting = frappe.get_doc("Paystack Gateway Setting", GATEWAY)
        setting.enabled = 1
        setting.test_mode = 1
        setting.suspense_account = suspense_account
        setting.flags.ignore_permissions = True
        setting.flags.ignore_links = True
        setting.save()
        return suspense_account

    setting = frappe.get_doc(
        {
            "doctype": "Paystack Gateway Setting",
            "gateway": GATEWAY,
            "company": COMPANY,
            # Test keys are only accepted in test mode.
            "test_mode": 1,
            "secret_key": "sk_test_paystack_ui",
            "public_key": "pk_test_paystack_ui",
            "suspense_account": suspense_account,
            "mode_of_payment": MODE_OF_PAYMENT,
            "currency": frappe.db.get_value("Company", COMPANY, "default_currency"),
            "enabled": 1,
        }
    )
    setting.flags.ignore_permissions = True
    setting.flags.ignore_links = True
    setting.insert()
    return suspense_account


def ensure_mode_of_payment_account(mode: str) -> None:
    """Give a Mode of Payment an account for the test company."""
    if not frappe.db.exists("Mode of Payment", mode):
        return
    if frappe.db.exists("Mode of Payment Account", {"parent": mode, "company": COMPANY}):
        return

    account = frappe.db.get_value("Company", COMPANY, "default_cash_account")
    if not account:
        account = SuspenseAccountFactory.create(COMPANY)

    doc = frappe.get_doc("Mode of Payment", mode)
    doc.append("accounts", {"company": COMPANY, "default_account": account})
    doc.flags.ignore_permissions = True
    doc.save()


def ensure_customer() -> str:
    """Create the shopper the payment link is addressed to, with its primary contact."""
    if not frappe.db.exists("Customer", CUSTOMER):
        customer = frappe.get_doc(
            {
                "doctype": "Customer",
                "customer_name": CUSTOMER,
                "customer_group": default_customer_group(),
                "territory": default_territory(),
                "customer_type": "Individual",
            }
        )
        customer.flags.ignore_permissions = True
        customer.flags.ignore_mandatory = True
        customer.insert()

    contact = ensure_contact()
    if frappe.db.get_value("Customer", CUSTOMER, "customer_primary_contact") != contact:
        customer = frappe.get_doc("Customer", CUSTOMER)
        customer.customer_primary_contact = contact
        customer.flags.ignore_permissions = True
        customer.flags.ignore_mandatory = True
        customer.save()

    return CUSTOMER


def ensure_contact() -> str:
    """Create the Contact carrying the shopper's email address."""
    name = frappe.db.get_value(
        "Contact",
        {"first_name": CONTACT_FIRST_NAME, "email_id": CUSTOMER_EMAIL},
        "name",
    )
    if name:
        return name

    contact = frappe.get_doc(
        {
            "doctype": "Contact",
            "first_name": CONTACT_FIRST_NAME,
            "email_ids": [{"email_id": CUSTOMER_EMAIL, "is_primary": 1}],
            "links": [{"link_doctype": "Customer", "link_name": CUSTOMER}],
        }
    )
    contact.flags.ignore_permissions = True
    contact.insert()
    return contact.name


def ensure_item() -> str:
    """Create a priced, non-stock item the till can always sell."""
    if not frappe.db.exists("Item", ITEM):
        item = frappe.get_doc(
            {
                "doctype": "Item",
                "item_code": ITEM,
                "item_name": ITEM,
                "item_group": default_item_group(),
                "stock_uom": "Nos",
                "is_stock_item": 0,
                "is_sales_item": 1,
            }
        )
        item.flags.ignore_permissions = True
        item.flags.ignore_mandatory = True
        item.insert()

    ensure_price_list()

    if not frappe.db.exists("Item Price", {"item_code": ITEM, "price_list": PRICE_LIST}):
        price = frappe.get_doc(
            {
                "doctype": "Item Price",
                "item_code": ITEM,
                "price_list": PRICE_LIST,
                "price_list_rate": ITEM_RATE,
            }
        )
        price.flags.ignore_permissions = True
        price.insert()

    return ITEM


def ensure_price_list() -> str:
    """Create the selling price list the till prices items from."""
    if frappe.db.exists("Price List", PRICE_LIST):
        return PRICE_LIST

    price_list = frappe.get_doc(
        {
            "doctype": "Price List",
            "price_list_name": PRICE_LIST,
            "currency": frappe.db.get_value("Company", COMPANY, "default_currency"),
            "selling": 1,
            "enabled": 1,
        }
    )
    price_list.flags.ignore_permissions = True
    price_list.insert()
    return price_list.name


def ensure_pos_profile() -> str:
    """Create the till the specs sell from, tendering cash and Paystack."""
    if frappe.db.exists("POS Profile", POS_PROFILE):
        return POS_PROFILE

    profile = frappe.get_doc(
        {
            "doctype": "POS Profile",
            "name": POS_PROFILE,
            "company": COMPANY,
            "currency": frappe.db.get_value("Company", COMPANY, "default_currency"),
            "warehouse": default_warehouse(),
            "selling_price_list": PRICE_LIST,
            "write_off_account": write_off_account(),
            "write_off_cost_center": frappe.db.get_value("Company", COMPANY, "cost_center"),
            "write_off_limit": 1,
            "update_stock": 0,
            "hide_unavailable_items": 0,
            "applicable_for_users": [{"user": POS_USER, "default": 1}],
            "payments": [
                {"mode_of_payment": CASH_MODE, "default": 1},
                {"mode_of_payment": MODE_OF_PAYMENT},
            ],
        }
    )
    profile.flags.ignore_permissions = True
    profile.insert()
    return profile.name


def ensure_opening_entry(profile: str) -> str:
    """Open the till, so POS goes straight to the sale screen."""
    open_entry = frappe.db.get_value(
        "POS Opening Entry",
        {"user": POS_USER, "pos_profile": profile, "status": "Open", "docstatus": 1},
        "name",
    )
    if open_entry:
        return open_entry

    entry = frappe.get_doc(
        {
            "doctype": "POS Opening Entry",
            "company": COMPANY,
            "pos_profile": profile,
            "user": POS_USER,
            "period_start_date": now_datetime(),
            "posting_date": nowdate(),
            "balance_details": [{"mode_of_payment": CASH_MODE, "opening_amount": 0}],
        }
    )
    entry.flags.ignore_permissions = True
    entry.insert()
    entry.submit()
    return entry.name


def remove_pos_invoices() -> None:
    """Delete the sales the specs rang up, and their payment logs."""
    invoices = frappe.get_all("POS Invoice", filters={"pos_profile": POS_PROFILE}, pluck="name")
    for invoice in invoices:
        for log in frappe.get_all(
            "Paystack Payment Log",
            filters={"linked_doctype": "POS Invoice", "linked_docname": invoice},
            pluck="name",
        ):
            frappe.db.set_value("Paystack Payment Log", log, {"status": "Pending", "payment_entry": None})
            delete_doc("Paystack Payment Log", log)
        delete_doc("POS Invoice", invoice)


def remove_opening_entries() -> None:
    """Close the fixture till."""
    for entry in frappe.get_all("POS Opening Entry", filters={"pos_profile": POS_PROFILE}, pluck="name"):
        delete_doc("POS Opening Entry", entry)


def remove_item_prices() -> None:
    """Delete the fixture item's prices."""
    for price in frappe.get_all("Item Price", filters={"item_code": ITEM}, pluck="name"):
        delete_doc("Item Price", price)


def remove_customer() -> None:
    """Delete the fixture shopper and the contact holding its address."""
    if frappe.db.exists("Customer", CUSTOMER):
        frappe.db.set_value("Customer", CUSTOMER, "customer_primary_contact", None)

    for contact in frappe.get_all("Contact", filters={"first_name": CONTACT_FIRST_NAME}, pluck="name"):
        delete_doc("Contact", contact)

    delete_doc("Customer", CUSTOMER)


def delete_doc(doctype: str, name: str) -> None:
    """Delete a document, cancelling it first if it was submitted."""
    if not name or not frappe.db.exists(doctype, name):
        return

    doc = frappe.get_doc(doctype, name)
    if doc.docstatus == 1:
        doc.flags.ignore_permissions = True
        doc.flags.ignore_links = True
        doc.cancel()

    frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)


def write_off_account() -> str:
    """Return an account the till can write small differences off to."""
    account = frappe.db.get_value("Company", COMPANY, "write_off_account")
    return account or SuspenseAccountFactory.create(COMPANY)


def default_warehouse() -> str:
    """Return the warehouse the till sells out of."""
    abbr = frappe.db.get_value("Company", COMPANY, "abbr")
    stores = f"Stores - {abbr}"
    if frappe.db.exists("Warehouse", stores):
        return stores

    return frappe.db.get_value("Warehouse", {"company": COMPANY, "is_group": 0}, "name")


def default_item_group() -> str:
    """Return an item group the POS item can hang off."""
    for group in ("Products", "All Item Groups"):
        if frappe.db.exists("Item Group", group):
            return group
    return frappe.db.get_value("Item Group", {"is_group": 0}, "name")


def default_customer_group() -> str:
    """Return a customer group for the fixture shopper."""
    for group in ("Individual", "All Customer Groups"):
        if frappe.db.exists("Customer Group", group):
            return group
    return frappe.db.get_value("Customer Group", {"is_group": 0}, "name")


def default_territory() -> str:
    """Return a territory for the fixture shopper."""
    if frappe.db.exists("Territory", "All Territories"):
        return "All Territories"
    return frappe.db.get_value("Territory", {"is_group": 1}, "name")


SHOP_COMPANY = "_Test Company 1"
SHOP_GATEWAY = "_Test Paystack Shop Gateway"

# The secret the specs' webhook signatures are verified against.
SHOP_SECRET_KEY = "sk_test_paystack_shop_ui"
SHOP_PUBLIC_KEY = "pk_test_paystack_shop_ui"

# The accounts a payout's legs are booked across.
SHOP_SUSPENSE_ACCOUNT = "_Test Paystack Shop Suspense"
SHOP_BANK_ACCOUNT = "_Test Paystack Shop Bank"
SHOP_FEE_ACCOUNT = "_Test Paystack Shop Fee"

SHOP_ITEM = "_Test Paystack Shop Item"
SHOP_ITEM_RATE = 250.0
SHOP_PRICE_LIST = "_Test Paystack Shop Price List"

BUYER = "_Test Paystack Shop Buyer"
BUYER_USER = "test.paystack.buyer@example.com"
BUYER_CONTACT = "Paystack Shop Buyer"

# A second shopper, for the portal's isolation test.
RIVAL = "_Test Paystack Shop Rival"
RIVAL_USER = "test.paystack.rival@example.com"
RIVAL_CONTACT = "Paystack Shop Rival"
RIVAL_AMOUNT = 90.0
RIVAL_REFERENCE = "psref-rival-shop"

SHOP_PAYMENT_LOG = "Paystack Payment Log"
SHOP_SETTLEMENT = "Paystack Settlement"

# Webshop Settings fields the setup overwrites.
WEBSHOP_FIELDS = (
    "enabled",
    "company",
    "price_list",
    "default_customer_group",
    "enable_checkout",
    "payment_gateway_account",
    "payment_success_url",
    "save_quotations_as_draft",
    "allow_items_not_in_stock",
    "show_price",
    "show_stock_availability",
)


@whitelist_for_tests
def setup_shop_fixtures() -> dict:
    """Build the shop the webshop, portal and settlement specs run against.

    The returned "restore" block is handed straight back to teardown_shop_fixtures.
    """
    restore = {
        "webshop": webshop_settings_snapshot(),
        "gateway_controller": frappe.db.get_value("Payment Gateway", "Paystack", "gateway_controller"),
        "mute_emails": cint(frappe.conf.get("mute_emails")),
    }

    accounts = ensure_shop_accounts()
    # Saving an enabled gateway registers it as the Payment Gateway controller.
    ensure_shop_gateway(accounts)
    gateway_account = create_payment_gateway_account(
        company=SHOP_COMPANY, suspense_account=accounts["suspense"], currency=shop_currency()
    )

    ensure_shop_price_list()
    ensure_shop_item()
    route = ensure_website_item()

    PortalCustomerFactory.create(BUYER, BUYER_USER, BUYER_CONTACT)
    PortalCustomerFactory.create(RIVAL, RIVAL_USER, RIVAL_CONTACT)
    rival_invoice, rival_log = ensure_rival_payment()

    ensure_webshop_settings(gateway_account)

    # Muting keeps the Email Queue record and leaves delivery off.
    update_site_config("mute_emails", 1)

    frappe.db.commit()

    return {
        "company": SHOP_COMPANY,
        "currency": shop_currency(),
        "gateway": SHOP_GATEWAY,
        "gateway_account": gateway_account,
        "item": SHOP_ITEM,
        "item_route": route,
        "rate": SHOP_ITEM_RATE,
        "price_list": SHOP_PRICE_LIST,
        "customer": BUYER,
        "buyer_user": BUYER_USER,
        "rival": RIVAL,
        "rival_user": RIVAL_USER,
        "rival_invoice": rival_invoice,
        "rival_log": rival_log,
        "rival_amount": RIVAL_AMOUNT,
        "password": PortalCustomerFactory.PASSWORD,
        "suspense_account": accounts["suspense"],
        "bank_account": accounts["bank"],
        "fee_account": accounts["fee"],
        "restore": restore,
    }


@whitelist_for_tests
def teardown_shop_fixtures(restore=None) -> None:
    """Remove the shop and put the site-wide settings back."""
    try:
        remove_shop_settlements()
        remove_shop_transactions()
        remove_shop_website_item()
        remove_shop_item_prices()
        delete_doc("Item", SHOP_ITEM)
        delete_doc("Price List", SHOP_PRICE_LIST)
        PortalCustomerFactory.cleanup(BUYER, BUYER_USER, BUYER_CONTACT, owned=True)
        PortalCustomerFactory.cleanup(RIVAL, RIVAL_USER, RIVAL_CONTACT, owned=True)
        delete_doc("Paystack Gateway Setting", SHOP_GATEWAY)
        remove_shop_authorizations()
        remove_shop_gateway_account()
        remove_shop_ledger_entries()
        remove_shop_accounts()
    finally:
        restore_shop_state(frappe.parse_json(restore) if restore else {})
        frappe.db.commit()


def restore_shop_state(restore: dict) -> None:
    """Put back the three site-wide settings the fixtures changed."""
    webshop = restore.get("webshop") or {}
    if webshop:
        settings = frappe.get_single("Webshop Settings")
        for fieldname in WEBSHOP_FIELDS:
            settings.set(fieldname, webshop.get(fieldname))
        settings.flags.ignore_permissions = True
        settings.flags.ignore_mandatory = True
        settings.save()

    controller = restore.get("gateway_controller")
    if controller and frappe.db.exists("Payment Gateway", "Paystack"):
        frappe.db.set_value("Payment Gateway", "Paystack", "gateway_controller", controller)

    update_site_config("mute_emails", cint(restore.get("mute_emails")))


@whitelist_for_tests
def sign_webhook(payload: str) -> str:
    """Sign a webhook body with the fixture gateway's secret."""
    return hmac_sha512(payload.encode("utf-8"), SHOP_SECRET_KEY)


@whitelist_for_tests
def shop_payment_logs(linked_doctype: str, linked_docname: str) -> list:
    """Return the Paystack Payment Logs raised against a document."""
    return frappe.get_all(
        SHOP_PAYMENT_LOG,
        filters={"linked_doctype": linked_doctype, "linked_docname": linked_docname},
        fields=[
            "name",
            "status",
            "amount",
            "amount_paid",
            "currency",
            "payment_reference",
            "payment_request",
            "payment_entry",
            "settlement",
            "transaction_id",
        ],
        order_by="creation asc",
    )


@whitelist_for_tests
def expire_payment_log(log: str) -> str:
    """Close a checkout link's validity window, the way the clock would."""
    frappe.db.set_value(SHOP_PAYMENT_LOG, log, "expires_at", add_to_date(now_datetime(), hours=-1))
    frappe.clear_document_cache(SHOP_PAYMENT_LOG, log)
    frappe.db.commit()
    return log


@whitelist_for_tests
def order_being_paid(amount: float = 120.0) -> dict:
    """Raise a Sales Order whose capture the Payment Entry has not booked."""
    order = ensure_shop_order(BUYER, flt(amount))
    log = insert_captured_order_log(order, flt(amount))
    frappe.db.commit()

    return {"order": order, "log": log, "amount": flt(amount)}


@whitelist_for_tests
def order_awaiting_payment(amount: float = 130.0) -> str:
    """Raise a Sales Order nobody has paid."""
    order = ensure_shop_order(BUYER, flt(amount))
    frappe.db.commit()
    return order


def ensure_portal_user(customer: str, user: str) -> None:
    """Give a shopper the Portal User row the order page reads."""
    if frappe.db.exists("Portal User", {"parent": customer, "user": user}):
        return

    doc = frappe.get_doc("Customer", customer)
    doc.append("portal_users", {"user": user})
    doc.flags.ignore_permissions = True
    doc.flags.ignore_mandatory = True
    doc.save()


def ensure_shop_order(customer: str, amount: float) -> str:
    """Submit a Sales Order the shop's payments can be raised against."""
    ensure_portal_user(customer, BUYER_USER)

    order = frappe.get_doc(
        {
            "doctype": "Sales Order",
            "company": SHOP_COMPANY,
            "customer": customer,
            "currency": shop_currency(),
            "conversion_rate": 1,
            "selling_price_list": SHOP_PRICE_LIST,
            "order_type": "Shopping Cart",
            "transaction_date": nowdate(),
            "delivery_date": nowdate(),
            "items": [{"item_code": SHOP_ITEM, "qty": 1, "rate": flt(amount)}],
        }
    )
    order.flags.ignore_permissions = True
    order.flags.ignore_mandatory = True
    order.insert()
    order.submit()
    return order.name


def insert_captured_order_log(order: str, amount: float) -> str:
    """Raise a captured payment log against a Sales Order.

    The log is inserted Pending and stamped captured afterwards.
    """
    log = frappe.new_doc(SHOP_PAYMENT_LOG)
    log.company = SHOP_COMPANY
    log.linked_doctype = "Sales Order"
    log.linked_docname = order
    log.amount = flt(amount)
    log.currency = shop_currency()
    log.status = "Pending"
    log.flags.ignore_permissions = True
    log.insert()

    frappe.db.set_value(
        SHOP_PAYMENT_LOG,
        log.name,
        {
            "status": "Processed",
            "amount_paid": flt(amount),
            "currency_paid": shop_currency(),
            "payment_date": nowdate(),
            "payment_reference": f"psref-{frappe.generate_hash(length=8)}",
        },
    )
    frappe.clear_document_cache(SHOP_PAYMENT_LOG, log.name)
    return log.name


@whitelist_for_tests
def capture_for_settlement(amount: float = 400.0, fee: float = 6.0) -> dict:
    """Raise a captured payment no payout has claimed, returning the log, invoice and amount."""
    invoice = ensure_shop_invoice(BUYER, flt(amount))
    log = insert_captured_log(
        invoice,
        flt(amount),
        "Processed",
        f"psref-{frappe.generate_hash(length=8)}",
        fee=flt(fee),
        transaction_id=frappe.generate_hash(length=10),
    )
    frappe.db.commit()

    return {"log": log, "invoice": invoice, "amount": flt(amount)}


def insert_captured_log(
    invoice: str,
    amount: float,
    status: str,
    reference: str,
    fee: float = 0.0,
    transaction_id: Optional[str] = None,
) -> str:
    """Raise a payment log that already holds captured money.

    The log is inserted Pending and stamped captured afterwards.
    """
    log = frappe.new_doc(SHOP_PAYMENT_LOG)
    log.company = SHOP_COMPANY
    log.linked_doctype = "Sales Invoice"
    log.linked_docname = invoice
    log.amount = flt(amount)
    log.currency = shop_currency()
    log.status = "Pending"
    log.flags.ignore_permissions = True
    log.insert()

    frappe.db.set_value(
        SHOP_PAYMENT_LOG,
        log.name,
        {
            "status": status,
            "amount_paid": flt(amount),
            "currency_paid": shop_currency(),
            "paystack_fee": flt(fee),
            "payment_date": nowdate(),
            "payment_reference": reference,
            "transaction_id": transaction_id,
        },
    )
    frappe.clear_document_cache(SHOP_PAYMENT_LOG, log.name)
    return log.name


@whitelist_for_tests
def complete_gateway_accounts() -> None:
    """Name the payout accounts a settlement journal entry is booked across."""
    accounts = ensure_shop_accounts()
    gateway = frappe.get_doc("Paystack Gateway Setting", SHOP_GATEWAY)
    gateway.settlement_bank_account = accounts["bank"]
    gateway.paystack_fee_account = accounts["fee"]
    gateway.flags.ignore_permissions = True
    gateway.flags.ignore_links = True
    gateway.save()
    frappe.db.commit()


@whitelist_for_tests
def post_shop_settlement(settlement: str) -> Optional[str]:
    """Book the named payout, returning its journal entry."""
    entry = post_settlement_entry(frappe.get_doc(SHOP_SETTLEMENT, settlement))
    frappe.db.commit()
    return entry


@whitelist_for_tests
def claim_captures(settlement: str, logs: str) -> int:
    """Stamp captures with the payout that cleared them, returning how many were stamped."""
    names = frappe.parse_json(logs) or []
    for name in names:
        frappe.db.set_value(SHOP_PAYMENT_LOG, name, "settlement", settlement, update_modified=False)

    frappe.db.commit()
    return len(names)


@whitelist_for_tests
def paystack_api_calls() -> int:
    """Count the requests this site has made to Paystack."""
    return frappe.db.count("Integration Request", {"url": ["like", "https://api.paystack.co%"]})


@whitelist_for_tests
def sales_order_billing(sales_order: str) -> dict:
    """Return how far a Sales Order has been billed, and by which invoice."""
    return {
        "per_billed": flt(frappe.db.get_value("Sales Order", sales_order, "per_billed")),
        "status": frappe.db.get_value("Sales Order", sales_order, "status"),
        "invoice": frappe.db.get_value(
            "Sales Invoice Item", {"sales_order": sales_order, "docstatus": 1}, "parent"
        ),
    }


def webshop_settings_snapshot() -> dict:
    """Return the Webshop Settings values the setup is about to overwrite."""
    settings = frappe.get_single("Webshop Settings")
    return {fieldname: settings.get(fieldname) for fieldname in WEBSHOP_FIELDS}


def shop_currency() -> str:
    """Return the currency the shop's company books in."""
    return frappe.db.get_value("Company", SHOP_COMPANY, "default_currency")


def ensure_shop_accounts() -> dict:
    """Create the three accounts a Paystack payout is booked across."""
    return {
        "suspense": ensure_shop_account(SHOP_SUSPENSE_ACCOUNT, "Asset", "Bank"),
        "bank": ensure_shop_account(SHOP_BANK_ACCOUNT, "Asset", "Bank"),
        "fee": ensure_shop_account(SHOP_FEE_ACCOUNT, "Expense", None),
    }


def ensure_shop_account(account_name: str, root_type: str, account_type: Optional[str]) -> str:
    """Create a leaf account on the shop company's chart, or return it."""
    abbr = frappe.db.get_value("Company", SHOP_COMPANY, "abbr")
    name = f"{account_name} - {abbr}"
    if frappe.db.exists("Account", name):
        return name

    parent = frappe.db.get_value(
        "Account", {"company": SHOP_COMPANY, "is_group": 1, "root_type": root_type}, "name"
    )

    account = frappe.get_doc(
        {
            "doctype": "Account",
            "account_name": account_name,
            "parent_account": parent,
            "company": SHOP_COMPANY,
            "root_type": root_type,
            "account_type": account_type,
            "account_currency": shop_currency(),
            "is_group": 0,
        }
    )
    account.flags.ignore_permissions = True
    account.flags.ignore_mandatory = True
    account.insert()
    return account.name


def ensure_shop_gateway(accounts: dict) -> str:
    """Enable Paystack for the shop company, leaving the payout accounts empty."""
    values = {
        "company": SHOP_COMPANY,
        "test_mode": 1,
        "secret_key": SHOP_SECRET_KEY,
        "public_key": SHOP_PUBLIC_KEY,
        "suspense_account": accounts["suspense"],
        "settlement_bank_account": None,
        "paystack_fee_account": None,
        "mode_of_payment": "Paystack",
        "currency": shop_currency(),
        "checkout_mode": "Inline",
        "payment_link_validity_hours": 0,
        "allowed_webhook_ips": None,
        "enabled": 1,
    }

    if frappe.db.exists("Paystack Gateway Setting", SHOP_GATEWAY):
        gateway = frappe.get_doc("Paystack Gateway Setting", SHOP_GATEWAY)
    else:
        gateway = frappe.new_doc("Paystack Gateway Setting")
        gateway.gateway = SHOP_GATEWAY

    gateway.update(values)
    gateway.flags.ignore_permissions = True
    gateway.flags.ignore_links = True
    gateway.save()
    return gateway.name


def ensure_shop_price_list() -> str:
    """Create the selling price list the shop prices its item from."""
    if frappe.db.exists("Price List", SHOP_PRICE_LIST):
        return SHOP_PRICE_LIST

    price_list = frappe.get_doc(
        {
            "doctype": "Price List",
            "price_list_name": SHOP_PRICE_LIST,
            "currency": shop_currency(),
            "selling": 1,
            "enabled": 1,
        }
    )
    price_list.flags.ignore_permissions = True
    price_list.insert()
    return price_list.name


def ensure_shop_item() -> str:
    """Create a priced, non-stock item the shop can always sell."""
    if not frappe.db.exists("Item", SHOP_ITEM):
        item = frappe.get_doc(
            {
                "doctype": "Item",
                "item_code": SHOP_ITEM,
                "item_name": SHOP_ITEM,
                "item_group": default_item_group(),
                "stock_uom": "Nos",
                "is_stock_item": 0,
                "is_sales_item": 1,
            }
        )
        item.flags.ignore_permissions = True
        item.flags.ignore_mandatory = True
        item.insert()

    if not frappe.db.exists("Item Price", {"item_code": SHOP_ITEM, "price_list": SHOP_PRICE_LIST}):
        price = frappe.get_doc(
            {
                "doctype": "Item Price",
                "item_code": SHOP_ITEM,
                "price_list": SHOP_PRICE_LIST,
                "price_list_rate": SHOP_ITEM_RATE,
            }
        )
        price.flags.ignore_permissions = True
        price.insert()

    return SHOP_ITEM


def ensure_website_item() -> str:
    """Publish the item to the shop, returning the route the spec browses to."""
    name = frappe.db.get_value("Website Item", {"item_code": SHOP_ITEM}, "name")
    if name:
        web_item = frappe.get_doc("Website Item", name)
    else:
        web_item = frappe.get_doc(
            {
                "doctype": "Website Item",
                "item_code": SHOP_ITEM,
                "web_item_name": SHOP_ITEM,
                "item_group": default_item_group(),
            }
        )

    web_item.published = 1
    web_item.flags.ignore_permissions = True
    web_item.flags.ignore_mandatory = True
    web_item.save()
    return web_item.route


def ensure_rival_payment() -> tuple:
    """Give the second shopper a captured payment, returning the invoice and its log."""
    existing = frappe.db.get_value(
        SHOP_PAYMENT_LOG,
        {"company": SHOP_COMPANY, "payment_reference": RIVAL_REFERENCE},
        ["name", "linked_docname"],
    )
    if existing:
        return existing[1], existing[0]

    invoice = ensure_shop_invoice(RIVAL, RIVAL_AMOUNT)
    log = insert_captured_log(invoice, RIVAL_AMOUNT, "Completed", RIVAL_REFERENCE)

    return invoice, log


def ensure_shop_invoice(customer: str, amount: float) -> str:
    """Submit a Sales Invoice the shop's payments can be raised against."""
    invoice = frappe.get_doc(
        {
            "doctype": "Sales Invoice",
            "company": SHOP_COMPANY,
            "customer": customer,
            "currency": shop_currency(),
            "conversion_rate": 1,
            "selling_price_list": SHOP_PRICE_LIST,
            "debit_to": shop_receivable_account(),
            "due_date": nowdate(),
            "items": [
                {
                    "item_code": SHOP_ITEM,
                    "qty": 1,
                    "rate": flt(amount),
                    "shop_income_account": shop_income_account(),
                    "cost_center": frappe.db.get_value("Company", SHOP_COMPANY, "cost_center"),
                }
            ],
        }
    )
    invoice.flags.ignore_permissions = True
    invoice.flags.ignore_mandatory = True
    invoice.insert()
    invoice.submit()
    return invoice.name


def ensure_webshop_settings(gateway_account: str) -> None:
    """Point the shop at the fixture company, price list and gateway account."""
    settings = frappe.get_single("Webshop Settings")
    settings.enabled = 1
    settings.company = SHOP_COMPANY
    settings.price_list = SHOP_PRICE_LIST
    settings.default_customer_group = default_customer_group()
    settings.enable_checkout = 1
    settings.payment_gateway_account = gateway_account
    settings.payment_success_url = "Orders"
    settings.save_quotations_as_draft = 0
    # The fixture item carries no stock.
    settings.allow_items_not_in_stock = 1
    settings.show_price = 1
    settings.show_stock_availability = 0
    settings.flags.ignore_permissions = True
    settings.flags.ignore_mandatory = True
    settings.save()


def remove_shop_settlements() -> None:
    """Delete the payouts the settlement spec recorded, and their journal entries."""
    for name in frappe.get_all(SHOP_SETTLEMENT, filters={"company": SHOP_COMPANY}, pluck="name"):
        entry = frappe.db.get_value(SHOP_SETTLEMENT, name, "journal_entry")
        frappe.db.set_value(SHOP_SETTLEMENT, name, "journal_entry", None)
        delete_doc(SHOP_SETTLEMENT, name)
        delete_doc("Journal Entry", entry)


def remove_shop_transactions() -> None:
    """Unwind the shop's sales, newest leg first."""
    for entry in shop_documents("Payment Entry"):
        delete_doc("Payment Entry", entry)

    for log in shop_payment_log_names():
        frappe.db.set_value(
            SHOP_PAYMENT_LOG,
            log,
            {"status": "Pending", "payment_entry": None, "settlement": None},
        )
        delete_doc(SHOP_PAYMENT_LOG, log)

    for request in shop_documents("Payment Request"):
        delete_doc("Payment Request", request)

    for doctype in ("Sales Invoice", "Sales Order", "Quotation"):
        for name in shop_documents(doctype):
            delete_doc(doctype, name)


def shop_documents(doctype: str) -> list:
    """Return the shop company's documents of a doctype, newest first."""
    return frappe.get_all(doctype, filters={"company": SHOP_COMPANY}, pluck="name", order_by="creation desc")


def shop_payment_log_names() -> list:
    """Return every payment log raised for the shop company."""
    return frappe.get_all(SHOP_PAYMENT_LOG, filters={"company": SHOP_COMPANY}, pluck="name")


def remove_shop_website_item() -> None:
    """Unpublish and delete the shop's website item."""
    name = frappe.db.get_value("Website Item", {"item_code": SHOP_ITEM}, "name")
    if not name:
        return

    frappe.db.set_value("Website Item", name, "published", 0)
    delete_doc("Website Item", name)


def remove_shop_item_prices() -> None:
    """Delete the fixture item's prices."""
    for price in frappe.get_all("Item Price", filters={"item_code": SHOP_ITEM}, pluck="name"):
        delete_doc("Item Price", price)


def remove_shop_gateway_account() -> None:
    """Delete the Payment Gateway Account enabling the fixture gateway raised."""
    name = frappe.db.get_value(
        "Payment Gateway Account",
        {"payment_gateway": "Paystack", "company": SHOP_COMPANY},
        "name",
    )
    delete_doc("Payment Gateway Account", name)


def remove_shop_authorizations() -> None:
    """Delete the cards a charge webhook stored for the shop."""
    for name in frappe.get_all(
        "Paystack Customer Authorization",
        filters={"company": SHOP_COMPANY},
        pluck="name",
    ):
        delete_doc("Paystack Customer Authorization", name)


def remove_shop_ledger_entries() -> None:
    """Drop the ledger rows the cancelled fixture vouchers left behind."""
    for doctype in ("GL Entry", "Payment Ledger Entry"):
        frappe.db.delete(doctype, {"company": SHOP_COMPANY})


def remove_shop_accounts() -> None:
    """Delete the three accounts the payout was booked across."""
    abbr = frappe.db.get_value("Company", SHOP_COMPANY, "abbr")
    for account_name in (SHOP_SUSPENSE_ACCOUNT, SHOP_BANK_ACCOUNT, SHOP_FEE_ACCOUNT):
        delete_doc("Account", f"{account_name} - {abbr}")


def shop_receivable_account() -> str:
    """Return the account the shop's customers are billed against."""
    return frappe.db.get_value(
        "Account",
        {
            "company": SHOP_COMPANY,
            "account_type": "Receivable",
            "is_group": 0,
            "account_currency": shop_currency(),
        },
        "name",
    )


def shop_income_account() -> str:
    """Return the account the shop's sales are booked to."""
    return frappe.db.get_value(
        "Account", {"company": SHOP_COMPANY, "root_type": "Income", "is_group": 0}, "name"
    )
