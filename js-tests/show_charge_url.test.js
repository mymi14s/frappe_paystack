// show_charge_url puts the checkout URL in front of the cashier.

import { beforeEach, describe, expect, it } from "vitest";
import { install_globals, recorded } from "./stubs.js";

install_globals();
const pos = await import("../frappe_paystack/public/js/paystack_pos.bundle.js");

const on_charge_url = recorded.realtime["paystack_pos_charge_url"];

const CHECKOUT_URL = "https://checkout.paystack.com/abc123";

describe("show_charge_url", () => {
	beforeEach(() => install_globals());

	it("is wired to the realtime event the server publishes", () => {
		expect(typeof on_charge_url).toBe("function");
	});

	it("shows the checkout link to the cashier", () => {
		expect(pos.show_charge_url({ url: CHECKOUT_URL })).toBe(true);

		expect(frappe.msgprint).toHaveBeenCalledTimes(1);
		const dialog = frappe.msgprint.mock.calls[0][0];
		expect(dialog.title).toBe("Paystack Checkout Link");
		expect(dialog.message).toContain(`href="${CHECKOUT_URL}"`);
	});

	it("escapes what the gateway sent rather than injecting it", () => {
		pos.show_charge_url({ url: '"><img src=x onerror=alert(1)>' });

		const dialog = frappe.msgprint.mock.calls[0][0];
		expect(dialog.message).not.toContain("<img");
		expect(dialog.message).toContain("&lt;img");
	});

	it("says nothing when the response carried no link", () => {
		expect(pos.show_charge_url({})).toBe(false);
		expect(pos.show_charge_url(null)).toBe(false);
		expect(frappe.msgprint).not.toHaveBeenCalled();
	});
});
