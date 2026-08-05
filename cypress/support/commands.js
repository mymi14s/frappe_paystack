// Commands for ERPNext's Point of Sale screen; every selector below belongs to POS itself.

const HELPERS = "frappe_paystack.tests.ui_test_helpers";

Cypress.Commands.add("pos_login", () => {
	cy.login(Cypress.env("posUser"), Cypress.env("posPassword"));
});

// cy.call reads the CSRF token off the window, so a desk page must be open first.
Cypress.Commands.add("setup_pos_fixtures", () => {
	cy.pos_login();
	cy.visit("/app");
	return cy.call(`${HELPERS}.setup_pos_fixtures`).then((response) => response.message);
});

Cypress.Commands.add("teardown_pos_fixtures", (restore) => {
	cy.visit("/app");
	return cy.call(`${HELPERS}.teardown_pos_fixtures`, {
		restore: JSON.stringify(restore || {}),
	});
});

Cypress.Commands.add("pos_payment_logs", (pos_invoice) => {
	return cy
		.call(`${HELPERS}.pos_payment_logs`, { pos_invoice })
		.then((response) => response.message);
});

Cypress.Commands.add("open_pos", () => {
	cy.visit("/app/point-of-sale");
	// The till is already open, so POS goes straight to the item list.
	cy.get(".items-selector .item-wrapper", { timeout: 60000 }).should("exist");
	// POS fills the invoice from the profile before lifting its freeze; a customer set earlier is overwritten.
	cy.window().its("cur_pos.frm.doc.pos_profile", { timeout: 60000 }).should("not.be.empty");
	cy.get("#freeze", { timeout: 60000 }).should("not.exist");
});

// Picks the customer through POS's own selector; its awesomplete list redraws mid-search.
Cypress.Commands.add("pos_set_customer", (customer) => {
	cy.window().then((win) => {
		win.cur_pos.cart.customer_field.set_value(customer);
	});
	cy.window().its("cur_frm.doc.customer").should("eq", customer);
	cy.get(".customer-details").should("contain", customer);
});

// Searches as a cashier does, then rings up through the tile's own handler; the list redraws mid-search.
Cypress.Commands.add("pos_add_item", (item_code) => {
	cy.get(".items-selector .search-field input").clear().type(item_code);
	cy.get(`.item-wrapper[data-item-code="${item_code}"]`).then(($tile) => {
		cy.window().then((win) => {
			return win.cur_pos.on_cart_update({
				field: "qty",
				value: "+1",
				item: {
					item_code,
					uom: $tile.attr("data-uom"),
					rate: $tile.attr("data-rate"),
					stock_uom: $tile.attr("data-stock-uom"),
				},
			});
		});
	});
	cy.get(".cart-items-section .cart-item-wrapper").should("have.length.at.least", 1);
});

Cypress.Commands.add("pos_checkout", () => {
	cy.get(".checkout-btn:visible").click();
	cy.get(".payment-container").should("be.visible");
	cy.get(".invoice-fields [data-fieldname='request_for_payment']").should("exist");
});

// Sets a tender through POS's own payment control, the path its number pad uses.
Cypress.Commands.add("pos_set_tender", (mode, amount) => {
	cy.get(`.mode-of-payment[data-mode="${mode}"]`).should("exist");
	cy.window().then((win) => {
		const control = win.cur_pos.payment[`${mode}_control`];
		expect(control, `${mode} tender control`).to.exist;
		control.set_value(amount);
	});
	cy.pos_tender_should_be(mode, amount);
});

// The tender is written by a chain of model updates, so the check has to be a retrying one.
Cypress.Commands.add("pos_tender_should_be", (mode, amount) => {
	cy.window().should((win) => {
		const row = (win.cur_frm.doc.payments || []).find(
			(entry) => entry.mode_of_payment.toLowerCase() === mode.toLowerCase()
		);
		expect(row, `${mode} tender row`).to.exist;
		expect(row.amount, `${mode} tender`).to.eq(amount);
	});
});

Cypress.Commands.add("pos_request_button", () => {
	return cy.get(".invoice-fields [data-fieldname='request_for_payment'] button");
});

Cypress.Commands.add("pos_invoice_field", (fieldname) => {
	return cy.get(`.invoice-fields [data-fieldname='${fieldname}']`);
});

Cypress.Commands.add("pos_invoice_name", () => {
	return cy.window().its("cur_frm.doc.name");
});

// POS runs the button through the script manager, so a refusal is triggered the same way.
// frappe.throw shows its message and then rethrows, so the rethrow is swallowed here.
Cypress.Commands.add("pos_trigger_request_for_payment", () => {
	return cy.window().then((win) => {
		const frm = win.cur_frm;
		return Promise.resolve(
			frm.script_manager.trigger("request_for_payment", frm.doc.doctype, frm.doc.name)
		).catch(() => null);
	});
});

// Writes the address through the control POS built for the field.
Cypress.Commands.add("pos_set_email", (email) => {
	cy.window().then((win) => {
		win.cur_pos.payment.contact_email_field.set_value(email);
	});
	cy.window().its("cur_frm.doc.contact_email").should("eq", email);
});

// Commands for the Webshop storefront, the customer portal and the Paystack webhook;
// the storefront selectors belong to webshop itself.

const WEBHOOK = "frappe_paystack.api.paystack_webhook";

// The one account whose password the specs know, used only for the fixture endpoints.
Cypress.Commands.add("fixture_login", () => {
	cy.login(Cypress.env("posUser"), Cypress.env("posPassword"));
});

// cy.call reads the CSRF token off the window, so a page must be open first.
Cypress.Commands.add("setup_shop_fixtures", () => {
	cy.fixture_login();
	cy.visit("/app");
	return cy.call(`${HELPERS}.setup_shop_fixtures`).then((res) => res.message);
});

Cypress.Commands.add("teardown_shop_fixtures", (restore) => {
	cy.fixture_login();
	cy.visit("/app");
	return cy.call(`${HELPERS}.teardown_shop_fixtures`, {
		restore: JSON.stringify(restore || {}),
	});
});

Cypress.Commands.add("shop_call", (method, args) => {
	return cy.call(`${HELPERS}.${method}`, args).then((res) => res.message);
});

// Signs a shopper in through the login API; the fixtures recreate these users between specs.
Cypress.Commands.add("shop_login", (user) => {
	cy.request({
		method: "POST",
		url: "/api/method/login",
		body: { usr: user, pwd: Cypress.env("shopPassword") },
	});
});

// The signature covers the exact bytes of the body, so one serialised string is signed and sent.
// Cypress attaches the signed-in session cookie, so the CSRF token has to go with it.
Cypress.Commands.add("paystack_webhook", (event) => {
	const payload = JSON.stringify(event);

	return cy.window().then((win) => {
		const csrf_token = win.frappe && win.frappe.csrf_token;

		return cy.shop_call("sign_webhook", { payload }).then((signature) => {
			return cy.request({
				method: "POST",
				url: `/api/method/${WEBHOOK}`,
				body: payload,
				headers: {
					"Content-Type": "application/json",
					"x-paystack-signature": signature,
					"X-Frappe-CSRF-Token": csrf_token,
				},
				failOnStatusCode: false,
			});
		});
	});
});

Cypress.Commands.add("shop_payment_logs", (linked_doctype, linked_docname) => {
	return cy.shop_call("shop_payment_logs", { linked_doctype, linked_docname });
});

// Adds an item to the cart; the button swaps itself for the cart link once the post lands.
Cypress.Commands.add("shop_add_to_cart", (route) => {
	cy.visit(`/${route}`);
	cy.get(".btn-add-to-cart:visible").click();
	cy.get(".btn-view-in-cart:visible", { timeout: 60000 }).should("exist");
});

// Closes the cart; webshop submits the quotation, raises the Sales Order and redirects to it.
Cypress.Commands.add("shop_place_order", () => {
	cy.visit("/cart");
	cy.get(".btn-place-order:visible").click();
	cy.location("pathname", { timeout: 90000 }).should("match", /^\/orders\//);
	return cy.location("pathname").then((path) => path.split("/").pop());
});

// Presses Pay: a plain link into make_payment_request, which redirects to the checkout URL.
Cypress.Commands.add("shop_pay_for_order", () => {
	cy.get("#pay-for-order").should("be.visible").click();
	cy.location("pathname", { timeout: 90000 }).should(
		"match",
		/^\/paystack-checkout\//
	);
	return cy.location("pathname").then((path) => path.split("/").pop());
});

// The checkout payload the page mounted Vue against.
Cypress.Commands.add("checkout_payload", () => {
	return cy
		.get("#paystack-checkout-data")
		.invoke("text")
		.then((text) => JSON.parse(text));
});

// Opens the Unsettled Payments report and yields the element, which cypress re-queries while an
// assertion retries. Every caller asserts on a row it expects to be there.
Cypress.Commands.add("unsettled_report", (company) => {
	cy.visit("/app/query-report/Paystack Unsettled Payments");
	cy.get(".datatable", { timeout: 90000 }).should("exist");

	cy.window().then((win) => {
		win.frappe.query_report.set_filter_value("company", company);
	});

	return cy.get(".datatable .dt-scrollable", { timeout: 90000 });
});

// What a field shows on the open desk form, scoped to the form control.
// A read-only field is static text and an empty one is absent, so all three shapes are read here.
Cypress.Commands.add("form_field_value", (fieldname) => {
	return cy.get(".form-layout").then(($layout) => {
		const $control = $layout
			.find(`.frappe-control[data-fieldname="${fieldname}"]`)
			.first();

		if (!$control.length) {
			return "";
		}

		const $input = $control.find("input, select, textarea");

		return $input.length
			? String($input.val() || "")
			: $control.find(".control-value").text().trim();
	});
});

// Fills a Paystack payload fixture in with the values that change per run. The merge keeps
// Paystack's full field list and writes into a copy, since cy.fixture shares one object per spec.
function fill_payload(shape, values) {
	const filled = Array.isArray(shape) ? shape.slice() : Object.assign({}, shape);

	Object.keys(values || {}).forEach((key) => {
		const value = values[key];
		const is_object = value && typeof value === "object" && !Array.isArray(value);

		filled[key] = is_object ? fill_payload(shape[key] || {}, value) : value;
	});

	return filled;
}

Cypress.Commands.add("paystack_payload", (name, values) => {
	return cy.fixture(name).then((shape) => fill_payload(shape, values));
});
