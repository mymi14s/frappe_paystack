// The Paystack Activity report script: its filters, default window and severity grading.

import { beforeEach, describe, expect, it } from "vitest";
import { TODAY, install_web_globals, load_activity_report } from "./web_stubs.js";

/** Return the report's filter definition by fieldname. */
function filter(report, fieldname) {
	return report.filters.find((entry) => entry.fieldname === fieldname);
}

/** Format a severity cell and return the markup. */
function grade(report, severity) {
	return report.formatter(
		severity,
		{},
		{ fieldname: "severity" },
		{},
		(value) => String(value)
	);
}

describe("paystack activity report", () => {
	let report;

	beforeEach(() => {
		install_web_globals();
		report = load_activity_report("Acme Ltd");
	});

	it("registers itself under the report name", () => {
		expect(report).toBeTruthy();
	});

	it("leaves the company optional, so an unattributed webhook still shows", () => {
		expect(filter(report, "company").reqd).toBe(0);
	});

	it("defaults the company to the user's own", () => {
		expect(filter(report, "company").default).toBe("Acme Ltd");
	});

	it("requires a window, because the feed is every table at once", () => {
		expect([filter(report, "from_date").reqd, filter(report, "to_date").reqd]).toEqual([
			1, 1,
		]);
	});

	it("opens on the last week", () => {
		expect(filter(report, "from_date").default).toBe(`${TODAY}-7`);
	});

	it("ends the window today", () => {
		expect(filter(report, "to_date").default).toBe(TODAY);
	});

	it("offers every severity the report grades, plus no filter at all", () => {
		expect(filter(report, "severity").options).toEqual(["", "Error", "Warning", "Info"]);
	});

	it("offers each of the four merged sources", () => {
		expect(filter(report, "source").options).toEqual([
			"",
			"Payments",
			"Refunds",
			"Payouts",
			"API Calls",
		]);
	});

	it("shows an error in red", () => {
		expect(grade(report, "Error")).toContain("--red-500");
	});

	it("shows a warning in orange", () => {
		expect(grade(report, "Warning")).toContain("--orange-500");
	});

	it("leaves information unmarked, so the marked rows stand out", () => {
		expect(grade(report, "Info")).toBe("Info");
	});

	it("keeps the formatted value inside the grading", () => {
		expect(grade(report, "Error")).toContain(">Error<");
	});

	// A severity value on a non-severity column; the fieldname decides the grading.
	it("leaves every other column to the default formatter", () => {
		const formatted = report.formatter(
			"Error",
			{},
			{ fieldname: "detail" },
			{},
			() => "Invalid Paystack signature"
		);

		expect(formatted).toBe("Invalid Paystack signature");
	});
});
