// Desk global stubs for the POS bundle, installed before the bundle is imported.

import { vi } from "vitest";

// Child rows the frappe.model.set_value stub can write to, keyed by row name.
const rows = new Map();

export const recorded = {
	form_events: {},
	app_ready: null,
	realtime: {},
};

// A jQuery promise: .then() derives a new one, .fail() returns the same one.
function jq_promise(promise) {
	const api = {
		then: (on_success, on_error) => jq_promise(promise.then(on_success, on_error)),
		catch: (on_error) => jq_promise(promise.catch(on_error)),
		fail: (on_error) => {
			promise.catch(on_error);
			return api;
		},
		always: (handler) => {
			promise.finally(handler).catch(() => {});
			return api;
		},
		finally: (handler) => jq_promise(promise.finally(handler)),
	};
	return api;
}

export function install_globals() {
	rows.clear();
	recorded.form_events = {};
	recorded.realtime = {};

	globalThis.flt = (value) => {
		const number = parseFloat(value);
		return Number.isNaN(number) ? 0 : number;
	};

	globalThis.__ = (text, args) =>
		(args || []).reduce((out, value, index) => out.replace(`{${index}}`, value), text);

	globalThis.$ = (target) => ({
		on: (event, handler) => {
			if (target === globalThis.document && event === "app_ready") {
				recorded.app_ready = handler;
			}
		},
	});

	globalThis.frappe = {
		db: {
			get_value: vi.fn(() => Promise.resolve({ message: {} })),
		},
		model: {
			set_value: vi.fn((doctype, name, field, value) => {
				const row = rows.get(name);
				if (row) {
					row[field] = value;
				}
				return Promise.resolve();
			}),
		},
		dom: {
			freeze_count: 1,
			freeze: vi.fn(),
			unfreeze: vi.fn(),
		},
		call: vi.fn(() => jq_promise(Promise.resolve({ message: null }))),
		show_alert: vi.fn(),
		msgprint: vi.fn(),
		throw: vi.fn((message) => {
			throw new Error(message);
		}),
		realtime: {
			on: (event, handler) => {
				recorded.realtime[event] = handler;
			},
		},
		utils: {
			escape_html: (value) =>
				String(value)
					.replace(/&/g, "&amp;")
					.replace(/</g, "&lt;")
					.replace(/>/g, "&gt;")
					.replace(/"/g, "&quot;"),
		},
		ui: {
			form: {
				handlers: {},
				on: (doctype, events) => {
					recorded.form_events[doctype] = events;
				},
			},
		},
	};

	globalThis.format_currency = vi.fn((value) => String(value));
	globalThis.cur_pos = { payment: { events: { submit_invoice: vi.fn() } } };
	globalThis.cur_frm = null;
}

// Builds a resolved frappe.call reply that exposes .fail().
export function call_reply(message) {
	return jq_promise(Promise.resolve({ message }));
}

export function call_failure(error) {
	return jq_promise(Promise.reject(error || new Error("call failed")));
}

export function make_frm(doc = {}) {
	const document_ = {
		doctype: "POS Invoice",
		name: "POS-INV-0001",
		customer: "Test Customer",
		contact_email: "",
		contact_mobile: "",
		paid_amount: 0,
		payments: [],
		...doc,
	};

	document_.payments.forEach((row, index) => {
		row.doctype = row.doctype || "POS Invoice Payment";
		row.name = row.name || `row-${index}`;
		rows.set(row.name, row);
	});

	return {
		doc: document_,
		paystack_settled: false,
		set_value: vi.fn((field, value) => {
			document_[field] = value;
			return Promise.resolve();
		}),
		toggle_display: vi.fn(),
		dirty: vi.fn(),
		save: vi.fn(() => Promise.resolve()),
	};
}

export function paystack_row(amount, extra = {}) {
	return { mode_of_payment: "Paystack", amount, ...extra };
}

// The desk markup the bundle reaches for.
export function mount_dom() {
	document.body.innerHTML = `
		<div class="invoice-fields">
			<div data-fieldname="contact_email"><input type="text" /></div>
			<div data-fieldname="contact_mobile"><input type="text" /></div>
			<div data-fieldname="request_for_payment"><button type="button">Request for Payment</button></div>
		</div>
		<div class="submit-order-btn">Complete Order</div>
	`;
	return {
		field: document.querySelector('[data-fieldname="request_for_payment"]'),
		button: document.querySelector('[data-fieldname="request_for_payment"] button'),
		email: document.querySelector('[data-fieldname="contact_email"]'),
		mobile: document.querySelector('[data-fieldname="contact_mobile"]'),
		submit: document.querySelector(".submit-order-btn"),
	};
}
