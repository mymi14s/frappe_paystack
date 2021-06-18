// Copyright (c) 2021, Anthony Emmanuel (Ghorz.com) and contributors
// For license information, please see license.txt

frappe.ui.form.on('Paystack Payment Request', {
	refresh: function(frm) {
    frm.disable_save();
	}
});
