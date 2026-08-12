// The desk commands the specs use - cy.login, cy.call, cy.get_doc - come from frappe.
import "../../../frappe/cypress/support/commands";
import "./commands";

// The desk fires unrelated background calls.
Cypress.on("uncaught:exception", () => false);
