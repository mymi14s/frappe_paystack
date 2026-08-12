// Copyright (c) 2026, Anthony Emmanuel and contributors
// For license information, please see license.txt

frappe.query_reports["Paystack Activity"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			reqd: 0,
			default: frappe.defaults.get_user_default("Company"),
		},
		{
			fieldname: "from_date",
			label: __("From"),
			fieldtype: "Date",
			reqd: 1,
			default: frappe.datetime.add_days(frappe.datetime.get_today(), -7),
		},
		{
			fieldname: "to_date",
			label: __("To"),
			fieldtype: "Date",
			reqd: 1,
			default: frappe.datetime.get_today(),
		},
		{
			fieldname: "severity",
			label: __("Severity"),
			fieldtype: "Select",
			options: ["", "Error", "Warning", "Info"],
		},
		{
			fieldname: "source",
			label: __("Source"),
			fieldtype: "Select",
			options: ["", "Payments", "Refunds", "Payouts", "API Calls"],
		},
	],
	// Colours the severity cell: red for Error, orange for Warning.
	formatter(value, row, column, data, default_formatter) {
		const formatted = default_formatter(value, row, column, data);
		if (column.fieldname !== "severity") {
			return formatted;
		}

		const colours = { Error: "red", Warning: "orange" };
		const colour = colours[value];
		if (!colour) {
			return formatted;
		}

		return `<span style="color: var(--${colour}-500)">${formatted}</span>`;
	},
};
