// Copyright (c) 2026, Anthony Emmanuel and contributors
// For license information, please see license.txt

frappe.query_reports["Paystack Settlements vs Ledger"] = {
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
			fieldname: "from_date",
			label: __("Paid Out On or After"),
			fieldtype: "Date",
			reqd: 0,
		},
		{
			fieldname: "to_date",
			label: __("Paid Out On or Before"),
			fieldtype: "Date",
			reqd: 0,
		},
		{
			fieldname: "only_discrepancies",
			label: __("Only Payouts That Do Not Tie Out"),
			fieldtype: "Check",
			default: 0,
		},
	],
	formatter(value, row, column, data, default_formatter) {
		const formatted = default_formatter(value, row, column, data);
		// Colours the cells that say Paystack and the books disagree.
		const flagged = ["difference", "unlinked_amount", "discrepancy"];
		if (!flagged.includes(column.fieldname) || !value) {
			return formatted;
		}

		return `<span style="color: var(--red-500)">${formatted}</span>`;
	},
};
