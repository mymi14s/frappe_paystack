// The Paystack Settlements vs Ledger report script: its filters and the cells it marks.

import { beforeEach, describe, expect, it } from "vitest";
import { install_web_globals, load_settlements_report } from "./web_stubs.js";

/** Return the report's filter definition by fieldname. */
function filter(report, fieldname) {
	return report.filters.find((entry) => entry.fieldname === fieldname);
}

/** Format one cell and return the markup. */
function cell(report, fieldname, value) {
	return report.formatter(
		value,
		{},
		{ fieldname },
		{},
		(formatted) => String(formatted)
	);
}

describe("settlements vs ledger report", () => {
	let report;

	beforeEach(() => {
		install_web_globals();
		report = load_settlements_report("Acme Ltd");
	});

	it("registers itself under the report name", () => {
		expect(report).toBeTruthy();
	});

	it("requires a company, because a payout belongs to one tenant", () => {
		expect(filter(report, "company").reqd).toBe(1);
		expect(filter(report, "company").options).toBe("Company");
	});

	it("defaults the company to the user's own", () => {
		expect(filter(report, "company").default).toBe("Acme Ltd");
	});

	it("offers an optional payout date range", () => {
		expect(filter(report, "from_date").fieldtype).toBe("Date");
		expect(filter(report, "from_date").reqd).toBe(0);
		expect(filter(report, "to_date").fieldtype).toBe("Date");
		expect(filter(report, "to_date").reqd).toBe(0);
	});

	it("offers to hide the payouts that tie out, and shows them by default", () => {
		expect(filter(report, "only_discrepancies").fieldtype).toBe("Check");
		expect(filter(report, "only_discrepancies").default).toBe(0);
	});

	it("leaves a column that reconciles nothing to the default formatter", () => {
		expect(cell(report, "gross_amount", 1000)).toBe("1000");
	});

	it("marks a bank difference the books do not explain", () => {
		expect(cell(report, "difference", -85)).toContain("--red-500");
	});

	it("marks gross that no capture accounts for", () => {
		expect(cell(report, "unlinked_amount", 600)).toContain("--red-500");
	});

	it("marks the sentence saying why a payout does not tie out", () => {
		expect(cell(report, "discrepancy", "Paystack net 985, ledger 900")).toContain(
			"--red-500"
		);
	});

	it("leaves a payout that ties out unmarked", () => {
		expect(cell(report, "difference", 0)).toBe("0");
		expect(cell(report, "discrepancy", "")).toBe("");
	});

	it("keeps the formatted value inside the marking", () => {
		expect(cell(report, "unlinked_amount", 600)).toContain(">600<");
	});
});
