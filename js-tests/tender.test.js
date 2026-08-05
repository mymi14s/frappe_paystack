import { beforeEach, describe, expect, it } from "vitest";
import { install_globals, make_frm, paystack_row } from "./stubs.js";

install_globals();
const pos = await import("../frappe_paystack/public/js/paystack_pos.bundle.js");

describe("has_paystack_tender", () => {
	beforeEach(() => install_globals());

	it("is true for a Paystack row with a positive amount", () => {
		const frm = make_frm({ payments: [paystack_row(1500)] });
		expect(pos.has_paystack_tender(frm)).toBe(true);
	});

	it("is false for a Paystack row of zero", () => {
		const frm = make_frm({ payments: [paystack_row(0)] });
		expect(pos.has_paystack_tender(frm)).toBe(false);
	});

	it("is false for a negative Paystack row", () => {
		const frm = make_frm({ payments: [paystack_row(-100)] });
		expect(pos.has_paystack_tender(frm)).toBe(false);
	});

	it("is false when only other modes of payment carry an amount", () => {
		const frm = make_frm({
			payments: [
				{ mode_of_payment: "Cash", amount: 2000 },
				{ mode_of_payment: "Bank Draft", amount: 500 },
			],
		});
		expect(pos.has_paystack_tender(frm)).toBe(false);
	});

	it("is true when a Paystack row sits alongside other tenders", () => {
		const frm = make_frm({
			payments: [{ mode_of_payment: "Cash", amount: 2000 }, paystack_row(500)],
		});
		expect(pos.has_paystack_tender(frm)).toBe(true);
	});

	it("is false with no payment rows at all", () => {
		expect(pos.has_paystack_tender(make_frm({ payments: [] }))).toBe(false);
		expect(pos.has_paystack_tender({ doc: {} })).toBe(false);
	});

	it("treats a string amount as a number", () => {
		const frm = make_frm({ payments: [paystack_row("250.50")] });
		expect(pos.has_paystack_tender(frm)).toBe(true);
	});
});

describe("prefill_customer_email", () => {
	beforeEach(() => install_globals());

	it("fills the contact email from the customer record", async () => {
		frappe.db.get_value.mockResolvedValue({ message: { email_id: "buyer@example.com" } });
		const frm = make_frm();

		await pos.prefill_customer_email(frm);

		expect(frappe.db.get_value).toHaveBeenCalledWith(
			"Customer",
			"Test Customer",
			"email_id"
		);
		expect(frm.set_value).toHaveBeenCalledWith("contact_email", "buyer@example.com");
	});

	it("does not look the customer up when an email is already on the invoice", async () => {
		const frm = make_frm({ contact_email: "typed@example.com" });

		await pos.prefill_customer_email(frm);

		expect(frappe.db.get_value).not.toHaveBeenCalled();
		expect(frm.set_value).not.toHaveBeenCalled();
	});

	it("does nothing without a customer", async () => {
		const frm = make_frm({ customer: "" });

		await pos.prefill_customer_email(frm);

		expect(frappe.db.get_value).not.toHaveBeenCalled();
	});

	it("keeps an address the cashier typed while the lookup was in flight", async () => {
		let release;
		frappe.db.get_value.mockReturnValue(
			new Promise((resolve) => {
				release = resolve;
			})
		);
		const frm = make_frm();

		const pending = pos.prefill_customer_email(frm);
		// The cashier types before the lookup comes back.
		frm.doc.contact_email = "cashier@example.com";
		release({ message: { email_id: "stale@example.com" } });
		await pending;

		expect(frm.set_value).not.toHaveBeenCalled();
		expect(frm.doc.contact_email).toBe("cashier@example.com");
	});

	it("leaves the field alone when the customer has no email", async () => {
		frappe.db.get_value.mockResolvedValue({ message: {} });
		const frm = make_frm();

		await pos.prefill_customer_email(frm);

		expect(frm.set_value).not.toHaveBeenCalled();
	});

	it("survives an empty response", async () => {
		frappe.db.get_value.mockResolvedValue(undefined);
		const frm = make_frm();

		await expect(pos.prefill_customer_email(frm)).resolves.not.toThrow();
		expect(frm.set_value).not.toHaveBeenCalled();
	});
});
