# Copyright (c) 2026, Anthony Emmanuel and contributors
# For license information, please see license.txt

"""
Reusable payment instruments Paystack hands back with a successful charge.

The authorization code charges the instrument and is held in a Password field.
"""

from typing import Any, Optional

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, getdate, nowdate

AUTHORIZATION_DOCTYPE = "Paystack Customer Authorization"

# Channels Paystack can charge again without the customer present.
REUSABLE_CHANNELS = ("card",)

# Fields the desk and the saved-card picker read.
PUBLIC_FIELDS = (
    "name",
    "card_label",
    "card_type",
    "brand",
    "bank",
    "last4",
    "exp_month",
    "exp_year",
    "channel",
)


class PaystackCustomerAuthorization(Document):
    def validate(self) -> None:
        self.card_label = self.build_label()

    def build_label(self) -> str:
        """Return a human label for the instrument: brand and last four digits."""
        brand = self.brand or self.card_type or self.channel or _("Card")
        if not self.last4:
            return str(brand).title()

        return f"{str(brand).title()} •••• {self.last4}"

    def has_expired(self) -> bool:
        """Report whether the stored expiry is in the past; an absent one reads unexpired."""
        month = cint(self.exp_month)
        year = cint(self.exp_year)
        if not month or not year:
            return False

        today = getdate(nowdate())
        return (year, month) < (today.year, today.month)

    def is_usable(self) -> bool:
        """Report whether this instrument can still be charged."""
        return bool(self.active and self.reusable) and not self.has_expired()

    def get_authorization_code(self) -> str:
        """Return the decrypted authorization code."""
        return self.get_password("authorization_code")


def authorization_fields(authorization: dict, customer_code: Optional[str]) -> dict:
    """Map a Paystack authorization object onto this doctype's fields."""
    return {
        "customer_code": customer_code,
        "authorization_code": authorization.get("authorization_code"),
        "signature": authorization.get("signature"),
        "last4": authorization.get("last4"),
        "exp_month": authorization.get("exp_month"),
        "exp_year": authorization.get("exp_year"),
        "card_type": authorization.get("card_type"),
        "brand": authorization.get("brand"),
        "bank": authorization.get("bank"),
        "channel": authorization.get("channel"),
        "reusable": 1 if authorization.get("reusable") else 0,
        "active": 1,
    }


def is_storable(authorization: dict) -> bool:
    """
    Report whether an authorization is worth keeping.

    True for a reusable authorization on a reusable channel carrying both a code
    and a signature.
    """
    if not authorization.get("reusable"):
        return False

    if authorization.get("channel") not in REUSABLE_CHANNELS:
        return False

    return bool(authorization.get("authorization_code") and authorization.get("signature"))


def store_authorization(
    customer: str,
    company: Optional[str],
    email: Optional[str],
    authorization: dict,
    customer_code: Optional[str] = None,
) -> Optional[str]:
    """
    Record a reusable instrument for a customer, returning its name.

    The signature identifies the card across charges. Returns None with no
    customer, or an authorization that is not storable.
    """
    if not customer or not is_storable(authorization):
        return None

    values = authorization_fields(authorization, customer_code)
    existing = frappe.db.get_value(
        AUTHORIZATION_DOCTYPE,
        {"customer": customer, "signature": values["signature"]},
        "name",
    )

    if existing:
        doc = frappe.get_doc(AUTHORIZATION_DOCTYPE, existing)
    else:
        doc = frappe.new_doc(AUTHORIZATION_DOCTYPE)
        doc.customer = customer

    doc.company = company or doc.company
    doc.email = email or doc.email
    doc.update(values)
    doc.flags.ignore_permissions = True
    doc.save()

    return doc.name


def capture_authorization(log: Any, tx: dict) -> Optional[str]:
    """Store the instrument a settled charge was paid with."""
    authorization = tx.get("authorization") or {}
    if not isinstance(authorization, dict):
        return None

    customer = frappe.db.get_value(log.linked_doctype, log.linked_docname, "customer")
    paystack_customer = tx.get("customer") or {}

    return store_authorization(
        customer=customer,
        company=log.company,
        email=paystack_customer.get("email"),
        authorization=authorization,
        customer_code=paystack_customer.get("customer_code"),
    )


def usable_authorizations(customer: str, company: Optional[str] = None) -> list:
    """Return a customer's chargeable instruments, newest first."""
    filters = {"customer": customer, "active": 1, "reusable": 1}
    if company:
        filters["company"] = company

    rows = frappe.get_all(
        AUTHORIZATION_DOCTYPE,
        filters=filters,
        fields=list(PUBLIC_FIELDS),
        order_by="modified desc",
    )

    return [row for row in rows if not frappe.get_cached_doc(AUTHORIZATION_DOCTYPE, row.name).has_expired()]
