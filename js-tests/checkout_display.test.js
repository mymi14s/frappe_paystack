// The checkout page's arithmetic and labelling.

import { beforeEach, describe, expect, it } from "vitest";
import {
	checkout_payload,
	flush,
	install_web_globals,
	load_checkout,
	make_instance,
	recorded,
	replies,
} from "./web_stubs.js";

const VALIDATE = "frappe_paystack.api.validate_payment_link";

function mount(overrides = {}) {
	const payload = checkout_payload(overrides);
	replies[VALIDATE] = payload;
	return make_instance(load_checkout(payload));
}

/** Open the inline popup and return the options it was built with. */
async function open_popup(overrides = {}) {
	const app = mount(overrides);
	await app.startPayment();
	await flush();
	return { app, options: recorded.paystack };
}

describe("minor units", () => {
	beforeEach(() => {
		install_web_globals();
	});

	it("sends a whole amount as its minor units", async () => {
		const { options } = await open_popup({ payment_amount: 1000 });

		expect(options.amount).toBe(100000);
	});

	it("sends the kobo of a fractional amount", async () => {
		const { options } = await open_popup({ payment_amount: 1234.56 });

		expect(options.amount).toBe(123456);
	});

	it("rounds away binary floating point error", async () => {
		// 19.99 * 100 is 1998.9999999999998 in binary floating point.
		const { options } = await open_popup({ payment_amount: 19.99 });

		expect(options.amount).toBe(1999);
	});

	it("rounds a half kobo up rather than dropping it", async () => {
		const { options } = await open_popup({ payment_amount: 10.005 });

		expect(options.amount).toBe(1001);
	});

	it("sends an integer, which is all Paystack accepts", async () => {
		const { options } = await open_popup({ payment_amount: 0.1 });

		expect(Number.isInteger(options.amount)).toBe(true);
		expect(options.amount).toBe(10);
	});
});

describe("popup outcomes", () => {
	beforeEach(() => {
		install_web_globals();
	});

	it("closes the payment out when the popup reports success", async () => {
		const { app, options } = await open_popup();

		options.onSuccess();

		expect(app.busy).toBe(false);
		expect(app.doc.status).toBe("Processed");
		expect(app.canPay).toBe(false);
		expect(app.feedback.tone).toBe("success");
	});

	it("leaves the link payable when the customer cancels", async () => {
		const { app, options } = await open_popup();

		options.onCancel();

		expect(app.busy).toBe(false);
		expect(app.canPay).toBe(true);
		expect(app.feedback.tone).toBe("warning");
	});

	it("reports the message the popup failed with", async () => {
		const { app, options } = await open_popup();

		options.onError({ message: "Card declined" });

		expect(app.busy).toBe(false);
		expect(app.feedback.message).toBe("Card declined");
		expect(app.feedback.tone).toBe("danger");
	});

	it("reports a failure that carries no message", async () => {
		const { app, options } = await open_popup();

		options.onError(null);

		expect(app.feedback.message).toMatch(/could not be completed/i);
	});
});

describe("money on the page", () => {
	beforeEach(() => {
		install_web_globals();
	});

	it("formats the charge in its own currency", () => {
		const app = mount({ payment_amount: 1000, currency: "NGN" });

		expect(app.formattedChargeAmount).toMatch(/1,000/);
	});

	it("falls back to a grouped number for an unusable currency code", () => {
		const app = mount({ payment_amount: 1000, currency: "" });

		expect(app.formattedChargeAmount).toBe("1,000.00");
	});

	it("shows the order total in the currency the order was raised in", () => {
		const app = mount({ grand_total: 2500, order_currency: "USD" });

		expect(app.formattedOrderTotal).toMatch(/2,500/);
	});

	it("hides the conversion when the charge is in the order currency", () => {
		const app = mount({ currency: "NGN", order_currency: "NGN" });

		expect(app.showConverted).toBe(false);
	});

	it("shows the conversion when the charge is in another currency", () => {
		const app = mount({ currency: "NGN", order_currency: "USD" });

		expect(app.showConverted).toBe(true);
	});

	it("quotes the exchange rate to four places", () => {
		const app = mount({ exchange_rate: 1543.21789 });

		expect(app.exchangeRate).toBe("1543.2179");
	});

	it("quotes par when the payload carries no rate", () => {
		const app = mount({ exchange_rate: 0 });

		expect(app.exchangeRate).toBe("1.0000");
	});
});

describe("status badge", () => {
	beforeEach(() => {
		install_web_globals();
	});

	it("marks an open payment as pending", () => {
		const app = mount({ status: "Pending" });

		expect(app.statusTone).toBe("pending");
		expect(app.statusLabel).toMatch(/awaiting/i);
	});

	it("marks a captured payment as a success", () => {
		const app = mount({ status: "Processed" });

		expect(app.statusTone).toBe("success");
	});

	it("marks a failed payment as a danger", () => {
		const app = mount({ status: "Failed" });

		expect(app.statusTone).toBe("danger");
		expect(app.statusLabel).toMatch(/failed/i);
	});

	it("still tells the payer their money arrived once the merchant gives up", () => {
		const app = mount({ status: "Needs Attention" });

		expect(app.statusTone).toBe("success");
		expect(app.statusLabel).toBe("Payment received");
	});

	it("shows an unmapped status as it stands", () => {
		const app = mount({ status: "Partially Refunded" });

		expect(app.statusTone).toBe("pending");
		expect(app.statusLabel).toBe("Partially Refunded");
	});
});

describe("email entry", () => {
	beforeEach(() => {
		install_web_globals();
	});

	it("asks for an email only when the payload carries none", () => {
		expect(mount({ email: "" }).needsEmail).toBe(true);
		expect(mount({ email: "buyer@example.com" }).needsEmail).toBe(false);
	});

	it("refuses an address that is not one", () => {
		const app = mount({ email: "" });
		app.email = "buyer@example";

		expect(app.isEmailValid).toBe(false);
		expect(app.canSubmit).toBe(false);
	});

	it("accepts an address that is", () => {
		const app = mount({ email: "" });
		app.email = "buyer@example.com";

		expect(app.canSubmit).toBe(true);
	});

	it("refuses a second submission while one is in flight", () => {
		const app = mount();
		app.busy = true;

		expect(app.canSubmit).toBe(false);
	});
});
