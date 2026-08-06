# Copyright (c) 2025, Anthony Emmanuel and contributors
# For license information, please see license.txt

from typing import Optional

import frappe
import requests
from frappe import _
from frappe.utils import flt

from frappe_paystack.utils import check_company_permission, get_gateway_secret

GATEWAY_DOCTYPE = "Paystack Gateway Setting"


def execute(filters: Optional[dict] = None) -> tuple:
    filters = filters or {}
    columns = get_columns()
    data = get_data(filters)
    return columns, data


def gateway_company(gateway: Optional[str]) -> Optional[str]:
    """Return the company whose Paystack account a gateway setting collects for."""
    return frappe.db.get_value(GATEWAY_DOCTYPE, gateway, "company")


def get_columns() -> list:
    return [
        {"label": _("Transaction ID"), "fieldname": "id", "fieldtype": "Int", "width": 180},
        {"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 100},
        {"label": _("Reference"), "fieldname": "reference", "fieldtype": "Data", "width": 200},
        {"label": _("Amount"), "fieldname": "amount", "fieldtype": "Currency", "width": 120},
        {"label": _("Currency"), "fieldname": "currency", "fieldtype": "Data", "width": 80},
        {"label": _("Customer"), "fieldname": "customer", "fieldtype": "Data", "width": 250},
        {"label": _("Email"), "fieldname": "email", "fieldtype": "Data", "width": 200},
        {
            "label": _("Doctype"),
            "fieldname": "reference_doctype",
            "fieldtype": "Data",
            "width": 200,
        },
        {
            "label": _("Docname"),
            "fieldname": "reference_docname",
            "fieldtype": "Dynamic Link",
            "options": "reference_doctype",
            "width": 200,
        },
        {
            "label": _("Payment Log"),
            "fieldname": "reference_log",
            "fieldtype": "Link",
            "options": "Paystack Payment Log",
            "width": 200,
        },
        {"label": _("Channel"), "fieldname": "channel", "fieldtype": "Data", "width": 120},
        {"label": _("Paid At"), "fieldname": "paid_at", "fieldtype": "Datetime", "width": 180},
        {
            "label": _("Created At"),
            "fieldname": "created_at",
            "fieldtype": "Datetime",
            "width": 180,
        },
        {
            "label": _("Gateway Response"),
            "fieldname": "gateway_response",
            "fieldtype": "Data",
            "width": 200,
        },
        {"label": _("Domain"), "fieldname": "domain", "fieldtype": "Data", "width": 200},
        {"label": _("IP"), "fieldname": "ip_address", "fieldtype": "Data", "width": 130},
    ]


def get_data(filters: dict) -> list:
    # The caller is held to the company the named gateway collects for.
    check_company_permission(gateway_company(filters.get("gateway")))

    url = "https://api.paystack.co/transaction"
    headers = {"Authorization": f"Bearer {get_gateway_secret(filters.get('gateway'))}"}
    params = {}
    if filters.get("per_page"):
        params["perPage"] = filters["per_page"]
    if filters.get("page"):
        params["page"] = filters["page"]
    if filters.get("customer"):
        params["customer"] = filters["customer"]
    if filters.get("terminalid"):
        params["terminalid"] = filters["terminalid"]
    if filters.get("status"):
        params["status"] = filters["status"]
    if filters.get("from_date"):
        params["from"] = filters["from_date"]
    if filters.get("to_date"):
        params["to"] = filters["to_date"]
    if filters.get("amount"):
        params["amount"] = filters["amount"]

    try:
        res = requests.get(url, headers=headers, params=params, timeout=30)
        res.raise_for_status()
        response = res.json()
    except Exception as e:
        frappe.throw(_("Paystack API Error: {0}").format(str(e)))

    data = []
    if response.get("status"):
        for tx in response.get("data", []):
            tx["email"] = (tx.get("customer") or {}).get("email")
            # Paystack returns "" for transactions created without metadata.
            metadata = tx.get("metadata") or {}
            if isinstance(metadata, dict):
                metadata["reference_log"] = metadata.get("reference")
                if metadata["reference_log"]:
                    metadata.pop("reference", None)
                tx.update(metadata)
            # Nested payloads the grid has no column for.
            for dl in ["log", "metadata", "authorization", "source"]:
                tx.pop(dl, None)
            tx["amount"] = flt(tx.get("amount")) / 100
            data.append(tx)

    return data
