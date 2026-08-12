"""Tests for the app as installed: its modules, fixtures, desk card and setup."""

import importlib
import json
import os
import pkgutil
from unittest.mock import patch

import frappe
from payments.utils import get_payment_gateway_controller

import frappe_paystack
from frappe_paystack.config.desktop import get_data
from frappe_paystack.setup import (
    DASHBOARD_CHART,
    DASHBOARD_CHARTS,
    GATEWAY_DOCTYPE,
    MODE_OF_PAYMENT,
    NUMBER_CARD,
    NUMBER_CARDS,
    POS_INVOICE_FIELDS,
    after_install,
    create_dashboard_widgets,
    create_payment_gateway_account,
    create_payment_gateway_accounts_for_existing_settings,
    create_payment_gateway_record,
    ensure_mode_of_payment,
    ensure_pos_invoice_fields,
    repoint_payment_gateway_controller,
    update_payment_gateway_controller,
)
from frappe_paystack.tests.factories import TEST_COMPANY, GatewaySettingFactory
from frappe_paystack.tests.test_base import PaystackTestCase

PAYMENT_GATEWAY = "Payment Gateway"
PAYMENT_GATEWAY_ACCOUNT = "Payment Gateway Account"
POS_SETTINGS = "POS Settings"

# A Mode of Payment name of this test's own.
SCRATCH_MODE_OF_PAYMENT = "Paystack Setup Test Mode"

# Gateway settings the repointing test owns: one it deletes, one it falls back to.
DOOMED_GATEWAY = "Paystack Repointing Test Gateway"
SURVIVING_GATEWAY = "Paystack Repointing Fallback Gateway"

# A Sales Invoice field outside the app's registered set.
SCRATCH_POS_FIELD = {
    "fieldname": "po_no",
    "label": "Customer's Purchase Order",
    "fieldtype": "Data",
    "read_only": 0,
}


class InstallStateTestCase(PaystackTestCase):
    """Reads back the Payment Gateway records the install routines rewrite."""

    def account_for(self, company: str) -> dict:
        """Return the Paystack Payment Gateway Account of a company."""
        return frappe.db.get_value(
            PAYMENT_GATEWAY_ACCOUNT,
            {"payment_gateway": "Paystack", "company": company},
            ["name", "payment_account", "currency"],
            as_dict=True,
        )


class TestEnsureModeOfPayment(PaystackTestCase):
    """ensure_mode_of_payment creates the mode only when it is missing."""

    def delete_scratch_mode(self) -> None:
        """Remove the Mode of Payment this test created."""
        frappe.delete_doc(
            "Mode of Payment",
            SCRATCH_MODE_OF_PAYMENT,
            force=True,
            ignore_permissions=True,
        )
        frappe.clear_document_cache("Mode of Payment", SCRATCH_MODE_OF_PAYMENT)
        frappe.db.commit()

    def test_existing_mode_is_returned_untouched(self) -> None:
        """The shipped Paystack mode is returned with its modified timestamp intact."""
        before = frappe.db.get_value("Mode of Payment", MODE_OF_PAYMENT, "modified")

        self.assertEqual(ensure_mode_of_payment(), MODE_OF_PAYMENT)
        self.assertEqual(frappe.db.get_value("Mode of Payment", MODE_OF_PAYMENT, "modified"), before)

    def test_missing_mode_is_created_as_general(self) -> None:
        """A missing mode is created enabled and typed General by default."""
        self.addCleanup(self.delete_scratch_mode)

        with patch("frappe_paystack.setup.MODE_OF_PAYMENT", SCRATCH_MODE_OF_PAYMENT):
            created = ensure_mode_of_payment()

        self.assertEqual(created, SCRATCH_MODE_OF_PAYMENT)
        mode = frappe.get_doc("Mode of Payment", SCRATCH_MODE_OF_PAYMENT)
        self.assertEqual(mode.type, "General")
        self.assertTrue(mode.enabled)

    def test_missing_mode_is_created_as_phone_for_pos(self) -> None:
        """A missing mode requested for POS is typed Phone."""
        self.addCleanup(self.delete_scratch_mode)

        with patch("frappe_paystack.setup.MODE_OF_PAYMENT", SCRATCH_MODE_OF_PAYMENT):
            ensure_mode_of_payment(pos_enabled=True)

        self.assertEqual(
            frappe.db.get_value("Mode of Payment", SCRATCH_MODE_OF_PAYMENT, "type"),
            "Phone",
        )


class TestEnsurePosInvoiceFields(PaystackTestCase):
    """ensure_pos_invoice_fields adds only the fields that are missing."""

    def test_already_present_fields_are_not_duplicated(self) -> None:
        """Running the setup twice leaves one row per field."""
        ensure_pos_invoice_fields()

        fieldnames = [row.fieldname for row in frappe.get_single("POS Settings").invoice_fields]
        self.assertEqual(len(fieldnames), len(set(fieldnames)))

    def test_missing_field_is_appended(self) -> None:
        """A field the POS screen lacks is appended with its label and type."""
        with patch("frappe_paystack.setup.POS_INVOICE_FIELDS", [SCRATCH_POS_FIELD]):
            ensure_pos_invoice_fields()

        rows = {row.fieldname: row for row in frappe.get_single("POS Settings").invoice_fields}
        self.assertIn(SCRATCH_POS_FIELD["fieldname"], rows)
        self.assertEqual(rows[SCRATCH_POS_FIELD["fieldname"]].label, SCRATCH_POS_FIELD["label"])

    def seed_site_field(self) -> str:
        """Give the POS screen a field of the site's own, at the top."""
        settings = frappe.get_single(POS_SETTINGS)
        settings.invoice_fields = []
        settings.append("invoice_fields", dict(SCRATCH_POS_FIELD))
        settings.flags.ignore_permissions = True
        settings.save()

        return SCRATCH_POS_FIELD["fieldname"]

    def screen_order(self) -> list:
        """Return the fieldnames the POS screen renders, in screen order."""
        return [row.fieldname for row in frappe.get_single(POS_SETTINGS).invoice_fields]

    def test_a_field_the_site_owns_is_kept(self) -> None:
        """A field the site already carries survives the rewrite."""
        own = self.seed_site_field()

        ensure_pos_invoice_fields()

        self.assertIn(own, self.screen_order())

    def test_a_field_the_site_owns_is_placed_below_the_paystack_fields(self) -> None:
        """The Paystack fields come first and the site's own follows them."""
        own = self.seed_site_field()

        ensure_pos_invoice_fields()

        self.assertEqual(
            self.screen_order(),
            [field["fieldname"] for field in POS_INVOICE_FIELDS] + [own],
        )


class TestUpdatePaymentGatewayController(InstallStateTestCase):
    """update_payment_gateway_controller retargets the Payment Gateway."""

    def test_controller_is_pointed_at_the_setting(self) -> None:
        """The Payment Gateway names the Paystack Gateway Setting it was given."""
        gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, gateway)

        update_payment_gateway_controller(gateway)

        self.assertEqual(
            frappe.db.get_value(PAYMENT_GATEWAY, "Paystack", "gateway_controller"),
            gateway,
        )

    def test_missing_payment_gateway_is_a_no_op(self) -> None:
        """The controller holds its value when no Payment Gateway record exists."""
        before = frappe.db.get_value(PAYMENT_GATEWAY, "Paystack", "gateway_controller")

        with patch("frappe_paystack.setup.frappe.db.exists", return_value=False):
            update_payment_gateway_controller("Nowhere")

        self.assertEqual(
            frappe.db.get_value(PAYMENT_GATEWAY, "Paystack", "gateway_controller"),
            before,
        )


class TestCreatePaymentGatewayAccount(InstallStateTestCase):
    """create_payment_gateway_account creates or retargets one account."""

    def test_existing_account_is_retargeted(self) -> None:
        """An existing account takes the payment account it is given."""
        suspense = self.suspense_account()

        name = create_payment_gateway_account(company=TEST_COMPANY, suspense_account=suspense, currency="NGN")

        account = frappe.get_doc(PAYMENT_GATEWAY_ACCOUNT, name)
        self.assertEqual(account.payment_account, suspense)
        self.assertEqual(account.company, TEST_COMPANY)

    def test_a_second_call_reuses_one_account(self) -> None:
        """A second call for the same company updates the one existing account."""
        suspense = self.suspense_account()

        first = create_payment_gateway_account(company=TEST_COMPANY, suspense_account=suspense)
        second = create_payment_gateway_account(
            company=TEST_COMPANY, suspense_account=suspense, currency="USD"
        )

        self.assertEqual(first, second)
        self.assertEqual(
            frappe.db.count(
                PAYMENT_GATEWAY_ACCOUNT,
                {"payment_gateway": "Paystack", "company": TEST_COMPANY},
            ),
            1,
        )


class TestAfterInstall(InstallStateTestCase):
    """after_install wires up the gateway, the mode of payment and the accounts."""

    def setUp(self) -> None:
        """Enable a Paystack gateway for the test company."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

    def test_payment_gateway_and_mode_of_payment_exist(self) -> None:
        """after_install leaves the Payment Gateway and Mode of Payment in place."""
        after_install()

        self.assertTrue(frappe.db.exists(PAYMENT_GATEWAY, "Paystack"))
        self.assertTrue(frappe.db.exists("Mode of Payment", MODE_OF_PAYMENT))

    def test_enabled_setting_gets_a_gateway_account(self) -> None:
        """Each enabled setting ends up with an account on its suspense account."""
        after_install()

        suspense = frappe.db.get_value(GATEWAY_DOCTYPE, self.gateway, "suspense_account")
        account = self.account_for(TEST_COMPANY)
        self.assertEqual(account["payment_account"], suspense)

    def test_gateway_record_creation_is_idempotent(self) -> None:
        """Registering the Payment Gateway twice leaves a single record."""
        create_payment_gateway_record()
        create_payment_gateway_record()

        self.assertEqual(frappe.db.count(PAYMENT_GATEWAY, {"name": "Paystack"}), 1)

    def test_setting_without_a_suspense_account_gets_no_account(self) -> None:
        """A setting with no suspense account leaves the existing account as is."""
        before = self.account_for(TEST_COMPANY)
        frappe.db.set_value(GATEWAY_DOCTYPE, self.gateway, "suspense_account", None)
        frappe.clear_document_cache(GATEWAY_DOCTYPE, self.gateway)

        create_payment_gateway_accounts_for_existing_settings()

        after = self.account_for(TEST_COMPANY)
        self.assertEqual(after["name"], before["name"])
        self.assertEqual(after["payment_account"], before["payment_account"])


class TestGatewayControllerRepointing(PaystackTestCase):
    """Deleting a gateway setting repoints the Payment Gateway."""

    def controller(self) -> str:
        """Return the Payment Gateway's current controller."""
        return frappe.db.get_value("Payment Gateway", "Paystack", "gateway_controller")

    def test_deleting_the_active_setting_clears_the_pointer(self) -> None:
        """The controller moves off a setting once that setting is deleted."""
        original = self.controller()
        self.addCleanup(update_payment_gateway_controller, original)

        gateway = GatewaySettingFactory.create()
        self.assertEqual(self.controller(), gateway)

        GatewaySettingFactory.cleanup(gateway)

        self.assertNotEqual(self.controller(), gateway)

    def test_the_controller_always_resolves(self) -> None:
        """get_payment_gateway_controller works after a setting is removed."""
        create_payment_gateway_record()
        self.addCleanup(update_payment_gateway_controller, self.controller())

        # Repointing picks an enabled setting.
        survivor = GatewaySettingFactory.create(gateway=SURVIVING_GATEWAY)
        self.addCleanup(GatewaySettingFactory.cleanup, survivor)

        removed = GatewaySettingFactory.create(gateway=DOOMED_GATEWAY, enabled=False)
        update_payment_gateway_controller(removed)
        GatewaySettingFactory.cleanup(removed)

        controller = get_payment_gateway_controller("Paystack")

        self.assertNotEqual(controller.name, removed)
        self.assertTrue(frappe.db.exists(GATEWAY_DOCTYPE, controller.name))

    def test_unrelated_setting_deletion_leaves_pointer_alone(self) -> None:
        """The pointer holds when some other setting is removed."""
        original = self.controller()
        self.addCleanup(update_payment_gateway_controller, original)

        repoint_payment_gateway_controller("Some Other Setting")

        self.assertEqual(self.controller(), original)


class TestLastSettingRemoval(PaystackTestCase):
    """Deleting the only setting retires the Payment Gateway."""

    def retire_only_setting(self) -> str:
        """Point the gateway at a setting and remove its last fallback."""
        create_payment_gateway_record()
        gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, gateway)
        update_payment_gateway_controller(gateway)

        with patch("frappe_paystack.setup.find_replacement_setting", return_value=None):
            repoint_payment_gateway_controller(gateway)

        return gateway

    def test_the_gateway_is_retired_rather_than_left_pointing_nowhere(self) -> None:
        """With no setting left, Paystack stops being offered as a gateway."""
        self.retire_only_setting()

        self.assertFalse(frappe.db.exists(PAYMENT_GATEWAY, "Paystack"))

    def test_no_settings_not_found_error_is_produced(self) -> None:
        """Resolving a retired gateway raises DoesNotExistError."""
        self.retire_only_setting()

        with self.assertRaises(frappe.DoesNotExistError):
            get_payment_gateway_controller("Paystack")

    def test_re_enabling_a_setting_brings_the_gateway_back(self) -> None:
        """A new enabled setting registers the Payment Gateway again."""
        GatewaySettingFactory.cleanup(self.retire_only_setting())

        revived = GatewaySettingFactory.create(gateway=SURVIVING_GATEWAY)
        self.addCleanup(GatewaySettingFactory.cleanup, revived)

        self.assertEqual(
            frappe.db.get_value(PAYMENT_GATEWAY, "Paystack", "gateway_controller"),
            revived,
        )


class TestDashboardWidgets(PaystackTestCase):
    """The Paystack workspace renders once every widget it names exists."""

    def workspace(self) -> dict:
        """Return the workspace definition the app ships."""
        path = frappe.get_app_path(
            "frappe_paystack",
            "frappe_paystack",
            "workspace",
            "paystack_dashboard",
            "paystack_dashboard.json",
        )
        with open(path, encoding="utf-8") as definition:
            return json.load(definition)

    def remove_widget(self, doctype: str, name: str) -> None:
        """Delete a shipped widget and register its recreation as a cleanup."""
        self.addCleanup(create_dashboard_widgets)
        frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)

    def test_a_deleted_number_card_is_raised_again(self) -> None:
        """A removed number card is recreated by create_dashboard_widgets."""
        card = NUMBER_CARDS[0]["name"]
        self.remove_widget(NUMBER_CARD, card)

        create_dashboard_widgets()

        self.assertTrue(frappe.db.exists(NUMBER_CARD, card))

    def test_a_deleted_chart_is_raised_again(self) -> None:
        """A removed dashboard chart is recreated by create_dashboard_widgets."""
        chart = DASHBOARD_CHARTS[0]["chart_name"]
        self.remove_widget(DASHBOARD_CHART, chart)

        create_dashboard_widgets()

        self.assertTrue(frappe.db.exists(DASHBOARD_CHART, chart))

    def test_every_number_card_the_workspace_names_is_created(self) -> None:
        """Every number card the workspace names exists after the widgets run."""
        create_dashboard_widgets()

        for row in self.workspace()["number_cards"]:
            self.assertTrue(frappe.db.exists(NUMBER_CARD, row["number_card_name"]))

    def test_every_chart_the_workspace_names_is_created(self) -> None:
        """Every chart the workspace names exists after the widgets run."""
        create_dashboard_widgets()

        for row in self.workspace()["charts"]:
            self.assertTrue(frappe.db.exists(DASHBOARD_CHART, row["chart_name"]))

    def test_every_report_the_workspace_links_is_shipped(self) -> None:
        """Every report the workspace links to ships with a module file."""
        for row in self.workspace()["links"]:
            if row["link_type"] != "Report":
                continue

            module = frappe.scrub(row["link_to"])
            with self.subTest(report=row["link_to"]):
                self.assertTrue(
                    os.path.exists(
                        frappe.get_app_path(
                            "frappe_paystack",
                            "frappe_paystack",
                            "report",
                            module,
                            f"{module}.py",
                        )
                    )
                )

    def test_every_card_break_counts_the_links_that_follow_it(self) -> None:
        """Each Card Break's link_count matches the links that follow it."""
        cards = []
        for row in self.workspace()["links"]:
            if row["type"] == "Card Break":
                cards.append([row["label"], row["link_count"], 0])
            else:
                cards[-1][2] += 1

        for label, declared, actual in cards:
            with self.subTest(card=label):
                self.assertEqual(declared, actual)

    def test_nothing_is_created_that_the_workspace_does_not_show(self) -> None:
        """The defined widgets are exactly the ones the workspace names."""
        named = {row["number_card_name"] for row in self.workspace()["number_cards"]}
        named |= {row["chart_name"] for row in self.workspace()["charts"]}

        defined = {card["name"] for card in NUMBER_CARDS}
        defined |= {chart["chart_name"] for chart in DASHBOARD_CHARTS}

        self.assertEqual(defined, named)

    def test_creating_the_widgets_twice_leaves_one_of_each(self) -> None:
        """Two runs of create_dashboard_widgets leave one card and one chart."""
        create_dashboard_widgets()
        create_dashboard_widgets()

        self.assertEqual(frappe.db.count(NUMBER_CARD, {"name": NUMBER_CARDS[0]["name"]}), 1)
        self.assertEqual(
            frappe.db.count(DASHBOARD_CHART, {"name": DASHBOARD_CHARTS[0]["chart_name"]}),
            1,
        )

    def test_every_card_filters_on_a_field_that_exists(self) -> None:
        """Every number card filters on a field its document type carries."""
        for card in NUMBER_CARDS:
            meta = frappe.get_meta(card["document_type"])
            for _dt, fieldname, _operator, _value in json.loads(card["filters_json"]):
                self.assertTrue(
                    meta.has_field(fieldname),
                    f"{card['name']} filters on missing {fieldname}",
                )

    def test_every_chart_plots_a_field_that_exists(self) -> None:
        """Every chart plots a field its document type carries."""
        plotted = ("based_on", "value_based_on", "group_by_based_on")

        for chart in DASHBOARD_CHARTS:
            meta = frappe.get_meta(chart["document_type"])
            for key in plotted:
                if key not in chart:
                    continue
                self.assertTrue(
                    meta.has_field(chart[key]),
                    f"{chart['chart_name']} plots missing {chart[key]}",
                )


class TestDesktopConfig(PaystackTestCase):
    """The desktop module card describes the Frappe Paystack module."""

    def test_module_card_is_described(self) -> None:
        """get_data returns one visible module card for Frappe Paystack."""
        cards = get_data()

        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["module_name"], "Frappe Paystack")
        self.assertEqual(cards[0]["type"], "module")
        self.assertEqual(cards[0]["hidden"], 0)


class TestFrappePaystackImports(PaystackTestCase):
    """Every module in frappe_paystack imports cleanly."""

    def test_import_all_modules_without_errors(self):
        package = frappe_paystack
        errors = []

        for _, module_name, _ in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
            try:
                importlib.import_module(module_name)
            except Exception as e:
                errors.append(f"Failed to import {module_name}: {e}")

        if errors:
            self.fail("Errors found while importing frappe_paystack modules:\n" + "\n".join(errors))

    def test_fixture_mode_of_payment_paystack_exists_after_migrate(self):
        """The 'Paystack' Mode of Payment fixture is in the database."""
        self.assertTrue(frappe.db.exists("Mode of Payment", MODE_OF_PAYMENT))
