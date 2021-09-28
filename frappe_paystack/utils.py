import frappe, hmac, json, math, hashlib, requests

def getip(**kwargs):
	return frappe.local.request_ip

def is_paystack_ip(integration, ip):
    ips = [i.ip for i in integration.ip_address]
    return ip in ips

def compute_received_hash(secretkey, data):
    try:
        hkey = frappe.utils.cstr(secretkey).encode()
        hobj = frappe.utils.cstr(data).encode()
        generated_hash = hmac.new(hkey, hobj, hashlib.sha512).hexdigest()
        return generated_hash
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), 'Paystack Hashkey')
        return None


def generate_digest(data, secret):
    return hmac.new(
        secret.encode("utf-8"), msg=data, digestmod=hashlib.sha512
    ).hexdigest()
