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

FRAPPE_MAJOR_VERSION = int(frappe.__version__.split(".")[0])

# Paystack charges this, so the test company is billed in it.
COMPANY_CURRENCY = "NGN"
TEST_COMPANY_ABBR = "_TC"
TEST_COMPANY_COUNTRY = "Nigeria"

# Warehouse types ERPNext links the default warehouse tree to.
WAREHOUSE_TYPES = ("Transit",)

# The company frappe's test-record machinery raises _Test Company against.
SETUP_WIZARD_ARGS = {
    "currency": "USD",
    "full_name": "Test User",
    "company_name": "Wind Power LLC",
    "timezone": "America/New_York",
    "company_abbr": "WP",
    "industry": "Manufacturing",
    "country": "United States",
    "language": "english",
    "company_tagline": "Testing",
    "email": "test@erpnext.com",
    "password": "test",
    "chart_of_accounts": "Standard",
}

# Importing this module runs the BootStrapTestData version-16 seeds tests from.
ERPNEXT_BOOTSTRAP_MODULE = "erpnext.tests.utils"


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


def raise_test_company() -> None:
    """
    Create the test company in a currency Paystack charges.

    The ERPNext fixtures raise it in a currency Paystack refuses. Creating it
    first leaves their generators nothing to make, so the currency stands.
    """
    if frappe.db.exists("Company", TEST_COMPANY):
        return

    # A new company raises its warehouse tree, which links to these.
    for warehouse_type in WAREHOUSE_TYPES:
        if not frappe.db.exists("Warehouse Type", warehouse_type):
            frappe.get_doc({"doctype": "Warehouse Type", "name": warehouse_type}).insert(
                ignore_permissions=True
            )

    company = frappe.new_doc("Company")
    company.company_name = TEST_COMPANY
    company.abbr = TEST_COMPANY_ABBR
    company.default_currency = COMPANY_CURRENCY
    company.country = TEST_COMPANY_COUNTRY
    company.flags.ignore_permissions = True
    company.insert()


def before_tests() -> None:
    """Prepare the site for a frappe_paystack test session."""
    frappe.clear_cache()
    raise_test_company()
    raise_erpnext_baseline()
    ensure_unprivileged_role()
    _enable_all_roles_for_admin()
    set_defaults_for_tests()
    frappe.db.commit()
