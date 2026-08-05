frappe.pages["paystack-reconciliation"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Paystack Reconciliation"),
		single_column: true,
	});

	new frappe_paystack.ReconciliationDashboard(page);
};

frappe.provide("frappe_paystack");

frappe_paystack.ReconciliationDashboard = class ReconciliationDashboard {
	constructor(page) {
		this.page = page;
		this.status_filter = null;
		this.setup_controls();
		this.render_layout();
		this.refresh();
	}

	setup_controls() {
		this.page.set_primary_action(__("Run Reconciliation"), () =>
			this.run_reconciliation()
		);

		this.status_field = this.page.add_select(
			__("Status"),
			["", "Pending", "Reconciled", "Mismatch", "Manual Override"]
		);
		this.status_field.on("change", () => {
			this.status_filter = this.status_field.val() || null;
			this.refresh();
		});
	}

	render_layout() {
		this.page.main.html(`
			<div class="paystack-reconciliation">
				<div class="ps-stats"></div>
				<div class="ps-table-wrap"><div class="ps-table"></div></div>
			</div>
		`);
		this.$stats = this.page.main.find(".ps-stats");
		this.$table = this.page.main.find(".ps-table");
	}

	refresh() {
		frappe.call({
			method: "frappe_paystack.utils.reconciliation_api.get_reconciliation_stats",
			callback: (r) => this.render_stats(r.message || {}),
		});

		frappe.call({
			method: "frappe_paystack.utils.reconciliation_api.get_reconciliation_report",
			args: { status: this.status_filter, limit: 100 },
			callback: (r) => this.render_table(r.message || []),
		});
	}

	render_stats(stats) {
		const cards = [
			{ label: __("Total"), value: stats.total || 0, cls: "" },
			{ label: __("Reconciled"), value: stats.Reconciled || 0, cls: "ps-green" },
			{ label: __("Mismatch"), value: stats.Mismatch || 0, cls: "ps-red" },
			{ label: __("Pending"), value: stats.Pending || 0, cls: "ps-blue" },
		];

		this.$stats.html(
			cards
				.map(
					(c) => `
				<div class="ps-card">
					<div class="ps-value ${c.cls}">${frappe.utils.escape_html(String(c.value))}</div>
					<div class="ps-label">${frappe.utils.escape_html(c.label)}</div>
				</div>`
				)
				.join("")
		);
	}

	render_table(rows) {
		if (!rows.length) {
			this.$table.html(
				`<p class="text-muted">${__("No reconciliation records found.")}</p>`
			);
			return;
		}

		const body = rows
			.map((row) => {
				const link = frappe.utils.get_form_link(
					"Paystack Payment Log",
					row.payment_log,
					true
				);
				return `
				<tr>
					<td>${link}</td>
					<td>${frappe.utils.escape_html(row.company || "")}</td>
					<td><span class="indicator-pill ${this.indicator(row.status)}">
						${frappe.utils.escape_html(row.status || "")}</span></td>
					<td class="text-right">${format_currency(row.paystack_amount)}</td>
					<td class="text-right">${format_currency(row.frappe_amount)}</td>
					<td class="text-right">${format_currency(row.difference)}</td>
					<td>${frappe.utils.escape_html(row.mismatch_reason || "")}</td>
					<td>${frappe.datetime.str_to_user(row.reconciled_at) || ""}</td>
				</tr>`;
			})
			.join("");

		this.$table.html(`
			<table class="table table-bordered">
				<thead>
					<tr>
						<th>${__("Payment Log")}</th>
						<th>${__("Company")}</th>
						<th>${__("Status")}</th>
						<th class="text-right">${__("Paystack")}</th>
						<th class="text-right">${__("ERPNext")}</th>
						<th class="text-right">${__("Difference")}</th>
						<th>${__("Reason")}</th>
						<th>${__("Reconciled At")}</th>
					</tr>
				</thead>
				<tbody>${body}</tbody>
			</table>
		`);
	}

	indicator(status) {
		return (
			{
				Reconciled: "green",
				Mismatch: "red",
				Pending: "orange",
				"Manual Override": "blue",
			}[status] || "gray"
		);
	}

	run_reconciliation() {
		this.page.set_indicator(__("Running..."), "orange");
		frappe.call({
			method: "frappe_paystack.utils.reconciliation_api.run_reconciliation",
			freeze: true,
			freeze_message: __("Reconciling payments with Paystack..."),
			callback: (r) => {
				const res = r.message || {};
				frappe.show_alert({
					message: __("Reconciled {0} of {1} payments", [
						res.reconciled || 0,
						res.total || 0,
					]),
					indicator: "green",
				});
				this.page.set_indicator(__("Updated"), "green");
				this.refresh();
			},
		});
	}
};
