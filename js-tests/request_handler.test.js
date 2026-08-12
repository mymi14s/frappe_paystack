import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
	call_failure,
	call_reply,
	install_globals,
	make_frm,
	mount_dom,
	paystack_row,
} from "./stubs.js";

install_globals();
const pos = await import("../frappe_paystack/public/js/paystack_pos.bundle.js");

async function flush() {
	for (let index = 0; index < 8; index++) {
		await Promise.resolve();
	}
}

function phone_frm() {
	return make_frm({
		payments: [{ mode_of_payment: "Cash", amount: 5000 }],
		contact_mobile: "08030000000",
		currency: "NGN",
	});
}

describe("send_payment_link", () => {
	beforeEach(() => {
		install_globals();
		mount_dom();
	});

	afterEach(() => pos.stop_watching());

	it("saves the invoice and asks the server for a link", async () => {
		const frm = make_frm({
			payments: [paystack_row(5000)],
			contact_email: "buyer@example.com",
		});

		await pos.send_payment_link(frm);

		expect(frm.dirty).toHaveBeenCalled();
		expect(frm.save).toHaveBeenCalled();
		expect(frappe.call).toHaveBeenCalledWith({
			method: "frappe_paystack.utils.pos_payment.send_pos_payment_link",
			args: { pos_invoice: "POS-INV-0001", email: "buyer@example.com" },
		});
	});

	it("refuses an invalid email before touching the server", () => {
		const frm = make_frm({ payments: [paystack_row(5000)], contact_email: "nope" });

		expect(() => pos.send_payment_link(frm)).toThrow("Enter a valid email address first.");
		expect(frm.save).not.toHaveBeenCalled();
		expect(frappe.call).not.toHaveBeenCalled();
	});

	it("confirms the link and starts watching for the payment", async () => {
		frappe.call.mockReturnValue(
			call_reply({ email: "buyer@example.com", log: "PAY-LOG-0001" })
		);
		const frm = make_frm({
			payments: [paystack_row(5000)],
			contact_email: "buyer@example.com",
		});

		await pos.send_payment_link(frm);

		expect(frappe.show_alert).toHaveBeenCalledWith({
			message: "Payment link sent to buyer@example.com",
			indicator: "green",
		});
		expect(frappe.dom.freeze).toHaveBeenCalledWith("Waiting for payment...");
	});

	it("unfreezes the till and tells the cashier when the send fails", async () => {
		frappe.call.mockReturnValue(call_failure());
		const frm = make_frm({
			payments: [paystack_row(5000)],
			contact_email: "buyer@example.com",
		});

		await pos.send_payment_link(frm).catch(() => {});

		expect(frappe.dom.unfreeze).toHaveBeenCalled();
		expect(frappe.msgprint).toHaveBeenCalledWith("Could not send the payment link.");
	});
});

describe("paystack_request_for_payment", () => {
	beforeEach(() => {
		install_globals();
		mount_dom();
	});

	afterEach(() => pos.stop_watching());

	it("sends a payment link for a Paystack tender", async () => {
		const frm = make_frm({
			payments: [paystack_row(5000)],
			contact_email: "buyer@example.com",
		});

		await pos.paystack_request_for_payment(frm);

		expect(frappe.call.mock.calls[0][0].method).toBe(
			"frappe_paystack.utils.pos_payment.send_pos_payment_link"
		);
	});

	it("does not demand a mobile number for a Paystack tender", () => {
		const frm = make_frm({
			payments: [paystack_row(5000)],
			contact_email: "buyer@example.com",
			contact_mobile: "",
		});

		expect(() => pos.paystack_request_for_payment(frm)).not.toThrow();
	});

	it("reproduces core's mobile number check for any other tender", () => {
		const frm = make_frm({ payments: [{ mode_of_payment: "Cash", amount: 5000 }] });

		expect(() => pos.paystack_request_for_payment(frm)).toThrow(
			"Please enter mobile number first."
		);
		expect(frm.save).not.toHaveBeenCalled();
		expect(frappe.call).not.toHaveBeenCalled();
	});

	it("falls through to core's payment request when a mobile number is set", async () => {
		frappe.call.mockReturnValue(call_reply({ name: "PREQ-0001" }));
		const frm = phone_frm();

		await pos.paystack_request_for_payment(frm);

		expect(frm.dirty).toHaveBeenCalled();
		expect(frm.save).toHaveBeenCalled();
		expect(frappe.dom.freeze).toHaveBeenCalledWith("Waiting for payment...");
		expect(frappe.call).toHaveBeenCalledWith({
			method: "create_payment_request",
			doc: frm.doc,
		});
	});

	it("reports a failed core payment request", async () => {
		frappe.call.mockReturnValue(call_failure());
		const frm = phone_frm();

		await pos.paystack_request_for_payment(frm).catch(() => {});

		expect(frappe.dom.unfreeze).toHaveBeenCalled();
		expect(frappe.msgprint).toHaveBeenCalledWith("Payment request failed");
	});
});

// Core freezes the till on a phone tender and unfreezes it a minute later.
describe("core payment request follow-up", () => {
	beforeEach(() => {
		install_globals();
		mount_dom();
		vi.useFakeTimers();
		frappe.call.mockReturnValue(call_reply({ name: "PREQ-0001" }));
	});

	afterEach(() => vi.useRealTimers());

	it("unfreezes the till and warns when the request is still unpaid", async () => {
		frappe.db.get_value.mockResolvedValue({ message: { status: "Initiated" } });

		pos.paystack_request_for_payment(phone_frm());
		await vi.advanceTimersByTimeAsync(60000);
		await flush();

		expect(frappe.db.get_value).toHaveBeenCalledWith("Payment Request", "PREQ-0001", [
			"status",
			"grand_total",
		]);
		expect(frappe.dom.unfreeze).toHaveBeenCalled();
		expect(frappe.msgprint).toHaveBeenCalledWith({
			message:
				"Payment Request took too long to respond. Please try requesting for payment again.",
			title: "Request Timeout",
		});
	});

	it("leaves the till frozen until the minute is up", async () => {
		frappe.db.get_value.mockResolvedValue({ message: { status: "Initiated" } });

		pos.paystack_request_for_payment(phone_frm());
		await vi.advanceTimersByTimeAsync(59000);
		await flush();

		expect(frappe.dom.unfreeze).not.toHaveBeenCalled();
	});

	it("completes the order once the request is paid", async () => {
		frappe.db.get_value.mockResolvedValue({
			message: { status: "Paid", grand_total: 5000 },
		});
		globalThis.cur_frm = { reload_doc: vi.fn() };

		pos.paystack_request_for_payment(phone_frm());
		await vi.advanceTimersByTimeAsync(60000);
		await flush();

		expect(frappe.dom.unfreeze).toHaveBeenCalled();
		expect(globalThis.cur_frm.reload_doc).toHaveBeenCalled();
		expect(cur_pos.payment.events.submit_invoice).toHaveBeenCalled();
		expect(frappe.show_alert).toHaveBeenCalledWith({
			message: "Payment of 5000 received successfully.",
			indicator: "green",
		});
	});

	it("leaves an already unfrozen till alone", async () => {
		frappe.db.get_value.mockResolvedValue({
			message: { status: "Paid", grand_total: 5000 },
		});
		frappe.dom.freeze_count = 0;
		globalThis.cur_frm = { reload_doc: vi.fn() };

		pos.paystack_request_for_payment(phone_frm());
		await vi.advanceTimersByTimeAsync(60000);
		await flush();

		expect(cur_pos.payment.events.submit_invoice).not.toHaveBeenCalled();
		expect(frappe.show_alert).not.toHaveBeenCalled();
	});

	it("does not run the follow-up for a Paystack tender", async () => {
		const frm = make_frm({
			payments: [paystack_row(5000)],
			contact_email: "buyer@example.com",
			contact_mobile: "08030000000",
		});

		pos.paystack_request_for_payment(frm);
		await vi.advanceTimersByTimeAsync(60000);
		await flush();

		expect(frappe.db.get_value).not.toHaveBeenCalled();
		pos.stop_watching();
	});
});

describe("install_request_handler", () => {
	beforeEach(() => install_globals());

	it("reports failure while core's form script is not loaded yet", () => {
		expect(pos.install_request_handler()).toBe(false);

		frappe.ui.form.handlers["POS Invoice"] = {};
		expect(pos.install_request_handler()).toBe(false);
	});

	it("replaces core's handler", () => {
		const core = vi.fn();
		frappe.ui.form.handlers["POS Invoice"] = { request_for_payment: [core] };

		expect(pos.install_request_handler()).toBe(true);

		const installed = frappe.ui.form.handlers["POS Invoice"].request_for_payment;
		expect(installed).toEqual([pos.paystack_request_for_payment]);
		expect(installed).not.toContain(core);
	});

	it("does not stack when installed repeatedly", () => {
		frappe.ui.form.handlers["POS Invoice"] = { request_for_payment: [vi.fn()] };

		pos.install_request_handler();
		pos.install_request_handler();
		pos.install_request_handler();

		expect(frappe.ui.form.handlers["POS Invoice"].request_for_payment).toHaveLength(1);
	});

	it("leaves other doctypes untouched", () => {
		const other = vi.fn();
		frappe.ui.form.handlers["POS Invoice"] = { request_for_payment: [vi.fn()] };
		frappe.ui.form.handlers["Sales Invoice"] = { request_for_payment: [other] };

		pos.install_request_handler();

		expect(frappe.ui.form.handlers["Sales Invoice"].request_for_payment).toEqual([other]);
	});

	it("reinstalls after core re-registers its own handler", () => {
		const core = vi.fn();
		frappe.ui.form.handlers["POS Invoice"] = { request_for_payment: [core] };
		pos.install_request_handler();

		frappe.ui.form.handlers["POS Invoice"].request_for_payment = [core];
		pos.install_request_handler();

		expect(frappe.ui.form.handlers["POS Invoice"].request_for_payment).toEqual([
			pos.paystack_request_for_payment,
		]);
	});
});
