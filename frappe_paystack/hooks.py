app_name = "frappe_paystack"
app_title = "Frappe Paystack"
app_publisher = "Anthony Emmanuel"
app_description = "Paystack integration for Frappe/ERPNext"
app_email = "hackacehuawei@gmail.com"
app_license = "mit"

required_apps = ["erpnext"]


doctype_js = {
    "Sales Invoice": "public/js/sales_invoice.js",
    "Sales Order": "public/js/sales_order.js",
}


website_route_rules = [
    {"from_route": "/paystack-checkout/<reference>", "to_route": "paystack-checkout"}
]


fixtures = [
    {
        "doctype": "Mode of Payment",
        "filters": [["name", "=", "Paystack"]]
    }
]

