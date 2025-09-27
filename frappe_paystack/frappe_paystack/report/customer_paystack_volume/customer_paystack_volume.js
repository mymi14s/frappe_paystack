// Copyright (c) 2025, Anthony Emmanuel and contributors
// For license information, please see license.txt

frappe.query_reports["Customer Paystack Volume"] = {
	"filters": [
		{
            fieldname: "company",
            label: __("Company"),
            fieldtype: "Link",
			options: "Company",
            reqd: 1,

        },
		{
            fieldname: "customer",
            label: __("Customer"),
            fieldtype: "Link",
			options: "Customer",
            reqd: 0,

        },
	]
};
