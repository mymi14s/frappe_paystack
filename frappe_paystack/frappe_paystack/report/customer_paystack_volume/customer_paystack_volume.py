import frappe

def execute(filters=None):
    cols=[
        {"label":"Customer","fieldname":"customer","fieldtype":"Link","options":"Customer","width":250},
        {"label":"Company","fieldname":"company","fieldtype":"Link","options":"Company","width":350},
        {"label":"Total Amount","fieldname":"total","fieldtype":"Currency", "options": "currency", "width":140},
        {"label":"Currency","fieldname":"currency","fieldtype":"Data","width":100}
    ]
    q="""
        select si.customer, p.company, sum(p.amount) as total, max(p.currency) as currency 
        from 
        `tabPaystack Payment Log` p 
        join `tabSales Invoice` si on si.name=p.linked_docname 
        where 
        p.status in ("Processed","Completed") group by si.customer, p.company
    """ 
    data=frappe.db.sql(q, as_dict=True)
    return cols, data
