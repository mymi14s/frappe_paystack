# Copyright (c) 2025, Anthony Emmanuel and contributors
# For license information, please see license.txt

from typing import Optional

import frappe
import requests

from frappe_paystack.utils import get_gateway_secret


def execute(filters: Optional[dict] = None):
	columns = get_columns()
	data = get_data(filters)
	return columns, data


def get_columns() -> list:
	return [
		{"label": "Transaction ID", "fieldname": "id", "fieldtype": "Int", "width": 180},
		{"label": "Status", "fieldname": "status", "fieldtype": "Data", "width": 100},
		{"label": "Reference", "fieldname": "reference", "fieldtype": "Data", "width": 200},
		{"label": "Amount", "fieldname": "amount", "fieldtype": "Currency", "width": 120},
		{"label": "Currency", "fieldname": "currency", "fieldtype": "Data", "width": 80},
		{"label": "Customer", "fieldname": "customer", "fieldtype": "Data", "width": 250},
		{"label": "Email", "fieldname": "email", "fieldtype": "Data", "width": 200},
		{
			"label": "Doctype",
			"fieldname": "reference_doctype",
			"fieldtype": "Data",
			"width": 200,
		},
		{
			"label": "Docname",
			"fieldname": "reference_docname",
			"fieldtype": "Dynamic Link",
			"options": "reference_doctype",
			"width": 200,
		},
		{
			"label": "Payment Log",
			"fieldname": "reference_log",
			"fieldtype": "Link",
			"options": "Paystack Payment Log",
			"width": 200,
		},
		{"label": "Channel", "fieldname": "channel", "fieldtype": "Data", "width": 120},
		{"label": "Paid At", "fieldname": "paid_at", "fieldtype": "Datetime", "width": 180},
		{
			"label": "Created At",
			"fieldname": "created_at",
			"fieldtype": "Datetime",
			"width": 180,
		},
		{
			"label": "Gateway Response",
			"fieldname": "gateway_response",
			"fieldtype": "Data",
			"width": 200,
		},
		{"label": "Domain", "fieldname": "domain", "fieldtype": "Data", "width": 200},
		{"label": "IP", "fieldname": "ip_address", "fieldtype": "Data", "width": 130},
	]


def get_data(filters: dict) -> list:
	url = "https://api.paystack.co/transaction"
	headers = {"Authorization": f"Bearer {get_gateway_secret(filters.gateway)}"}
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
		frappe.throw(f"Paystack API Error: {e}")

	data = []
	if response.get("status"):
		for tx in response.get("data", []):
			tx["email"] = (tx.get("customer") or {}).get("email")
			metadata = tx["metadata"]
			metadata["reference_log"] = metadata.get("reference")
			if metadata["reference_log"]:
				del metadata["reference"]
			tx.update(metadata)
			for dl in ["log", "metadata", "authorization", "source"]:
				del tx[dl]
			tx["amount"] /= 100
			data.append(tx)

	return data