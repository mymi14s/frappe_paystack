import importlib
import pkgutil

import frappe
from frappe.tests.utils import FrappeTestCase

import frappe_paystack


class TestFrappePaystackImports(FrappeTestCase):
	"""Sanity check: imports all modules in frappe_paystack to catch
	syntax errors, missing imports, or runtime errors on load.
	"""

	def test_import_all_modules(self):
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

	def test_app_is_installed(self):
		"""The frappe_paystack app must be in the installed apps list."""
		installed_apps = frappe.get_installed_apps()
		self.assertIn("frappe_paystack", installed_apps)

	def test_mode_of_payment_fixture_exists(self):
		"""The 'Paystack' Mode of Payment fixture must exist."""
		self.assertTrue(
			frappe.db.exists("Mode of Payment", "Paystack"),
			"Mode of Payment 'Paystack' not found - run bench migrate to load fixtures",
		)