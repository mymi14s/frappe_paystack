const isEmail = (str) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(str);

const { createApp } = Vue;

createApp({
	delimiters: ["[%", "%]"],
	data() {
		return {
			id: "",
			paymentData: {},
			gateway: "",
			showDiv: false,
			doc: window.doc,
		};
	},
	methods: {
		payWithPaystack() {
			const handler = PaystackPop.setup({
				key: this.doc.public_key,
				amount: this.doc.payment_amount * 100,
				currency: this.doc.currency,
				email: this.doc.email,
				metadata: {
					reference_doctype: this.doc.reference_doctype,
					reference_docname: this.doc.reference_docname,
					customer: this.doc.customer,
					reference: this.doc.reference,
					email: this.doc.email,
				},
				onClose() {
					alert("Payment Terminated.");
				},
				callback() {
					$("#paymentBTN").hide();
					Swal.fire(
						"Successful",
						"Your payment was successful, we will issue you receipt shortly.",
						"success"
					);
				},
			});

			handler.openIframe();
		},
		getData() {
			frappe
				.call("frappe_paystack.api.validate_payment_link", {
					docname: this.doc.reference,
				})
				.then((res) => {
					const data = res.message;
					let error = "";

					if (["Completed", "Closed'", "Paid"].includes(data.order_status)) {
						error = "Paid or Completed";
					} else if (["Processed", "Completed"].includes(data.status)) {
						error = "Payment already processed.";
					} else if ([0, 2].includes(data.order_docstatus)) {
						error = "Payment link expired or invalid.";
					}

					if (error) {
						window.location.reload();
						return;
					}

					if (this.doc.email) {
						this.payWithPaystack();
						return;
					}

					Swal.fire({
						title: "Your email",
						input: "text",
						inputAttributes: {
							autocapitalize: "off",
						},
						showCancelButton: false,
						confirmButtonText: "Continue",
						showLoaderOnConfirm: true,
						allowOutsideClick: () => !Swal.isLoading(),
					}).then((value) => {
						if (value.isConfirmed) {
							if (isEmail(value.value)) {
								this.doc.email = value.value;
								this.payWithPaystack();
							} else {
								Swal.fire({
									title: "Invalid Email",
									text: "Retry",
									icon: "warning",
								});
							}
						}
					});
				});
		},
		formatCurrency(amount, currency) {
			if (currency) {
				return Intl.NumberFormat("en-US", {
					currency: currency,
					style: "currency",
				}).format(amount);
			}
			return Intl.NumberFormat("en-US").format(amount);
		},
	},
	mounted() {},
}).mount("#app");