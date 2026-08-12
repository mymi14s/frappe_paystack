"""Create the number cards and charts the Paystack workspace refers to."""

from frappe_paystack.setup import create_dashboard_widgets


def execute() -> None:
    create_dashboard_widgets()
