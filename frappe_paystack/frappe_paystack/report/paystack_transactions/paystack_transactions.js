// Copyright (c) 2025, Anthony Emmanuel and contributors
// For license information, please see license.txt

frappe.query_reports["Paystack Transactions"] = {
    "filters": [
		{
            fieldname: "gateway",
            label: __("Gateway"),
            fieldtype: "Link",
            reqd: 1,
			options: "Paystack Gateway Setting",
			 get_query: function() {
				return {
					filters: {
						enabled: 1
					}
				};
			}
        },
        {
            fieldname: "customer",
            label: __("Customer ID"),
            fieldtype: "Int",
            reqd: 0
        },
        {
            fieldname: "status",
            label: __("Status"),
            fieldtype: "Select",
            options: ["", "success", "failed", "abandoned"],
            reqd: 0
        },
        {
            fieldname: "from_date",
            label: __("From Date"),
            fieldtype: "Datetime",
            reqd: 0
        },
        {
            fieldname: "to_date",
            label: __("To Date"),
            fieldtype: "Datetime",
            reqd: 0
        },
        {
            fieldname: "amount",
            label: __("Amount"),
            fieldtype: "Int",
            reqd: 0
        },
        {
            fieldname: "per_page",
            label: __("Per Page"),
            fieldtype: "Int",
            default: 50
        },
        {
            fieldname: "page",
            label: __("Page Number"),
            fieldtype: "Int",
            default: 1
        }
    ]
};

