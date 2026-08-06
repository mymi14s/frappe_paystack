// Copyright (c) 2024, Anthony C. Emmanuel and contributors
// For license information, please see license.txt

// Swaps the POS Request for Payment handler so a Paystack tender emails a link.

const EMAIL_PATTERN = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const PAYSTACK_MODE = "Paystack";
const PAID_AMOUNT_POLL_MS = 100;
const PAID_AMOUNT_TIMEOUT_MS = 5000;
// Core waits this long for a Payment Request before it gives up on the till.
const PAYMENT_REQUEST_TIMEOUT_MS = 60000;

function has_paystack_tender(frm) {
	return (frm.doc.payments || []).some(
		(row) => row.mode_of_payment === PAYSTACK_MODE && flt(row.amount) > 0
	);
}

function prefill_customer_email(frm) {
	if (frm.doc.contact_email || !frm.doc.customer) {
		return Promise.resolve();
	}
	return frappe.db
		.get_value("Customer", frm.doc.customer, "email_id")
		.then((response) => {
			const email = response?.message?.email_id;
			if (email && !frm.doc.contact_email) {
				frm.set_value("contact_email", email);
			}
		});
}

function send_payment_link(frm) {
	if (!EMAIL_PATTERN.test(frm.doc.contact_email || "")) {
		frappe.throw(__("Enter a valid email address first."));
	}

	frm.dirty();
	return frm.save().then(() => {
		frappe.dom.freeze(__("Sending payment link..."));
		return frappe
			.call({
				method: "frappe_paystack.utils.pos_payment.send_pos_payment_link",
				args: { pos_invoice: frm.doc.name, email: frm.doc.contact_email },
			})
			.then((response) => {
				frappe.dom.unfreeze();
				if (!response.message) {
					return;
				}
				frappe.show_alert({
					message: __("Payment link sent to {0}", [response.message.email]),
					indicator: "green",
				});
				watch_payment(response.message.log);
			})
			.fail(() => {
				frappe.dom.unfreeze();
				frappe.msgprint(__("Could not send the payment link."));
			});
	});
}

// POS renders the invoice fields outside the form layout, keyed by fieldname.
function toggle_pos_field(fieldname, visible) {
	const field = document.querySelector(`[data-fieldname="${fieldname}"]`);
	if (field) {
		field.style.display = visible ? "" : "none";
	}
}

function relabel_button(frm) {
	const field = document.querySelector('[data-fieldname="request_for_payment"]');
	const button = field?.querySelector("button");
	if (!button) {
		return;
	}

	if (frm.paystack_settled) {
		field.style.display = "none";
		return;
	}
	field.style.display = "";

	const paystack = has_paystack_tender(frm);
	button.textContent = paystack ? __("Send Payment Link") : __("Request for Payment");

	const ready = paystack
		? EMAIL_PATTERN.test(frm.doc.contact_email || "")
		: Boolean(frm.doc.contact_mobile);
	button.disabled = !ready;
	button.classList.toggle("btn-primary", ready);

	frm.toggle_display("contact_email", paystack);
	frm.toggle_display("contact_mobile", !paystack);
	toggle_pos_field("contact_email", paystack);
	toggle_pos_field("contact_mobile", !paystack);
}

frappe.ui.form.on("POS Invoice", {
	customer(frm) {
		prefill_customer_email(frm).then(() => relabel_button(frm));
	},

	contact_email(frm) {
		relabel_button(frm);
	},

	payments_on_form_rendered(frm) {
		prefill_customer_email(frm).then(() => relabel_button(frm));
	},

	// POS rebuilds the invoice fields each time it renders the payment screen.
	after_payment_render(frm) {
		prefill_customer_email(frm).then(() => relabel_button(frm));
	},

	refresh(frm) {
		install_request_handler();
		prefill_customer_email(frm).then(() => relabel_button(frm));
	},

	onload() {
		install_request_handler();
	},
});

// The cashier picks the tender after the form events above have run.
frappe.ui.form.on("Sales Invoice Payment", {
	amount(frm) {
		relabel_button(frm);
	},
});

// Replaces core's request_for_payment; a Paystack tender sends a link.
function paystack_request_for_payment(frm) {
	if (has_paystack_tender(frm)) {
		return send_payment_link(frm);
	}

	return core_request_for_payment(frm);
}

// A port of ERPNext's own request_for_payment; freezes the till until it answers.
function core_request_for_payment(frm) {
	if (!frm.doc.contact_mobile) {
		frappe.throw(__("Please enter mobile number first."));
	}

	frm.dirty();
	return frm.save().then(() => {
		frappe.dom.freeze(__("Waiting for payment..."));
		return frappe
			.call({ method: "create_payment_request", doc: frm.doc })
			.fail(() => {
				frappe.dom.unfreeze();
				frappe.msgprint(__("Payment request failed"));
			})
			.then(({ message }) => {
				const payment_request = message.name;
				setTimeout(() => {
					frappe.db
						.get_value("Payment Request", payment_request, ["status", "grand_total"])
						.then(({ message }) => {
							if (message.status != "Paid") {
								frappe.dom.unfreeze();
								frappe.msgprint({
									message: __(
										"Payment Request took too long to respond. Please try requesting for payment again."
									),
									title: __("Request Timeout"),
								});
							} else if (frappe.dom.freeze_count != 0) {
								frappe.dom.unfreeze();
								cur_frm.reload_doc();  // nosemgrep - ports ERPNext's own POS flow, which drives the global form
								cur_pos.payment.events.submit_invoice();
								frappe.show_alert({
									message: __("Payment of {0} received successfully.", [
										format_currency(message.grand_total, frm.doc.currency, 0),
									]),
									indicator: "green",
								});
							}
						});
				}, PAYMENT_REQUEST_TIMEOUT_MS);
			});
	});
}

function install_request_handler() {
	const handlers = frappe.ui.form.handlers["POS Invoice"];
	if (!handlers || !handlers.request_for_payment) {
		return false;
	}
	if (handlers.request_for_payment[0] === paystack_request_for_payment) {
		return true;
	}
	handlers.request_for_payment = [paystack_request_for_payment];
	return true;
}

$(document).on("app_ready", () => {
	let attempts = 0;
	const timer = setInterval(() => {
		if (install_request_handler() || ++attempts > 40) {
			clearInterval(timer);
		}
	}, 250);
});

// Realtime is the fast path; polling is the backstop.
let poll_timer = null;

function stop_watching() {
	if (poll_timer) {
		clearInterval(poll_timer);
		poll_timer = null;
	}
	frappe.dom.unfreeze();
}

function apply_payment(frm, data) {
	// The tender row carries what Paystack collected; any remainder stays unallocated.
	const row = (frm.doc.payments || []).find(
		(entry) => entry.mode_of_payment === PAYSTACK_MODE
	);
	if (!row) {
		return Promise.resolve();
	}

	return Promise.resolve(
		frappe.model.set_value(row.doctype, row.name, "amount", flt(data.amount_paid))
	).then(() => {
		frm.paystack_settled = Boolean(data.fully_paid);
		relabel_button(frm);
	});
}

function paid_amount_settled(frm, expected) {
	// Waits for the doc.paid_amount core recalculates from the tender rows.
	return new Promise((resolve) => {
		let waited = 0;
		const timer = setInterval(() => {
			waited += PAID_AMOUNT_POLL_MS;
			const paid = flt(frm?.doc?.paid_amount);
			if (paid >= expected || waited >= PAID_AMOUNT_TIMEOUT_MS) {
				clearInterval(timer);
				resolve(paid);
			}
		}, PAID_AMOUNT_POLL_MS);
	});
}

function complete_order(frm, expected) {
	// Drives POS's own Complete Order path.
	const button = document.querySelector(".submit-order-btn");
	if (!button) {
		return;
	}

	if (!frm) {
		button.click();
		return;
	}

	paid_amount_settled(frm, expected).then((paid) => {
		if (paid <= 0) {
			frappe.msgprint(__("The payment did not reach the tender. Complete the order manually."));
			return;
		}
		button.click();
	});
}

function announce(data) {
	stop_watching();

	const frm = window.cur_frm;
	const current = frm && frm.doc && frm.doc.name === data.pos_invoice ? frm : null;
	const applied = current ? apply_payment(current, data) : Promise.resolve();

	if (!data.fully_paid) {
		frappe.show_alert({
			message: __("Part payment received. Outstanding: {0}", [data.outstanding]),
			indicator: "orange",
		});
		return;
	}

	frappe.show_alert({ message: __("Payment received in full"), indicator: "green" });
	applied.then(() => complete_order(current, flt(data.amount_paid)));
}

function watch_payment(log) {
	stop_watching();
	frappe.dom.freeze(__("Waiting for payment..."));

	let elapsed = 0;
	poll_timer = setInterval(() => {
		elapsed += 5;
		if (elapsed > 600) {
			stop_watching();
			frappe.msgprint(__("Still waiting for payment. Check the Paystack Payment Log."));
			return;
		}
		frappe.call({
			method: "frappe_paystack.utils.pos_payment.pos_payment_status",
			args: { log },
		}).then((response) => {
			const data = response.message;
			if (data && flt(data.amount_paid) > 0) {
				announce(data);
			}
		});
	}, 5000);
}

// Shows the cashier the checkout URL a Phone tender's Payment Request got back.
function show_charge_url(data) {
	if (!data || !data.url) {
		return false;
	}

	const url = frappe.utils.escape_html(data.url);
	frappe.msgprint({
		title: __("Paystack Checkout Link"),
		message: `<p>${__("Ask the customer to open this link to pay:")}</p>
			<p><a href="${url}" target="_blank" rel="noopener">${url}</a></p>`,
		wide: true,
	});
	return true;
}

frappe.realtime.on("paystack_pos_charge_url", (data) => show_charge_url(data));

frappe.realtime.on("paystack_pos_paid", (data) => announce(data));

// Exported for the unit tests; esbuild drops the clause from the IIFE bundle.
export {
	has_paystack_tender,
	prefill_customer_email,
	send_payment_link,
	relabel_button,
	paystack_request_for_payment,
	install_request_handler,
	apply_payment,
	paid_amount_settled,
	complete_order,
	announce,
	watch_payment,
	stop_watching,
	show_charge_url,
};
