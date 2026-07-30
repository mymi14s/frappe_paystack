frappe.ui.form.on("Sales Invoice", {
	refresh(frm) {
		if (!frm.doc.company) {
			return;
		}

		const canPay = frm.doc.docstatus === 1 && flt(frm.doc.outstanding_amount || 0) > 0;

		frappe.call({
			method: "frappe_paystack.api.is_enabled_for_company",
			args: { company: frm.doc.company },
		}).then((r) => {
			const enabled = !!r.message;
			if (!enabled) {
				frm.dashboard.add_comment(__("Paystack disabled or not configured"), "red", true);
				return;
			}

			if (!canPay) {
				return;
			}

			frm.dashboard.add_comment(__("Paystack enabled"), "green", true);

			frm.add_custom_button(__("Pay Now"), () => {
				makePaystackLink(frm, {
					doctype: frm.doc.doctype,
					docname: frm.doc.name,
					amount: flt(frm.doc.outstanding_amount || 0),
					currency: frm.doc.currency || "NGN",
				});
			}, __("Paystack"));

			frm.add_custom_button(__("Send Payment Link"), () => {
				frappe.call("frappe_paystack.utils.get_customer_email", {
					customer: frm.doc.customer,
				}).then((res) => {
					if (!res.message) {
						frappe.throw("Please set customer Email ID in Customer Doctype");
					}
					makePaystackLink(frm, {
						doctype: frm.doc.doctype,
						docname: frm.doc.name,
						amount: flt(frm.doc.outstanding_amount || 0),
						currency: frm.doc.currency || "NGN",
					}, { send: true, email: res.message });
				});
			}, __("Paystack"));

			frm.add_custom_button(__("Partial Payment"), () => {
				const partialDialog = new frappe.ui.Dialog({
					title: __("Partial Payment"),
					fields: [
						{
							fieldtype: "Currency",
							fieldname: "amount",
							label: __( `Amount (${frm.doc.currency})`),
							options: frm.doc.currency,
							reqd: 1,
						},
						{
							fieldtype: "Select",
							fieldname: "mode",
							label: __("Payment Mode"),
							options: ["", "Pay Now", "Send Payment Link"],
							reqd: 1,
						},
					],
					primary_action_label: __("Create"),
					primary_action(values) {
						if (values.amount > 0 && values.amount <= frm.doc.outstanding_amount) {
							if (values.mode === "Pay Now") {
								makePaystackLink(frm, {
									doctype: frm.doc.doctype,
									docname: frm.doc.name,
									amount: flt(values.amount || 0),
									currency: frm.doc.currency || "NGN",
								});
								partialDialog.hide();
							} else {
								frappe.call("frappe_paystack.utils.get_customer_email", {
									customer: frm.doc.customer,
								}).then((res) => {
									if (!res.message) {
										frappe.throw("Please set customer Email ID in Customer Doctype");
									}
									makePaystackLink(frm, {
										doctype: frm.doc.doctype,
										docname: frm.doc.name,
										amount: flt(values.amount || 0),
										currency: frm.doc.currency || "NGN",
									}, { send: true, email: res.message });
									partialDialog.hide();
								});
							}
						} else {
							frappe.throw("Amount must be > 0 or <= outstanding_amount");
						}
					},
				});
				partialDialog.show();
			}, __("Paystack"));
		});
	},
});

function makePaystackLink(frm, { doctype, docname, amount, currency }, opts = {}) {
	frappe.call({
		method: "frappe_paystack.api.create_payment_link",
		args: { doctype, docname, amount, currency },
	}).then((res) => {
		const url = res?.message;
		if (!url) {
			frappe.msgprint(__("Could not generate Paystack link."));
			return;
		}
		showLinkDialog(frm, url, opts);
	});
}

function showLinkDialog(frm, url, opts) {
	if (opts.send) {
		promptSendEmail(frm, url, opts);
		return;
	}

	const d = new frappe.ui.Dialog({
		title: __("Pay via Paystack"),
		fields: [
			{
				fieldtype: "Data",
				fieldname: "link",
				label: __("Payment Link"),
				read_only: 1,
				default: url,
				bold: 1,
			},
		],
		primary_action_label: __("Open Link"),
		primary_action: () => {
			window.open(url, "_blank");
			d.hide();
		},
	});

	d.set_secondary_action_label(__("Copy Link"));
	d.set_secondary_action(() => {
		const val = d.get_value("link");
		if (navigator.clipboard?.writeText) {
			navigator.clipboard.writeText(val).then(() =>
				frappe.show_alert({ message: __("Copied!"), indicator: "green" })
			);
		} else {
			frappe.msgprint(__("Copy this link") + ":<br>" + val);
		}
	});

	d.show();
}

function promptSendEmail(frm, url, opts) {
	const emailDialog = new frappe.ui.Dialog({
		title: __("Send Payment Link"),
		fields: [
			{
				fieldtype: "Data",
				fieldname: "to",
				label: __("To (Email)"),
				reqd: 1,
				default: opts.email,
			},
			{
				fieldtype: "Data",
				fieldname: "subject",
				label: __("Subject"),
				default: __("Payment link for {0}", [frm.doc.name]),
			},
			{
				fieldtype: "Small Text",
				fieldname: "message",
				label: __("Message"),
				default:
					__("Hello,") +
					"<br>" +
					__("Please use the Paystack link below to complete payment for {0}.", [
						frm.doc.name,
					]) +
					"<br>" +
					url +
					__("<br><br>Thank you!"),
			},
		],
		primary_action_label: __("Send"),
		primary_action(values) {
			frappe.call({
				method: "frappe.core.doctype.communication.email.make",
				args: {
					recipients: values.to,
					subject: values.subject,
					content: values.message,
					doctype: frm.doc.doctype,
					name: frm.doc.name,
					send_email: 1,
				},
			}).then(() => {
				frappe.show_alert({ message: __("Email sent"), indicator: "green" });
				emailDialog.hide();
				frappe.msgprint(
					`Payment link has been sent to ${frm.doc.customer} via ${opts.email}`
				);
			});
		},
	});
	emailDialog.show();
}