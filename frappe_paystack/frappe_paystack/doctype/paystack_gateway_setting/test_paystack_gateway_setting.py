# Copyright (c) 2023, Anthony C. Emmanuel and Contributors
# See license.txt

from unittest.mock import patch

import frappe

from frappe_paystack.tests.factories import GatewaySettingFactory, cleanup_doc, ensure_mode_of_payment
from frappe_paystack.tests.test_base import PaystackTestCase, marked_translation
from frappe_paystack.utils import SUPPORTED_CURRENCIES

TEST_COMPANY = "_Test Company"

GATEWAY_MODULE = "frappe_paystack.frappe_paystack.doctype.paystack_gateway_setting.paystack_gateway_setting"

# A supported Paystack currency other than the test company's own.
FOREIGN_CURRENCY = "GHS"

# Currencies the acceptance check is offered, in order: every supported one
# followed by codes Paystack does not settle in.
PROBE_CURRENCIES = SUPPORTED_CURRENCIES + ["EUR", "GBP", "JPY", "INR"]


def company_currency(company: str = TEST_COMPANY) -> str:
    """Return the company's default currency."""
    return frappe.db.get_value("Company", company, "default_currency")


def accepts_currency(setting, currency: str) -> bool:
    """Report whether the controller lets a charge in this currency through."""
    try:
        setting.validate_transaction_currency(currency)
    except frappe.ValidationError:
        return False

    return True


class TestPaystackGatewaySetting(PaystackTestCase):
    """Tests for the Paystack Gateway Setting doctype."""

    def setUp(self):
        """Start with no gateway enabled for the test company."""
        super().setUp()
        self.disable_company_gateways()

    def make_gateway(self, gateway: str, enabled: bool = True) -> str:
        """Create a gateway setting and delete it after the test."""
        name = GatewaySettingFactory.create(gateway=gateway, enabled=enabled)
        self.addCleanup(GatewaySettingFactory.cleanup, name)
        return name

    def test_validate_allows_single_enabled_gateway_per_company(self):
        """A single enabled gateway per company passes check_enabled()."""
        setting_name = self.make_gateway("Gateway Single")
        self.assertIsNotNone(setting_name)

        setting = frappe.get_doc("Paystack Gateway Setting", setting_name)
        setting.check_enabled()

    def test_validate_throws_when_second_gateway_enabled_for_same_company(self):
        """A second enabled gateway for the same company throws."""
        self.make_gateway("Gateway A")

        setting2 = frappe.get_doc(
            {
                "doctype": "Paystack Gateway Setting",
                "gateway": "Gateway B",
                "company": "_Test Company",
                "secret_key": "sk_test_456",
                "public_key": "pk_test_456",
                "suspense_account": (
                    GatewaySettingFactory.create.__wrapped__
                    if hasattr(GatewaySettingFactory.create, "__wrapped__")
                    else None
                ),
                "mode_of_payment": "Paystack",
                "currency": "NGN",
                "enabled": 1,
            }
        )
        setting2.flags.ignore_permissions = True

        with self.assertRaises(frappe.ValidationError):
            setting2.check_enabled()

    def test_validate_allows_disabled_gateway_alongside_enabled(self):
        """A disabled gateway passes check_enabled() alongside an enabled one."""
        self.make_gateway("Gateway C")

        setting2_name = self.make_gateway("Gateway D", enabled=False)

        setting2 = frappe.get_doc("Paystack Gateway Setting", setting2_name)
        self.assertIsNone(setting2.check_enabled())

        # The same record enabled is what the guard is there to refuse.
        setting2.enabled = 1
        with self.assertRaises(frappe.ValidationError):
            setting2.check_enabled()

    def test_validate_transaction_currency_accepts_all_supported(self):
        """validate_transaction_currency accepts the supported currencies and no others."""
        setting_name = self.make_gateway("Gateway Currency Test")
        setting = frappe.get_doc("Paystack Gateway Setting", setting_name)

        accepted = [currency for currency in PROBE_CURRENCIES if accepts_currency(setting, currency)]

        self.assertEqual(accepted, SUPPORTED_CURRENCIES)

    def test_validate_transaction_currency_throws_for_unsupported(self):
        """validate_transaction_currency throws for an unsupported currency."""
        setting_name = self.make_gateway("Gateway EUR Test")
        setting = frappe.get_doc("Paystack Gateway Setting", setting_name)

        with self.assertRaises(frappe.ValidationError):
            setting.validate_transaction_currency("EUR")

    def test_get_secret_key_returns_plaintext_password(self):
        """get_secret_key returns the decrypted secret."""
        setting_name = self.make_gateway("Gateway Secret Test")
        setting = frappe.get_doc("Paystack Gateway Setting", setting_name)

        self.assertEqual(setting.get_secret_key(), "sk_test_123")

    def test_supported_currencies_constant_matches_utils(self):
        """The doctype's supported_currencies is the list from utils."""
        setting_name = self.make_gateway("Gateway Currencies Match")
        setting = frappe.get_doc("Paystack Gateway Setting", setting_name)

        self.assertEqual(setting.supported_currencies, SUPPORTED_CURRENCIES)
        self.assertIn("KES", setting.supported_currencies)

    def test_get_supported_currency_returns_list(self):
        """get_supported_currency returns the full currency list."""
        setting_name = self.make_gateway("Gateway List Test")
        setting = frappe.get_doc("Paystack Gateway Setting", setting_name)

        result = setting.get_supported_currency()
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), len(SUPPORTED_CURRENCIES))

    def test_webhook_secret_stored_as_password_type(self):
        """The webhook_secret field is a Password field."""
        meta = frappe.get_meta("Paystack Gateway Setting")
        field = meta.get_field("webhook_secret")
        self.assertEqual(field.fieldtype, "Password")

    def test_allowed_webhook_ips_stored_as_small_text(self):
        """The allowed_webhook_ips field is a Small Text field."""
        meta = frappe.get_meta("Paystack Gateway Setting")
        field = meta.get_field("allowed_webhook_ips")
        self.assertEqual(field.fieldtype, "Small Text")

    def test_test_mode_defaults_to_zero(self):
        """test_mode defaults to 0."""
        meta = frappe.get_meta("Paystack Gateway Setting")
        field = meta.get_field("test_mode")
        self.assertEqual(field.default, "0")

    def test_insert_with_webhook_secret_and_allowed_ips(self):
        """A gateway carrying webhook_secret and allowed_webhook_ips saves."""

        ensure_mode_of_payment()
        setting = frappe.get_doc(
            {
                "doctype": "Paystack Gateway Setting",
                "gateway": "Full Config Gateway",
                "company": "_Test Company",
                "test_mode": 1,
                "secret_key": "sk_test_123",
                "public_key": "pk_test_123",
                "webhook_secret": "wh_secret_xyz",
                "allowed_webhook_ips": "52.31.139.74\n52.31.139.75",
                "suspense_account": self.suspense_account(),
                "mode_of_payment": "Paystack",
                "currency": company_currency(),
                "enabled": 0,
            }
        )
        setting.flags.ignore_permissions = True
        setting.flags.ignore_links = True
        setting.insert()
        self.addCleanup(cleanup_doc, "Paystack Gateway Setting", setting.name)

        self.assertTrue(frappe.db.exists("Paystack Gateway Setting", setting.name))
        self.assertEqual(
            setting.get("allowed_webhook_ips"),
            "52.31.139.74\n52.31.139.75",
        )


class TestGatewayCurrency(PaystackTestCase):
    """The gateway currency is the currency the company books in."""

    def setUp(self) -> None:
        """Start with no gateway enabled for the test company."""
        super().setUp()
        self.disable_company_gateways()

    def build(self, currency: str, gateway: str) -> object:
        """Build an unsaved gateway setting in a given currency."""
        ensure_mode_of_payment()

        setting = frappe.get_doc(
            {
                "doctype": "Paystack Gateway Setting",
                "gateway": gateway,
                "company": TEST_COMPANY,
                "test_mode": 1,
                "secret_key": "sk_test_123",
                "public_key": "pk_test_123",
                "suspense_account": self.suspense_account(),
                "mode_of_payment": "Paystack",
                "currency": currency,
                "enabled": 0,
            }
        )
        setting.flags.ignore_permissions = True
        setting.flags.ignore_links = True
        return setting

    def test_company_currency_is_accepted(self) -> None:
        """A gateway in the company's own currency saves."""
        setting = self.build(company_currency(), "Gateway Matching Currency")
        setting.insert()
        self.addCleanup(cleanup_doc, "Paystack Gateway Setting", setting.name)

        self.assertEqual(setting.currency, company_currency())

    def test_foreign_currency_is_refused(self) -> None:
        """A gateway in another currency is refused, naming the company currency."""
        setting = self.build(FOREIGN_CURRENCY, "Gateway Foreign Currency")
        self.assertNotEqual(FOREIGN_CURRENCY, company_currency())

        with self.assertRaises(frappe.ValidationError) as caught:
            setting.insert()

        self.assertIn(company_currency(), str(caught.exception))

    def test_an_enabled_gateway_is_checked_too(self) -> None:
        """An enabled gateway meets the same currency check, and is not stored."""
        setting = self.build(FOREIGN_CURRENCY, "Gateway Foreign Enabled")
        setting.enabled = 1

        with self.assertRaises(frappe.ValidationError):
            setting.insert()

        self.assertFalse(frappe.db.exists("Paystack Gateway Setting", "Gateway Foreign Enabled"))


class TestKeyMode(PaystackTestCase):
    """Test Mode and the keys describe the same Paystack environment."""

    def setUp(self) -> None:
        """Start with no gateway enabled for the test company."""
        super().setUp()
        self.disable_company_gateways()

    def build(self, gateway: str, secret: str, public: str, test_mode: int) -> object:
        """Build an unsaved gateway setting carrying a given key pair."""
        ensure_mode_of_payment()

        setting = frappe.get_doc(
            {
                "doctype": "Paystack Gateway Setting",
                "gateway": gateway,
                "company": TEST_COMPANY,
                "test_mode": test_mode,
                "secret_key": secret,
                "public_key": public,
                "suspense_account": self.suspense_account(),
                "mode_of_payment": "Paystack",
                "currency": company_currency(),
                "enabled": 0,
            }
        )
        setting.flags.ignore_permissions = True
        setting.flags.ignore_links = True
        return setting

    def save(self, setting: object) -> object:
        """Insert a gateway setting and delete it after the test."""
        setting.insert()
        self.addCleanup(cleanup_doc, "Paystack Gateway Setting", setting.name)
        return setting

    def test_test_keys_save_in_test_mode(self) -> None:
        """Test keys save under Test Mode."""
        setting = self.build("Gateway Test Keys", "sk_test_1", "pk_test_1", 1)

        self.assertTrue(self.save(setting).name)

    def test_live_keys_save_outside_test_mode(self) -> None:
        """Live keys save outside Test Mode."""
        setting = self.build("Gateway Live Keys", "sk_live_1", "pk_live_1", 0)

        self.assertTrue(self.save(setting).name)

    def test_test_keys_are_refused_outside_test_mode(self) -> None:
        """Test keys outside Test Mode are refused, naming the Secret Key."""
        setting = self.build("Gateway Silent Keys", "sk_test_1", "pk_test_1", 0)

        with self.assertRaises(frappe.ValidationError) as caught:
            setting.insert()

        self.assertIn("Secret Key", str(caught.exception))

    def test_a_test_public_key_is_refused_outside_test_mode(self) -> None:
        """A test public key outside Test Mode is refused, naming the Public Key."""
        setting = self.build("Gateway Split Keys", "sk_live_1", "pk_test_1", 0)

        with self.assertRaises(frappe.ValidationError) as caught:
            setting.insert()

        self.assertIn("Public Key", str(caught.exception))

    def test_live_keys_are_refused_in_test_mode(self) -> None:
        """Live keys under Test Mode are refused, naming the live key."""
        setting = self.build("Gateway Real Money", "sk_live_1", "pk_live_1", 1)

        with self.assertRaises(frappe.ValidationError) as caught:
            setting.insert()

        self.assertIn("live key", str(caught.exception))

    def test_a_live_public_key_is_refused_in_test_mode(self) -> None:
        """A live public key under Test Mode is refused, naming the Public Key."""
        setting = self.build("Gateway Real Public", "sk_test_1", "pk_live_1", 1)

        with self.assertRaises(frappe.ValidationError) as caught:
            setting.insert()

        self.assertIn("Public Key", str(caught.exception))

    def test_an_unrecognised_prefix_is_not_judged(self) -> None:
        """A key pair with an unrecognised prefix saves."""
        setting = self.build("Gateway Custom Keys", "custom_secret", "custom_public", 0)

        self.assertTrue(self.save(setting).name)

    def test_a_refused_key_names_a_translated_label(self) -> None:
        """The key-mode refusal puts the Secret Key label through the translator."""
        setting = self.build("Gateway Translated Label", "sk_test_1", "pk_test_1", 0)

        with patch(f"{GATEWAY_MODULE}._", marked_translation):
            with self.assertRaises(frappe.ValidationError) as caught:
                setting.insert()

        self.assertIn(marked_translation("Secret Key"), str(caught.exception))

    def test_a_saved_gateway_is_rechecked_from_the_stored_key(self) -> None:
        """A reloaded gateway is rechecked against its stored key."""
        setting = self.save(self.build("Gateway Recheck", "sk_test_1", "pk_test_1", 1))

        stored = frappe.get_doc("Paystack Gateway Setting", setting.name)
        stored.test_mode = 0

        with self.assertRaises(frappe.ValidationError):
            stored.save()
