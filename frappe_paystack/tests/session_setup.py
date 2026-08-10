"""Test-session setup for frappe_paystack.

Raises the ERPNext baseline the suite builds on: company, chart of accounts,
warehouse types, fiscal year and group roots.
"""

import importlib

import frappe
from frappe.desk.page.setup_wizard.setup_wizard import setup_complete
from frappe.utils.data import now_datetime

from frappe_paystack.tests.factories import ensure_unprivileged_role

from erpnext.setup.utils import _enable_all_roles_for_admin, set_defaults_for_tests

FRAPPE_MAJOR_VERSION = int(frappe.__version__.split(".")[0])

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


def before_tests() -> None:
    """Prepare the site for a frappe_paystack test session."""
    frappe.clear_cache()
    raise_erpnext_baseline()
    ensure_unprivileged_role()
    _enable_all_roles_for_admin()
    set_defaults_for_tests()
    frappe.db.commit()
