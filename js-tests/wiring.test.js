// The bundle registers itself on import, so the registrations are captured once.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { install_globals, make_frm, mount_dom, paystack_row, recorded } from "./stubs.js";

install_globals();
const pos = await import("../frappe_paystack/public/js/paystack_pos.bundle.js");

const form_events = { ...recorded.form_events["POS Invoice"] };
const tender_events = { ...recorded.form_events["Sales Invoice Payment"] };
const app_ready = recorded.app_ready;
const on_paid = recorded.realtime["paystack_pos_paid"];

// The form handlers fire and forget their lookups; the wait is on the microtask queue.
async function flush() {
	for (let index = 0; index < 8; index++) {
		await Promise.resolve();
	}
}

describe("desk registration", () => {
	beforeEach(() => {
		install_globals();
		mount_dom();
	});

	afterEach(() => pos.stop_watching());

	it("hooks the POS Invoice form events it needs", () => {
		expect(Object.keys(form_events).sort()).toEqual([
			"after_payment_render",
			"contact_email",
			"customer",
			"onload",
			"payments_on_form_rendered",
			"refresh",
		]);
	});

	it("relabels when POS renders the payment screen", async () => {
		frappe.db.get_value.mockResolvedValue({ message: { email_id: "buyer@example.com" } });
		const frm = make_frm({ payments: [paystack_row(5000)] });

		form_events.after_payment_render(frm);
		await flush();

		expect(document.querySelector('[data-fieldname="request_for_payment"] button').textContent).toBe(
			"Send Payment Link"
		);
	});

	it("relabels when the cashier sets a tender", () => {
		const frm = make_frm({ payments: [paystack_row(5000)] });

		tender_events.amount(frm);

		expect(document.querySelector('[data-fieldname="request_for_payment"] button').textContent).toBe(
			"Send Payment Link"
		);
		expect(document.querySelector('[data-fieldname="contact_mobile"]').style.display).toBe("none");
	});

	it("prefills and relabels when the customer changes", async () => {
		frappe.db.get_value.mockResolvedValue({ message: { email_id: "buyer@example.com" } });
		const frm = make_frm({ payments: [paystack_row(5000)] });

		form_events.customer(frm);
		await flush();

		expect(frm.set_value).toHaveBeenCalledWith("contact_email", "buyer@example.com");
		expect(document.querySelector('[data-fieldname="request_for_payment"] button').textContent).toBe(
			"Send Payment Link"
		);
	});

	it("installs the request handler on refresh", async () => {
		frappe.ui.form.handlers["POS Invoice"] = { request_for_payment: [vi.fn()] };
		const frm = make_frm({ payments: [paystack_row(5000)] });

		form_events.refresh(frm);
		await flush();

		expect(frappe.ui.form.handlers["POS Invoice"].request_for_payment).toEqual([
			pos.paystack_request_for_payment,
		]);
	});

	it("retries the handler install until core's form script arrives", () => {
		vi.useFakeTimers();
		try {
			app_ready();
			vi.advanceTimersByTime(1000);
			expect(frappe.ui.form.handlers["POS Invoice"]).toBeUndefined();

			const core = vi.fn();
			frappe.ui.form.handlers["POS Invoice"] = { request_for_payment: [core] };
			vi.advanceTimersByTime(250);

			expect(frappe.ui.form.handlers["POS Invoice"].request_for_payment).toEqual([
				pos.paystack_request_for_payment,
			]);

			// The retry loop stops once installed.
			frappe.ui.form.handlers["POS Invoice"].request_for_payment = [core];
			vi.advanceTimersByTime(5000);
			expect(frappe.ui.form.handlers["POS Invoice"].request_for_payment).toEqual([core]);
		} finally {
			vi.useRealTimers();
		}
	});

	it("stops retrying after ten seconds", () => {
		vi.useFakeTimers();
		try {
			app_ready();
			vi.advanceTimersByTime(11000);

			const core = vi.fn();
			frappe.ui.form.handlers["POS Invoice"] = { request_for_payment: [core] };
			vi.advanceTimersByTime(5000);

			expect(frappe.ui.form.handlers["POS Invoice"].request_for_payment).toEqual([core]);
		} finally {
			vi.useRealTimers();
		}
	});

	it("listens for the realtime payment event", async () => {
		expect(typeof on_paid).toBe("function");

		on_paid({ pos_invoice: "POS-INV-0001", amount_paid: 5000, fully_paid: 1 });
		await Promise.resolve();

		expect(frappe.show_alert).toHaveBeenCalledWith({
			message: "Payment received in full",
			indicator: "green",
		});
	});
});
