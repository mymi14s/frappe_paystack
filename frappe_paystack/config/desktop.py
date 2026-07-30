from frappe import _


def get_data() -> list:
	return [
		{
			"module_name": "Frappe Paystack",
			"category": "Modules",
			"label": _("Frappe Paystack"),
			"icon": "octicon octicon-credit-card",
			"type": "module",
			"hidden": 0,
		}
	]