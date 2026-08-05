frappe.ui.form.on("Dunning", {
	refresh(frm) {
		frappe_paystack.actions.setupForm(frm, {
			getOutstanding: (form) => flt(form.doc.grand_total),
			isPayable: (form) => form.doc.status !== "Resolved",
		});
	},
});
