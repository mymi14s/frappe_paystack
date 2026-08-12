// The POS payment step for a Paystack tender: label, address field and sent link.

const SEND_LINK_METHOD = "frappe_paystack.utils.pos_payment.send_pos_payment_link";
const STATUS_METHOD = "frappe_paystack.utils.pos_payment.pos_payment_status";
const PAYSTACK = "paystack";
const CASH = "cash";

describe("POS Paystack payment link", () => {
	let fixtures;

	before(() => {
		cy.setup_pos_fixtures().then((data) => {
			fixtures = data;
		});
	});

	after(() => {
		cy.teardown_pos_fixtures(fixtures && fixtures.restore);
	});

	beforeEach(() => {
		// Answers the gateway host and the poll that follows a sent link.
		cy.intercept({ hostname: "api.paystack.co" }, { statusCode: 500 }).as("paystack_api");
		cy.intercept("POST", `/api/method/${STATUS_METHOD}`, { body: { message: null } });

		cy.open_pos();
		cy.pos_set_customer(fixtures.customer);
		cy.pos_add_item(fixtures.item);
		cy.pos_checkout();
		cy.pos_set_tender(CASH, 0);
		cy.pos_set_tender(PAYSTACK, fixtures.rate);
	});

	it("labels the button Send Payment Link", () => {
		cy.pos_request_button().should("have.text", "Send Payment Link");
	});

	it("asks for an email address and hides the mobile number", () => {
		cy.pos_invoice_field("contact_email").should("be.visible");
		cy.pos_invoice_field("contact_mobile").should("not.be.visible");
	});

	it("prefills the email address from the customer", () => {
		cy.window().its("cur_frm.doc.contact_email").should("eq", fixtures.customer_email);
		cy.pos_invoice_field("contact_email")
			.find("input")
			.should("have.value", fixtures.customer_email);
	});

	it("never overwrites an address the cashier typed", () => {
		const typed = "till.override@example.com";

		cy.pos_set_email(typed);
		cy.window().its("cur_frm.doc.contact_email").should("eq", typed);

		// Returning to the cart re-renders the fields and runs the prefill again.
		cy.get(".edit-cart-btn:visible").click();
		cy.pos_checkout();

		cy.window().its("cur_frm.doc.contact_email").should("eq", typed);
		cy.pos_invoice_field("contact_email").find("input").should("have.value", typed);
	});

	it("refuses to send without an email address", () => {
		cy.pos_set_email("");
		cy.pos_request_button().should("be.disabled");

		cy.pos_trigger_request_for_payment();
		cy.get(".modal:visible .msgprint").should("contain", "Enter a valid email address first.");
		cy.get(".modal:visible .msgprint").should("not.contain", "Please enter mobile number first.");
	});

	it("refuses to send to an address that is not one", () => {
		cy.pos_set_email("not-an-address");
		cy.pos_request_button().should("be.disabled");

		cy.pos_trigger_request_for_payment();
		cy.get(".modal:visible .msgprint").should("contain", "Enter a valid email address first.");
		cy.get(".modal:visible .msgprint").should("not.contain", "Please enter mobile number first.");
	});

	it("sends the link, logs it against the draft invoice and says so", () => {
		cy.intercept("POST", `/api/method/${SEND_LINK_METHOD}`).as("send_link");

		cy.pos_invoice_name().then((pos_invoice) => {
			cy.pos_request_button().should("be.enabled").click();
			cy.wait("@send_link").then(({ response }) => {
				expect(response.statusCode).to.eq(200);
				expect(response.body.message.email).to.eq(fixtures.customer_email);
			});

			cy.get_doc("POS Invoice", pos_invoice).then(({ data }) => {
				expect(data.docstatus, "invoice is still a draft").to.eq(0);
			});

			cy.pos_payment_logs(pos_invoice).then((logs) => {
				expect(logs).to.have.length(1);
				expect(logs[0].amount).to.eq(fixtures.rate);
				expect(logs[0].status).to.eq("Pending");
				// Nothing is paid yet, so the log carries no reference.
				expect(logs[0].payment_reference || "").to.eq("");
			});
		});

		cy.get("#alert-container .alert-message").should("contain", "Payment link sent");
		cy.get("@paystack_api.all").should("have.length", 0);
	});
});
