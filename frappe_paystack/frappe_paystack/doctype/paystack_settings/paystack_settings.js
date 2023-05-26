// Copyright (c) 2021, Anthony Emmanuel (Ghorz.com) and contributors
// For license information, please see license.txt

frappe.ui.form.on('Paystack Settings', {
	refresh: function(frm) {
		// filter cost center
		frm.set_query('cost_center', () => {
			return {
				filters: {
					is_group: 0,
					disabled: 0
				}
			}
		})
	}
});
