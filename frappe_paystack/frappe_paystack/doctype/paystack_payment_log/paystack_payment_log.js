const COMPLETE_PAYMENT =
	"frappe_paystack.frappe_paystack.doctype.paystack_payment_log.paystack_payment_log.complete_payment";

const COMPLETION_EVENT = "paystack_payment_completed";

// Log statuses holding captured money that no Payment Entry has booked yet.
const COMPLETABLE_STATUSES = ["Processed", "Needs Attention"];

frappe.ui.form.on("Paystack Payment Log", {
	onload(frm) {
		frappe.realtime.on(COMPLETION_EVENT, (data) => {
			report_completion(frm, data);
		});
	},

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
			"Needs Attention": "red",
			Completed: "green",
			"Partially Refunded": "yellow",
			Refunded: "purple",
			Failed: "red",
		}[s] || "gray";
		frm.dashboard.clear_headline();
		frm.dashboard.set_headline_alert(
			__('Status: <strong style="text-transform:uppercase">{0}</strong>', [s]),
			color
		);
	},

	action_buttons(frm) {
		if (settlement_outstanding(frm)) {
			frm.add_custom_button(__("Complete Payment"), () => {
				complete_payment(frm);
			}, __("Actions"));
		}

		if (!frm.doc.transaction_id) {
			return;
		}

		frm.add_custom_button(__("Open in Paystack"), () => {
			const url = `https://dashboard.paystack.com/#/transactions/${frm.doc.transaction_id}/analytics`;
			window.open(url, "_blank");
		}, __("Actions"));

		frm.add_custom_button(__("Verify Transaction"), () => {
			frm.call("validate_payment").then((res) => {
				if (res.message && res.message.message) {
					frappe.msgprint(res.message.message);
				}
			});
		}, __("Actions"));

		if (refundable_amount(frm) > 0) {
			frm.add_custom_button(__("Refund"), () => {
				show_refund_dialog(frm);
			}, __("Actions"));
		}
	},
});

function settlement_outstanding(frm) {
	return COMPLETABLE_STATUSES.includes(frm.doc.status) && !frm.doc.payment_entry;
}

function complete_payment(frm) {
	if (frm.paystack_completing) {
		return;
	}

	frm.paystack_completing = true;
	frappe.call({
		method: COMPLETE_PAYMENT,
		args: { payment_log_name: frm.doc.name },
	}).then((r) => {
		if (!r.message) {
			frm.paystack_completing = false;
			return;
		}
		frappe.show_alert({
			message: __("Verifying this payment with Paystack..."),
			indicator: "blue",
		});
	}).catch(() => {
		frm.paystack_completing = false;
	});
}

function report_completion(frm, data) {
	if (!data || data.log !== frm.doc.name) {
		return;
	}

	frm.paystack_completing = false;

	if (!data.booked) {
		frappe.msgprint({
			title: __("Payment not completed"),
			message: data.message,
			indicator: "red",
		});
		return;
	}

	frappe.show_alert({ message: data.message, indicator: "green" });
	frm.reload_doc();
}

function refundable_amount(frm) {
	const settled = ["Completed", "Partially Refunded"].includes(frm.doc.status);
	if (!settled) {
		return 0;
	}
	return flt(frm.doc.amount_paid) - flt(frm.doc.total_refunded);
}

function show_refund_dialog(frm) {
	const maxRefund = refundable_amount(frm);
	// The refundable balance is held in the currency Paystack settled in.
	const chargeCurrency = frm.doc.currency_paid || frm.doc.currency;

	const dialog = new frappe.ui.Dialog({
		title: __("Initiate Refund"),
		fields: [
			{
				fieldtype: "Currency",
				fieldname: "amount",
				label: __("Refund Amount"),
				reqd: 1,
				default: maxRefund,
				description: __("Maximum refundable: {0}", [
					format_currency(maxRefund, chargeCurrency),
				]),
			},
			{
				fieldtype: "Small Text",
				fieldname: "reason",
				label: __("Reason"),
			},
		],
		primary_action_label: __("Refund"),
		primary_action(values) {
			if (values.amount <= 0 || values.amount > maxRefund) {
				frappe.throw(
					__("Amount must be greater than 0 and at most {0}", [
						format_currency(maxRefund, chargeCurrency),
					])
				);
				return;
			}

			frappe.call({
				method: "frappe_paystack.api.initiate_refund_from_log",
				args: {
					payment_log_name: frm.doc.name,
					amount: values.amount,
					reason: values.reason,
				},
			}).then((r) => {
				if (r.message) {
					frappe.show_alert({
						message: __("Refund initiated: {0}", [r.message]),
						indicator: "blue",
					});
					dialog.hide();
					frm.refresh();
				}
			});
		},
	});
	dialog.show();
}