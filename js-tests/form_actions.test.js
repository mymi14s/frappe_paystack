// The Paystack button group: the payment link QR code, and charging a saved card.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { flush, install_web_globals, recorded, replies } from "./web_stubs.js";

install_web_globals();
await import("../frappe_paystack/public/js/paystack_actions.bundle.js");
const actions = globalThis.frappe_paystack.actions;

const ENABLED = "frappe_paystack.api.is_enabled_for_company";
const CREATE_LINK = "frappe_paystack.api.create_payment_link";
const QR = "frappe_paystack.api.payment_link_qr";
const SAVED_CARDS = "frappe_paystack.api.saved_cards";
const CHARGE = "frappe_paystack.api.charge_saved_card";

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

async function setup(doc = {}) {
	replies[ENABLED] = true;
	const frm = make_frm(doc);
	actions.setupForm(frm, {
		getOutstanding: (form) => form.doc.outstanding_amount,
		isPayable: () => true,
	});
	await flush();
	return frm;
}

describe("payment link QR code", () => {
	beforeEach(() => {
		install_web_globals();
	});

	it("asks for the QR of the link it just raised", async () => {
		replies[CREATE_LINK] = CHECKOUT_URL;
		replies[QR] = "data:image/svg+xml;base64,AAA";
		const frm = await setup();

		frm.buttons["Pay now"]();
		await flush();

		const call = globalThis.frappe.call.mock.calls
			.map(([options]) => options)
			.find((options) => options && options.method === QR);
		expect(call.args).toEqual({ reference: "PSLOG-9" });
	});

	it("renders the code inside the link dialog", async () => {
		replies[CREATE_LINK] = CHECKOUT_URL;
		replies[QR] = "data:image/svg+xml;base64,AAA";
		const frm = await setup();

		frm.buttons["Pay now"]();
		await flush();

		const dialog = recorded.dialogs.at(-1);
		expect(dialog.shown).toBe(true);
		const [markup] = dialog.fields_dict.qr.$wrapper.html.mock.calls[0];
		expect(markup).toContain("data:image/svg+xml;base64,AAA");
	});

	it("renders nothing when no code came back", async () => {
		replies[CREATE_LINK] = CHECKOUT_URL;
		replies[QR] = null;
		const frm = await setup();

		frm.buttons["Pay now"]();
		await flush();

		expect(
			recorded.dialogs.at(-1).fields_dict.qr.$wrapper.html
		).not.toHaveBeenCalled();
	});
});

describe("charging a saved card", () => {
	beforeEach(() => {
		install_web_globals();
	});

	it("offers the button on a payable document", async () => {
		const frm = await setup();

		expect(Object.keys(frm.buttons)).toContain("Charge saved card");
	});

	it("says so when the customer has no saved card", async () => {
		replies[SAVED_CARDS] = [];
		const frm = await setup();

		frm.buttons["Charge saved card"]();
		await flush();

		expect(globalThis.frappe.msgprint).toHaveBeenCalled();
		expect(recorded.dialogs).toHaveLength(0);
	});

	it("lists the cards by their label", async () => {
		replies[SAVED_CARDS] = [
			{ name: "auth-1", card_label: "Visa •••• 4081" },
			{ name: "auth-2" },
		];
		const frm = await setup();

		frm.buttons["Charge saved card"]();
		await flush();

		const field = recorded.dialogs.at(-1).options.fields[0];
		expect(field.options).toEqual([
			{ label: "Visa •••• 4081", value: "auth-1" },
			{ label: "auth-2", value: "auth-2" },
		]);
		expect(field.default).toBe("auth-1");
	});

	it("charges the chosen card for the amount entered", async () => {
		replies[SAVED_CARDS] = [{ name: "auth-1", card_label: "Visa •••• 4081" }];
		replies[CHARGE] = "PSLOG-9";
		const frm = await setup();

		frm.buttons["Charge saved card"]();
		await flush();
		await recorded.dialogs.at(-1).submit({ amount: 2000 });
		await flush();

		const call = globalThis.frappe.call.mock.calls
			.map(([options]) => options)
			.find((options) => options && options.method === CHARGE);
		expect(call.args).toEqual({
			doctype: "Sales Invoice",
			docname: "ACC-SINV-0001",
			authorization: "auth-1",
			amount: 2000,
		});
		expect(globalThis.frappe.show_alert).toHaveBeenCalled();
	});

	it("refuses an amount above what is outstanding", async () => {
		replies[SAVED_CARDS] = [{ name: "auth-1", card_label: "Visa" }];
		const frm = await setup();

		frm.buttons["Charge saved card"]();
		await flush();
		recorded.dialogs.at(-1).submit({ amount: 9999 });
		await flush();

		expect(globalThis.frappe.msgprint).toHaveBeenCalled();
		expect(
			globalThis.frappe.call.mock.calls.some(
				([options]) => options && options.method === CHARGE
			)
		).toBe(false);
	});

	it("refuses a zero amount", async () => {
		replies[SAVED_CARDS] = [{ name: "auth-1", card_label: "Visa" }];
		const frm = await setup();

		frm.buttons["Charge saved card"]();
		await flush();
		recorded.dialogs.at(-1).submit({ amount: 0 });
		await flush();

		expect(recorded.dialogs.at(-1).hidden).toBe(false);
	});

	it("says nothing when the charge answered with no log", async () => {
		replies[SAVED_CARDS] = [{ name: "auth-1", card_label: "Visa" }];
		replies[CHARGE] = null;
		const frm = await setup();

		frm.buttons["Charge saved card"]();
		await flush();
		await recorded.dialogs.at(-1).submit({ amount: 1000 });
		await flush();

		expect(globalThis.frappe.show_alert).not.toHaveBeenCalled();
	});
});
