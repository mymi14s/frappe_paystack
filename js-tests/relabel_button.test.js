import { beforeEach, describe, expect, it } from "vitest";
import { install_globals, make_frm, mount_dom, paystack_row } from "./stubs.js";

install_globals();
const pos = await import("../frappe_paystack/public/js/paystack_pos.bundle.js");

describe("relabel_button", () => {
	let dom;

	beforeEach(() => {
		install_globals();
		dom = mount_dom();
	});

	it("offers to send a payment link for a Paystack tender", () => {
		const frm = make_frm({
			payments: [paystack_row(5000)],
			contact_email: "buyer@example.com",
		});

		pos.relabel_button(frm);

		expect(dom.button.textContent).toBe("Send Payment Link");
	});

	it("keeps core's label for any other tender", () => {
		const frm = make_frm({
			payments: [{ mode_of_payment: "Cash", amount: 5000 }],
			contact_mobile: "08030000000",
		});

		pos.relabel_button(frm);

		expect(dom.button.textContent).toBe("Request for Payment");
	});

	it("hides the control once the invoice is settled", () => {
		const frm = make_frm({
			payments: [paystack_row(5000)],
			contact_email: "buyer@example.com",
		});
		frm.paystack_settled = true;

		pos.relabel_button(frm);

		expect(dom.field.style.display).toBe("none");
	});

	it("shows the control again while the invoice is unsettled", () => {
		const frm = make_frm({ payments: [paystack_row(5000)] });
		dom.field.style.display = "none";

		pos.relabel_button(frm);

		expect(dom.field.style.display).toBe("");
	});

	it("stays disabled until the Paystack email is valid", () => {
		const frm = make_frm({ payments: [paystack_row(5000)] });

		pos.relabel_button(frm);
		expect(dom.button.disabled).toBe(true);

		frm.doc.contact_email = "not-an-email";
		pos.relabel_button(frm);
		expect(dom.button.disabled).toBe(true);

		frm.doc.contact_email = "buyer@example.com";
		pos.relabel_button(frm);
		expect(dom.button.disabled).toBe(false);
		expect(dom.button.classList.contains("btn-primary")).toBe(true);
	});

	it("enables a non-Paystack tender only on a mobile number", () => {
		const frm = make_frm({ payments: [{ mode_of_payment: "Cash", amount: 5000 }] });

		pos.relabel_button(frm);
		expect(dom.button.disabled).toBe(true);

		frm.doc.contact_mobile = "08030000000";
		pos.relabel_button(frm);
		expect(dom.button.disabled).toBe(false);
	});

	it("ignores the mobile number for a Paystack tender", () => {
		const frm = make_frm({
			payments: [paystack_row(5000)],
			contact_mobile: "08030000000",
		});

		pos.relabel_button(frm);

		expect(dom.button.disabled).toBe(true);
	});

	it("swaps the visible contact field to match the tender", () => {
		const paystack = make_frm({ payments: [paystack_row(5000)] });
		pos.relabel_button(paystack);
		expect(paystack.toggle_display).toHaveBeenCalledWith("contact_email", true);
		expect(paystack.toggle_display).toHaveBeenCalledWith("contact_mobile", false);

		const cash = make_frm({ payments: [{ mode_of_payment: "Cash", amount: 5000 }] });
		pos.relabel_button(cash);
		expect(cash.toggle_display).toHaveBeenCalledWith("contact_email", false);
		expect(cash.toggle_display).toHaveBeenCalledWith("contact_mobile", true);
	});

	it("shows only the email field on the POS screen for a Paystack tender", () => {
		const frm = make_frm({ payments: [paystack_row(5000)] });

		pos.relabel_button(frm);

		expect(dom.email.style.display).toBe("");
		expect(dom.mobile.style.display).toBe("none");
	});

	it("shows only the mobile field on the POS screen for any other tender", () => {
		const frm = make_frm({ payments: [{ mode_of_payment: "Cash", amount: 5000 }] });

		pos.relabel_button(frm);

		expect(dom.email.style.display).toBe("none");
		expect(dom.mobile.style.display).toBe("");
	});

	it("does nothing when the control is not on screen", () => {
		document.body.innerHTML = "";
		const frm = make_frm({ payments: [paystack_row(5000)] });

		expect(() => pos.relabel_button(frm)).not.toThrow();
		expect(frm.toggle_display).not.toHaveBeenCalled();
	});
});
