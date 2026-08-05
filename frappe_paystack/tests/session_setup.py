"""Test-session setup for frappe_paystack.

Raises the ERPNext baseline the suite builds on: company, chart of accounts,
warehouse types, fiscal year and group roots.
"""

import frappe
from frappe.desk.page.setup_wizard.setup_wizard import setup_complete
from frappe.utils.data import now_datetime

from erpnext.setup.utils import _enable_all_roles_for_admin, set_defaults_for_tests

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


def before_tests() -> None:
    """Prepare the site for a frappe_paystack test session."""
    frappe.clear_cache()
    complete_erpnext_setup()
    _enable_all_roles_for_admin()
    set_defaults_for_tests()
    frappe.db.commit()
