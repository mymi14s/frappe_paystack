frappe.ui.form.on("Sales Invoice", {
	refresh(frm) {
		frappe_paystack.actions.setupForm(frm, {
			getOutstanding: (form) => flt(form.doc.outstanding_amount),
			isPayable: (form) => !form.doc.is_return,
		});
	},
});
