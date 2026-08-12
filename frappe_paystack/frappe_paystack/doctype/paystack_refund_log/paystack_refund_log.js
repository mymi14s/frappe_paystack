frappe.ui.form.on("Paystack Refund Log", {
	refresh(frm) {
		frm.trigger("decorate");
		frm.trigger("action_buttons");
	},

	status(frm) {
		frm.trigger("decorate");
	},

	decorate(frm) {
		const s = frm.doc.status || "Pending";
		const color = {
			Pending: "orange",
			Processed: "blue",
			Completed: "green",
			Failed: "red",
		}[s] || "gray";
		frm.dashboard.clear_headline();
		frm.dashboard.set_headline_alert(
			__('Status: <strong style="text-transform:uppercase">{0}</strong>', [s]),
			color
		);
	},

	action_buttons(frm) {
		if (frm.doc.status !== "Completed") {
			return;
		}

		if (frm.doc.reversal_payment_entry) {
			frm.add_custom_button(__("View Reversal Entry"), () => {
				frappe.set_route("Form", "Payment Entry", frm.doc.reversal_payment_entry);
			}, __("Actions"));
		}

		frm.add_custom_button(__("Send Receipt"), () => {
			frappe.call({
				method: "frappe_paystack.frappe_paystack.doctype.paystack_refund_log.paystack_refund_log.send_refund_receipt",
				args: { refund_log_name: frm.doc.name },
			}).then((r) => {
				if (r.message) {
					frappe.show_alert({
						message: __("Receipt email sent"),
						indicator: "green",
					});
				}
			});
		}, __("Actions"));
	},
});