// The public checkout page: which checkout it opens, and what a closed link says.

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
const HOSTED = "frappe_paystack.api.start_hosted_checkout";

function mount(overrides = {}) {
	const payload = checkout_payload(overrides);
	replies[VALIDATE] = payload;
	return make_instance(load_checkout(payload));
}

describe("checkout page bootstrap", () => {
	beforeEach(() => {
		install_web_globals();
	});

	it("mounts nothing when the page carries no payload", () => {
		expect(load_checkout()).toBeNull();
	});

	it("mounts nothing when the payload is not JSON", () => {
		expect(load_checkout("{not json")).toBeNull();
	});

	it("mounts the app for a real payload", () => {
		expect(load_checkout(checkout_payload())).not.toBeNull();
	});
});

describe("inline checkout", () => {
	beforeEach(() => {
		install_web_globals();
	});

	it("opens the Paystack popup with the amount in minor units", async () => {
		const app = mount();

		await app.startPayment();
		await flush();

		expect(recorded.paystack.amount).toBe(100000);
		expect(recorded.paystack.reference).toBe("PSLOG-1");
		expect(recorded.redirect).toBe("");
	});

	it("sends the metadata the webhook resolves the payment through", async () => {
		const app = mount();

		await app.startPayment();
		await flush();

		expect(recorded.paystack.metadata.reference).toBe("PSLOG-1");
		expect(recorded.paystack.metadata.reference_docname).toBe("ACC-SINV-0001");
	});

	it("stops when the link was paid in another tab", async () => {
		const app = mount();
		replies[VALIDATE] = checkout_payload({ status: "Processed", is_payable: false });

		await app.startPayment();
		await flush();

		expect(recorded.paystack).toBeNull();
		expect(app.feedback.tone).toBe("warning");
		expect(app.busy).toBe(false);
	});

	it("reports a server it could not reach", async () => {
		const app = mount();
		globalThis.frappe.call = () => Promise.reject(new Error("offline"));

		await app.startPayment();
		await flush();

		expect(app.feedback.tone).toBe("danger");
		expect(app.busy).toBe(false);
	});

	it("refuses to submit without a valid email", async () => {
		const app = mount({ email: "" });
		app.email = "not-an-email";

		await app.startPayment();
		await flush();

		expect(app.emailTouched).toBe(true);
		expect(recorded.paystack).toBeNull();
	});
});

describe("hosted checkout", () => {
	beforeEach(() => {
		install_web_globals();
	});

	it("redirects to the page Paystack opened", async () => {
		const app = mount({ checkout_mode: "Hosted" });
		replies[HOSTED] = "https://checkout.paystack.com/abc";

		await app.startPayment();
		await flush();

		expect(recorded.redirect).toBe("https://checkout.paystack.com/abc");
		expect(recorded.paystack).toBeNull();
	});

	it("sends the email the customer typed", async () => {
		const app = mount({ checkout_mode: "Hosted", email: "" });
		replies[HOSTED] = "https://checkout.paystack.com/abc";
		app.email = "typed@example.com";

		await app.startPayment();
		await flush();

		const [, args] = globalThis.frappe.call.mock.calls.at(-1);
		expect(args).toEqual({ reference: "PSLOG-1", email: "typed@example.com" });
	});

	it("reports a checkout Paystack would not open", async () => {
		const app = mount({ checkout_mode: "Hosted" });
		replies[HOSTED] = null;

		await app.startPayment();
		await flush();

		expect(recorded.redirect).toBe("");
		expect(app.feedback.tone).toBe("danger");
		expect(app.busy).toBe(false);
	});

	it("reports a server it could not reach", async () => {
		const app = mount({ checkout_mode: "Hosted" });
		globalThis.frappe.call = (method) =>
			method === HOSTED
				? Promise.reject(new Error("offline"))
				: Promise.resolve({ message: checkout_payload({ checkout_mode: "Hosted" }) });

		await app.startPayment();
		await flush();

		expect(recorded.redirect).toBe("");
		expect(app.feedback.tone).toBe("danger");
	});
});

describe("closed checkouts", () => {
	beforeEach(() => {
		install_web_globals();
	});

	it("says an expired link has expired", () => {
		const app = mount({ is_expired: true, is_payable: false });

		expect(app.canPay).toBe(false);
		expect(app.closedReason).toMatch(/expired/i);
	});

	it("says a paid link is paid, even once it has also expired", () => {
		const app = mount({ status: "Completed", is_expired: true, is_payable: false });

		expect(app.closedReason).toMatch(/already been completed/i);
	});

	it("says a capture the merchant gave up on is already paid", () => {
		const app = mount({ status: "Needs Attention", is_payable: false });

		expect(app.canPay).toBe(false);
		expect(app.closedReason).toMatch(/already been completed/i);
	});

	it("says a cancelled document cannot be paid", () => {
		const app = mount({ order_docstatus: 2, is_payable: false });

		expect(app.closedReason).toMatch(/no longer active/i);
	});

	it("says a settled document is settled", () => {
		const app = mount({ is_payable: false });

		expect(app.closedReason).toMatch(/settled/i);
	});
});
