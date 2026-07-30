"""Compatibility patches for ERPNext issues.

These patches are applied at startup to fix ERPNext bugs that affect
this app's functionality. They are monkey-patches applied in-memory
and do not modify ERPNext source files.
"""

import frappe
from frappe.utils import today


def apply_loyalty_program_fix():
	"""Fix ERPNext get_loyalty_programs SQL incompatibility with MariaDB 12+.

	The original function uses `ifnull(to_date, '2500-01-01')` as a filter
	key, which Frappe's query builder translates to invalid SQL on
	MariaDB 12+. This patch replaces the function with a version that
	filters `to_date` in Python instead of SQL.
	"""
	try:
		from erpnext.selling.doctype.customer import customer as customer_module

		if getattr(customer_module, "_paystack_loyalty_patched", False):
			return

		def get_loyalty_programs_fixed(doc):
			"""Returns applicable loyalty programs for a customer."""
			from erpnext.selling.doctype.customer.customer import (
				get_nested_links,
			)

			lp_details = []
			loyalty_programs = frappe.get_all(
				"Loyalty Program",
				fields=["name", "customer_group", "customer_territory", "to_date"],
				filters={
					"auto_opt_in": 1,
					"from_date": ["<=", today()],
				},
			)
			loyalty_programs = [
				lp
				for lp in loyalty_programs
				if not lp.get("to_date") or lp["to_date"] >= today()
			]

			for loyalty_program in loyalty_programs:
				if (
					not loyalty_program.customer_group
					or doc.customer_group
					in get_nested_links(
						"Customer Group",
						loyalty_program.customer_group,
						doc.flags.ignore_permissions,
					)
				) and (
					not loyalty_program.customer_territory
					or doc.territory
					in get_nested_links(
						"Territory",
						loyalty_program.customer_territory,
						doc.flags.ignore_permissions,
					)
				):
					lp_details.append(loyalty_program.name)

			return lp_details

		customer_module.get_loyalty_programs = get_loyalty_programs_fixed
		customer_module._paystack_loyalty_patched = True

	except ImportError:
		pass


def apply_all_patches():
	"""Apply all compatibility patches."""
	apply_loyalty_program_fix()