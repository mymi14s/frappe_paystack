// What the till does while it waits for payment, and once Paystack reports it.

const SEND_LINK_METHOD = "frappe_paystack.utils.pos_payment.send_pos_payment_link";
const STATUS_METHOD = "frappe_paystack.utils.pos_payment.pos_payment_status";
const PAYSTACK = "paystack";
const CASH = "cash";
const POLL_MS = 5000;
const POLL_GIVE_UP_MS = 605000;

describe("POS Paystack settlement", () => {
	let fixtures;
	// What the stubbed poll reports back.
	let settlement;

	before(() => {
		cy.setup_pos_fixtures().then((data) => {
			fixtures = data;
		});
	});

	after(() => {
		cy.teardown_pos_fixtures(fixtures && fixtures.restore);
	});

	beforeEach(() => {
		settlement = null;

		cy.intercept({ hostname: "api.paystack.co" }, { statusCode: 500 }).as("paystack_api");
		cy.intercept("POST", `/api/method/${STATUS_METHOD}`, (req) => {
			req.reply({ body: { message: settlement } });
		}).as("payment_status");
		cy.intercept("POST", `/api/method/${SEND_LINK_METHOD}`).as("send_link");

		cy.open_pos();
		cy.pos_set_customer(fixtures.customer);
		cy.pos_add_item(fixtures.item);
		cy.pos_checkout();
		cy.pos_set_tender(CASH, 0);
		cy.pos_set_tender(PAYSTACK, fixtures.rate);
	});

	// Sends the link and yields the sale the till is now waiting on.
	function send_link() {
		return cy.pos_invoice_name().then((pos_invoice) => {
			cy.pos_request_button().should("be.enabled").click();
			cy.wait("@send_link");
			return cy.wrap(pos_invoice);
		});
	}

	function report(pos_invoice, amount_paid) {
		const outstanding = fixtures.rate - amount_paid;
		settlement = {
			pos_invoice,
			log: null,
			status: outstanding > 0 ? "Processed" : "Completed",
			amount_paid,
			outstanding: outstanding > 0 ? outstanding : 0,
			fully_paid: outstanding <= 0,
		};
	}

	it("waits for the payment, then books what was paid and closes the sale", () => {
		send_link().then((pos_invoice) => {
			cy.get("#freeze .freeze-message").should("contain", "Waiting for payment");
			report(pos_invoice, fixtures.rate);

			cy.get("#freeze", { timeout: 60000 }).should("not.exist");
			cy.get("#alert-container .alert-message").should("contain", "Payment received in full");
			cy.pos_tender_should_be(PAYSTACK, fixtures.rate);

			// Complete Order goes through the form's submit, which asks for confirmation.
			cy.get(".modal:visible").contains("button", "Yes").click();

			cy.get(".past-order-summary .invoice-summary-wrapper", { timeout: 60000 }).should(
				"be.visible"
			);
			cy.get_doc("POS Invoice", pos_invoice).then(({ data }) => {
				expect(data.docstatus, "sale is closed").to.eq(1);
				expect(data.paid_amount).to.eq(fixtures.rate);
			});
		});
	});

	it("reports a part payment without completing the order", () => {
		const paid = 40;

		send_link().then((pos_invoice) => {
			report(pos_invoice, paid);

			cy.get("#freeze", { timeout: 60000 }).should("not.exist");
			cy.get("#alert-container .alert-message").should("contain", "Part payment received");
			cy.get("#alert-container .alert-message").should(
				"contain",
				`${fixtures.rate - paid}`
			);

			// The tender carries what Paystack collected.
			cy.pos_tender_should_be(PAYSTACK, paid);
			cy.get(".payment-container").should("be.visible");
			cy.get(".past-order-summary .invoice-summary-wrapper").should("not.be.visible");
			cy.get_doc("POS Invoice", pos_invoice).then(({ data }) => {
				expect(data.docstatus, "sale is still open").to.eq(0);
			});
		});
	});

	it("gives up on an unpaid link instead of freezing the till", () => {
		// cy.tick drives the poll's timer.
		cy.clock(Date.now(), ["setInterval", "clearInterval"]);

		send_link().then((pos_invoice) => {
			report(pos_invoice, 0);

			cy.get("#freeze .freeze-message").should("contain", "Waiting for payment");
			cy.tick(POLL_MS);
			cy.get("@payment_status.all").should("have.length.at.least", 1);

			cy.tick(POLL_GIVE_UP_MS);
			cy.get(".modal:visible .msgprint").should("contain", "Still waiting for payment");
			cy.get("#freeze").should("not.exist");
			cy.pos_tender_should_be(PAYSTACK, fixtures.rate);
		});
	});
});
