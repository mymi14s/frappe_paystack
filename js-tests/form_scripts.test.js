// The three doctype_js form scripts: what each binds to, owes and may be paid.

import { beforeEach, describe, expect, it } from "vitest";
import { install_web_globals, load_form_script } from "./web_stubs.js";

const DOCTYPES = ["Dunning", "Sales Invoice", "Sales Order"];

/** Run a doctype's refresh handler, returning the form and what it delegated. */
function refresh(doctype, doc = {}) {
	const events = load_form_script(doctype);
	const frm = { doc: { doctype, ...doc } };
	events[doctype].refresh(frm);

	const calls = globalThis.frappe_paystack.actions.setupForm.mock.calls;
	return { frm, form: calls.at(-1)[0], config: calls.at(-1)[1] };
}

/** Return what a doctype reports as still collectable. */
function outstanding(doctype, doc) {
	const { frm, config } = refresh(doctype, doc);
	return config.getOutstanding(frm);
}

/** Return whether a doctype will let the document be paid. */
function payable(doctype, doc) {
	const { frm, config } = refresh(doctype, doc);
	return config.isPayable(frm);
}

describe.each(DOCTYPES)("the %s form script", (doctype) => {
	beforeEach(() => {
		install_web_globals();
	});

	it("binds to its own doctype and to no other", () => {
		const events = load_form_script(doctype);

		expect(Object.keys(events)).toEqual([doctype]);
	});

	it("offers the buttons on refresh, so a reload keeps them", () => {
		const events = load_form_script(doctype);

		expect(typeof events[doctype].refresh).toBe("function");
	});

	it("hands the form itself to the shared action bundle", () => {
		const { frm, form } = refresh(doctype);

		expect(form).toBe(frm);
	});

	it("hands over both rules the bundle asks for", () => {
		const { config } = refresh(doctype);

		expect(typeof config.getOutstanding).toBe("function");
		expect(typeof config.isPayable).toBe("function");
	});

	it("reads an empty document as owing nothing", () => {
		expect(outstanding(doctype, {})).toBe(0);
	});
});

describe("Sales Invoice", () => {
	beforeEach(() => {
		install_web_globals();
	});

	it("collects the outstanding amount, not the whole invoice", () => {
		expect(
			outstanding("Sales Invoice", { grand_total: 1000, outstanding_amount: 250 })
		).toBe(250);
	});

	it("lets an ordinary invoice be paid", () => {
		expect(payable("Sales Invoice", { is_return: 0 })).toBe(true);
	});

	it("refuses a credit note, which owes the customer rather than bills them", () => {
		expect(payable("Sales Invoice", { is_return: 1 })).toBe(false);
	});
});

describe("Sales Order", () => {
	beforeEach(() => {
		install_web_globals();
	});

	it("nets off the advance already paid", () => {
		expect(
			outstanding("Sales Order", { grand_total: 1000, advance_paid: 300 })
		).toBe(700);
	});

	it("never offers to collect a negative amount", () => {
		expect(
			outstanding("Sales Order", { grand_total: 1000, advance_paid: 1500 })
		).toBe(0);
	});

	it("lets an order still being delivered be paid", () => {
		expect(payable("Sales Order", { status: "To Deliver and Bill" })).toBe(true);
	});

	it("refuses a completed order", () => {
		expect(payable("Sales Order", { status: "Completed" })).toBe(false);
	});

	it("refuses a closed order", () => {
		expect(payable("Sales Order", { status: "Closed" })).toBe(false);
	});
});

describe("Dunning", () => {
	beforeEach(() => {
		install_web_globals();
	});

	it("collects the whole dunning total, interest and charges included", () => {
		expect(outstanding("Dunning", { grand_total: 1200 })).toBe(1200);
	});

	it("lets an unresolved dunning be paid", () => {
		expect(payable("Dunning", { status: "Unresolved" })).toBe(true);
	});

	it("refuses a resolved dunning, which has already been collected", () => {
		expect(payable("Dunning", { status: "Resolved" })).toBe(false);
	});
});
