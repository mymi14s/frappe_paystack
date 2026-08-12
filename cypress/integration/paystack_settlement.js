// Paystack payouts: a signed webhook in, the report and the payout record out.

const MINOR_UNITS = 100;

describe("Paystack settlement", () => {
	let shop;
	let fixtures;
	let baseline_api_calls;
	let settled_capture;
	let refused_capture;

	before(() => {
		cy.fixture("shop").then((data) => {
			shop = data;
		});

		cy.setup_shop_fixtures().then((data) => {
			fixtures = data;
			expect(fixtures.company).to.eq(shop.company);
			expect(fixtures.currency).to.eq(shop.currency);

			cy.shop_call("paystack_api_calls").then((count) => {
				baseline_api_calls = count;
			});
			cy.shop_call("capture_for_settlement", shop.capture).then((created) => {
				settled_capture = created;
			});
			cy.shop_call("capture_for_settlement", shop.second_capture).then((created) => {
				refused_capture = created;
			});
		});
	});

	after(() => {
		cy.teardown_shop_fixtures(fixtures && fixtures.restore);
	});

	beforeEach(() => {
		cy.intercept({ hostname: "api.paystack.co" }, { statusCode: 500 }).as("paystack_api");
		cy.fixture_login();
	});

	it("lists both captures while their money is still in suspense", () => {
		cy.unsettled_report(shop.company)
			.should("contain", settled_capture.log)
			.and("contain", refused_capture.log);
	});

	it("records a payout it cannot book yet, and says why", () => {
		payout(settled_id(), shop.capture).then((event) => {
			cy.paystack_webhook(event).then((response) => {
				expect(response.status, "Paystack is acknowledged").to.eq(200);
			});
		});

		cy.visit(`/app/paystack-settlement/${settled_id()}`);
		cy.get(".title-text", { timeout: 60000 }).should("contain", settled_id());

		cy.form_field_value("status").should("eq", "Pending");
		cy.form_field_value("journal_entry").should("eq", "");
		cy.form_field_value("errors").should(
			"contain",
			"before this payout can be booked"
		);
	});

	it("leaves the captures on the report while the payout is unbooked", () => {
		cy.unsettled_report(shop.company).should("contain", settled_capture.log);
	});

	it("books the payout once the gateway names its accounts", () => {
		cy.shop_call("complete_gateway_accounts");
		cy.shop_call("post_shop_settlement", { settlement: settled_id() }).then((entry) => {
			expect(entry, "payout reached the ledger").to.not.be.empty;
		});

		cy.visit(`/app/paystack-settlement/${settled_id()}`);
		cy.get(".title-text", { timeout: 60000 }).should("contain", settled_id());

		cy.form_field_value("status").should("eq", "Processed");
		cy.form_field_value("journal_entry").should("not.eq", "");
		cy.form_field_value("errors").should("eq", "");
	});

	it("takes the settled captures off the report", () => {
		cy.shop_call("claim_captures", {
			settlement: settled_id(),
			logs: JSON.stringify([settled_capture.log]),
		});

		cy.unsettled_report(shop.company)
			.should("contain", refused_capture.log)
			.and("not.contain", settled_capture.log);
	});

	it("refuses a payout whose totals do not add up", () => {
		// A net of gross less fees only.
		payout(refused_id(), shop.second_capture, shop.settlement.deductions).then((event) => {
			event.data.effective_amount =
				event.data.total_amount - event.data.total_fees;

			cy.paystack_webhook(event).then((response) => {
				expect(response.status, "Paystack is still acknowledged").to.eq(200);
			});
		});

		cy.visit(`/app/paystack-settlement/${refused_id()}`);
		cy.get(".title-text", { timeout: 60000 }).should("contain", refused_id());

		cy.form_field_value("status").should("eq", "Pending");
		cy.form_field_value("journal_entry").should("eq", "");
		cy.form_field_value("errors").should("contain", "which is not the");
	});

	it("keeps a refused payout's captures on the report", () => {
		// A payout still off the ledger has cleared nothing.
		cy.unsettled_report(shop.company).should("contain", refused_capture.log);
	});

	it("never let anything reach Paystack", () => {
		cy.shop_call("paystack_api_calls").then((count) => {
			expect(count, "no server-side Paystack calls").to.eq(baseline_api_calls);
		});
	});

	function settled_id() {
		return shop.settlement.settled_id;
	}

	function refused_id() {
		return shop.settlement.refused_id;
	}

	// A payout for one capture, on Paystack's own payload shape.
	function payout(settlement_id, capture, deductions) {
		const now = new Date().toISOString();
		const gross = minor(capture.amount);
		const fees = minor(capture.fee);
		const held = minor(deductions || 0);

		return cy.paystack_payload("settlement_success", {
			data: {
				id: settlement_id,
				currency: shop.currency,
				total_amount: gross,
				total_fees: fees,
				total_processed: gross,
				deductions: held,
				effective_amount: gross - fees - held,
				settlement_date: now.slice(0, 10),
				createdAt: now,
				updatedAt: now,
			},
		});
	}

	function minor(amount) {
		return Math.round(amount * MINOR_UNITS);
	}
});
