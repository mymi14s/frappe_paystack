// The Paystack Unsettled Payments report script: its filters and the days-waiting grading.

import { beforeEach, describe, expect, it } from "vitest";
import { install_web_globals, load_unsettled_report } from "./web_stubs.js";

/** Return the report's filter definition by fieldname. */
function filter(report, fieldname) {
	return report.filters.find((entry) => entry.fieldname === fieldname);
}

/** Format a days_waiting cell and return the markup. */
function grade(report, days) {
	return report.formatter(
		days,
		{},
		{ fieldname: "days_waiting" },
		{},
		(value) => String(value)
	);
}

describe("unsettled payments report", () => {
	let report;

	beforeEach(() => {
		install_web_globals();
		report = load_unsettled_report("Acme Ltd");
	});

	it("registers itself under the report name", () => {
		expect(report).toBeTruthy();
	});

	it("requires a company, because suspense is held per company", () => {
		expect(filter(report, "company").reqd).toBe(1);
		expect(filter(report, "company").options).toBe("Company");
	});

	it("defaults the company to the user's own", () => {
		expect(filter(report, "company").default).toBe("Acme Ltd");
	});

	it("offers an optional cut-off date", () => {
		expect(filter(report, "to_date").fieldtype).toBe("Date");
		expect(filter(report, "to_date").reqd).toBe(0);
	});

	it("leaves every other column to the default formatter", () => {
		const formatted = report.formatter(
			1000,
			{},
			{ fieldname: "amount_paid" },
			{},
			() => "NGN 1000"
		);

		expect(formatted).toBe("NGN 1000");
	});

	it("shows a week or more in suspense as red", () => {
		expect(grade(report, 7)).toContain("--red-500");
	});

	it("shows a few days as orange", () => {
		expect(grade(report, 3)).toContain("--orange-500");
	});

	it("shows a fresh capture as green", () => {
		expect(grade(report, 2)).toContain("--green-500");
	});

	it("keeps the formatted value inside the grading", () => {
		expect(grade(report, 9)).toContain(">9<");
	});
});
