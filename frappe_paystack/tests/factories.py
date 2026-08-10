"""Test factories for creating valid Paystack-related records.

Each factory has a create() classmethod returning the document name and a
cleanup() classmethod deleting it.
"""

from typing import Optional
from unittest.mock import patch

import frappe
from frappe.utils import add_days, flt, getdate, random_string, today

from frappe_paystack.setup import ensure_mode_of_payment as setup_ensure_mode_of_payment

FRAPPE_MAJOR_VERSION = int(frappe.__version__.split(".")[0])

# A desk role carrying no Paystack or accounting permission. version-16 dropped
# frappe's Blogger role, so the permission tests own this one.
UNPRIVILEGED_ROLE = "_Test Paystack Unprivileged"

TEST_COMPANY = "_Test Company"
TEST_CUSTOMER = "_Test Customer"
TEST_ITEM = "_Test Item Home Products 100"

# The price list a selling document is raised against.
SELLING_PRICE_LIST = "Standard Selling"

POS_INVOICE = "POS Invoice"

# The customer a foreign-currency invoice bills.
CHARGE_CUSTOMER = "_Test Paystack Chargeable Customer"

VALIDATE_PAYMENT_PATCH_TARGET = (
    "frappe_paystack.frappe_paystack.doctype.paystack_payment_log."
    "paystack_payment_log.PaystackPaymentLog.validate_payment"
)


ensure_mode_of_payment = setup_ensure_mode_of_payment


def ensure_unprivileged_role() -> str:
    """Create the role the permission tests sign in as."""
    if not frappe.db.exists("Role", UNPRIVILEGED_ROLE):
        role = frappe.new_doc("Role")
        role.role_name = UNPRIVILEGED_ROLE
        role.desk_access = 1
        role.flags.ignore_permissions = True
        role.insert()

    return UNPRIVILEGED_ROLE


def cleanup_linked_payment_entries(reference_doctype: str, reference_docname: str) -> None:
    """Cancel and delete Payment Entries referencing a document."""
    pe_names = frappe.get_all(
        "Payment Entry Reference",
        filters={
            "reference_doctype": reference_doctype,
            "reference_name": reference_docname,
        },
        pluck="parent",
    )

    for pe_name in set(pe_names):
        cleanup_doc("Payment Entry", pe_name)


def cleanup_linked_payment_requests(reference_doctype: str, reference_docname: str) -> None:
    """Cancel and delete Payment Requests billing a document."""
    for name in frappe.get_all(
        "Payment Request",
        filters={
            "reference_doctype": reference_doctype,
            "reference_name": reference_docname,
        },
        pluck="name",
    ):
        cleanup_doc("Payment Request", name)


def cleanup_user(email: str) -> None:
    """Delete a test user, and the Contact Frappe raises alongside it."""
    frappe.set_user("Administrator")

    contacts = set(frappe.get_all("Contact Email", filters={"email_id": email}, pluck="parent"))
    for contact in contacts:
        cleanup_doc("Contact", contact)

    cleanup_doc("User", email)


def cleanup_doc(doctype: str, name: str) -> None:
    """Delete a document if it exists, handling submittable doctypes."""
    if not frappe.db.exists(doctype, name):
        return

    doc = frappe.get_doc(doctype, name)

    # ERPNext refuses to cancel a Payment Request its document was paid against.
    if doctype == "Payment Request" and doc.docstatus == 1:
        cleanup_linked_payment_entries(doc.reference_doctype, doc.reference_name)
        # Cancelling those entries bumps this document's timestamp.
        doc.reload()

    if doc.docstatus == 1:
        doc.flags.ignore_permissions = True
        doc.cancel()

    frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
    frappe.db.commit()


class BankAccountFactory:
    """Factory for creating Bank Accounts."""

    # The accounts create() inserted. cleanup() removes only these.
    inserted = set()

    @classmethod
    def create(cls, company: str = TEST_COMPANY) -> str:
        """Create a Bank account for a company, returning its name, or the existing one."""
        account_name = f"Test Paystack Bank - {company.split(' ')[0]}"
        if frappe.db.exists("Account", account_name):
            return account_name

        root = frappe.db.get_value(
            "Account",
            {"company": company, "is_group": 1, "parent_account": None},
            "name",
        )
        if not root:
            root = frappe.db.get_value(
                "Account",
                {"company": company, "is_group": 1},
                "name",
            )

        account = frappe.get_doc(
            {
                "doctype": "Account",
                "account_name": "Test Paystack Bank",
                "parent_account": root,
                "company": company,
                "account_type": "Bank",
                "is_group": 0,
            }
        )
        account.flags.ignore_permissions = True
        account.flags.ignore_mandatory = True
        account.insert()
        cls.inserted.add(account.name)
        return account.name

    @classmethod
    def cleanup(cls, name: str) -> None:
        """Delete a Bank account create() inserted."""
        if name not in cls.inserted:
            return

        cls.inserted.discard(name)
        cleanup_doc("Account", name)


class SuspenseAccountFactory:
    """Factory for the clearing account Paystack settlements land in."""

    @classmethod
    def create(cls, company: str = TEST_COMPANY) -> str:
        """Return the company-currency clearing account: Bank, then Cash, then any leaf account."""
        company_currency = frappe.db.get_value("Company", company, "default_currency")
        base_filters = {
            "company": company,
            "is_group": 0,
            "account_currency": company_currency,
        }

        for account_type in ("Bank", "Cash"):
            account = frappe.db.get_value("Account", {**base_filters, "account_type": account_type}, "name")
            if account:
                return account

        account = frappe.db.get_value("Account", base_filters, "name")
        return account or BankAccountFactory.create(company)

    @classmethod
    def cleanup(cls, name: str) -> None:
        """Delete the account when create() inserted one."""
        BankAccountFactory.cleanup(name)


class LedgerAccountFactory:
    """Factory for a named leaf account in a company's own currency."""

    # The accounts create() inserted. cleanup() removes only these.
    inserted = set()

    @classmethod
    def create(
        cls,
        account_name: str,
        root_type: str,
        account_type: Optional[str] = None,
        company: str = TEST_COMPANY,
    ) -> str:
        """Create a leaf account under the company's root of a type, or return it."""
        abbr = frappe.db.get_value("Company", company, "abbr")
        name = f"{account_name} - {abbr}"
        if frappe.db.exists("Account", name):
            return name

        parent = frappe.db.get_value(
            "Account",
            {"company": company, "is_group": 1, "root_type": root_type},
            "name",
        )

        account = frappe.get_doc(
            {
                "doctype": "Account",
                "account_name": account_name,
                "parent_account": parent,
                "company": company,
                "root_type": root_type,
                "account_type": account_type,
                "account_currency": frappe.db.get_value("Company", company, "default_currency"),
                "is_group": 0,
            }
        )
        account.flags.ignore_permissions = True
        account.flags.ignore_mandatory = True
        account.insert()
        cls.inserted.add(account.name)
        return account.name

    @classmethod
    def cleanup(cls, name: str) -> None:
        """Delete an account create() inserted."""
        if name not in cls.inserted:
            return

        cls.inserted.discard(name)
        cleanup_doc("Account", name)


class CurrencyExchangeFactory:
    """Factory for the site-wide rate a foreign-currency document converts at."""

    DOCTYPE = "Currency Exchange"

    # The rates create() inserted. cleanup() removes only these.
    inserted = set()

    @classmethod
    def create(cls, from_currency: str, to_currency: str, exchange_rate: float) -> str:
        """Post today's rate between two currencies, for buying and for selling.

        A rate dated today outranks every earlier record the site carries.
        """
        filters = {"date": today(), "from_currency": from_currency, "to_currency": to_currency}
        name = frappe.db.get_value(cls.DOCTYPE, filters, "name")
        if name:
            return name

        rate = frappe.get_doc(
            {
                "doctype": cls.DOCTYPE,
                "date": today(),
                "from_currency": from_currency,
                "to_currency": to_currency,
                "exchange_rate": exchange_rate,
                "for_buying": 1,
                "for_selling": 1,
            }
        )
        rate.flags.ignore_permissions = True
        rate.insert()
        frappe.db.commit()
        cls.inserted.add(rate.name)
        return rate.name

    @classmethod
    def cleanup(cls, name: str) -> None:
        """Delete a rate create() inserted."""
        if name not in cls.inserted:
            return

        cls.inserted.discard(name)
        cleanup_doc(cls.DOCTYPE, name)


class SettlementFactory:
    """Factory for recorded Paystack payouts."""

    DOCTYPE = "Paystack Settlement"

    @classmethod
    def create(
        cls,
        settlement_id: Optional[str] = None,
        company: str = TEST_COMPANY,
        currency: Optional[str] = None,
        gross_amount: float = 1000.0,
        total_fees: float = 15.0,
        deductions: float = 0.0,
        net_amount: float = 985.0,
        status: str = "Pending",
    ) -> str:
        """Insert a Paystack Settlement, returning its name; currency defaults to the company's."""
        settlement = frappe.get_doc(
            {
                "doctype": cls.DOCTYPE,
                "settlement_id": settlement_id or f"stl_{random_string(8)}",
                "company": company,
                "currency": currency or frappe.db.get_value("Company", company, "default_currency"),
                "settlement_date": today(),
                "gross_amount": gross_amount,
                "total_fees": total_fees,
                "deductions": deductions,
                "net_amount": net_amount,
                "status": status,
            }
        )
        settlement.flags.ignore_permissions = True
        settlement.insert()
        frappe.db.commit()
        return settlement.name

    @classmethod
    def cleanup(cls, name: str) -> None:
        """Delete a payout and the journal entry it posted."""
        if not frappe.db.exists(cls.DOCTYPE, name):
            return

        journal_entry = frappe.db.get_value(cls.DOCTYPE, name, "journal_entry")
        frappe.db.set_value(cls.DOCTYPE, name, "journal_entry", None)
        frappe.delete_doc(cls.DOCTYPE, name, force=True, ignore_permissions=True)

        if journal_entry:
            cleanup_doc("Journal Entry", journal_entry)

        frappe.db.commit()


class CustomerFactory:
    """Factory for creating Customer records."""

    # The customers create() inserted. cleanup() removes only these.
    inserted = set()

    @classmethod
    def create(
        cls,
        customer_name: str = TEST_CUSTOMER,
        customer_group: str = "Individual",
        territory: str = "All Territories",
        customer_type: str = "Individual",
    ) -> str:
        """Create a Customer, returning its name, or the existing one."""
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
        cls.inserted.add(customer_name)
        return customer_name

    @classmethod
    def cleanup(cls, name: str, owned: bool = False) -> None:
        """Delete a Customer create() inserted; owned=True deletes it on the caller's word."""
        if not owned and name not in cls.inserted:
            return

        cls.inserted.discard(name)
        cleanup_doc("Customer", name)


class ItemFactory:
    """Factory for creating Item records."""

    # The items create() inserted. cleanup() removes only these.
    inserted = set()

    @classmethod
    def create(
        cls,
        item_code: str = TEST_ITEM,
        item_name: Optional[str] = None,
        item_group: str = "All Item Groups",
        stock_uom: str = "Nos",
        is_stock_item: bool = False,
    ) -> str:
        """Create an Item, returning its name, or the existing one."""
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
        cls.inserted.add(item_code)
        return item_code

    @classmethod
    def cleanup(cls, name: str) -> None:
        """Delete an Item create() inserted."""
        if name not in cls.inserted:
            return

        cls.inserted.discard(name)
        cleanup_doc("Item", name)


class PortalCustomerFactory:
    """Factory for a customer who can sign in to the portal and shop.

    Creates the User, Contact, Dynamic Link and billing Address the portal and
    Webshop cart read.
    """

    # The password every portal fixture user shares.
    PASSWORD = "paystack-ui-test-passphrase"

    # The addresses ensure_address() inserted. cleanup() removes only these.
    inserted_addresses = set()

    # The roles a shopper needs to get through the Webshop cart.
    ROLES = ("Customer", "Accounts User")

    @classmethod
    def create(
        cls,
        customer_name: str,
        email: str,
        first_name: str,
        customer_group: str = "Individual",
        territory: str = "All Territories",
        roles: tuple = ROLES,
    ) -> str:
        """Create a portal-ready customer, returning its name."""
        CustomerFactory.create(
            customer_name=customer_name,
            customer_group=customer_group,
            territory=territory,
        )

        cls.ensure_user(email, first_name, roles)
        contact = cls.ensure_contact(customer_name, first_name, email)
        cls.ensure_address(customer_name, first_name)

        if frappe.db.get_value("Customer", customer_name, "customer_primary_contact") != contact:
            customer = frappe.get_doc("Customer", customer_name)
            customer.customer_primary_contact = contact
            customer.flags.ignore_permissions = True
            customer.flags.ignore_mandatory = True
            customer.save()

        return customer_name

    @classmethod
    def ensure_user(cls, email: str, first_name: str, roles: tuple = ROLES) -> str:
        """Create the login, or reset the one already there to a known password."""
        if not frappe.db.exists("User", email):
            user = frappe.new_doc("User")
            user.email = email
            user.first_name = first_name
            user.send_welcome_email = 0
        else:
            user = frappe.get_doc("User", email)

        user.flags.ignore_password_policy = True
        user.new_password = cls.PASSWORD

        held = {row.role for row in user.roles}
        for role in roles:
            if role not in held and frappe.db.exists("Role", role):
                user.append("roles", {"role": role})

        user.enabled = 1
        user.flags.ignore_permissions = True
        user.save()
        return email

    @classmethod
    def ensure_contact(cls, customer_name: str, first_name: str, email: str) -> str:
        """Return the Contact that ties this login to its customer, linking an existing one."""
        name = frappe.db.get_value("Contact", {"email_id": email}, "name")
        if name:
            cls.link_contact_to_customer(name, customer_name)
            return name

        contact = frappe.get_doc(
            {
                "doctype": "Contact",
                "first_name": first_name,
                "user": email,
                "email_ids": [{"email_id": email, "is_primary": 1}],
                "links": [{"link_doctype": "Customer", "link_name": customer_name}],
            }
        )
        contact.flags.ignore_permissions = True
        contact.insert()
        return contact.name

    @classmethod
    def link_contact_to_customer(cls, contact: str, customer_name: str) -> None:
        """Give an existing Contact the Dynamic Link customer_for() reads."""
        if frappe.db.exists(
            "Dynamic Link",
            {
                "parenttype": "Contact",
                "parent": contact,
                "link_doctype": "Customer",
                "link_name": customer_name,
            },
        ):
            return

        doc = frappe.get_doc("Contact", contact)
        doc.append("links", {"link_doctype": "Customer", "link_name": customer_name})
        doc.flags.ignore_permissions = True
        doc.save()

    @classmethod
    def ensure_address(cls, customer_name: str, title: str) -> str:
        """Give the customer the billing address place_order insists on."""
        name = frappe.db.get_value("Address", {"address_title": title, "address_type": "Billing"}, "name")
        if name:
            return name

        address = frappe.get_doc(
            {
                "doctype": "Address",
                "address_title": title,
                "address_type": "Billing",
                "address_line1": "1 Test Street",
                "city": "Lagos",
                "country": frappe.db.get_single_value("System Settings", "country") or "Nigeria",
                "links": [{"link_doctype": "Customer", "link_name": customer_name}],
            }
        )
        address.flags.ignore_permissions = True
        address.insert()
        cls.inserted_addresses.add(address.name)
        return address.name

    @classmethod
    def cleanup(cls, customer_name: str, email: str, first_name: str, owned: bool = False) -> None:
        """Remove the customer, its address, its login and every Contact behind it."""
        if frappe.db.exists("Customer", customer_name):
            frappe.db.set_value("Customer", customer_name, "customer_primary_contact", None)

        for address in frappe.get_all("Address", filters={"address_title": first_name}, pluck="name"):
            if not owned and address not in cls.inserted_addresses:
                continue

            cls.inserted_addresses.discard(address)
            cleanup_doc("Address", address)

        cleanup_user(email)
        CustomerFactory.cleanup(customer_name, owned=owned)


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
        currency: Optional[str] = None,
        conversion_rate: Optional[float] = None,
        debit_to: Optional[str] = None,
        overdue_days: int = 0,
        subscription: Optional[str] = None,
    ) -> str:
        """Create and submit an Unpaid Sales Invoice, returning its name.

        Pass currency/conversion_rate/debit_to together for a foreign-currency invoice.
        overdue_days backdates the posting and due dates.
        """
        CustomerFactory.create(customer_name=customer)
        ItemFactory.create(item_code=item)

        raised_on = add_days(today(), -overdue_days)
        sinv = frappe.get_doc(
            {
                "doctype": "Sales Invoice",
                "customer": customer,
                "company": company,
                "due_date": raised_on,
                "posting_date": raised_on,
                # Holds posting_date at the value set above.
                "set_posting_time": 1,
                "currency": currency or frappe.db.get_value("Company", company, "default_currency") or "NGN",
                "items": [
                    {
                        "item_code": item,
                        "qty": qty,
                        "rate": rate,
                        "price_list_rate": rate,
                    }
                ],
            }
        )
        if conversion_rate:
            sinv.conversion_rate = conversion_rate
        if debit_to:
            sinv.debit_to = debit_to
        if subscription:
            sinv.subscription = subscription

        sinv.flags.ignore_permissions = True
        sinv.flags.ignore_mandatory = True
        sinv.insert()
        sinv.submit()
        return sinv.name

    @classmethod
    def cleanup(cls, name: str) -> None:
        """Cancel and delete a Sales Invoice and anything raised against it."""
        cleanup_linked_payment_entries("Sales Invoice", name)
        cleanup_linked_payment_requests("Sales Invoice", name)
        cleanup_doc("Sales Invoice", name)


class ChargeableInvoiceFactory:
    """Factory for Sales Invoices in a currency Paystack can charge, billed to their own customer."""

    # The company's own currency, so the invoice needs no foreign receivable and
    # the customer never holds entries in two currencies.
    CURRENCY = None
    RECEIVABLE = None
    CONVERSION_RATE = None

    # The customer each invoice was billed to, read by cleanup().
    billed = {}

    @classmethod
    def create(
        cls,
        rate: float = 1000,
        customer: str = CHARGE_CUSTOMER,
        company: str = TEST_COMPANY,
        currency: Optional[str] = None,
        debit_to: Optional[str] = None,
        conversion_rate: Optional[float] = None,
        subscription: Optional[str] = None,
        overdue_days: int = 0,
    ) -> str:
        """Raise a submitted Sales Invoice Paystack can collect, returning its name."""
        CustomerFactory.create(customer_name=customer)

        invoice = SalesInvoiceFactory.create(
            rate=rate,
            company=company,
            customer=customer,
            currency=currency or cls.CURRENCY,
            conversion_rate=conversion_rate or cls.CONVERSION_RATE,
            debit_to=debit_to or cls.RECEIVABLE,
            subscription=subscription,
            overdue_days=overdue_days,
        )
        cls.billed[invoice] = customer
        return invoice

    @classmethod
    def cleanup(cls, name: str) -> None:
        """Cancel and delete the invoice, then the customer once no invoice bills it."""
        customer = cls.billed.pop(name, None) or frappe.db.get_value("Sales Invoice", name, "customer")
        SalesInvoiceFactory.cleanup(name)

        if customer and not frappe.db.exists("Sales Invoice", {"customer": customer}):
            CustomerFactory.cleanup(customer)


class SubscriptionFactory:
    """Factory for an ERPNext Subscription and the plan it bills."""

    PLAN = "_Test Paystack Subscription Plan"

    # Holds the shared plan once create_plan() inserts it.
    inserted_plan = set()

    @classmethod
    def create_plan(cls, company: str = TEST_COMPANY, cost: float = 1000) -> str:
        """Create the Subscription Plan a test subscription bills, or return it."""
        if frappe.db.exists("Subscription Plan", cls.PLAN):
            return cls.PLAN

        ItemFactory.create(item_code=TEST_ITEM)
        plan = frappe.get_doc(
            {
                "doctype": "Subscription Plan",
                "plan_name": cls.PLAN,
                "item": TEST_ITEM,
                "price_determination": "Fixed Rate",
                "cost": cost,
                "currency": frappe.db.get_value("Company", company, "default_currency"),
                "billing_interval": "Month",
                "billing_interval_count": 1,
            }
        )
        plan.flags.ignore_permissions = True
        plan.flags.ignore_mandatory = True
        plan.insert()
        cls.inserted_plan.add(cls.PLAN)
        return cls.PLAN

    @classmethod
    def create(
        cls,
        customer: str = TEST_CUSTOMER,
        company: str = TEST_COMPANY,
        cost: float = 1000,
    ) -> str:
        """Create a Subscription for a customer, returning its name."""
        CustomerFactory.create(customer_name=customer)
        plan = cls.create_plan(company=company, cost=cost)

        subscription = frappe.get_doc(
            {
                "doctype": "Subscription",
                "party_type": "Customer",
                "party": customer,
                "company": company,
                "start_date": add_days(today(), -30),
                "generate_invoice_at": "End of the current subscription period",
                "plans": [{"plan": plan, "qty": 1}],
            }
        )
        subscription.flags.ignore_permissions = True
        subscription.flags.ignore_mandatory = True
        subscription.insert()
        return subscription.name

    @classmethod
    def cleanup(cls, name: str) -> None:
        """Delete a Subscription, and the shared plan once nothing bills it."""
        cleanup_doc("Subscription", name)

        if cls.PLAN not in cls.inserted_plan:
            return

        if frappe.db.exists("Subscription Plan Detail", {"plan": cls.PLAN}):
            return

        cls.inserted_plan.discard(cls.PLAN)
        cleanup_doc("Subscription Plan", cls.PLAN)


class POSInvoiceFactory:
    """Factory for creating draft POS Invoices."""

    @classmethod
    def create(
        cls,
        amount: float = 1000,
        customer: str = TEST_CUSTOMER,
        item: str = TEST_ITEM,
        company: str = TEST_COMPANY,
        mode_of_payment: Optional[str] = "Paystack",
    ) -> str:
        """Insert a draft POS Invoice with validation skipped, returning its name.

        Pass mode_of_payment=None for a sale carrying no tender row.
        """
        CustomerFactory.create(customer_name=customer)
        ItemFactory.create(item_code=item)

        tender = (
            [{"mode_of_payment": mode_of_payment, "type": "Email", "amount": amount}]
            if mode_of_payment
            else []
        )

        invoice = frappe.get_doc(
            {
                "doctype": POS_INVOICE,
                "company": company,
                "customer": customer,
                "is_pos": 1,
                "update_stock": 0,
                "posting_date": today(),
                "due_date": today(),
                "currency": frappe.db.get_value("Company", company, "default_currency"),
                "conversion_rate": 1,
                "items": [{"item_code": item, "qty": 1, "rate": amount, "amount": amount}],
                "payments": tender,
                "total": amount,
                "net_total": amount,
                "grand_total": amount,
                "base_grand_total": amount,
            }
        )
        invoice.flags.ignore_permissions = True
        invoice.flags.ignore_validate = True
        invoice.flags.ignore_mandatory = True
        invoice.insert()
        return invoice.name

    @classmethod
    def cancel(cls, name: str) -> None:
        """Write a POS Invoice's docstatus straight to the row as cancelled."""
        frappe.db.set_value(POS_INVOICE, name, "docstatus", 2, update_modified=False)
        frappe.clear_document_cache(POS_INVOICE, name)

    @classmethod
    def cleanup(cls, name: str) -> None:
        """Delete a POS Invoice and anything raised against it."""
        cleanup_linked_payment_entries(POS_INVOICE, name)
        cleanup_linked_payment_requests(POS_INVOICE, name)
        cleanup_doc(POS_INVOICE, name)


def get_income_account(company: str = TEST_COMPANY) -> str:
    """Return the account a dunning books its interest and fee to."""
    return frappe.db.get_value("Company", company, "default_income_account") or (
        frappe.get_all(
            "Account",
            filters={
                "company": company,
                "is_group": 0,
                "root_type": "Income",
            },
            limit=1,
            pluck="name",
        )[0]
    )


class DunningFactory:
    """Factory for creating submitted Dunnings against an overdue invoice."""

    @classmethod
    def create(
        cls,
        invoice: str,
        dunning_fee: float = 10.0,
        rate_of_interest: float = 10.0,
    ) -> str:
        """Create and submit a Dunning for a Sales Invoice, returning its name.

        The interest computed follows the invoice's overdue_days.
        """
        inv = frappe.get_doc("Sales Invoice", invoice)
        schedule = inv.payment_schedule[0] if inv.payment_schedule else None

        dunning = frappe.get_doc(
            {
                "doctype": "Dunning",
                "company": inv.company,
                "customer": inv.customer,
                "posting_date": today(),
                "currency": inv.currency,
                "conversion_rate": flt(inv.conversion_rate) or 1,
                "income_account": get_income_account(inv.company),
                "cost_center": frappe.db.get_value("Company", inv.company, "cost_center"),
                "dunning_fee": dunning_fee,
                "rate_of_interest": rate_of_interest,
                "overdue_payments": [
                    {
                        "sales_invoice": inv.name,
                        "payment_schedule": schedule.name if schedule else None,
                        "payment_term": schedule.payment_term if schedule else None,
                        "due_date": inv.due_date,
                        "payment_amount": inv.grand_total,
                        "outstanding": inv.outstanding_amount,
                    }
                ],
            }
        )
        dunning.flags.ignore_permissions = True
        dunning.insert()
        dunning.submit()
        return dunning.name

    @classmethod
    def cleanup(cls, name: str) -> None:
        """Cancel and delete a Dunning and anything raised against it."""
        cleanup_linked_payment_entries("Dunning", name)
        cleanup_doc("Dunning", name)


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
        order_type: str = "Sales",
    ) -> str:
        """Create and submit a Sales Order, returning its name."""
        CustomerFactory.create(customer_name=customer)
        ItemFactory.create(item_code=item)

        so = frappe.get_doc(
            {
                "doctype": "Sales Order",
                "customer": customer,
                "company": company,
                "order_type": order_type,
                "delivery_date": today(),
                "transaction_date": today(),
                "currency": frappe.db.get_value("Company", company, "default_currency") or "NGN",
                "selling_price_list": SELLING_PRICE_LIST,
                "items": [
                    {
                        "item_code": item,
                        "qty": qty,
                        "rate": rate,
                        "price_list_rate": rate,
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
        """Cancel and delete a Sales Order and anything raised against it."""
        cleanup_linked_payment_entries("Sales Order", name)
        cleanup_linked_payment_requests("Sales Order", name)
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
        currency: Optional[str] = None,
        conversion_rate: Optional[float] = None,
        debit_to: Optional[str] = None,
    ) -> str:
        """Create and submit a credit note against an invoice.

        Pass currency/conversion_rate/debit_to together to return a foreign-currency invoice.
        """
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
                "currency": currency or frappe.db.get_value("Company", company, "default_currency") or "NGN",
                "items": [
                    {
                        "item_code": item,
                        "qty": -qty,
                        "rate": rate,
                        "price_list_rate": rate,
                    }
                ],
            }
        )
        if conversion_rate:
            cn.conversion_rate = conversion_rate
        if debit_to:
            cn.debit_to = debit_to

        cn.flags.ignore_permissions = True
        cn.flags.ignore_mandatory = True
        cn.insert()
        cn.submit()
        return cn.name

    @classmethod
    def cleanup(cls, name: str) -> None:
        """Cancel and delete a credit note and any Payment Entries against it."""
        cleanup_linked_payment_entries("Sales Invoice", name)
        cleanup_doc("Sales Invoice", name)


class GatewaySettingFactory:
    """Factory for creating Paystack Gateway Settings."""

    DOCTYPE = "Paystack Gateway Setting"

    # Each setting create() inserted, against the creation stamp of the row it wrote.
    inserted = {}

    @classmethod
    def create(
        cls,
        gateway: str = "Test Paystack Gateway",
        company: str = TEST_COMPANY,
        enabled: bool = True,
        webhook_secret: Optional[str] = None,
        allowed_ips: Optional[str] = None,
        auto_refund: bool = False,
        currency: Optional[str] = None,
        secret_key: str = "sk_test_123",
    ) -> str:
        """Create a Paystack Gateway Setting, returning its name, or the existing one.

        The currency defaults to the company's own.
        """
        ensure_mode_of_payment()

        currency = currency or frappe.db.get_value("Company", company, "default_currency")

        suspense = SuspenseAccountFactory.create(company)

        if frappe.db.exists("Paystack Gateway Setting", gateway):
            existing = frappe.get_doc("Paystack Gateway Setting", gateway)
            if existing.currency != currency or not existing.test_mode or (enabled and not existing.enabled):
                existing.currency = currency
                existing.test_mode = 1
                existing.enabled = 1 if enabled else existing.enabled
                existing.flags.ignore_permissions = True
                existing.save()
            return gateway

        setting = frappe.get_doc(
            {
                "doctype": "Paystack Gateway Setting",
                "gateway": gateway,
                "company": company,
                # Test keys are only accepted in test mode.
                "test_mode": 1,
                "secret_key": secret_key,
                "public_key": "pk_test_123",
                "webhook_secret": webhook_secret,
                "allowed_webhook_ips": allowed_ips,
                "suspense_account": suspense,
                "mode_of_payment": "Paystack",
                "currency": currency,
                "enabled": 1 if enabled else 0,
                "auto_refund_on_credit_note": 1 if auto_refund else 0,
            }
        )
        setting.flags.ignore_permissions = True
        setting.flags.ignore_links = True
        setting.insert()
        cls.inserted[setting.name] = frappe.db.get_value(cls.DOCTYPE, setting.name, "creation")
        return setting.name

    @classmethod
    def configure_settlement(
        cls,
        gateway: str,
        suspense_account: str,
        settlement_bank_account: str,
        paystack_fee_account: str,
    ) -> None:
        """Point a gateway at the three accounts a payout journal entry touches."""
        setting = frappe.get_doc("Paystack Gateway Setting", gateway)
        setting.suspense_account = suspense_account
        setting.settlement_bank_account = settlement_bank_account
        setting.paystack_fee_account = paystack_fee_account
        setting.flags.ignore_permissions = True
        setting.save()

    @classmethod
    def set_auto_charge_subscriptions(cls, gateway: str, enabled: bool) -> None:
        """Switch a gateway's automatic subscription collection on or off."""
        frappe.db.set_value(
            "Paystack Gateway Setting",
            gateway,
            "auto_charge_subscriptions",
            1 if enabled else 0,
            update_modified=False,
        )
        frappe.clear_document_cache("Paystack Gateway Setting", gateway)

    @classmethod
    def cleanup(cls, name: str) -> None:
        """Delete a gateway create() inserted, and the account raised for it."""
        stamp = cls.inserted.pop(name, None)
        if stamp is None or frappe.db.get_value(cls.DOCTYPE, name, "creation") != stamp:
            return

        suspense = frappe.db.get_value(cls.DOCTYPE, name, "suspense_account")
        cleanup_doc(cls.DOCTYPE, name)

        if suspense:
            SuspenseAccountFactory.cleanup(suspense)


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

        Without linked_docname a Sales Invoice is created. validate_payment() is mocked.
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
            frappe.db.commit()
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
            frappe.db.commit()
        return log.name

    @classmethod
    def cleanup(cls, name: str) -> None:
        """Clean up a Payment Log, its Payment Entry, and its linked document."""
        if not frappe.db.exists("Paystack Payment Log", name):
            return

        linked_doctype, linked_docname = frappe.db.get_value(
            "Paystack Payment Log", name, ["linked_doctype", "linked_docname"]
        )

        frappe.db.set_value(
            "Paystack Payment Log",
            name,
            {"status": "Pending", "payment_entry": None},
        )

        frappe.delete_doc("Paystack Payment Log", name, force=True, ignore_permissions=True)

        if linked_doctype and linked_docname:
            cleanup_linked_payment_entries(linked_doctype, linked_docname)
            cleanup_linked_payment_requests(linked_doctype, linked_docname)
            cleanup_doc(linked_doctype, linked_docname)

        frappe.db.commit()


class CustomerAuthorizationFactory:
    """Factory for the card authorizations Paystack returns with a charge."""

    DOCTYPE = "Paystack Customer Authorization"

    @classmethod
    def create(
        cls,
        customer: str = TEST_CUSTOMER,
        company: str = TEST_COMPANY,
        authorization_code: str = "AUTH_test_1234",
        signature: Optional[str] = None,
        email: str = "saved.card@example.com",
        reusable: bool = True,
        active: bool = True,
        exp_month: str = "12",
        exp_year: Optional[str] = None,
    ) -> str:
        """Store a card authorization for a customer, returning its name.

        The expiry defaults to three years ahead.
        """
        CustomerFactory.create(customer_name=customer)

        authorization = frappe.get_doc(
            {
                "doctype": cls.DOCTYPE,
                "customer": customer,
                "company": company,
                "email": email,
                "authorization_code": authorization_code,
                "signature": signature or f"SIG_{random_string(8)}",
                "last4": "4081",
                "brand": "visa",
                "card_type": "visa DEBIT",
                "bank": "Test Bank",
                "channel": "card",
                "exp_month": exp_month,
                "exp_year": exp_year or str(getdate(today()).year + 3),
                "reusable": 1 if reusable else 0,
                "active": 1 if active else 0,
            }
        )
        authorization.flags.ignore_permissions = True
        authorization.insert()
        return authorization.name

    @classmethod
    def cleanup(cls, name: str) -> None:
        """Delete a stored authorization if it exists."""
        cleanup_doc(cls.DOCTYPE, name)


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

        The referenced Payment Log must be Completed with amount_paid covering refund_amount.
        """
        if transaction_id is None:
            transaction_id = frappe.db.get_value("Paystack Payment Log", payment_log_name, "transaction_id")

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
        frappe.db.commit()
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
        """Create a Refund Log bypassing validation, for states validation rejects."""
        if transaction_id is None:
            transaction_id = frappe.db.get_value("Paystack Payment Log", payment_log_name, "transaction_id")

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
        frappe.db.commit()
        return refund_log.name

    @classmethod
    def cleanup(cls, name: str) -> None:
        """Clean up a Refund Log and its reversal Payment Entry."""
        if not frappe.db.exists("Paystack Refund Log", name):
            return

        reversal_pe = frappe.db.get_value("Paystack Refund Log", name, "reversal_payment_entry")

        frappe.db.set_value(
            "Paystack Refund Log",
            name,
            {"status": "Pending", "reversal_payment_entry": None},
        )
        frappe.delete_doc("Paystack Refund Log", name, force=True, ignore_permissions=True)

        if reversal_pe:
            cleanup_doc("Payment Entry", reversal_pe)

        frappe.db.commit()
