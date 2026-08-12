// The status headline on the Payment Log form: the colour each status is shown in.

import { beforeEach, describe, expect, it } from "vitest";
import { install_web_globals, make_payment_log_frm } from "./web_stubs.js";

/** Run refresh on a Payment Log and return the headline it set. */
function headline(status) {
	const { events, frm } = make_payment_log_frm({ status });
	events.refresh(frm);
	return frm.dashboard.set_headline_alert.mock.calls.at(-1);
}

describe("the status headline", () => {
	beforeEach(() => {
		install_web_globals();
	});

	it("shows a capture the sweep gave up on in red", () => {
		const [message, colour] = headline("Needs Attention");

		expect(message).toBe(
			'Status: <strong style="text-transform:uppercase">Needs Attention</strong>'
		);
		expect(colour).toBe("red");
	});

	it("shows a capture still being retried in blue", () => {
		expect(headline("Processed")[1]).toBe("blue");
	});

	it("shows a booked capture in green", () => {
		expect(headline("Completed")[1]).toBe("green");
	});

	it("shows a payment that never happened in red", () => {
		expect(headline("Failed")[1]).toBe("red");
	});

	it("falls back to grey for a status it does not know", () => {
		expect(headline("Manual Override")[1]).toBe("gray");
	});
});
