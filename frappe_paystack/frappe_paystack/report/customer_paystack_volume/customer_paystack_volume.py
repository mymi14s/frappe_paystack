import frappe

def execute(filters=None):
    cols = [
        {"label":"Customer","fieldname":"customer","fieldtype":"Link","options":"Customer","width":250},
        {"label":"Company","fieldname":"company","fieldtype":"Link","options":"Company","width":350},
        {"label":"Total Amount","fieldname":"total","fieldtype":"Currency", "options": "currency", "width":140},
        {"label":"Currency","fieldname":"currency","fieldtype":"Data","width":100}
    ]

    conditions = []
    values = {}

    if filters.get("customer"):
        conditions.append("coalesce(si.customer, so.customer) = %(customer)s")
        values["customer"] = filters["customer"]

    q = f"""
        select 
            coalesce(si.customer, so.customer) as customer,
            p.company,
            sum(p.amount) as total,
            max(p.currency) as currency
        from `tabPaystack Payment Log` p
        left join `tabSales Invoice` si on si.name = p.linked_docname
        left join `tabSales Order` so on so.name = p.linked_docname
        where p.status in ("Processed","Completed") and p.company="{filters.company}"
        {(" and " + " and ".join(conditions)) if conditions else ""}
        group by coalesce(si.customer, so.customer), p.company
    """

    data = frappe.db.sql(q, values=values, as_dict=True)
    return cols, data
