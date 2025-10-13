import unittest
import importlib
import pkgutil
import frappe_paystack

class TestFrappePaystackCode(unittest.TestCase):
    """
    Sanity check: imports all modules in frappe_paystack to catch
    syntax errors, missing imports, or runtime errors on load.
    """

    def test_import_all_modules(self):
        package = frappe_paystack
        errors = []

        for loader, module_name, is_pkg in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
            try:
                importlib.import_module(module_name)
            except Exception as e:
                errors.append(f"Failed to import {module_name}: {e}")

        if errors:
            self.fail("Errors found while importing frappe_paystack modules:\n" + "\n".join(errors))
