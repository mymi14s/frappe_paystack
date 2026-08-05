// The Complete Payment button on the Payment Log form.

import { beforeEach, describe, expect, it } from "vitest";
import {
	flush,
	install_web_globals,
	make_payment_log_frm,
	recorded,
	rejections,
	replies,
} from "./web_stubs.js";

const COMPLETE_PAYMENT =
	"frappe_paystack.frappe_paystack.doctype.paystack_payment_log.paystack_payment_log.complete_payment";

const COMPLETION_EVENT = "paystack_payment_completed";

/** Run refresh on a Payment Log and return the form with the buttons it offered. */
function refresh(doc = {}) {
	const { events, frm } = make_payment_log_frm(doc);
	events.refresh(frm);
	return frm;
}

/** Press Complete Payment on a stuck log and let the call settle. */
async function press(doc = {}) {
	replies[COMPLETE_PAYMENT] = "PSLOG-1";
	const frm = refresh(doc);
	frm.buttons["Complete Payment"]();
	await flush();
	return frm;
}

/** Return the frappe.call the form made for the completion. */
function completion_call() {
	return globalThis.frappe.call.mock.calls
		.map(([options]) => options)
		.find((options) => options && options.method === COMPLETE_PAYMENT);
}

describe("offering the button", () => {
	beforeEach(() => {
		install_web_globals();
	});

	it("offers it on a capture that never booked a Payment Entry", () => {
		const frm = refresh({ status: "Processed", payment_entry: "" });

		expect(Object.keys(frm.buttons)).toContain("Complete Payment");
	});

	it("files it under the Actions group, beside the other money actions", () => {
		const frm = refresh({ status: "Processed" });

		const [, , group] = frm.add_custom_button.mock.calls.find(
			([label]) => label === "Complete Payment"
		);
		expect(group).toBe("Actions");
	});

	it("offers it on a capture the sweep gave up on", () => {
		const frm = refresh({ status: "Needs Attention", payment_entry: "" });

		expect(Object.keys(frm.buttons)).toContain("Complete Payment");
	});

	it("withholds it on a given-up capture that has since been booked", () => {
		const frm = refresh({
			status: "Needs Attention",
			payment_entry: "ACC-PAY-0001",
		});

		expect(Object.keys(frm.buttons)).not.toContain("Complete Payment");
	});

	it("withholds it once a Payment Entry exists", () => {
		const frm = refresh({ status: "Processed", payment_entry: "ACC-PAY-0001" });

		expect(Object.keys(frm.buttons)).not.toContain("Complete Payment");
	});

	it.each(["Pending", "Completed", "Partially Refunded", "Refunded", "Failed"])(
		"withholds it on a %s log",
		(status) => {
			const frm = refresh({ status, payment_entry: "" });

			expect(Object.keys(frm.buttons)).not.toContain("Complete Payment");
		}
	);

	it("offers it on a log whose webhook never wrote a transaction id", () => {
		const frm = refresh({ status: "Processed", transaction_id: "" });

		expect(Object.keys(frm.buttons)).toContain("Complete Payment");
	});
});

describe("pressing the button", () => {
	beforeEach(() => {
		install_web_globals();
	});

	it("asks the server to complete the log on screen", async () => {
		await press();

		expect(completion_call().args).toEqual({ payment_log_name: "PSLOG-1" });
	});

	it("says the work has started, because the answer arrives later", async () => {
		await press();

		expect(globalThis.frappe.show_alert).toHaveBeenCalledWith({
			message: "Verifying this payment with Paystack...",
			indicator: "blue",
		});
	});

	it("ignores a second press while the first is still running", async () => {
		const frm = await press();

		frm.buttons["Complete Payment"]();
		await flush();

		expect(
			globalThis.frappe.call.mock.calls.filter(
				([options]) => options && options.method === COMPLETE_PAYMENT
			)
		).toHaveLength(1);
	});

	it("lets the user retry when the server refused the request", async () => {
		rejections[COMPLETE_PAYMENT] = new Error("already running");
		const frm = refresh();

		frm.buttons["Complete Payment"]();
		await flush();
		frm.buttons["Complete Payment"]();
		await flush();

		expect(
			globalThis.frappe.call.mock.calls.filter(
				([options]) => options && options.method === COMPLETE_PAYMENT
			)
		).toHaveLength(2);
	});

	it("lets the user retry when the server queued nothing", async () => {
		replies[COMPLETE_PAYMENT] = null;
		const frm = refresh();

		frm.buttons["Complete Payment"]();
		await flush();
		frm.buttons["Complete Payment"]();
		await flush();

		expect(
			globalThis.frappe.call.mock.calls.filter(
				([options]) => options && options.method === COMPLETE_PAYMENT
			)
		).toHaveLength(2);
	});

	it("stays quiet when the server queued nothing", async () => {
		replies[COMPLETE_PAYMENT] = null;
		const frm = refresh();

		frm.buttons["Complete Payment"]();
		await flush();

		expect(globalThis.frappe.show_alert).not.toHaveBeenCalled();
	});
});

describe("reporting the outcome", () => {
	beforeEach(() => {
		install_web_globals();
	});

	it("listens for the outcome from the moment the form loads", () => {
		const { events, frm } = make_payment_log_frm();
		events.onload(frm);

		expect(typeof recorded.realtime[COMPLETION_EVENT]).toBe("function");
	});

	it("shows what was booked and reloads the log", () => {
		const { events, frm } = make_payment_log_frm();
		events.onload(frm);

		recorded.realtime[COMPLETION_EVENT]({
			log: "PSLOG-1",
			booked: true,
			payment_entry: "ACC-PAY-0001",
			message: "Payment Entry ACC-PAY-0001 booked.",
		});

		expect(globalThis.frappe.show_alert).toHaveBeenCalledWith({
			message: "Payment Entry ACC-PAY-0001 booked.",
			indicator: "green",
		});
		expect(frm.reload_doc).toHaveBeenCalled();
	});

	it("reports a refusal without reloading, so the reason stays on screen", () => {
		const { events, frm } = make_payment_log_frm();
		events.onload(frm);

		recorded.realtime[COMPLETION_EVENT]({
			log: "PSLOG-1",
			booked: false,
			payment_entry: null,
			message: "Paystack captured 900.0 but this log records 1000.0.",
		});

		expect(globalThis.frappe.msgprint).toHaveBeenCalledWith({
			title: "Payment not completed",
			message: "Paystack captured 900.0 but this log records 1000.0.",
			indicator: "red",
		});
		expect(frm.reload_doc).not.toHaveBeenCalled();
	});

	it("ignores an outcome belonging to another log", () => {
		const { events, frm } = make_payment_log_frm();
		events.onload(frm);

		recorded.realtime[COMPLETION_EVENT]({
			log: "PSLOG-2",
			booked: true,
			payment_entry: "ACC-PAY-0002",
			message: "Payment Entry ACC-PAY-0002 booked.",
		});

		expect(globalThis.frappe.show_alert).not.toHaveBeenCalled();
		expect(frm.reload_doc).not.toHaveBeenCalled();
	});

	it("ignores an event carrying nothing", () => {
		const { events, frm } = make_payment_log_frm();
		events.onload(frm);

		recorded.realtime[COMPLETION_EVENT](null);

		expect(globalThis.frappe.msgprint).not.toHaveBeenCalled();
	});

	it("lets the user press again once a refusal has been reported", async () => {
		replies[COMPLETE_PAYMENT] = "PSLOG-1";
		const { events, frm } = make_payment_log_frm();
		events.onload(frm);
		events.refresh(frm);

		frm.buttons["Complete Payment"]();
		await flush();
		recorded.realtime[COMPLETION_EVENT]({
			log: "PSLOG-1",
			booked: false,
			payment_entry: null,
			message: "Paystack has no transaction under reference PSLOG-1.",
		});
		frm.buttons["Complete Payment"]();
		await flush();

		expect(
			globalThis.frappe.call.mock.calls.filter(
				([options]) => options && options.method === COMPLETE_PAYMENT
			)
		).toHaveLength(2);
	});
});
