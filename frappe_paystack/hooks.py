app_name = "frappe_paystack"
app_title = "Frappe Paystack"
app_publisher = "Anthony Emmanuel"
app_description = "Paystack integration for Frappe/ERPNext"
app_email = "hackacehuawei@gmail.com"
app_license = "mit"

required_apps = ["erpnext", "payments"]


after_install = "frappe_paystack.setup.after_install"


# Raises the ERPNext baseline for a test session.
before_tests = "frappe_paystack.tests.session_setup.before_tests"


app_include_css = "paystack_reconciliation.bundle.css"
app_include_js = ["paystack_actions.bundle.js", "paystack_pos.bundle.js"]
web_include_js = "paystack_cart_guard.bundle.js"


doctype_js = {
    "Sales Invoice": "public/js/sales_invoice.js",
    "Sales Order": "public/js/sales_order.js",
    "Dunning": "public/js/dunning.js",
}


# Authorises a portal user's receipt by ownership of the Payment Log.
has_website_permission = {
    "Paystack Payment Log": "frappe_paystack.utils.portal.has_payment_log_website_permission",
}


# Carries a document's Paystack payment state into the portal order pages.
update_website_context = ["frappe_paystack.utils.portal.apply_payment_state"]


doc_events = {
    "Sales Invoice": {
        "on_submit": "frappe_paystack.events.sales_invoice_on_submit",
    },
}


scheduler_events = {
    # Ten-minute re-drive of money that was received and never booked.
    "cron": {
        "*/10 * * * *": [
            "frappe_paystack.frappe_paystack.doctype.paystack_payment_log"
            ".paystack_payment_log.retry_stuck_settlements",
        ],
    },
    # Hourly re-drive of payouts whose gross is still in suspense.
    "hourly_long": [
        "frappe_paystack.utils.settlement.retry_unposted_settlements",
    ],
    # Reconciliation makes one synchronous Paystack call per payment.
    "daily_long": [
        "frappe_paystack.utils.scheduled_jobs.run_daily_reconciliation",
        # ERPNext's Subscription raises the invoice; this collects it.
        "frappe_paystack.utils.subscription.collect_subscription_payments",
    ],
}


website_route_rules = [{"from_route": "/paystack-checkout/<reference>", "to_route": "paystack-checkout"}]


# Read-only helpers a print format calls to show a document's checkout link.
jinja = {
    "methods": [
        "frappe_paystack.utils.printing.paystack_payment_link",
        "frappe_paystack.utils.printing.paystack_payment_qr",
    ]
}
