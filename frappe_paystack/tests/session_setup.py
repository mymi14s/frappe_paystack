"""Test-session setup for frappe_paystack.

Raises the ERPNext baseline the suite builds on: company, chart of accounts,
warehouse types, fiscal year and group roots.
"""

import importlib

import frappe
from frappe.desk.page.setup_wizard.setup_wizard import setup_complete
from frappe.utils.data import now_datetime

from frappe_paystack.tests.factories import TEST_COMPANY, ensure_unprivileged_role

from erpnext.setup.utils import _enable_all_roles_for_admin, set_defaults_for_tests

try:
    from frappe.tests.utils.generators import make_test_records
except ImportError:
    # version-15 raises the whole fixture set before before_tests is called.
    make_test_records = None

FRAPPE_MAJOR_VERSION = int(frappe.__version__.split(".")[0])

# Paystack charges this. The ERPNext fixtures raise the company in INR, which it refuses.
COMPANY_CURRENCY = "NGN"
COMPANY_COUNTRY = "Nigeria"

# The currency the ERPNext fixtures raise _Test Company and its ledgers in.
FIXTURE_CURRENCY = "INR"

# The company frappe's test-record machinery raises _Test Company against.
SETUP_WIZARD_ARGS = {
    "currency": "NGN",
    "full_name": "Test User",
    "company_name": "Wind Power LLC",
    "timezone": "Africa/Lagos",
    "company_abbr": "WP",
    "industry": "Manufacturing",
    "country": "Nigeria",
    "language": "english",
    "company_tagline": "Testing",
    "email": "test@erpnext.com",
    "password": "test",
    "chart_of_accounts": "Standard",
}

# Importing this module runs the BootStrapTestData version-16 seeds tests from.
ERPNEXT_BOOTSTRAP_MODULE = "erpnext.tests.utils"

# The app doctypes whose test-record dependencies reach the ERPNext fixtures.
BOOTSTRAP_DOCTYPES = (
    "Paystack Payment Log",
    "Paystack Settlement",
    "Paystack Refund Log",
    "Paystack Reconciliation Log",
    "Paystack Customer Authorization",
)


def complete_erpnext_setup() -> None:
    """Run the setup wizard when the site carries no company."""
    if frappe.db.a_row_exists("Company"):
        return

    current_year = now_datetime().year
    setup_complete(
        {
            **SETUP_WIZARD_ARGS,
            "fy_start_date": f"{current_year}-01-01",
            "fy_end_date": f"{current_year}-12-31",
        }
    )


def bootstrap_erpnext_test_data() -> None:
    """Raise the ERPNext test masters, companies included, on version-16."""
    importlib.import_module(ERPNEXT_BOOTSTRAP_MODULE)


def raise_erpnext_baseline() -> None:
    """Seed the masters the suite links to, the way the branch expects."""
    if FRAPPE_MAJOR_VERSION < 16:
        complete_erpnext_setup()
        return

    bootstrap_erpnext_test_data()


def raise_dependent_test_records() -> None:
    """
    Raise the ERPNext test records the suite links to, while they still fit.

    ERPNext writes them in the fixture currency against _Test Company's ledgers,
    so once those ledgers move the records cannot be raised at all and every
    version-16 class that reaches them dies in setUpClass.
    """
    if make_test_records is None:
        return

    for doctype in BOOTSTRAP_DOCTYPES:
        make_test_records(doctype, commit=True)


def bill_the_test_company_in_a_paystack_currency() -> None:
    """
    Put the test company and its ledgers in a currency Paystack charges.

    The fixtures pin account_currency, so the accounts do not follow the company
    on their own. Written straight to the table because the controllers refuse a
    currency change, and no entry has been posted yet.

    Only what the fixtures raised in FIXTURE_CURRENCY moves, leaving the ledgers
    they deliberately hold in a foreign currency to the multi-currency tests.
    Runs on every call: version-16 raises test records lazily, so ledgers in the
    fixture currency keep arriving after the session opened.
    """
    if frappe.db.exists("Company", TEST_COMPANY):
        frappe.db.set_value("Company", TEST_COMPANY, "default_currency", COMPANY_CURRENCY)
        frappe.db.set_value("Company", TEST_COMPANY, "country", COMPANY_COUNTRY)
        # version-16 books every GL entry in the company's reporting currency and
        # refuses to post without a rate from the default currency to it.
        if frappe.get_meta("Company").has_field("reporting_currency"):
            frappe.db.set_value("Company", TEST_COMPANY, "reporting_currency", COMPANY_CURRENCY)

    for account in frappe.get_all(
        "Account",
        filters={"company": TEST_COMPANY, "is_group": 0, "account_currency": FIXTURE_CURRENCY},
        pluck="name",
    ):
        frappe.db.set_value("Account", account, "account_currency", COMPANY_CURRENCY)

    # Selling documents read the rate from the price list, so it is moved too.
    for price_list in frappe.get_all("Price List", filters={"currency": FIXTURE_CURRENCY}, pluck="name"):
        frappe.db.set_value("Price List", price_list, "currency", COMPANY_CURRENCY)

    # A customer billed in another currency needs a receivable in that currency.
    for customer in frappe.get_all("Customer", filters={"default_currency": FIXTURE_CURRENCY}, pluck="name"):
        frappe.db.set_value("Customer", customer, "default_currency", COMPANY_CURRENCY)

    # The rate every fixture document converts at.
    frappe.db.set_single_value("Global Defaults", "default_currency", COMPANY_CURRENCY)
    frappe.db.set_single_value("Global Defaults", "country", COMPANY_COUNTRY)
    frappe.db.set_single_value("System Settings", "country", COMPANY_COUNTRY)

    # erpnext.get_company_currency() memoises into frappe.flags, which clear_cache()
    # does not touch, so the currency the fixtures shipped is dropped by hand.
    frappe.flags.company_currency = {}
    frappe.clear_cache()


def before_tests() -> None:
    """Prepare the site for a frappe_paystack test session."""
    frappe.clear_cache()
    raise_erpnext_baseline()
    raise_dependent_test_records()
    bill_the_test_company_in_a_paystack_currency()
    ensure_unprivileged_role()
    _enable_all_roles_for_admin()
    set_defaults_for_tests()
    frappe.db.commit()
