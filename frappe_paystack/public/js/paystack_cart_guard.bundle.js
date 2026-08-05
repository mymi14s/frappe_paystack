/** Guard the Webshop cart's Pay button against a repeated request. */
frappe.ready(() => {
	const payment_button = document.getElementById("pay-for-order");

	if (!payment_button) {
		return;
	}

	let navigating = false;

	payment_button.addEventListener("click", (event) => {
		if (navigating) {
			event.preventDefault();
			return;
		}

		navigating = true;
		payment_button.classList.add("disabled");
		payment_button.setAttribute("aria-disabled", "true");
		payment_button.textContent = __("Redirecting to payment...");
	});
});
