// The Reconciliation Log form: how an operator resolves a mismatch.

import { beforeEach, describe, expect, it } from "vitest";
import {
	flush,
	install_web_globals,
	make_reconciliation_log_frm,
	recorded,
	replies,
} from "./web_stubs.js";

const MARK_MANUAL_OVERRIDE = "frappe_paystack.utils.reconciliation_api.mark_manual_override";

/** Refresh a Reconciliation Log and return the form it ran against. */
function refresh(doc) {
	const { events, frm } = make_reconciliation_log_frm(doc);
	events.refresh(frm);
	return frm;
}

/** Open the resolve dialog on a mismatched reconciliation. */
function open_dialog() {
	const frm = refresh({ status: "Mismatch" });
	frm.buttons["Resolve Mismatch"]();
	return { dialog: recorded.dialogs.at(-1), frm };
}

describe("the resolve action", () => {
	beforeEach(() => {
		install_web_globals();
	});

	it("is offered on a mismatch", () => {
		expect(Object.keys(refresh({ status: "Mismatch" }).buttons)).toEqual([
			"Resolve Mismatch",
		]);
	});

	it("is not offered on a reconciled payment", () => {
		expect(refresh({ status: "Reconciled" }).buttons).toEqual({});
	});

	it("is not offered on a reconciliation already overridden", () => {
		expect(refresh({ status: "Manual Override" }).buttons).toEqual({});
	});

	it("asks for the note the override needs", () => {
		const { dialog } = open_dialog();

		expect(dialog.shown).toBe(true);
		expect(dialog.fields_dict.notes.df.reqd).toBe(1);
	});

	it("sends the note against the payment the reconciliation names", async () => {
		replies[MARK_MANUAL_OVERRIDE] = { payment_log: "PSLOG-9", status: "Manual Override" };
		const { dialog } = open_dialog();

		dialog.submit({ notes: "Matched against the bank statement" });
		await flush();

		expect(frappe.call).toHaveBeenCalledWith({
			method: MARK_MANUAL_OVERRIDE,
			args: {
				payment_log_name: "PSLOG-1",
				notes: "Matched against the bank statement",
			},
		});
	});

	it("reloads the form once the override is written", async () => {
		replies[MARK_MANUAL_OVERRIDE] = { payment_log: "PSLOG-1", status: "Manual Override" };
		const { dialog, frm } = open_dialog();

		dialog.submit({ notes: "Cleared by hand" });
		await flush();

		expect(dialog.hidden).toBe(true);
		expect(frm.reload_doc).toHaveBeenCalled();
	});

	it("leaves the dialog open when nothing came back", async () => {
		const { dialog, frm } = open_dialog();

		dialog.submit({ notes: "Cleared by hand" });
		await flush();

		expect(dialog.hidden).toBe(false);
		expect(frm.reload_doc).not.toHaveBeenCalled();
	});

	it("refuses an empty note without calling the server", () => {
		const { dialog } = open_dialog();

		expect(() => dialog.submit({ notes: "" })).toThrow(
			"A note explaining the override is required."
		);
		expect(frappe.call).not.toHaveBeenCalled();
	});
});
