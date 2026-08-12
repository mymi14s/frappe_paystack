// The Webshop cart guard: the Pay button may be clicked once and no more.

import { beforeEach, describe, expect, it } from "vitest";
import { install_web_globals, load_cart_guard } from "./web_stubs.js";

// The guard reads the click alone.
const CART = '<a id="pay-for-order">Pay</a>';

/** Run the guard against a cart, returning the Pay button it bound to. */
function arm(markup = CART) {
	const ready = load_cart_guard(markup);
	ready();
	return document.getElementById("pay-for-order");
}

/** Click the button, returning whether the navigation was allowed through. */
function click(button) {
	const event = new window.MouseEvent("click", { cancelable: true });
	button.dispatchEvent(event);
	return !event.defaultPrevented;
}

describe("cart pay button", () => {
	beforeEach(() => {
		install_web_globals();
	});

	it("does nothing on a page with no Pay button", () => {
		expect(() => arm("<div></div>")).not.toThrow();
	});

	it("lets the first click through", () => {
		const button = arm();

		expect(click(button)).toBe(true);
	});

	it("blocks every click after the first", () => {
		const button = arm();

		click(button);

		expect(click(button)).toBe(false);
		expect(click(button)).toBe(false);
	});

	it("marks the button disabled for the assistive tree as well", () => {
		const button = arm();

		click(button);

		expect(button.classList.contains("disabled")).toBe(true);
		expect(button.getAttribute("aria-disabled")).toBe("true");
	});

	it("tells the customer the redirect is under way", () => {
		const button = arm();

		click(button);

		expect(button.textContent).toMatch(/redirecting/i);
	});
});
