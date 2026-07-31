"""Test factories for creating valid Paystack-related records.

Each factory is a class with a create() classmethod that returns the
document name, and a cleanup() classmethod that deletes it.
Factories create real records that pass all validation rules.
"""

from typing import Optional
from unittest.mock import patch

import frappe
from frappe.utils import flt, random_string, today

from frappe_paystack.patches import apply_all_patches

TEST_COMPANY = "_Test Company"
TEST_CUSTOMER = "_Test Customer"
TEST_ITEM = "_Test Item Home Products 100"

VALIDATE_PAYMENT_PATCH_TARGET = (
	"frappe_paystack.frappe_paystack.doctype.paystack_payment_log."
	"paystack_payment_log.PaystackPaymentLog.validate_payment"
)

apply_all_patches()


def get_suspense_account(company: str = TEST_COMPANY) -> str:
	"""Return a valid bank account for the test company."""
	account = frappe.db.get_value(
		"Account",
		{"company": company, "account_type": "Bank"},
		"name",
	)
	if not account:
		account = frappe.db.get_value(
			"Account",
			{"company": company, "is_group": 0},
			"name",
		)
	return account


def ensure_mode_of_payment() -> str:
	"""Ensure the 'Paystack' Mode of Payment exists."""
	if not frappe.db.exists("Mode of Payment", "Paystack"):
		mop = frappe.get_doc(
			{
				"doctype": "Mode of Payment",
				"mode_of_payment": "Paystack",
				"enabled": 1,
			}
		)
		mop.flags.ignore_permissions = True
		mop.insert()
	return "Paystack"


def cleanup_doc(doctype: str, name: str) -> None:
	"""Delete a document if it exists, handling submittable doctypes."""
	if not frappe.db.exists(doctype, name):
		return

	doc = frappe.get_doc(doctype, name)

	if doc.docstatus == 1:
		doc.flags.ignore_permissions = True
		doc.cancel()

	frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)


class CustomerFactory:
	"""Factory for creating Customer records."""

	@classmethod
	def create(
		cls,
		customer_name: str = TEST_CUSTOMER,
		customer_group: str = "Individual",
		territory: str = "All Territories",
		customer_type: str = "Individual",
	) -> str:
		"""Create a Customer, returning its name.

		If the customer already exists, returns the existing name.
		"""
		if frappe.db.exists("Customer", customer_name):
			return customer_name

		customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": customer_name,
				"customer_group": customer_group,
				"territory": territory,
				"customer_type": customer_type,
			}
		)
		customer.flags.ignore_permissions = True
		customer.flags.ignore_mandatory = True
		customer.insert()
		return customer_name

	@classmethod
	def cleanup(cls, name: str) -> None:
		"""Delete a Customer if it exists."""
		cleanup_doc("Customer", name)


class ItemFactory:
	"""Factory for creating Item records."""

	@classmethod
	def create(
		cls,
		item_code: str = TEST_ITEM,
		item_name: Optional[str] = None,
		item_group: str = "All Item Groups",
		stock_uom: str = "Nos",
		is_stock_item: bool = False,
	) -> str:
		"""Create an Item, returning its name.

		If the item already exists, returns the existing name.
		"""
		if frappe.db.exists("Item", item_code):
			return item_code

		item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": item_code,
				"item_name": item_name or item_code,
				"item_group": item_group,
				"stock_uom": stock_uom,
				"is_stock_item": 1 if is_stock_item else 0,
			}
		)
		item.flags.ignore_permissions = True
		item.flags.ignore_mandatory = True
		item.insert()
		return item_code

	@classmethod
	def cleanup(cls, name: str) -> None:
		"""Delete an Item if it exists."""
		cleanup_doc("Item", name)


class SalesInvoiceFactory:
	"""Factory for creating submitted Sales Invoices."""

	@classmethod
	def create(
		cls,
		rate: float = 1000,
		company: str = TEST_COMPANY,
		customer: str = TEST_CUSTOMER,
		item: str = TEST_ITEM,
		qty: float = 1,
	) -> str:
		"""Create and submit a Sales Invoice, returning its name.

		The invoice will have an outstanding amount equal to grand_total,
		so its status will be 'Unpaid' - which passes validate_record().
		"""
		CustomerFactory.create(customer_name=customer)
		ItemFactory.create(item_code=item)

		sinv = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"customer": customer,
				"company": company,
				"due_date": today(),
				"posting_date": today(),
				"currency": frappe.db.get_value(
					"Company", company, "default_currency"
				)
				or "INR",
				"items": [
					{
						"item_code": item,
						"qty": qty,
						"rate": rate,
					}
				],
			}
		)
		sinv.flags.ignore_permissions = True
		sinv.flags.ignore_mandatory = True
		sinv.insert()
		sinv.submit()
		return sinv.name

	@classmethod
	def cleanup(cls, name: str) -> None:
		"""Cancel and delete a Sales Invoice."""
		cleanup_doc("Sales Invoice", name)


class SalesOrderFactory:
	"""Factory for creating submitted Sales Orders."""

	@classmethod
	def create(
		cls,
		rate: float = 1000,
		company: str = TEST_COMPANY,
		customer: str = TEST_CUSTOMER,
		item: str = TEST_ITEM,
		qty: float = 1,
	) -> str:
		"""Create and submit a Sales Order, returning its name."""
		CustomerFactory.create(customer_name=customer)
		ItemFactory.create(item_code=item)

		so = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": customer,
				"company": company,
				"delivery_date": today(),
				"transaction_date": today(),
				"currency": frappe.db.get_value(
					"Company", company, "default_currency"
				)
				or "INR",
				"items": [
					{
						"item_code": item,
						"qty": qty,
						"rate": rate,
					}
				],
			}
		)
		so.flags.ignore_permissions = True
		so.flags.ignore_mandatory = True
		so.insert()
		so.submit()
		return so.name

	@classmethod
	def cleanup(cls, name: str) -> None:
		"""Cancel and delete a Sales Order."""
		cleanup_doc("Sales Order", name)


class CreditNoteFactory:
	"""Factory for creating credit notes (return Sales Invoices)."""

	@classmethod
	def create(
		cls,
		return_against: str,
		rate: float = 500,
		company: str = TEST_COMPANY,
		customer: str = TEST_CUSTOMER,
		item: str = TEST_ITEM,
		qty: float = 1,
	) -> str:
		"""Create and submit a credit note against an invoice."""
		CustomerFactory.create(customer_name=customer)
		ItemFactory.create(item_code=item)

		cn = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"customer": customer,
				"company": company,
				"due_date": today(),
				"posting_date": today(),
				"is_return": 1,
				"return_against": return_against,
				"currency": frappe.db.get_value(
					"Company", company, "default_currency"
				)
				or "INR",
				"items": [
					{
						"item_code": item,
						"qty": -qty,
						"rate": rate,
					}
				],
			}
		)
		cn.flags.ignore_permissions = True
		cn.flags.ignore_mandatory = True
		cn.insert()
		cn.submit()
		return cn.name

	@classmethod
	def cleanup(cls, name: str) -> None:
		"""Cancel and delete a credit note."""
		cleanup_doc("Sales Invoice", name)


class GatewaySettingFactory:
	"""Factory for creating Paystack Gateway Settings."""

	@classmethod
	def create(
		cls,
		gateway: str = "Test Paystack Gateway",
		company: str = TEST_COMPANY,
		enabled: bool = True,
		webhook_secret: Optional[str] = None,
		allowed_ips: Optional[str] = None,
		auto_refund: bool = False,
	) -> str:
		"""Create a Paystack Gateway Setting, returning its name.

		If a gateway with the same name already exists, it is returned as-is.
		"""
		ensure_mode_of_payment()

		if frappe.db.exists("Paystack Gateway Setting", gateway):
			existing = frappe.get_doc("Paystack Gateway Setting", gateway)
			if not existing.enabled and enabled:
				existing.enabled = 1
				existing.flags.ignore_permissions = True
				existing.save()
			return gateway

		setting = frappe.get_doc(
			{
				"doctype": "Paystack Gateway Setting",
				"gateway": gateway,
				"company": company,
				"secret_key": "sk_test_123",
				"public_key": "pk_test_123",
				"webhook_secret": webhook_secret,
				"allowed_webhook_ips": allowed_ips,
				"suspense_account": get_suspense_account(company),
				"mode_of_payment": "Paystack",
				"currency": "NGN",
				"enabled": 1 if enabled else 0,
				"auto_refund_on_credit_note": 1 if auto_refund else 0,
			}
		)
		setting.flags.ignore_permissions = True
		setting.flags.ignore_links = True
		setting.insert()
		return setting.name

	@classmethod
	def cleanup(cls, name: str) -> None:
		"""Delete a Paystack Gateway Setting."""
		cleanup_doc("Paystack Gateway Setting", name)


class PaymentLogFactory:
	"""Factory for creating Paystack Payment Logs."""

	@classmethod
	def create(
		cls,
		linked_doctype: str = "Sales Invoice",
		linked_docname: Optional[str] = None,
		amount: float = 1000,
		currency: str = "NGN",
		status: str = "Pending",
		company: str = TEST_COMPANY,
		transaction_id: Optional[str] = None,
		amount_paid: Optional[float] = None,
	) -> str:
		"""Create a Paystack Payment Log that passes all validation.

		If linked_docname is not provided, a real Sales Invoice is created.
		The before_insert hook calls validate_payment() which hits the
		Paystack API - this is mocked automatically.
		"""
		if linked_docname is None:
			linked_docname = SalesInvoiceFactory.create(rate=amount)

		if transaction_id is None:
			transaction_id = f"ref_test_{random_string(8)}"

		with patch(
			VALIDATE_PAYMENT_PATCH_TARGET,
			return_value={"status": True, "data": {"status": "success"}},
		):
			log = frappe.get_doc(
				{
					"doctype": "Paystack Payment Log",
					"company": company,
					"linked_doctype": linked_doctype,
					"linked_docname": linked_docname,
					"amount": amount,
					"amount_paid": amount_paid if amount_paid is not None else 0,
					"currency": currency,
					"status": status,
					"transaction_id": transaction_id,
				}
			)
			log.flags.ignore_permissions = True
			log.insert()
		return log.name

	@classmethod
	def create_completed(
		cls,
		amount: float = 1000,
		amount_paid: Optional[float] = None,
		currency: str = "NGN",
		company: str = TEST_COMPANY,
		transaction_id: Optional[str] = None,
	) -> str:
		"""Create a Completed Paystack Payment Log with amount_paid set."""
		if transaction_id is None:
			transaction_id = f"ref_comp_{random_string(8)}"

		if amount_paid is None:
			amount_paid = amount

		sinv_name = SalesInvoiceFactory.create(rate=amount)

		with patch(
			VALIDATE_PAYMENT_PATCH_TARGET,
			return_value={"status": True, "data": {"status": "success"}},
		):
			log = frappe.get_doc(
				{
					"doctype": "Paystack Payment Log",
					"company": company,
					"linked_doctype": "Sales Invoice",
					"linked_docname": sinv_name,
					"amount": amount,
					"amount_paid": amount_paid,
					"currency": currency,
					"status": "Completed",
					"transaction_id": transaction_id,
				}
			)
			log.flags.ignore_permissions = True
			log.insert()
		return log.name

	@classmethod
	def cleanup(cls, name: str) -> None:
		"""Clean up a Payment Log and its linked Sales Invoice."""
		if not frappe.db.exists("Paystack Payment Log", name):
			return

		frappe.db.set_value("Paystack Payment Log", name, "status", "Pending")
		frappe.db.set_value("Paystack Payment Log", name, "payment_entry", None)

		linked_doctype = frappe.db.get_value(
			"Paystack Payment Log", name, "linked_doctype"
		)
		linked_docname = frappe.db.get_value(
			"Paystack Payment Log", name, "linked_docname"
		)

		frappe.delete_doc(
			"Paystack Payment Log", name, force=True, ignore_permissions=True
		)

		if linked_doctype and linked_docname:
			cleanup_doc(linked_doctype, linked_docname)


class RefundLogFactory:
	"""Factory for creating Paystack Refund Logs."""

	@classmethod
	def create(
		cls,
		payment_log_name: str,
		refund_amount: float = 100,
		currency: str = "NGN",
		status: str = "Pending",
		company: str = TEST_COMPANY,
		transaction_id: Optional[str] = None,
		refund_reference: Optional[str] = None,
	) -> str:
		"""Create a Refund Log that passes validation.

		The referenced Payment Log must be Completed with sufficient
		amount_paid for the refund_amount.
		"""
		if transaction_id is None:
			transaction_id = frappe.db.get_value(
				"Paystack Payment Log", payment_log_name, "transaction_id"
			)

		refund_log = frappe.get_doc(
			{
				"doctype": "Paystack Refund Log",
				"payment_log": payment_log_name,
				"company": company,
				"refund_amount": refund_amount,
				"currency": currency,
				"status": status,
				"transaction_id": transaction_id,
				"refund_reference": refund_reference,
			}
		)
		refund_log.flags.ignore_permissions = True
		refund_log.insert()
		return refund_log.name

	@classmethod
	def create_bypass_validate(
		cls,
		payment_log_name: str,
		refund_amount: float = 100,
		currency: str = "NGN",
		status: str = "Pending",
		company: str = TEST_COMPANY,
		transaction_id: Optional[str] = None,
		refund_reference: Optional[str] = None,
	) -> str:
		"""Create a Refund Log bypassing validation.

		Use for states that would normally fail validation
		(e.g. Failed status, or exceeding amount_paid).
		"""
		if transaction_id is None:
			transaction_id = frappe.db.get_value(
				"Paystack Payment Log", payment_log_name, "transaction_id"
			)

		refund_log = frappe.get_doc(
			{
				"doctype": "Paystack Refund Log",
				"payment_log": payment_log_name,
				"company": company,
				"refund_amount": refund_amount,
				"currency": currency,
				"status": status,
				"transaction_id": transaction_id,
				"refund_reference": refund_reference,
			}
		)
		refund_log.flags.ignore_permissions = True
		refund_log.flags.ignore_validate = True
		refund_log.flags.ignore_on_update = True
		refund_log.insert()
		return refund_log.name

	@classmethod
	def cleanup(cls, name: str) -> None:
		"""Clean up a Refund Log."""
		if not frappe.db.exists("Paystack Refund Log", name):
			return

		frappe.db.set_value("Paystack Refund Log", name, "status", "Pending")
		frappe.db.set_value(
			"Paystack Refund Log", name, "reversal_payment_entry", None
		)
		frappe.delete_doc(
			"Paystack Refund Log", name, force=True, ignore_permissions=True
		)