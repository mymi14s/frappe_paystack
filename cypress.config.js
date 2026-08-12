// Specs live in cypress/integration; bench installs cypress under apps/frappe.
module.exports = {
	defaultCommandTimeout: 20000,
	pageLoadTimeout: 30000,
	video: false,
	viewportHeight: 960,
	viewportWidth: 1400,
	retries: {
		runMode: 1,
		openMode: 0,
	},
	// The cashier the specs sign in as.
	env: {
		posUser: "test.paystack.pos@example.com",
		posPassword: "paystack-ui-test-passphrase",
		// The webshop shoppers and portal users.
		shopPassword: "paystack-ui-test-passphrase",
	},
	e2e: {
		testIsolation: false,
		baseUrl: "http://localhost:8000",
		specPattern: "./cypress/integration/*.js",
		supportFile: "./cypress/support/e2e.js",
	},
};
