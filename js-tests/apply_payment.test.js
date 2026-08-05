// apply_payment writes amount_paid to the Paystack tender row.

import { beforeEach, describe, expect, it } from "vitest";
import { install_globals, make_frm, mount_dom, paystack_row } from "./stubs.js";

install_globals();
const pos = await import("../frappe_paystack/public/js/paystack_pos.bundle.js");

describe("apply_payment", () => {
	beforeEach(() => {
		install_globals();
		mount_dom();
	});

	it("books the full amount collected on a fully paid invoice", async () => {
		const row = paystack_row(0);
		const frm = make_frm({ payments: [row] });

		await pos.apply_payment(frm, { amount_paid: 5000, outstanding: 0, fully_paid: 1 });

		expect(row.amount).toBe(5000);
	});

	it("books only what Paystack collected on a part payment", async () => {
		const row = paystack_row(0);
		const frm = make_frm({ payments: [row] });

		await pos.apply_payment(frm, { amount_paid: 2000, outstanding: 3000, fully_paid: 0 });

		expect(row.amount).toBe(2000);
	});

	it("never books the outstanding balance to the Paystack row", async () => {
		const row = paystack_row(0);
		const frm = make_frm({ payments: [row] });

		await pos.apply_payment(frm, { amount_paid: 2000, outstanding: 3000, fully_paid: 0 });

		expect(row.amount).not.toBe(3000);
		expect(row.amount).not.toBe(5000);
		const [, , field, value] = frappe.model.set_value.mock.calls[0];
		expect(field).toBe("amount");
		expect(value).toBe(2000);
	});

	it("leaves a fully paid row non-zero so Complete Order accepts the sale", async () => {
		const row = paystack_row(0);
		const frm = make_frm({ payments: [row] });

		await pos.apply_payment(frm, { amount_paid: 5000, outstanding: 0, fully_paid: 1 });

		expect(row.amount).toBeGreaterThan(0);
	});

	it("writes to the Paystack row and no other tender", async () => {
		const cash = { mode_of_payment: "Cash", amount: 1000, name: "cash-row" };
		const row = paystack_row(0, { name: "paystack-row" });
		const frm = make_frm({ payments: [cash, row] });

		await pos.apply_payment(frm, { amount_paid: 4000, outstanding: 0, fully_paid: 1 });

		expect(row.amount).toBe(4000);
		expect(cash.amount).toBe(1000);
		expect(frappe.model.set_value).toHaveBeenCalledTimes(1);
		expect(frappe.model.set_value.mock.calls[0][1]).toBe("paystack-row");
	});

	it("marks the invoice settled only when the payment is complete", async () => {
		const frm = make_frm({ payments: [paystack_row(0)] });

		await pos.apply_payment(frm, { amount_paid: 2000, outstanding: 3000, fully_paid: 0 });
		expect(frm.paystack_settled).toBe(false);

		await pos.apply_payment(frm, { amount_paid: 5000, outstanding: 0, fully_paid: 1 });
		expect(frm.paystack_settled).toBe(true);
	});

	it("does nothing when the invoice has no Paystack tender", async () => {
		const frm = make_frm({ payments: [{ mode_of_payment: "Cash", amount: 1000 }] });

		await expect(
			pos.apply_payment(frm, { amount_paid: 5000, fully_paid: 1 })
		).resolves.not.toThrow();
		expect(frappe.model.set_value).not.toHaveBeenCalled();
	});

	it("coerces a string amount from the gateway", async () => {
		const row = paystack_row(0);
		const frm = make_frm({ payments: [row] });

		await pos.apply_payment(frm, { amount_paid: "1250.75", fully_paid: 0 });

		expect(row.amount).toBe(1250.75);
	});

	it("refreshes the button once the tender is written", async () => {
		const { field } = mount_dom();
		const frm = make_frm({ payments: [paystack_row(0)] });

		await pos.apply_payment(frm, { amount_paid: 5000, outstanding: 0, fully_paid: 1 });

		expect(field.style.display).toBe("none");
	});
});
