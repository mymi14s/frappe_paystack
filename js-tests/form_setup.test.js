// The Paystack button group: when it is offered, and the link flows behind it.

import { beforeEach, describe, expect, it, vi } from "vitest";
import {
	flush,
	install_clipboard,
	install_web_globals,
	recorded,
	replies,
} from "./web_stubs.js";

install_web_globals();
await import("../frappe_paystack/public/js/paystack_actions.bundle.js");
const actions = globalThis.frappe_paystack.actions;

const ENABLED = "frappe_paystack.api.is_enabled_for_company";
const CREATE_LINK = "frappe_paystack.api.create_payment_link";
const CUSTOMER_EMAIL = "frappe_paystack.utils.get_customer_email";
const SEND_EMAIL = "frappe.core.doctype.communication.email.make";

const CHECKOUT_URL = "https://site.test/paystack-checkout/PSLOG-9";

function make_frm(doc = {}) {
	const buttons = {};
	return {
		buttons,
		doc: {
			doctype: "Sales Invoice",
			name: "ACC-SINV-0001",
			company: "_Test Company",
			customer: "Test Customer",
			currency: "NGN",
			docstatus: 1,
			outstanding_amount: 5000,
			...doc,
		},
		add_custom_button: vi.fn((label, handler) => {
			buttons[label] = handler;
		}),
	};
}

/** Wire the button group up, returning the form it was added to. */
async function setup(doc = {}, config = {}) {
	const frm = make_frm(doc);
	actions.setupForm(frm, {
		getOutstanding: (form) => form.doc.outstanding_amount,
		isPayable: () => true,
		...config,
	});
	await flush();
	return frm;
}

/** Return the frappe.call options recorded for a method, in either call style. */
function call_for(method) {
	return globalThis.frappe.call.mock.calls
		.map(([first, args]) =>
			typeof first === "string" ? { method: first, args: args } : first
		)
		.find((options) => options && options.method === method);
}

describe("when the buttons are offered", () => {
	beforeEach(() => {
		install_web_globals();
		replies[ENABLED] = true;
	});

	it("offers the whole group on a payable submitted document", async () => {
		const frm = await setup();

		expect(Object.keys(frm.buttons)).toEqual([
			"Pay now",
			"Email payment link",
			"Partial payment",
			"Charge saved card",
		]);
	});

	it("offers nothing on a draft", async () => {
		const frm = await setup({ docstatus: 0 });

		expect(frm.add_custom_button).not.toHaveBeenCalled();
	});

	it("offers nothing on a cancelled document", async () => {
		const frm = await setup({ docstatus: 2 });

		expect(frm.add_custom_button).not.toHaveBeenCalled();
	});

	it("offers nothing without a company, which names the gateway", async () => {
		const frm = await setup({ company: "" });

		expect(frm.add_custom_button).not.toHaveBeenCalled();
		expect(globalThis.frappe.call).not.toHaveBeenCalled();
	});

	it("offers nothing on a document the doctype calls unpayable", async () => {
		const frm = await setup({}, { isPayable: () => false });

		expect(frm.add_custom_button).not.toHaveBeenCalled();
	});

	it("offers nothing when there is nothing left to collect", async () => {
		const frm = await setup({ outstanding_amount: 0 });

		expect(frm.add_custom_button).not.toHaveBeenCalled();
	});

	it("offers nothing when Paystack is off for the company", async () => {
		replies[ENABLED] = false;
		const frm = await setup();

		expect(frm.add_custom_button).not.toHaveBeenCalled();
	});
});

describe("raising a payment link", () => {
	beforeEach(() => {
		install_web_globals();
		replies[ENABLED] = true;
	});

	it("bills the whole outstanding amount", async () => {
		replies[CREATE_LINK] = CHECKOUT_URL;
		const frm = await setup();

		frm.buttons["Pay now"]();
		await flush();

		expect(call_for(CREATE_LINK).args).toEqual({
			doctype: "Sales Invoice",
			docname: "ACC-SINV-0001",
			amount: 5000,
			currency: "NGN",
		});
	});

	it("falls back to naira when the document names no currency", async () => {
		replies[CREATE_LINK] = CHECKOUT_URL;
		const frm = await setup({ currency: "" });

		frm.buttons["Pay now"]();
		await flush();

		expect(call_for(CREATE_LINK).args.currency).toBe("NGN");
	});

	it("says so when no link came back, and opens no dialog", async () => {
		replies[CREATE_LINK] = null;
		const frm = await setup();

		frm.buttons["Pay now"]();
		await flush();

		expect(globalThis.frappe.msgprint).toHaveBeenCalled();
		expect(recorded.dialogs).toHaveLength(0);
	});

	it("opens the link in a new tab, without handing it the opener", async () => {
		replies[CREATE_LINK] = CHECKOUT_URL;
		window.open = vi.fn();
		const frm = await setup();

		frm.buttons["Pay now"]();
		await flush();
		recorded.dialogs.at(-1).submit();

		expect(window.open).toHaveBeenCalledWith(CHECKOUT_URL, "_blank", "noopener");
		expect(recorded.dialogs.at(-1).hidden).toBe(true);
	});
});

describe("copying a payment link", () => {
	beforeEach(() => {
		install_web_globals();
		replies[ENABLED] = true;
		replies[CREATE_LINK] = CHECKOUT_URL;
	});

	async function open_link_dialog() {
		const frm = await setup();
		frm.buttons["Pay now"]();
		await flush();
		return recorded.dialogs.at(-1);
	}

	it("writes the link to the clipboard when there is one", async () => {
		install_clipboard();
		const dialog = await open_link_dialog();

		dialog.options.secondary_action();
		await flush();

		expect(recorded.copied).toEqual([CHECKOUT_URL]);
		expect(globalThis.frappe.show_alert).toHaveBeenCalled();
	});

	it("shows the link when the browser has no clipboard", async () => {
		const dialog = await open_link_dialog();

		dialog.options.secondary_action();
		await flush();

		expect(globalThis.frappe.msgprint).toHaveBeenCalled();
		expect(globalThis.frappe.msgprint.mock.calls.at(-1)[0]).toContain(CHECKOUT_URL);
	});

	it("shows the link when the clipboard refuses the write", async () => {
		install_clipboard(new Error("denied"));
		const dialog = await open_link_dialog();

		dialog.options.secondary_action();
		await flush();

		expect(globalThis.frappe.msgprint.mock.calls.at(-1)[0]).toContain(CHECKOUT_URL);
	});
});

describe("emailing a payment link", () => {
	beforeEach(() => {
		install_web_globals();
		replies[ENABLED] = true;
	});

	it("stops before raising a link when the customer has no address", async () => {
		replies[CUSTOMER_EMAIL] = null;
		const frm = await setup();

		frm.buttons["Email payment link"]();
		await flush();

		expect(globalThis.frappe.msgprint).toHaveBeenCalled();
		expect(call_for(CREATE_LINK)).toBeUndefined();
	});

	it("addresses the draft to the customer's own address", async () => {
		replies[CUSTOMER_EMAIL] = "buyer@example.com";
		replies[CREATE_LINK] = CHECKOUT_URL;
		const frm = await setup();

		frm.buttons["Email payment link"]();
		await flush();

		const fields = recorded.dialogs.at(-1).options.fields;
		expect(fields[0].default).toBe("buyer@example.com");
		expect(fields[1].default).toContain("ACC-SINV-0001");
		expect(fields[2].default).toContain(CHECKOUT_URL);
	});

	it("escapes the document name it puts in the body", async () => {
		replies[CUSTOMER_EMAIL] = "buyer@example.com";
		replies[CREATE_LINK] = CHECKOUT_URL;
		const frm = await setup({ name: "<img src=x onerror=alert(1)>" });

		frm.buttons["Email payment link"]();
		await flush();

		const body = recorded.dialogs.at(-1).options.fields[2].default;
		expect(body).not.toContain("<img");
		expect(body).toContain("&lt;img");
	});

	it("sends the draft against the document it bills", async () => {
		replies[CUSTOMER_EMAIL] = "buyer@example.com";
		replies[CREATE_LINK] = CHECKOUT_URL;
		const frm = await setup();

		frm.buttons["Email payment link"]();
		await flush();
		const dialog = recorded.dialogs.at(-1);
		dialog.submit();
		await flush();

		expect(call_for(SEND_EMAIL).args).toMatchObject({
			recipients: "buyer@example.com",
			doctype: "Sales Invoice",
			name: "ACC-SINV-0001",
			send_email: 1,
		});
		expect(dialog.hidden).toBe(true);
		expect(globalThis.frappe.show_alert).toHaveBeenCalled();
	});
});

describe("partial payment", () => {
	beforeEach(() => {
		install_web_globals();
		replies[ENABLED] = true;
		replies[CREATE_LINK] = CHECKOUT_URL;
	});

	async function open_partial_dialog() {
		const frm = await setup();
		frm.buttons["Partial payment"]();
		await flush();
		return recorded.dialogs.at(-1);
	}

	it("offers the whole outstanding amount as the default", async () => {
		const dialog = await open_partial_dialog();

		expect(dialog.options.fields[0].default).toBe(5000);
		expect(dialog.options.fields[1].default).toBe("NGN");
	});

	it("raises a link for the amount that was entered", async () => {
		const dialog = await open_partial_dialog();

		dialog.submit({ amount: 1200, mode: "Open link" });
		await flush();

		expect(call_for(CREATE_LINK).args.amount).toBe(1200);
	});

	it("emails the link when that is the mode chosen", async () => {
		replies[CUSTOMER_EMAIL] = "buyer@example.com";
		const dialog = await open_partial_dialog();

		dialog.submit({ amount: 1200, mode: "Email link" });
		await flush();

		expect(call_for(CUSTOMER_EMAIL)).toBeDefined();
	});

	it("refuses more than the document owes", async () => {
		const dialog = await open_partial_dialog();

		dialog.submit({ amount: 5001, mode: "Open link" });
		await flush();

		expect(globalThis.frappe.msgprint).toHaveBeenCalled();
		expect(call_for(CREATE_LINK)).toBeUndefined();
		expect(dialog.hidden).toBe(false);
	});

	it("refuses a negative amount", async () => {
		const dialog = await open_partial_dialog();

		dialog.submit({ amount: -100, mode: "Open link" });
		await flush();

		expect(call_for(CREATE_LINK)).toBeUndefined();
	});
});
