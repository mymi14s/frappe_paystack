import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { call_reply, install_globals, make_frm, mount_dom, paystack_row } from "./stubs.js";

install_globals();
const pos = await import("../frappe_paystack/public/js/paystack_pos.bundle.js");

// Lets the promise chains inside announce settle between timer advances.
async function flush() {
	for (let index = 0; index < 8; index++) {
		await Promise.resolve();
	}
}

describe("watch_payment", () => {
	let dom;

	beforeEach(() => {
		install_globals();
		dom = mount_dom();
		vi.useFakeTimers();
	});

	afterEach(() => {
		pos.stop_watching();
		vi.useRealTimers();
	});

	it("freezes the till and polls every five seconds", async () => {
		frappe.call.mockReturnValue(call_reply({ amount_paid: 0 }));

		pos.watch_payment("PAY-LOG-0001");

		expect(frappe.dom.freeze).toHaveBeenCalledWith("Waiting for payment...");
		expect(frappe.call).not.toHaveBeenCalled();

		await vi.advanceTimersByTimeAsync(5000);
		expect(frappe.call).toHaveBeenCalledWith({
			method: "frappe_paystack.utils.pos_payment.pos_payment_status",
			args: { log: "PAY-LOG-0001" },
		});

		await vi.advanceTimersByTimeAsync(10000);
		expect(frappe.call).toHaveBeenCalledTimes(3);
	});

	it("keeps polling while nothing has been collected", async () => {
		frappe.call.mockReturnValue(call_reply({ amount_paid: 0 }));

		pos.watch_payment("PAY-LOG-0001");
		await vi.advanceTimersByTimeAsync(20000);

		expect(frappe.show_alert).not.toHaveBeenCalled();
		expect(frappe.call).toHaveBeenCalledTimes(4);
	});

	it("announces the payment as soon as a poll finds one", async () => {
		frappe.call.mockReturnValue(
			call_reply({ pos_invoice: "POS-INV-0001", amount_paid: 5000, fully_paid: 1 })
		);

		pos.watch_payment("PAY-LOG-0001");
		await vi.advanceTimersByTimeAsync(5000);
		await flush();

		expect(frappe.show_alert).toHaveBeenCalledWith({
			message: "Payment received in full",
			indicator: "green",
		});
	});

	it("stops polling once the payment lands", async () => {
		frappe.call.mockReturnValue(
			call_reply({ pos_invoice: "POS-INV-0001", amount_paid: 5000, fully_paid: 1 })
		);

		pos.watch_payment("PAY-LOG-0001");
		await vi.advanceTimersByTimeAsync(5000);
		await flush();
		const calls_at_announce = frappe.call.mock.calls.length;

		await vi.advanceTimersByTimeAsync(30000);
		expect(frappe.call).toHaveBeenCalledTimes(calls_at_announce);
	});

	it("gives up after ten minutes rather than leaving the till frozen", async () => {
		frappe.call.mockReturnValue(call_reply({ amount_paid: 0 }));

		pos.watch_payment("PAY-LOG-0001");
		await vi.advanceTimersByTimeAsync(10 * 60 * 1000);
		expect(frappe.msgprint).not.toHaveBeenCalled();

		await vi.advanceTimersByTimeAsync(10 * 1000);
		expect(frappe.msgprint).toHaveBeenCalledWith(
			"Still waiting for payment. Check the Paystack Payment Log."
		);
		expect(frappe.dom.unfreeze).toHaveBeenCalled();

		const calls_at_timeout = frappe.call.mock.calls.length;
		await vi.advanceTimersByTimeAsync(60000);
		expect(frappe.call).toHaveBeenCalledTimes(calls_at_timeout);
	});

	it("drops the previous watch when a second link is sent", async () => {
		frappe.call.mockReturnValue(call_reply({ amount_paid: 0 }));

		pos.watch_payment("PAY-LOG-0001");
		pos.watch_payment("PAY-LOG-0002");
		await vi.advanceTimersByTimeAsync(5000);

		expect(frappe.call).toHaveBeenCalledTimes(1);
		expect(frappe.call.mock.calls[0][0].args).toEqual({ log: "PAY-LOG-0002" });
	});

	it("unfreezes and clears the timer on stop_watching", async () => {
		frappe.call.mockReturnValue(call_reply({ amount_paid: 0 }));

		pos.watch_payment("PAY-LOG-0001");
		pos.stop_watching();
		await vi.advanceTimersByTimeAsync(30000);

		expect(frappe.call).not.toHaveBeenCalled();
		expect(frappe.dom.unfreeze).toHaveBeenCalled();
	});

	it("is safe to stop when nothing is being watched", () => {
		expect(() => pos.stop_watching()).not.toThrow();
		expect(frappe.dom.unfreeze).toHaveBeenCalled();
	});

	it("keeps the Complete Order button untouched by a poll that found nothing", async () => {
		const click = vi.fn();
		dom.submit.addEventListener("click", click);
		frappe.call.mockReturnValue(call_reply({ amount_paid: 0 }));

		pos.watch_payment("PAY-LOG-0001");
		await vi.advanceTimersByTimeAsync(20000);

		expect(click).not.toHaveBeenCalled();
	});
});

describe("announce", () => {
	let dom;
	let click;

	beforeEach(() => {
		install_globals();
		dom = mount_dom();
		click = vi.fn();
		dom.submit.addEventListener("click", click);
		vi.useFakeTimers();
	});

	afterEach(() => {
		pos.stop_watching();
		globalThis.cur_frm = null;
		vi.useRealTimers();
	});

	it("books the payment on the open invoice and completes the order", async () => {
		const row = paystack_row(0);
		const frm = make_frm({ payments: [row] });
		globalThis.cur_frm = frm;

		pos.announce({
			pos_invoice: "POS-INV-0001",
			amount_paid: 5000,
			outstanding: 0,
			fully_paid: 1,
		});
		await flush();

		expect(row.amount).toBe(5000);
		// Core recalculates paid_amount from the tender rows.
		frm.doc.paid_amount = 5000;
		await vi.advanceTimersByTimeAsync(200);

		expect(click).toHaveBeenCalledTimes(1);
		expect(frappe.show_alert).toHaveBeenCalledWith({
			message: "Payment received in full",
			indicator: "green",
		});
	});

	it("never completes the order on a part payment", async () => {
		const row = paystack_row(0);
		const frm = make_frm({ payments: [row] });
		globalThis.cur_frm = frm;

		pos.announce({
			pos_invoice: "POS-INV-0001",
			amount_paid: 2000,
			outstanding: 3000,
			fully_paid: 0,
		});
		await flush();
		frm.doc.paid_amount = 2000;
		await vi.advanceTimersByTimeAsync(10000);

		expect(row.amount).toBe(2000);
		expect(click).not.toHaveBeenCalled();
		expect(frappe.show_alert).toHaveBeenCalledWith({
			message: "Part payment received. Outstanding: 3000",
			indicator: "orange",
		});
	});

	it("leaves the button live after a part payment so the cashier can retry", async () => {
		const frm = make_frm({ payments: [paystack_row(0)], contact_email: "buyer@x.com" });
		globalThis.cur_frm = frm;

		pos.announce({
			pos_invoice: "POS-INV-0001",
			amount_paid: 2000,
			outstanding: 3000,
			fully_paid: 0,
		});
		await flush();

		expect(frm.paystack_settled).toBe(false);
		expect(dom.field.style.display).toBe("");
	});

	it("stops the poll before doing anything else", async () => {
		frappe.call.mockReturnValue(call_reply({ amount_paid: 0 }));
		pos.watch_payment("PAY-LOG-0001");

		pos.announce({ pos_invoice: "POS-INV-0001", amount_paid: 5000, fully_paid: 1 });
		await flush();
		await vi.advanceTimersByTimeAsync(30000);

		expect(frappe.call).not.toHaveBeenCalled();
		expect(frappe.dom.unfreeze).toHaveBeenCalled();
	});

	it("ignores a payment for an invoice the cashier is not on", async () => {
		const row = paystack_row(0);
		const frm = make_frm({ payments: [row] });
		globalThis.cur_frm = frm;

		pos.announce({ pos_invoice: "POS-INV-9999", amount_paid: 5000, fully_paid: 1 });
		await flush();

		expect(row.amount).toBe(0);
		expect(frappe.model.set_value).not.toHaveBeenCalled();
	});

	it("does not fall over when no invoice is open", async () => {
		globalThis.cur_frm = null;

		expect(() =>
			pos.announce({ pos_invoice: "POS-INV-0001", amount_paid: 5000, fully_paid: 1 })
		).not.toThrow();
		await flush();
	});
});

describe("paid_amount_settled", () => {
	beforeEach(() => {
		install_globals();
		mount_dom();
		vi.useFakeTimers();
	});

	afterEach(() => vi.useRealTimers());

	it("resolves as soon as core has recalculated the paid amount", async () => {
		const frm = make_frm({ paid_amount: 0 });
		const settled = pos.paid_amount_settled(frm, 5000);

		await vi.advanceTimersByTimeAsync(300);
		frm.doc.paid_amount = 5000;
		await vi.advanceTimersByTimeAsync(100);

		await expect(settled).resolves.toBe(5000);
	});

	it("gives up after five seconds", async () => {
		const frm = make_frm({ paid_amount: 0 });
		const settled = pos.paid_amount_settled(frm, 5000);

		await vi.advanceTimersByTimeAsync(5000);

		await expect(settled).resolves.toBe(0);
	});
});

describe("complete_order", () => {
	let dom;
	let click;

	beforeEach(() => {
		install_globals();
		dom = mount_dom();
		click = vi.fn();
		dom.submit.addEventListener("click", click);
		vi.useFakeTimers();
	});

	afterEach(() => vi.useRealTimers());

	it("clicks Complete Order once the tender has settled", async () => {
		const frm = make_frm({ paid_amount: 0 });

		pos.complete_order(frm, 5000);
		await vi.advanceTimersByTimeAsync(200);
		frm.doc.paid_amount = 5000;
		await vi.advanceTimersByTimeAsync(100);

		expect(click).toHaveBeenCalledTimes(1);
	});

	it("tells the cashier instead of clicking when the tender never lands", async () => {
		const frm = make_frm({ paid_amount: 0 });

		pos.complete_order(frm, 5000);
		await vi.advanceTimersByTimeAsync(6000);

		expect(click).not.toHaveBeenCalled();
		expect(frappe.msgprint).toHaveBeenCalledWith(
			"The payment did not reach the tender. Complete the order manually."
		);
	});

	it("does nothing when POS is not on screen", async () => {
		document.body.innerHTML = "";
		const frm = make_frm({ paid_amount: 5000 });

		pos.complete_order(frm, 5000);
		await vi.advanceTimersByTimeAsync(6000);

		expect(frappe.msgprint).not.toHaveBeenCalled();
	});
});
