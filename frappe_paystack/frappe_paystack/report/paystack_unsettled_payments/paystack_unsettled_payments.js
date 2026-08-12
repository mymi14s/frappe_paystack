// Copyright (c) 2026, Anthony Emmanuel and contributors
// For license information, please see license.txt

frappe.query_reports["Paystack Unsettled Payments"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			reqd: 1,
			default: frappe.defaults.get_user_default("Company"),
		},
		{
			fieldname: "to_date",
			label: __("Captured On or Before"),
			fieldtype: "Date",
			reqd: 0,
		},
	],
	// Colours the wait: red from 7 days, orange from 3, green below that.
	formatter(value, row, column, data, default_formatter) {
		const formatted = default_formatter(value, row, column, data);
		if (column.fieldname !== "days_waiting") {
			return formatted;
		}

		const colour = value >= 7 ? "red" : value >= 3 ? "orange" : "green";
		return `<span style="color: var(--${colour}-500)">${formatted}</span>`;
	},
};
