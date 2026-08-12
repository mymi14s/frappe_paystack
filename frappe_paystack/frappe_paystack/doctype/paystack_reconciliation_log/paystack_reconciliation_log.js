const MARK_MANUAL_OVERRIDE = "frappe_paystack.utils.reconciliation_api.mark_manual_override";

frappe.ui.form.on("Paystack Reconciliation Log", {
	refresh(frm) {
		if (frm.doc.status !== "Mismatch") {
			return;
		}

		frm.add_custom_button(__("Resolve Mismatch"), () => {
			show_override_dialog(frm);
		});
	}
});

function show_override_dialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Resolve Mismatch"),
		fields: [
			{
				fieldtype: "Small Text",
				fieldname: "notes",
				label: __("How this was resolved"),
				reqd: 1,
			},
		],
		primary_action_label: __("Mark Manual Override"),
		primary_action(values) {
			if (!values.notes) {
				frappe.throw(__("A note explaining the override is required."));
				return;
			}

			frappe.call({
				method: MARK_MANUAL_OVERRIDE,
				args: {
					payment_log_name: frm.doc.payment_log,
					notes: values.notes,
				},
			}).then((r) => {
				if (r.message) {
					frappe.show_alert({
						message: __("Reconciliation marked {0}.", [r.message.status]),
						indicator: "green",
					});
					dialog.hide();
					frm.reload_doc();
				}
			});
		},
	});
	dialog.show();
}
