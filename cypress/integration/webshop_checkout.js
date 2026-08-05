// The shopper's whole journey: cart, order, Pay, checkout page, signed charge.success.

// Paystack charges in minor units.
const MINOR_UNITS = 100;

describe("Webshop Paystack checkout", () => {
	let shop;
	let fixtures;
	let baseline_api_calls;
	let sales_order;
	let payment_log;

	before(() => {
		cy.fixture("shop").then((data) => {
			shop = data;
		});

		cy.setup_shop_fixtures().then((data) => {
			fixtures = data;

			// The fixtures and this spec describe the same shop.
			expect(fixtures.item).to.eq(shop.item);
			expect(fixtures.rate).to.eq(shop.rate);
			expect(fixtures.currency).to.eq(shop.currency);
			expect(fixtures.customer).to.eq(shop.customer);
			expect(fixtures.buyer_user).to.eq(shop.buyer_user);

			cy.shop_call("paystack_api_calls").then((count) => {
				baseline_api_calls = count;
			});
		});
	});

	after(() => {
		cy.teardown_shop_fixtures(fixtures && fixtures.restore);
	});

	beforeEach(() => {
		// Paystack's API and the script the checkout page pulls in are both answered here.
		cy.intercept({ hostname: "api.paystack.co" }, { statusCode: 500 }).as("paystack_api");
		cy.intercept({ hostname: "js.paystack.co" }, { body: "" }).as("paystack_js");
	});

	it("puts the shopper's item in the cart", () => {
		cy.shop_login(shop.buyer_user);
		cy.shop_add_to_cart(fixtures.item_route);

		cy.visit("/cart");
		cy.get(`.cart-items .cart-qty[data-item-code="${shop.item}"]`).should(
			"have.value",
			"1"
		);
	});

	it("places the order the cart was filled for", () => {
		cy.shop_place_order().then((order) => {
			sales_order = order;
		});

		cy.get("#pay-for-order").should("be.visible");
	});

	it("sends the shopper to a checkout page backed by exactly one payment log", () => {
		cy.shop_pay_for_order().then((reference) => {
			payment_log = reference;

			// ERPNext asks the gateway for the checkout URL twice per press, and both share one log.
			cy.shop_payment_logs("Sales Order", sales_order).then((logs) => {
				expect(logs, "one payment log per order").to.have.length(1);
				expect(logs[0].name).to.eq(reference);
				expect(logs[0].status).to.eq("Pending");
				expect(logs[0].amount).to.eq(shop.rate);
				expect(logs[0].payment_request, "log settles through a Payment Request").to
					.not.be.empty;
			});
		});

		// The checkout page has loaded, so browser calls to Paystack are recorded by now.
		cy.get("@paystack_api.all").should("have.length", 0);
	});

	it("renders the payment card rather than a blank page", () => {
		// The log survives Frappe's GET rollback.
		cy.get(".ps-card--message").should("not.exist");

		// v-cloak hides the card until Vue mounts, so visible interpolated text means it mounted.
		cy.get(".ps-card").should("be.visible");
		cy.get(".ps-amount").should("not.be.empty");
		cy.get(".ps-actions .ps-button").should("be.visible");
	});

	it("shows the amount, currency and reference the order was billed for", () => {
		cy.checkout_payload().then((payload) => {
			expect(payload.reference).to.eq(payment_log);
			expect(payload.order_no).to.eq(sales_order);
			expect(payload.currency).to.eq(shop.currency);
			expect(payload.payment_amount).to.eq(shop.rate);
			expect(payload.is_payable).to.be.true;
		});

		cy.get(".ps-amount").should("contain", String(shop.rate));
		cy.get(".ps-details code").should("have.text", payment_log);
		cy.get(".ps-details").should("contain", sales_order);
		cy.get(".ps-details").should("contain", shop.customer);
	});

	it("bills the order when a signed charge.success arrives", () => {
		charge_success().then((payload) => {
			cy.paystack_webhook(payload).then((response) => {
				expect(response.status, "Paystack is acknowledged").to.eq(200);
			});
		});

		cy.shop_payment_logs("Sales Order", sales_order).then((logs) => {
			expect(logs).to.have.length(1);
			expect(logs[0].status).to.eq("Completed");
			expect(logs[0].amount_paid).to.eq(shop.rate);
			expect(logs[0].payment_entry, "capture booked a Payment Entry").to.not.be.empty;
		});

		// PaymentRequest.set_as_paid() bills the order, so the invoice is the proof.
		cy.shop_call("sales_order_billing", { sales_order }).then((billing) => {
			expect(billing.per_billed, "order is fully billed").to.eq(100);
			expect(billing.invoice, "paid order raised a Sales Invoice").to.not.be.empty;
		});
	});

	it("refuses a signature that is not Paystack's", () => {
		charge_success().then((payload) => {
			payload.data.id = 424242;
			payload.data.reference = "psref-forged";

			cy.request({
				method: "POST",
				url: "/api/method/frappe_paystack.api.paystack_webhook",
				body: JSON.stringify(payload),
				headers: {
					"Content-Type": "application/json",
					"x-paystack-signature": "0".repeat(128),
				},
				failOnStatusCode: false,
			}).then((response) => {
				expect(response.status, "forged signature is rejected").to.eq(403);
			});
		});
	});

	it("shows the settled payment in the shopper's portal", () => {
		cy.shop_login(shop.buyer_user);
		cy.visit("/my-payments");

		cy.get("table").should("contain", payment_log_reference());
		cy.get("table").should("contain", sales_order);
		cy.get("table").should("contain", "Completed");
	});

	it("never let anything reach Paystack", () => {
		// The site's own record of its Paystack requests is checked here.
		cy.shop_call("paystack_api_calls").then((count) => {
			expect(count, "no server-side Paystack calls").to.eq(baseline_api_calls);
		});
	});

	it("closes a checkout link whose validity window has passed", () => {
		// A second trip through the shop: the first order's link is already paid.
		cy.shop_login(shop.buyer_user);
		cy.shop_add_to_cart(fixtures.item_route);
		cy.shop_place_order();
		cy.shop_pay_for_order().then((reference) => {
			cy.shop_call("expire_payment_log", { log: reference });
			cy.reload();
		});

		cy.get(".ps-card").should("be.visible");
		cy.get(".ps-actions .ps-closed").should("contain", "expired");
		cy.get(".ps-actions .ps-button").should("not.exist");

		cy.checkout_payload().then((payload) => {
			expect(payload.is_expired).to.be.true;
			expect(payload.is_payable).to.be.false;
		});

		cy.get("@paystack_api.all").should("have.length", 0);
	});

	function payment_log_reference() {
		return `psref-${payment_log}`;
	}

	// The charge for this order on Paystack's own payload shape; metadata.reference is the Payment Log name.
	function charge_success() {
		const now = new Date().toISOString();
		const minor = shop.rate * MINOR_UNITS;

		return cy.paystack_payload("charge_success", {
			data: {
				reference: payment_log_reference(),
				amount: minor,
				requested_amount: minor,
				currency: shop.currency,
				paid_at: now,
				paidAt: now,
				created_at: now,
				createdAt: now,
				customer: { email: shop.buyer_user },
				metadata: {
					reference: payment_log,
					reference_doctype: "Sales Order",
					reference_docname: sales_order,
					customer: shop.customer,
					email: shop.buyer_user,
				},
			},
		});
	}
});

