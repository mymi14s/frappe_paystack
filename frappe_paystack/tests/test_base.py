"""Base test class with database snapshotting and teardown."""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt, now

from frappe_paystack.tests.factories import SuspenseAccountFactory, cleanup_user

TEST_COMPANY = "_Test Company"

# A company the test company's Paystack records do not belong to.
OTHER_COMPANY = "_Test Company 1"

# An Accounts Manager held to a single company by a user permission.
RESTRICTED_ACCOUNTANT = "paystack-restricted@example.com"

ACCOUNTS_SETTINGS = "Accounts Settings"
CUSTOM_FIELD = "Custom Field"
GATEWAY_SETTING = "Paystack Gateway Setting"
MODE_OF_PAYMENT = "Mode of Payment"
PAYMENT_GATEWAY = "Payment Gateway"
PAYMENT_GATEWAY_ACCOUNT = "Payment Gateway Account"
POS_SETTINGS = "POS Settings"
PROPERTY_SETTER = "Property Setter"

PAYSTACK = "Paystack"

# The POS Invoice field setup_pos_payment_mode adds by ALTER TABLE.
POS_EMAIL_FIELD = {"dt": "POS Invoice", "fieldname": "contact_email"}

# Payment Gateway Account columns the install and POS routines rewrite.
GATEWAY_ACCOUNT_FIELDS = [
    "payment_gateway",
    "payment_account",
    "currency",
    "company",
    "is_default",
    "payment_channel",
]

# POS Field columns carried by each POS Settings.invoice_fields row.
POS_FIELD_COLUMNS = [
    "name",
    "fieldname",
    "fieldtype",
    "label",
    "options",
    "reqd",
    "read_only",
    "default_value",
]


def append_row(parent, fieldname: str, values: dict) -> None:
    """Append a child row and assign it the name carried in `values`."""
    row = parent.append(fieldname, {key: value for key, value in values.items() if key != "name"})
    row.name = values["name"]


def restore_ledger_deletion(value) -> None:
    """Put the Accounts Settings ledger-deletion flag back."""
    frappe.db.set_single_value(ACCOUNTS_SETTINGS, "delete_linked_ledger_entries", value)
    frappe.db.commit()


def restore_gateway_setting_enabled(name: str) -> None:
    """Re-enable a gateway setting a test switched off."""
    if not frappe.db.exists(GATEWAY_SETTING, name):
        return

    frappe.db.set_value(GATEWAY_SETTING, name, "enabled", 1, update_modified=False)
    frappe.clear_document_cache(GATEWAY_SETTING, name)
    frappe.db.commit()


class PaystackTestCase(FrappeTestCase):
    """Base test class for frappe_paystack with enhanced teardown capabilities."""

    # Side-effect tables purged by diffing against a setUp snapshot.
    transient_doctypes = [
        "Integration Request",
        "Paystack Reconciliation Log",
        "Communication",
        "Number Card",
        "Dashboard Chart",
        # tabError Log is MyISAM, so its rows survive the rollback.
        "Error Log",
    ]

    @classmethod
    def setUpClass(cls) -> None:
        """Make ERPNext delete GL/Payment Ledger rows when test vouchers are removed."""
        cls.addClassCleanup(
            restore_ledger_deletion,
            frappe.db.get_single_value(ACCOUNTS_SETTINGS, "delete_linked_ledger_entries"),
        )
        super().setUpClass()
        frappe.db.set_single_value(ACCOUNTS_SETTINGS, "delete_linked_ledger_entries", 1)
        frappe.db.commit()

    def setUp(self) -> None:
        """Snapshot the shared records and the transient rows present now."""
        super().setUp()
        self.created_docs = []
        self.started_at = now()
        self.preexisting_rows = {
            doctype: set(frappe.get_all(doctype, pluck="name")) for doctype in self.transient_doctypes
        }
        self.snapshot_shared_records()

    def snapshot_shared_records(self) -> None:
        """Record the site-wide rows this app rewrites and register their restore."""
        self.pos_email_field_snapshot = self.read_pos_email_field()
        self.addCleanup(self.restore_pos_email_field)

        self.property_setter_snapshot = self.read_property_setters()
        self.addCleanup(self.restore_property_setters)

        self.pos_settings_snapshot = self.read_pos_settings()
        self.addCleanup(self.restore_pos_settings)

        self.mode_of_payment_snapshot = self.read_mode_of_payment()
        self.addCleanup(self.restore_mode_of_payment)

        self.gateway_account_snapshot = self.read_gateway_accounts()
        self.addCleanup(self.restore_gateway_accounts)

        self.payment_gateway_snapshot = self.read_payment_gateway()
        self.addCleanup(self.restore_payment_gateway)

    def read_payment_gateway(self) -> dict:
        """Return the shared Payment Gateway row, empty when it is absent."""
        return (
            frappe.db.get_value(
                PAYMENT_GATEWAY,
                PAYSTACK,
                ["gateway_settings", "gateway_controller"],
                as_dict=True,
            )
            or {}
        )

    def restore_payment_gateway(self) -> None:
        """Put the shared Payment Gateway back exactly as the test found it."""
        snapshot = self.payment_gateway_snapshot
        if self.read_payment_gateway() == snapshot:
            return

        if not snapshot:
            frappe.delete_doc(PAYMENT_GATEWAY, PAYSTACK, force=True, ignore_permissions=True)
        elif frappe.db.exists(PAYMENT_GATEWAY, PAYSTACK):
            frappe.db.set_value(PAYMENT_GATEWAY, PAYSTACK, snapshot, update_modified=False)
            frappe.clear_document_cache(PAYMENT_GATEWAY, PAYSTACK)
        else:
            gateway = frappe.get_doc({"doctype": PAYMENT_GATEWAY, "gateway": PAYSTACK, **snapshot})
            gateway.flags.ignore_permissions = True
            gateway.flags.ignore_links = True
            gateway.insert()

        frappe.db.commit()

    def read_gateway_accounts(self) -> dict:
        """Return every Payment Gateway Account, keyed by name."""
        return {
            row.pop("name"): row
            for row in frappe.get_all(PAYMENT_GATEWAY_ACCOUNT, fields=["name"] + GATEWAY_ACCOUNT_FIELDS)
        }

    def restore_gateway_accounts(self) -> None:
        """Drop accounts the test created and undo its edits to the rest."""
        snapshot = self.gateway_account_snapshot
        current = self.read_gateway_accounts()
        if current == snapshot:
            return

        for name in current.keys() - snapshot.keys():
            frappe.delete_doc(PAYMENT_GATEWAY_ACCOUNT, name, force=True, ignore_permissions=True)

        for name, values in snapshot.items():
            if name not in current:
                account = frappe.get_doc({"doctype": PAYMENT_GATEWAY_ACCOUNT, **values})
                account.flags.ignore_permissions = True
                account.flags.ignore_links = True
                account.insert(set_name=name)
            elif current[name] != values:
                frappe.db.set_value(PAYMENT_GATEWAY_ACCOUNT, name, values, update_modified=False)
                frappe.clear_document_cache(PAYMENT_GATEWAY_ACCOUNT, name)

        frappe.db.commit()

    def read_mode_of_payment(self) -> dict:
        """Return the shared Mode of Payment, empty when it is absent."""
        if not frappe.db.exists(MODE_OF_PAYMENT, PAYSTACK):
            return {}

        mode = frappe.get_doc(MODE_OF_PAYMENT, PAYSTACK)
        return {
            "type": mode.type,
            "enabled": mode.enabled,
            "accounts": [
                {
                    "name": row.name,
                    "company": row.company,
                    "default_account": row.default_account,
                }
                for row in mode.accounts
            ],
        }

    def restore_mode_of_payment(self) -> None:
        """Put the Mode of Payment back exactly as the test found it."""
        snapshot = self.mode_of_payment_snapshot
        current = self.read_mode_of_payment()
        if current == snapshot:
            return

        if not snapshot:
            frappe.delete_doc(MODE_OF_PAYMENT, PAYSTACK, force=True, ignore_permissions=True)
            frappe.db.commit()
            return

        if current:
            mode = frappe.get_doc(MODE_OF_PAYMENT, PAYSTACK)
        else:
            mode = frappe.new_doc(MODE_OF_PAYMENT)
            mode.mode_of_payment = PAYSTACK

        mode.type = snapshot["type"]
        mode.enabled = snapshot["enabled"]
        mode.accounts = []
        for row in snapshot["accounts"]:
            append_row(mode, "accounts", row)

        mode.flags.ignore_permissions = True
        if current:
            mode.save()
        else:
            mode.insert(set_child_names=False)
        frappe.db.commit()

    def read_pos_settings(self) -> list:
        """Return the POS invoice field rows, in screen order."""
        return [
            {column: row.get(column) for column in POS_FIELD_COLUMNS}
            for row in frappe.get_single(POS_SETTINGS).invoice_fields
        ]

    def restore_pos_settings(self) -> None:
        """Put the POS invoice fields back, in their original order."""
        if self.read_pos_settings() == self.pos_settings_snapshot:
            return

        settings = frappe.get_single(POS_SETTINGS)
        settings.invoice_fields = []
        for row in self.pos_settings_snapshot:
            append_row(settings, "invoice_fields", row)

        settings.flags.ignore_permissions = True
        settings.save()
        frappe.db.commit()

    def read_property_setters(self) -> set:
        """Return the Mode of Payment property setters that already exist."""
        return set(frappe.get_all(PROPERTY_SETTER, filters={"doc_type": MODE_OF_PAYMENT}, pluck="name"))

    def restore_property_setters(self) -> None:
        """Remove the Mode of Payment property setters the test added."""
        added = self.read_property_setters() - self.property_setter_snapshot
        if not added:
            return

        for name in added:
            frappe.delete_doc(PROPERTY_SETTER, name, force=True, ignore_permissions=True)

        frappe.clear_cache(doctype=MODE_OF_PAYMENT)
        frappe.db.commit()

    def read_pos_email_field(self) -> bool:
        """Report whether POS Invoice already carries the contact_email field."""
        return bool(frappe.db.exists(CUSTOM_FIELD, POS_EMAIL_FIELD))

    def restore_pos_email_field(self) -> None:
        """Drop the POS Invoice email field if the test was the one to add it."""
        if self.pos_email_field_snapshot:
            return

        name = frappe.db.get_value(CUSTOM_FIELD, POS_EMAIL_FIELD, "name")
        if not name:
            return

        frappe.delete_doc(CUSTOM_FIELD, name, force=True, ignore_permissions=True)
        frappe.db.commit()

    def suspense_account(self, company: str = TEST_COMPANY) -> str:
        """Return the clearing account Paystack settles into, registering its cleanup."""
        account = SuspenseAccountFactory.create(company)
        self.addCleanup(SuspenseAccountFactory.cleanup, account)
        return account

    def become_restricted_accountant(self, company: str = OTHER_COMPANY) -> str:
        """Sign in as an Accounts Manager carrying a single Company permission."""
        if not frappe.db.exists("User", RESTRICTED_ACCOUNTANT):
            user = frappe.get_doc(
                {
                    "doctype": "User",
                    "email": RESTRICTED_ACCOUNTANT,
                    "first_name": "Paystack Restricted Accountant",
                    "send_welcome_email": 0,
                    "roles": [{"role": "Accounts Manager"}],
                }
            )
            user.flags.ignore_permissions = True
            user.insert()
            frappe.db.commit()
        self.addCleanup(cleanup_user, RESTRICTED_ACCOUNTANT)

        permission = frappe.get_doc(
            {
                "doctype": "User Permission",
                "user": RESTRICTED_ACCOUNTANT,
                "allow": "Company",
                "for_value": company,
            }
        )
        permission.flags.ignore_permissions = True
        permission.insert()
        self.addCleanup(
            frappe.delete_doc,
            "User Permission",
            permission.name,
            force=True,
            ignore_permissions=True,
        )
        frappe.db.commit()
        frappe.clear_cache(user=RESTRICTED_ACCOUNTANT)

        self.addCleanup(frappe.set_user, "Administrator")
        frappe.set_user(RESTRICTED_ACCOUNTANT)
        return RESTRICTED_ACCOUNTANT

    def disable_company_gateways(self, company: str = TEST_COMPANY) -> None:
        """Switch off a company's enabled gateways, re-enabling them afterwards."""
        for name in frappe.get_all(GATEWAY_SETTING, filters={"enabled": 1, "company": company}, pluck="name"):
            self.addCleanup(restore_gateway_setting_enabled, name)
            frappe.db.set_value(GATEWAY_SETTING, name, "enabled", 0, update_modified=False)
            frappe.clear_document_cache(GATEWAY_SETTING, name)

    def tearDown(self) -> None:
        """Tear down test fixtures and clean up database."""
        try:
            super().tearDown()
        finally:
            self.cleanup_test_data()

    def track_doc(self, doctype: str, docname: str) -> None:
        """Track a document so tearDown deletes it."""
        self.created_docs.append((doctype, docname))

    def cleanup_test_data(self) -> None:
        """Delete tracked documents durably and drop them from cache."""
        for doctype, docname in reversed(self.created_docs):
            frappe.delete_doc(doctype, docname, force=True, ignore_permissions=True)
            frappe.clear_document_cache(doctype, docname)

        self.created_docs.clear()
        self.cleanup_transient_rows()
        # Holds the deletions through FrappeTestCase's class-teardown rollback.
        frappe.db.commit()

    def cleanup_transient_rows(self) -> None:
        """Delete the session-owned side-effect rows that appeared after setUp."""
        for doctype, existing in self.preexisting_rows.items():
            mine = frappe.get_all(
                doctype,
                filters={
                    "owner": frappe.session.user,
                    "creation": [">=", self.started_at],
                },
                pluck="name",
            )
            for name in set(mine) - existing:
                frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)

    def create_and_track_doc(
        self,
        doctype: str,
        doc_dict: dict,
        commit: bool = True,
        ignore_permissions: bool = True,
    ) -> str:
        """Create a document, track it for cleanup, and return its name."""
        doc = frappe.get_doc(doctype, doc_dict)
        doc.flags.ignore_permissions = ignore_permissions
        doc.insert()

        if commit:
            frappe.db.commit()

        self.track_doc(doctype, doc.name)
        return doc.name

    def cleanup_reconciliation_log(self, payment_log_name: str) -> None:
        """Delete the reconciliation log tied to a payment log, if any."""
        if frappe.db.exists("Paystack Reconciliation Log", payment_log_name):
            frappe.delete_doc(
                "Paystack Reconciliation Log",
                payment_log_name,
                force=True,
                ignore_permissions=True,
            )
            frappe.db.commit()

    def gl_entries_by_account(self, voucher_no: str) -> dict:
        """Return {account: {"debit": x, "credit": y}} for a submitted voucher."""
        rows = {}
        for row in frappe.get_all(
            "GL Entry",
            filters={"voucher_no": voucher_no, "is_cancelled": 0},
            fields=["account", "debit", "credit"],
        ):
            totals = rows.setdefault(row.account, {"debit": 0.0, "credit": 0.0})
            totals["debit"] += flt(row.debit)
            totals["credit"] += flt(row.credit)

        return rows

    def assert_doc_exists(self, doctype: str, docname: str) -> None:
        """Assert that a document exists in the database."""
        exists = frappe.db.exists(doctype, docname)
        self.assertTrue(exists, f"{doctype} {docname} does not exist")

    def assert_doc_not_exists(self, doctype: str, docname: str) -> None:
        """Assert that a document does not exist in the database."""
        exists = frappe.db.exists(doctype, docname)
        self.assertFalse(exists, f"{doctype} {docname} should not exist")

    def assert_field_value(self, doctype: str, docname: str, fieldname: str, expected_value) -> None:
        """Assert that a document field has the expected value."""
        actual_value = frappe.db.get_value(doctype, docname, fieldname)
        self.assertEqual(
            actual_value,
            expected_value,
            f"{doctype}.{fieldname} is {actual_value}, expected {expected_value}",
        )
