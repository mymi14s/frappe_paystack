// The /my-payments portal: each shopper's list holds only their own payments.

describe("Customer payments portal", () => {
	let shop;
	let fixtures;
	let capture;
	let settling_order;
	let open_order;

	before(() => {
		cy.fixture("shop").then((data) => {
			shop = data;
		});

		cy.setup_shop_fixtures().then((data) => {
			fixtures = data;
			expect(fixtures.rival_user).to.eq(shop.rival_user);

			cy.shop_call("capture_for_settlement", shop.capture).then((created) => {
				capture = created;
			});

			cy.shop_call("order_being_paid").then((created) => {
				settling_order = created.order;
			});

			cy.shop_call("order_awaiting_payment").then((created) => {
				open_order = created;
			});
		});
	});

	after(() => {
		cy.teardown_shop_fixtures(fixtures && fixtures.restore);
	});

	beforeEach(() => {
		cy.intercept({ hostname: "api.paystack.co" }, { statusCode: 500 }).as("paystack_api");
	});

	it("lists the shopper's own payment", () => {
		cy.shop_login(shop.buyer_user);
		cy.visit("/my-payments");

		cy.get("table").should("contain", capture.invoice);
		cy.get("table").should("contain", "Processed");
		cy.get("@paystack_api.all").should("have.length", 0);
	});

	it("offers a receipt for a captured payment", () => {
		cy.get(`a[href*="download_payment_receipt"][href*="${capture.log}"]`)
			.should("be.visible")
			.invoke("attr", "href")
			.then((href) => {
				cy.request(href).then((response) => {
					expect(response.status).to.eq(200);
					expect(response.headers["content-type"]).to.contain("application/pdf");
				});
			});
	});

	it("offers no Pay Now for an invoice whose capture is still settling", () => {
		cy.get(`.paystack-payment-state[data-docname="${capture.invoice}"]`)
			.invoke("text")
			.invoke("trim")
			.should("eq", "Processing Payment");

		cy.get(`.pay-now[data-docname="${capture.invoice}"]`).should("not.exist");
	});

	it("keeps the second shopper's payment out of the first shopper's list", () => {
		cy.get("table").should("not.contain", fixtures.rival_invoice);
		cy.get("table").should("not.contain", shop.rival_reference);
	});

	it("shows Processing Payment in place of Pay on an order being settled", () => {
		cy.shop_login(shop.buyer_user);
		cy.visit(`/orders/${settling_order}`);

		cy.get("#pay-for-order").should("not.exist");
		cy.get(".indicator-container .indicator-pill")
			.invoke("text")
			.invoke("trim")
			.should("eq", "Processing Payment");
	});

	it("still offers Pay on an order nobody has paid", () => {
		cy.visit(`/orders/${open_order}`);

		cy.get("#pay-for-order").should("be.visible");
		cy.get(".indicator-container .indicator-pill")
			.invoke("text")
			.invoke("trim")
			.should("eq", "To Deliver and Bill");
	});

	it("shows the second shopper their own payment and nobody else's", () => {
		cy.shop_login(shop.rival_user);
		cy.visit("/my-payments");

		// The second shopper's own payment is listed.
		cy.get("table").should("contain", fixtures.rival_invoice);
		cy.get("table").should("contain", shop.rival_reference);

		cy.get("table").should("not.contain", capture.invoice);
	});

	it("still offers Pay Now on an invoice nobody is paying", () => {
		cy.get(`.pay-now[data-docname="${fixtures.rival_invoice}"]`).should("be.visible");
	});

	it("refuses the second shopper the first shopper's receipt", () => {
		cy.request({
			url: `/api/method/frappe_paystack.utils.portal.download_payment_receipt?reference=${capture.log}`,
			failOnStatusCode: false,
		}).then((response) => {
			expect(response.status, "another customer's receipt is refused").to.eq(403);
		});
	});
});
