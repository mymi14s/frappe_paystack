from typing import Optional

import frappe
from frappe import _

from frappe_paystack.utils import check_company_permission


def execute(filters: Optional[dict] = None) -> tuple:
    filters = filters or {}
    check_company_permission(filters.get("company"))

    cols = [
        {
            "label": _("Customer"),
            "fieldname": "customer",
            "fieldtype": "Link",
            "options": "Customer",
            "width": 250,
        },
        {
            "label": _("Company"),
            "fieldname": "company",
            "fieldtype": "Link",
            "options": "Company",
            "width": 350,
        },
        {
            "label": _("Total Amount"),
            "fieldname": "total",
            "fieldtype": "Currency",
            "options": "currency",
            "width": 140,
        },
        {"label": _("Currency"), "fieldname": "currency", "fieldtype": "Data", "width": 100},
    ]

    conditions = []
    values = {"company": filters.get("company")}

    if filters.get("customer"):
        conditions.append("coalesce(si.customer, so.customer) = %(customer)s")
        values["customer"] = filters["customer"]

    # nosemgrep - the interpolated text is fixed literals; values are bound
    q = f"""
        select
            coalesce(si.customer, so.customer) as customer,
            p.company,
            sum(p.amount) as total,
            max(p.currency) as currency
        from `tabPaystack Payment Log` p
        left join `tabSales Invoice` si on si.name = p.linked_docname
        left join `tabSales Order` so on so.name = p.linked_docname
        where p.status in ("Processed","Needs Attention","Completed")
        and p.company=%(company)s
        {(" and " + " and ".join(conditions)) if conditions else ""}
        group by coalesce(si.customer, so.customer), p.company
    """

    data = frappe.db.sql(q, values=values, as_dict=True)
    return cols, data
