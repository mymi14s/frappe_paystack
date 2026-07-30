import importlib
import pkgutil

import frappe
from frappe.tests.utils import FrappeTestCase

import frappe_paystack


class TestFrappePaystackImports(FrappeTestCase):
	"""Sanity check: imports all modules in frappe_paystack to catch
	syntax errors, missing imports, or runtime errors on load.

	This test catches circular imports, missing dependencies, and
	syntax errors across the entire codebase in a single pass.
	"""

	def test_import_all_modules_without_errors(self):
		package = frappe_paystack
		errors = []

		for loader, module_name, is_pkg in pkgutil.walk_packages(
			package.__path__, package.__name__ + "."
		):
			try:
				importlib.import_module(module_name)
			except Exception as e:
				errors.append(f"Failed to import {module_name}: {e}")

		if errors:
			self.fail(
				"Errors found while importing frappe_paystack modules:\n"
				+ "\n".join(errors)
			)

	def test_fixture_mode_of_payment_paystack_exists_after_migrate(self):
		"""The 'Paystack' Mode of Payment fixture must exist in the database.

		This verifies that the fixtures hook in hooks.py is correctly
		configured and that bench migrate has been run.
		"""
		self.assertTrue(
			frappe.db.exists("Mode of Payment", "Paystack"),
			"Mode of Payment 'Paystack' not found - the fixtures hook may be "
			"misconfigured or bench migrate has not been run",
		)