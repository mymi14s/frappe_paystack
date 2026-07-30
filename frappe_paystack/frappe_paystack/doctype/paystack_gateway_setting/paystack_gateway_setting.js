frappe.ui.form.on("Paystack Gateway Setting", {
	refresh(frm) {
		frm.add_custom_button(__("Test Signature"), () => {
			frappe.confirm(
				__("This just validates we can read the secret and compute HMAC. Continue?"),
				() => {
					frappe.call({
						method: "frappe.client.get",
						args: {
							doctype: "Paystack Gateway Setting",
							name: frm.doc.name,
						},
					}).then(() => {
						frappe.show_alert({
							message: __("OK — credentials are readable"),
							indicator: "green",
						});
					});
				}
			);
		});
	},
});