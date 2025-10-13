import frappe
from frappe.tests.utils import FrappeTestCase
from frappe_paystack.frappe_paystack.doctype.paystack_payment_log.paystack_payment_log import PaystackPaymentLog


class TestPaystackPaymentLog(FrappeTestCase):

    def setUp(self):
        self.customer = frappe.get_doc({
            "doctype": "Customer",
            "customer_name": "Test Customer",
            "customer_type": "Individual"
        }).insert(ignore_permissions=True)

        self.invoice = frappe.get_doc({
            "doctype": "Sales Invoice",
            "customer": self.customer.name,
            "company": "_Test Company",
            "posting_date": frappe.utils.nowdate(),
            "due_date": frappe.utils.nowdate(),
            "debit_to": "_Test Receivable - _TC",
            "items": [{
                "item_code": "_Test Item",
                "qty": 1,
                "rate": 100
            }]
        }).insert(ignore_permissions=True)

        self.invoice.submit()


    def test_currency_normalization(self):
        log = frappe.new_doc("Paystack Payment Log")
        log.company = "_Test Company"
        log.linked_doctype = "Sales Invoice"
        log.linked_docname = self.invoice.name
        log.amount = 100.0
        log.currency = "usd"
        log.status = "Pending"

        log.save(ignore_permissions=True)
        self.assertEqual(log.currency, "USD", "Currency should be normalized to uppercase")


    def test_negative_amount_rejected(self):
        log = frappe.new_doc("Paystack Payment Log")
        log.company = "_Test Company"
        log.linked_doctype = "Sales Invoice"
        log.linked_docname = self.invoice.name
        log.amount = -100.0
        log.currency = "USD"
        log.status = "Pending"

        with self.assertRaises(frappe.ValidationError):
            log.save(ignore_permissions=True)


    def test_on_update_auto_complete(self):
        log = frappe.new_doc("Paystack Payment Log")
        log.company = "_Test Company"
        log.linked_doctype = "Sales Invoice"
        log.linked_docname = self.invoice.name
        log.amount = 100.0
        log.amount_paid = 100.0
        log.currency = "USD"
        log.status = "Processed"
        log.payment_reference = "PS1234"
        log.payment_date = frappe.utils.nowdate()
        log.insert(ignore_permissions=True)

        log.on_update()

        log.reload()
        self.assertEqual(log.status, "Completed", "Payment Log should be marked Completed after successful processing")
        self.assertTrue(log.payment_entry, "Payment Entry should be created automatically")
