# Copyright (c) 2024, Anthony C. Emmanuel and contributors
# For license information, please see license.txt

from typing import Any, Optional

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.model.document import Document
from frappe.utils import call_hook_method, flt, get_url

from frappe_paystack.setup import (
    GATEWAY_DOCTYPE,
    create_payment_gateway_account,
    create_payment_gateway_record,
    repoint_payment_gateway_controller,
    update_payment_gateway_controller,
)
from frappe_paystack.utils import PAYSTACK_SERVICE, SUPPORTED_CURRENCIES, request_pos_charge
from frappe_paystack.utils.pos_payment import notify_pos_charge_started

# Paystack names the environment in the key prefix.
TEST_KEY_PREFIXES = ("sk_test_", "pk_test_")
LIVE_KEY_PREFIXES = ("sk_live_", "pk_live_")

KEY_FIELDS = ("secret_key", "public_key")

# Accounts the settlement journal entry is raised against.
SETTLEMENT_ACCOUNT_FIELDS = (
    "suspense_account",
    "settlement_bank_account",
    "paystack_fee_account",
)


def company_for_reference(doctype: Optional[str], docname: Optional[str]) -> Optional[str]:
    """
    Return the company a document books against.

    A doctype with no company field, and an unnamed reference, both answer None.
    """
    if not doctype or not docname:
        return None

    if not frappe.get_meta(doctype).has_field("company"):
        return None

    return frappe.db.get_value(doctype, docname, "company")


class PaystackGatewaySetting(Document):
    supported_currencies = SUPPORTED_CURRENCIES

    def validate(self) -> None:
        self.check_enabled()
        self.validate_company_currency()
        self.validate_key_mode()
        self.validate_settlement_accounts()
        if self.enabled:
            self.register_as_payment_gateway()

    def get_secret_key(self) -> str:
        return self.get_password("secret_key")

    def for_company(self, company: Optional[str]) -> "PaystackGatewaySetting":
        """
        Return the gateway setting that collects a company's money.

        An unnamed company, and the company this setting already serves, both
        answer this setting.
        """
        if not company or company == self.company:
            return self

        name = frappe.db.get_value(GATEWAY_DOCTYPE, {"company": company, "enabled": 1}, "name")
        if not name:
            frappe.throw(
                _(
                    "Paystack is not enabled for {0}. Enable a Paystack Gateway "
                    "Setting for {0} before collecting its payments."
                ).format(company)
            )

        return frappe.get_doc(GATEWAY_DOCTYPE, name)

    def validate_key_mode(self) -> None:
        """Throw on live keys under Test Mode, and on test keys outside it."""
        for fieldname in KEY_FIELDS:
            key = self.get_password(fieldname, raise_exception=False) or ""
            label = _(self.meta.get_label(fieldname))

            if self.test_mode and key.startswith(LIVE_KEY_PREFIXES):
                frappe.throw(
                    _(
                        "Test Mode is on, so {0} must be a Paystack test key. "
                        "This is a live key and would charge real cards."
                    ).format(label)
                )

            if not self.test_mode and key.startswith(TEST_KEY_PREFIXES):
                frappe.throw(
                    _(
                        "{0} is a Paystack test key, which captures no money. "
                        "Switch Test Mode on, or enter the live key."
                    ).format(label)
                )

    def validate_settlement_accounts(self) -> None:
        """Refuse a settlement account belonging to another company."""
        for fieldname in SETTLEMENT_ACCOUNT_FIELDS:
            account = self.get(fieldname)
            if not account:
                continue

            owner = frappe.db.get_value("Account", account, "company")
            if owner != self.company:
                frappe.throw(
                    _("{0} {1} belongs to {2}, not {3}.").format(
                        _(self.meta.get_label(fieldname)), account, owner, self.company
                    )
                )

    def validate_company_currency(self) -> None:
        """Refuse a gateway whose currency differs from the company's default."""
        company_currency = frappe.db.get_value("Company", self.company, "default_currency")
        if not company_currency or self.currency == company_currency:
            return

        frappe.throw(
            _(
                "Gateway currency {0} is not the default currency of {1}, which is "
                "{2}. Paystack is charged the company-currency amount, so set the "
                "gateway currency to {2}."
            ).format(self.currency, self.company, company_currency)
        )

    def check_enabled(self) -> None:
        """Allow one enabled gateway per company."""
        if self.enabled:
            enabled_gateway = frappe.db.get_list(
                self.doctype,
                filters={
                    "enabled": 1,
                    "company": self.company,
                    "name": ["!=", self.name],
                },
                fields=["name"],
            )
            if enabled_gateway:
                route = self.doctype.lower().replace(" ", "-")
                other = enabled_gateway[0].name
                link = f'<a class="text-danger" href="/app/{route}/{other}">{other}</a>'
                frappe.throw(
                    _("Another gateway is enabled, disable it before enabling this one.") + f"<br>{link}"
                )

    def validate_transaction_currency(self, currency: str) -> None:
        if currency not in self.supported_currencies:
            frappe.throw(
                _(
                    "Please select another payment method. Paystack does not support "
                    "transactions in currency '{0}'"
                ).format(currency)
            )

    def get_supported_currency(self) -> list:
        return self.supported_currencies

    def get_payment_url(self, **kwargs) -> str:
        """Return the Paystack checkout URL for a Payment Request."""
        amount = kwargs.get("amount", 0)
        currency = kwargs.get("currency", "NGN")
        reference_doctype = kwargs.get("reference_doctype")
        reference_docname = kwargs.get("reference_docname")

        payment_request = None
        if reference_doctype == "Payment Request":
            # ERPNext names the Payment Request; resolve through to the document it bills.
            payment_request = reference_docname
            reference_doctype, reference_docname = frappe.db.get_value(
                "Payment Request",
                payment_request,
                ["reference_doctype", "reference_name"],
            )

        # ERPNext asks for the URL twice per checkout; the open log is reused.
        if payment_request:
            existing = frappe.db.get_value(
                "Paystack Payment Log",
                {"payment_request": payment_request, "status": "Pending"},
                "name",
            )
            if existing:
                return get_url(f"/paystack-checkout/{existing}")

        # The log carries the company that is billed.
        setting = self.for_company(company_for_reference(reference_doctype, reference_docname))

        log = frappe.new_doc("Paystack Payment Log")
        log.company = setting.company
        log.linked_doctype = reference_doctype
        log.linked_docname = reference_docname
        log.amount = amount
        log.currency = currency
        log.status = "Pending"
        log.payment_request = payment_request
        log.flags.ignore_permissions = True
        log.insert()

        # Keeps the inserted log through Frappe's GET rollback.
        frappe.local.flags.commit = True

        return get_url(f"/paystack-checkout/{log.name}")

    def on_payment_request_submission(self, payment_request: Any) -> bool:
        """
        Report whether Paystack can collect this Payment Request.

        ERPNext skips the checkout URL and the payment email when this is false.
        """
        return payment_request.currency in self.supported_currencies

    def request_for_payment(self, **kwargs) -> None:
        """
        Start a Paystack charge for a POS "Phone" mode of payment.

        Initialises the charge and pushes the checkout URL to the till.
        """
        args = frappe._dict(kwargs)
        payment_request = frappe.get_doc("Payment Request", args.reference_docname)
        setting = self.for_company(payment_request.company)

        integration_request = create_request_log(
            kwargs,
            service_name=PAYSTACK_SERVICE,
            is_remote_request=1,
            status="Queued",
            url="pos/charge",
            reference_doctype="Payment Request",
            reference_docname=payment_request.name,
        )

        try:
            response = request_pos_charge(
                company=setting.company,
                email=payment_request.email_to,
                amount=flt(args.request_amount),
                currency=args.currency,
                reference=integration_request.name,
            )
            integration_request.handle_success(response)
            notify_pos_charge_started(payment_request, integration_request.name, response)
        except Exception:
            integration_request.handle_failure({"message": _("Could not start the Paystack charge.")})
            frappe.log_error(
                title=f"Paystack POS charge failed: {payment_request.name}",
                message=frappe.get_traceback(),
            )
            raise

    def on_trash(self) -> None:
        """Repoint the Payment Gateway away from this setting."""
        repoint_payment_gateway_controller(self.name)

    def register_as_payment_gateway(self) -> None:
        """
        Register this setting as a Payment Gateway and create its account.

        A failure here is logged and the save continues.
        """
        try:
            create_payment_gateway_record()
            update_payment_gateway_controller(self.name)
            call_hook_method("payment_gateway_enabled", gateway="Paystack")

            if self.suspense_account:
                create_payment_gateway_account(
                    company=self.company,
                    suspense_account=self.suspense_account,
                    currency=self.currency or "NGN",
                )
        except Exception:
            frappe.log_error(
                "Paystack could not be registered as a Payment Gateway",
                frappe.get_traceback(),
            )
