import frappe, unittest

class TestPaystackPaymentLog(unittest.TestCase):
    def test_meta(self):
        self.assertTrue(frappe.get_meta('Paystack Payment Log'))
