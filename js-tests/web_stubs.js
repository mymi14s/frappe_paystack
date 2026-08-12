// Stubs for the non-POS browser bundles. Each is a plain <script> that exports nothing,
// so it is read off disk and evaluated against a stub that captures what it registers.

import fs from "node:fs";
import path from "node:path";
import { vi } from "vitest";

// Paths resolve from the package root, which readFileSync opens under jsdom.
const CHECKOUT_SOURCE = fs.readFileSync(
	path.resolve(process.cwd(), "frappe_paystack/public/js/paystack_checkout.js"),
	"utf8"
);

// Read off disk, so each test evaluates it afresh.
const CART_GUARD_SOURCE = fs.readFileSync(
	path.resolve(process.cwd(), "frappe_paystack/public/js/paystack_cart_guard.bundle.js"),
	"utf8"
);

// Desk report scripts are read off disk the same way.
const UNSETTLED_REPORT_SOURCE = fs.readFileSync(
	path.resolve(
		process.cwd(),
		"frappe_paystack/frappe_paystack/report/paystack_unsettled_payments/paystack_unsettled_payments.js"
	),
	"utf8"
);

const SETTLEMENTS_REPORT_SOURCE = fs.readFileSync(
	path.resolve(
		process.cwd(),
		"frappe_paystack/frappe_paystack/report/paystack_settlements_vs_ledger/paystack_settlements_vs_ledger.js"
	),
	"utf8"
);

const ACTIVITY_REPORT_SOURCE = fs.readFileSync(
	path.resolve(
		process.cwd(),
		"frappe_paystack/frappe_paystack/report/paystack_activity/paystack_activity.js"
	),
	"utf8"
);

// The three form scripts hooks.py registers as doctype_js, read off disk the same way.
const FORM_SCRIPT_SOURCES = {};
Object.entries({
	Dunning: "dunning.js",
	"Sales Invoice": "sales_invoice.js",
	"Sales Order": "sales_order.js",
}).forEach(([doctype, file]) => {
	FORM_SCRIPT_SOURCES[doctype] = fs.readFileSync(
		path.resolve(process.cwd(), `frappe_paystack/public/js/${file}`),
		"utf8"
	);
});

// The Payment Log form script, read off disk the same way.
const PAYMENT_LOG_FORM_SOURCE = fs.readFileSync(
	path.resolve(
		process.cwd(),
		"frappe_paystack/frappe_paystack/doctype/paystack_payment_log/paystack_payment_log.js"
	),
	"utf8"
);

// The Reconciliation Log form script, read off disk the same way.
const RECONCILIATION_LOG_FORM_SOURCE = fs.readFileSync(
	path.resolve(
		process.cwd(),
		"frappe_paystack/frappe_paystack/doctype/paystack_reconciliation_log/paystack_reconciliation_log.js"
	),
	"utf8"
);

export const recorded = {
	dialogs: [],
	paystack: null,
	redirect: "",
	copied: [],
	form_events: {},
	realtime: {},
};

// Rejections frappe.call answers with, keyed by whitelisted method. Checked
// before replies, so a method can be made to fail on its own.
export const rejections = {};

// Replies frappe.call hands back, keyed by whitelisted method.
export const replies = {};

function reply(method) {
	if (Object.prototype.hasOwnProperty.call(rejections, method)) {
		return Promise.reject(rejections[method]);
	}

	return Promise.resolve({
		message: Object.prototype.hasOwnProperty.call(replies, method)
			? replies[method]
			: null,
	});
}

export function install_web_globals() {
	recorded.dialogs = [];
	recorded.paystack = null;
	recorded.redirect = "";
	recorded.copied = [];
	recorded.form_events = {};
	recorded.realtime = {};
	Object.keys(replies).forEach((key) => delete replies[key]);
	Object.keys(rejections).forEach((key) => delete rejections[key]);
	delete navigator.clipboard;

	globalThis.flt = (value) => {
		const number = parseFloat(value);
		return Number.isNaN(number) ? 0 : number;
	};

	globalThis.__ = (text, args) =>
		(args || []).reduce((out, value, index) => out.replace(`{${index}}`, value), text);

	globalThis.format_currency = (value, currency) => `${currency || ""} ${value}`.trim();

	globalThis.frappe = {
		// Both call styles: positional on the website, an options object in the desk.
		call: vi.fn((method, args) => {
			if (typeof method === "string") {
				return reply(method);
			}
			return reply(method.method);
		}),
		msgprint: vi.fn(),
		show_alert: vi.fn(),
		throw: vi.fn((message) => {
			throw new Error(message);
		}),
		ready: (handler) => {
			recorded.ready = handler;
		},
		provide: (path) => {
			path.split(".").reduce((node, key) => {
				node[key] = node[key] || {};
				return node[key];
			}, globalThis);
		},
		utils: {
			escape_html: (value) =>
				String(value)
					.replace(/&/g, "&amp;")
					.replace(/</g, "&lt;")
					.replace(/>/g, "&gt;")
					.replace(/"/g, "&quot;"),
		},
		ui: { Dialog: DialogStub },
	};

	Object.defineProperty(window, "location", {
		configurable: true,
		writable: true,
		value: {
			set href(url) {
				recorded.redirect = url;
			},
			get href() {
				return recorded.redirect;
			},
		},
	});
}

/** A frappe.ui.Dialog that records what it was built with. */
class DialogStub {
	constructor(options) {
		this.options = options;
		this.shown = false;
		this.hidden = false;
		this.fields_dict = {};
		(options.fields || []).forEach((field) => {
			this.fields_dict[field.fieldname] = {
				df: field,
				$wrapper: { html: vi.fn() },
			};
		});
		recorded.dialogs.push(this);
	}

	show() {
		this.shown = true;
	}

	hide() {
		this.hidden = true;
	}

	/** Run the primary action with the dialog's defaults, plus overrides. */
	submit(values = {}) {
		const defaults = {};
		(this.options.fields || []).forEach((field) => {
			defaults[field.fieldname] = field.default;
		});
		return this.options.primary_action({ ...defaults, ...values });
	}
}

/** Evaluate the checkout page bundle and return its Vue component options. */
export function load_checkout(payload) {
	document.body.innerHTML =
		payload === undefined
			? ""
			: `<div id="paystack-checkout"></div><script type="application/json" id="paystack-checkout-data">${
					typeof payload === "string" ? payload : JSON.stringify(payload)
			  }</script>`;

	let captured = null;
	globalThis.Vue = {
		createApp: (options) => {
			captured = options;
			return { mount: vi.fn() };
		},
	};
	globalThis.PaystackPop = function PaystackPop() {
		return {
			newTransaction: (options) => {
				recorded.paystack = options;
			},
		};
	};

	// eslint-disable-next-line no-new-func
	new Function(CHECKOUT_SOURCE)();
	return captured;
}

/** Give the page a clipboard API that records writes. */
export function install_clipboard(rejection) {
	navigator.clipboard = {
		writeText: (text) => {
			if (rejection) {
				return Promise.reject(rejection);
			}
			recorded.copied.push(text);
			return Promise.resolve();
		},
	};
}

/** Evaluate the cart guard bundle and return the frappe.ready handler it set. */
export function load_cart_guard(markup) {
	document.body.innerHTML = markup === undefined ? "" : markup;
	recorded.ready = null;

	// eslint-disable-next-line no-new-func
	new Function(CART_GUARD_SOURCE)();
	return recorded.ready;
}

/** Evaluate the unsettled payments report script and return what it registered. */
export function load_unsettled_report(company = "Test Company") {
	globalThis.frappe.query_reports = {};
	globalThis.frappe.defaults = { get_user_default: () => company };

	// eslint-disable-next-line no-new-func
	new Function(UNSETTLED_REPORT_SOURCE)();
	return globalThis.frappe.query_reports["Paystack Unsettled Payments"];
}

/** Evaluate the settlements report script and return what it registered. */
export function load_settlements_report(company = "Test Company") {
	globalThis.frappe.query_reports = {};
	globalThis.frappe.defaults = { get_user_default: () => company };

	// eslint-disable-next-line no-new-func
	new Function(SETTLEMENTS_REPORT_SOURCE)();
	return globalThis.frappe.query_reports["Paystack Settlements vs Ledger"];
}

// The date the activity report's stub clock reports as today.
export const TODAY = "2026-08-02";

/** Evaluate the activity report script and return what it registered. */
export function load_activity_report(company = "Test Company") {
	globalThis.frappe.query_reports = {};
	globalThis.frappe.defaults = { get_user_default: () => company };
	globalThis.frappe.datetime = {
		get_today: () => TODAY,
		add_days: (date, days) => `${date}${days >= 0 ? "+" : ""}${days}`,
	};

	// eslint-disable-next-line no-new-func
	new Function(ACTIVITY_REPORT_SOURCE)();
	return globalThis.frappe.query_reports["Paystack Activity"];
}

/** Evaluate a doctype's form script against a stubbed action bundle, returning its handlers. */
export function load_form_script(doctype) {
	recorded.form_events = {};
	globalThis.frappe.ui.form = {
		on: (name, events) => {
			recorded.form_events[name] = events;
		},
	};
	globalThis.frappe_paystack = { actions: { setupForm: vi.fn() } };

	// eslint-disable-next-line no-new-func
	new Function(FORM_SCRIPT_SOURCES[doctype])();
	return recorded.form_events;
}

/** Evaluate the Payment Log form script and return the handlers it registered. */
export function load_payment_log_form() {
	recorded.form_events = {};
	recorded.realtime = {};
	globalThis.frappe.ui.form = {
		on: (name, events) => {
			recorded.form_events[name] = events;
		},
	};
	globalThis.frappe.realtime = {
		on: (event, handler) => {
			recorded.realtime[event] = handler;
		},
	};

	// eslint-disable-next-line no-new-func
	new Function(PAYMENT_LOG_FORM_SOURCE)();
	return recorded.form_events["Paystack Payment Log"];
}

/** Build a Payment Log form the registered handlers can be run against. */
export function make_payment_log_frm(doc = {}) {
	const events = load_payment_log_form();
	const buttons = {};
	const frm = {
		buttons,
		doc: {
			doctype: "Paystack Payment Log",
			name: "PSLOG-1",
			status: "Processed",
			payment_entry: "",
			transaction_id: "",
			amount_paid: 1000,
			currency: "NGN",
			...doc,
		},
		add_custom_button: vi.fn((label, handler) => {
			buttons[label] = handler;
		}),
		dashboard: { clear_headline: vi.fn(), set_headline_alert: vi.fn() },
		call: vi.fn(() => Promise.resolve({ message: null })),
		reload_doc: vi.fn(),
		trigger: (name) => events[name](frm),
	};

	return { events, frm };
}

/** Evaluate the Reconciliation Log form script and return the handlers it registered. */
export function load_reconciliation_log_form() {
	recorded.form_events = {};
	globalThis.frappe.ui.form = {
		on: (name, events) => {
			recorded.form_events[name] = events;
		},
	};

	// eslint-disable-next-line no-new-func
	new Function(RECONCILIATION_LOG_FORM_SOURCE)();
	return recorded.form_events["Paystack Reconciliation Log"];
}

/** Build a Reconciliation Log form the registered handlers can be run against. */
export function make_reconciliation_log_frm(doc = {}) {
	const events = load_reconciliation_log_form();
	const buttons = {};
	const frm = {
		buttons,
		doc: {
			doctype: "Paystack Reconciliation Log",
			name: "PSLOG-1",
			payment_log: "PSLOG-1",
			status: "Mismatch",
			...doc,
		},
		add_custom_button: vi.fn((label, handler) => {
			buttons[label] = handler;
		}),
		reload_doc: vi.fn(),
	};

	return { events, frm };
}

/** Build a working instance from captured Vue options. */
export function make_instance(options) {
	const vm = { ...options.data() };

	Object.entries(options.computed).forEach(([name, getter]) => {
		Object.defineProperty(vm, name, {
			configurable: true,
			get: () => getter.call(vm),
		});
	});

	Object.entries(options.methods).forEach(([name, method]) => {
		vm[name] = method.bind(vm);
	});

	return vm;
}

export function checkout_payload(overrides = {}) {
	return {
		reference: "PSLOG-1",
		status: "Pending",
		customer: "Test Customer",
		email: "buyer@example.com",
		order_no: "ACC-SINV-0001",
		order_docstatus: 1,
		order_currency: "NGN",
		currency: "NGN",
		grand_total: 1000,
		payment_amount: 1000,
		exchange_rate: 1,
		reference_doctype: "Sales Invoice",
		reference_docname: "ACC-SINV-0001",
		public_key: "pk_test_1",
		checkout_mode: "Inline",
		expires_at: "",
		is_expired: false,
		is_payable: true,
		...overrides,
	};
}

/** Let every pending promise settle. */
export function flush() {
	return new Promise((resolve) => setTimeout(resolve, 0));
}
