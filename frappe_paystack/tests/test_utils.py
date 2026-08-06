"""Tests for frappe_paystack.utils, including its failure and fallback paths."""

import hashlib
import hmac
import json
from base64 import b64decode
from typing import Any
from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.model.document import Document

from frappe_paystack.tests.factories import TEST_COMPANY, GatewaySettingFactory, PaymentLogFactory
from frappe_paystack.tests.test_base import FORMAT_MARKER, MisformattedError, PaystackTestCase
from frappe_paystack.utils import (
    SUPPORTED_CURRENCIES,
    charge_authorization,
    coalesce_currency,
    ensure_supported_currency,
    error_log_link,
    from_minor_units,
    get_company_row_settings,
    hmac_sha512,
    initialize_transaction,
    initiate_refund,
    is_ip_allowed,
    is_paystack_enabled,
    log_integration_request,
    normalize_currency,
    parse_paystack_response,
    record_failure,
    request_pos_charge,
    resolve_paystack_settings,
    resolve_settings_for_signature,
    to_minor_units,
    validate_payment,
    verify_signature,
)
from frappe_paystack.utils.qr import qr_data_uri, qr_svg

GATEWAY_DOCTYPE = "Paystack Gateway Setting"
SECRET_KEY = "sk_test_123"
COMPANY_WITHOUT_PAYSTACK = "_Test Company 2"

UTILS_GET = "frappe_paystack.utils.utils.requests.get"
UTILS_POST = "frappe_paystack.utils.utils.requests.post"
UTILS_REQUEST_LOG = "frappe_paystack.utils.utils.create_request_log"

# Every supported currency followed by codes Paystack does not settle in.
PROBE_CURRENCIES = SUPPORTED_CURRENCIES + ["EUR", "GBP", "JPY", "INR"]


def is_accepted_currency(currency: str) -> bool:
    """Report whether ensure_supported_currency lets a currency through."""
    try:
        ensure_supported_currency(currency)
    except frappe.ValidationError:
        return False

    return True


class TestPaystackCurrency(PaystackTestCase):
    """Tests for currency normalization, conversion, and coalescing."""

    def test_normalize_currency_lowercases_and_strips(self):
        self.assertEqual(normalize_currency("usd"), "USD")
        self.assertEqual(normalize_currency(" ghs "), "GHS")

    def test_normalize_currency_unsupported_falls_back_to_ngn(self):
        self.assertEqual(normalize_currency("EUR"), "NGN")
        self.assertEqual(normalize_currency(None), "NGN")
        self.assertEqual(normalize_currency(""), "NGN")

    def test_to_minor_units_converts_decimal_to_kobo(self):
        self.assertEqual(to_minor_units(100.0, "NGN"), 10000)
        self.assertEqual(to_minor_units(99.99, "NGN"), 9999)

    def test_to_minor_units_handles_all_supported_currencies(self):
        for currency in SUPPORTED_CURRENCIES:
            result = to_minor_units(1.0, currency)
            self.assertEqual(result, 100, f"Failed for {currency}")

    def test_from_minor_units_reverses_to_minor_units(self):
        for amount in [0.01, 1.0, 99.99, 1000.0]:
            minor = to_minor_units(amount, "NGN")
            result = from_minor_units(minor, "NGN")
            self.assertAlmostEqual(result, amount, places=2)

    def test_coalesce_currency_prefers_explicit_over_company_and_settings(self):
        settings = {"default_currency": "GHS"}
        self.assertEqual(coalesce_currency("USD", "_Test Company", settings), "USD")

    def test_coalesce_currency_falls_back_to_settings_when_no_explicit(self):
        settings = {"default_currency": "GHS"}
        self.assertEqual(coalesce_currency(None, None, settings), "GHS")

    def test_coalesce_currency_falls_back_to_ngn_when_nothing_provided(self):
        self.assertEqual(coalesce_currency(None, None, None), "NGN")

    def test_ensure_supported_currency_throws_for_unsupported(self):
        with self.assertRaises(frappe.ValidationError):
            ensure_supported_currency("EUR")

    def test_ensure_supported_currency_accepts_all_supported(self):
        """The supported currencies pass and nothing else does."""
        accepted = [currency for currency in PROBE_CURRENCIES if is_accepted_currency(currency)]

        self.assertEqual(accepted, SUPPORTED_CURRENCIES)


class TestPaystackSignature(PaystackTestCase):
    """Tests for HMAC-SHA512 signature verification used by the webhook."""

    def test_verify_signature_accepts_correct_hmac(self):
        secret = "sk_test_abc123"
        payload = b'{"event":"charge.success","data":{"id":12345}}'
        signature = hmac.new(secret.encode("utf-8"), payload, hashlib.sha512).hexdigest()
        self.assertTrue(verify_signature(payload, signature, secret))

    def test_verify_signature_rejects_tampered_payload(self):
        secret = "sk_test_abc123"
        payload = b'{"event":"charge.success"}'
        signature = hmac_sha512(payload, secret)
        tampered = b'{"event":"charge.failed"}'
        self.assertFalse(verify_signature(tampered, signature, secret))

    def test_verify_signature_rejects_wrong_secret(self):
        payload = b"payload"
        signature = hmac_sha512(payload, "correct_secret")
        self.assertFalse(verify_signature(payload, signature, "wrong_secret"))

    def test_verify_signature_rejects_missing_signature(self):
        self.assertFalse(verify_signature(b"payload", None, "secret"))
        self.assertFalse(verify_signature(b"payload", "", "secret"))

    def test_hmac_sha512_matches_python_stdlib(self):
        secret = "test"
        payload = b"data"
        expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha512).hexdigest()
        self.assertEqual(hmac_sha512(payload, secret), expected)


class TestPaystackIpAllowlist(PaystackTestCase):
    """Tests for webhook IP allowlisting."""

    def test_empty_allowlist_allows_any_ip(self):
        self.assertTrue(is_ip_allowed(None, "1.2.3.4"))
        self.assertTrue(is_ip_allowed("", "10.0.0.1"))

    def test_ip_in_allowlist_is_allowed(self):
        allowed = "52.31.139.74\n52.31.139.75"
        self.assertTrue(is_ip_allowed(allowed, "52.31.139.74"))
        self.assertTrue(is_ip_allowed(allowed, "52.31.139.75"))

    def test_ip_not_in_allowlist_is_rejected(self):
        self.assertFalse(is_ip_allowed("52.31.139.74", "1.1.1.1"))

    def test_allowlist_strips_whitespace_and_blank_lines(self):
        allowed = "  52.31.139.74  \n\n  52.31.139.75  \n"
        self.assertTrue(is_ip_allowed(allowed, "52.31.139.74"))
        self.assertTrue(is_ip_allowed(allowed, "52.31.139.75"))

    def test_no_request_ip_with_allowlist_is_rejected(self):
        self.assertFalse(is_ip_allowed("52.31.139.74", None))


class TestPaystackSettingsResolution(PaystackTestCase):
    """Tests for resolving Paystack Gateway Settings for a company."""

    def setUp(self):
        """Start with no gateway enabled for the test company."""
        super().setUp()
        self.disable_company_gateways()

    def test_is_paystack_enabled_returns_false_when_no_gateway(self):
        self.assertFalse(is_paystack_enabled("_Test Company"))

    def test_resolve_paystack_settings_returns_none_when_no_gateway(self):
        self.assertIsNone(resolve_paystack_settings("_Test Company"))

    def test_resolve_paystack_settings_returns_dict_when_gateway_enabled(self):
        setting_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, setting_name)

        settings = resolve_paystack_settings("_Test Company")
        self.assertIsNotNone(settings)
        self.assertEqual(settings["public_key"], "pk_test_123")
        self.assertEqual(settings["secret_key"], "sk_test_123")
        self.assertTrue(settings["test_mode"] is True)

    def test_resolve_paystack_settings_uses_webhook_secret_when_set(self):
        setting_name = GatewaySettingFactory.create(webhook_secret="wh_secret_abc")
        self.addCleanup(GatewaySettingFactory.cleanup, setting_name)

        settings = resolve_paystack_settings("_Test Company")
        self.assertEqual(settings["webhook_secret"], "wh_secret_abc")

    def test_resolve_paystack_settings_falls_back_to_secret_key_for_webhook(self):
        setting_name = GatewaySettingFactory.create(webhook_secret=None)
        self.addCleanup(GatewaySettingFactory.cleanup, setting_name)

        settings = resolve_paystack_settings("_Test Company")
        self.assertEqual(settings["webhook_secret"], settings["secret_key"])

    def test_resolve_paystack_settings_returns_allowed_ips(self):
        setting_name = GatewaySettingFactory.create(allowed_ips="52.31.139.74\n52.31.139.75")
        self.addCleanup(GatewaySettingFactory.cleanup, setting_name)

        settings = resolve_paystack_settings("_Test Company")
        self.assertIn("52.31.139.74", settings["allowed_webhook_ips"])


class TestPaystackIntegrationRequestLogging(PaystackTestCase):
    """Tests for Integration Request audit logging."""

    def test_log_integration_request_creates_record(self):
        name = log_integration_request(
            status="Completed",
            url="webhook",
            request_data={"event": "charge.success"},
            response_data={"processed": True},
            reference_doctype="Paystack Payment Log",
            reference_docname="TEST-LOG-001",
        )
        self.assertIsNotNone(name)
        self.assertTrue(frappe.db.exists("Integration Request", name))

        ir = frappe.get_doc("Integration Request", name)
        self.assertEqual(ir.status, "Completed")
        self.assertEqual(ir.integration_request_service, "Paystack")
        self.assertEqual(ir.url, "webhook")
        self.assertTrue(ir.is_remote_request)
        # A reference that does not resolve is dropped.
        self.assertIsNone(ir.reference_doctype)
        parsed = json.loads(ir.data)
        self.assertEqual(parsed["event"], "charge.success")

    def test_log_integration_request_keeps_resolvable_reference(self):
        """A reference to a real document is recorded."""
        gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, gateway)
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)

        name = log_integration_request(
            status="Completed",
            url="webhook",
            request_data={"event": "charge.success"},
            reference_doctype="Paystack Payment Log",
            reference_docname=log_name,
        )
        self.addCleanup(frappe.delete_doc, "Integration Request", name, force=True)

        ir = frappe.get_doc("Integration Request", name)
        self.assertEqual(ir.reference_doctype, "Paystack Payment Log")
        self.assertEqual(ir.reference_docname, log_name)

        frappe.delete_doc("Integration Request", name, force=True, ignore_permissions=True)

    def test_log_integration_request_with_error(self):
        name = log_integration_request(
            status="Failed",
            url="https://api.paystack.co/transaction/verify/xxx",
            request_data={"transaction_id": "xxx"},
            error="Connection timeout",
            reference_doctype="Paystack Payment Log",
            reference_docname="TEST-LOG-002",
        )
        self.assertIsNotNone(name)

        ir = frappe.get_doc("Integration Request", name)
        self.assertEqual(ir.status, "Failed")
        self.assertIn("Connection timeout", ir.error)

        frappe.delete_doc("Integration Request", name, force=True, ignore_permissions=True)


class TestPaystackValidatePayment(PaystackTestCase):
    """Tests for the Paystack transaction verification API call."""

    @patch("frappe_paystack.utils.utils.requests.get")
    @patch("frappe_paystack.utils.utils.resolve_paystack_settings")
    def test_validate_payment_success(self, mock_settings, mock_get):
        """validate_payment returns API data and logs a Completed Integration Request."""

        mock_settings.return_value = {"secret_key": "sk_test_123"}

        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.json.return_value = {
            "status": True,
            "data": {"status": "success", "amount": 50000},
        }
        mock_get.return_value = mock_response

        doc = MagicMock()
        doc.company = "_Test Company"
        doc.transaction_id = "ref_test_001"
        doc.name = "TEST-LOG-VALIDATE"

        result = validate_payment(doc)

        self.assertTrue(result["status"])
        self.assertEqual(result["data"]["status"], "success")
        mock_get.assert_called_once()

    @patch("frappe_paystack.utils.utils.requests.get")
    @patch("frappe_paystack.utils.utils.resolve_paystack_settings")
    def test_validate_payment_api_failure_throws(self, mock_settings, mock_get):
        """validate_payment throws when the API call raises an exception."""

        mock_settings.return_value = {"secret_key": "sk_test_123"}
        mock_get.side_effect = requests.ConnectionError("Connection refused")

        doc = MagicMock()
        doc.company = "_Test Company"
        doc.transaction_id = "ref_test_002"
        doc.name = "TEST-LOG-VALIDATE-FAIL"

        with self.assertRaises(frappe.ValidationError):
            validate_payment(doc)

    @patch("frappe_paystack.utils.utils.resolve_paystack_settings", return_value=None)
    def test_validate_payment_throws_when_not_enabled(self, mock_settings):
        """validate_payment throws when Paystack is not enabled for the company."""

        doc = MagicMock()
        doc.company = "_Test Company"

        with self.assertRaises(frappe.ValidationError):
            validate_payment(doc)


class TestPaystackIpAllowlistRanges(PaystackTestCase):
    """The allowlist accepts CIDR ranges as well as single addresses."""

    def test_address_inside_a_cidr_range_is_allowed(self):
        self.assertTrue(is_ip_allowed("52.31.139.0/24", "52.31.139.75"))

    def test_address_outside_a_cidr_range_is_rejected(self):
        self.assertFalse(is_ip_allowed("52.31.139.0/24", "52.49.173.169"))

    def test_ranges_and_single_addresses_mix(self):
        allowed = "52.31.139.75\n52.49.173.0/24\n52.214.14.220"
        self.assertTrue(is_ip_allowed(allowed, "52.31.139.75"))
        self.assertTrue(is_ip_allowed(allowed, "52.49.173.169"))
        self.assertTrue(is_ip_allowed(allowed, "52.214.14.220"))
        self.assertFalse(is_ip_allowed(allowed, "52.214.14.221"))

    def test_unparseable_entries_are_skipped(self):
        self.assertTrue(is_ip_allowed("not-an-ip\n52.31.139.75", "52.31.139.75"))
        self.assertFalse(is_ip_allowed("not-an-ip", "52.31.139.75"))

    def test_unparseable_request_ip_is_rejected(self):
        self.assertFalse(is_ip_allowed("52.31.139.75", "not-an-ip"))


class TestPaystackGatewayIpDefault(PaystackTestCase):
    """The allowlist default and description shipped with the gateway doctype."""

    def gateway_field(self) -> dict:
        """Return the allowed_webhook_ips field as the doctype ships it."""
        path = frappe.get_app_path(
            "frappe_paystack",
            "frappe_paystack",
            "doctype",
            "paystack_gateway_setting",
            "paystack_gateway_setting.json",
        )
        with open(path, encoding="utf-8") as handle:
            fields = json.load(handle)["fields"]

        return next(f for f in fields if f["fieldname"] == "allowed_webhook_ips")

    def test_no_addresses_are_shipped_as_a_default(self):
        """The allowed_webhook_ips field ships with no default value."""
        self.assertFalse(self.gateway_field().get("default"))

    def test_the_current_addresses_are_documented(self):
        """The field description lists the addresses Paystack sends from."""
        description = self.gateway_field()["description"]
        for address in ("52.31.139.75", "52.49.173.169", "52.214.14.220"):
            self.assertIn(address, description)


class TestPaystackValidatePaymentWithoutReference(PaystackTestCase):
    """validate_payment on a log that carries no transaction reference."""

    @patch("frappe_paystack.utils.utils.requests.get")
    def test_empty_reference_makes_no_api_call(self, mock_get):
        """validate_payment returns None and makes no call to Paystack."""
        doc = frappe.new_doc("Paystack Payment Log")
        doc.company = "_Test Company"

        self.assertIsNone(validate_payment(doc))
        mock_get.assert_not_called()

    @patch("frappe_paystack.utils.utils.requests.get")
    def test_empty_reference_leaves_no_integration_request(self, mock_get):
        """The Integration Request count is unchanged."""
        before = frappe.db.count("Integration Request")

        doc = frappe.new_doc("Paystack Payment Log")
        doc.company = "_Test Company"
        validate_payment(doc)

        self.assertEqual(frappe.db.count("Integration Request"), before)


class TestPaystackPosChargeLogging(PaystackTestCase):
    """A POS charge is auditable through its Integration Request."""

    @patch("frappe_paystack.utils.utils.requests.post")
    @patch("frappe_paystack.utils.utils.resolve_paystack_settings")
    def test_successful_charge_is_logged(self, mock_settings, mock_post):
        """The initialise call leaves a Completed Integration Request."""
        mock_settings.return_value = {"secret_key": "sk_test_123"}
        mock_post.return_value = MagicMock(
            ok=True,
            **{"json.return_value": {"status": True, "data": {"reference": "x"}}},
        )
        before = frappe.db.count("Integration Request")

        request_pos_charge(
            company="_Test Company",
            email="customer@example.com",
            amount=500,
            currency="NGN",
            reference="POS-CHARGE-001",
        )

        self.assertEqual(frappe.db.count("Integration Request"), before + 1)
        self.assertEqual(frappe.get_last_doc("Integration Request").status, "Completed")

    @patch("frappe_paystack.utils.utils.requests.post")
    @patch("frappe_paystack.utils.utils.resolve_paystack_settings")
    def test_failed_charge_is_logged_and_raised(self, mock_settings, mock_post):
        """A transport error is recorded before it propagates."""
        mock_settings.return_value = {"secret_key": "sk_test_123"}
        mock_post.side_effect = requests.ConnectionError("paystack down")

        with self.assertRaises(requests.ConnectionError):
            request_pos_charge(
                company="_Test Company",
                email="customer@example.com",
                amount=500,
                currency="NGN",
                reference="POS-CHARGE-002",
            )

        latest = frappe.get_last_doc("Integration Request")
        self.assertEqual(latest.status, "Failed")
        self.assertIn("paystack down", latest.error)


class CompanylessMeta:
    """A gateway meta that reports no company field, standing in for a global one."""

    def __init__(self, meta: Any) -> None:
        """Wrap the real meta object."""
        self.meta = meta

    def __getattr__(self, name: str) -> Any:
        """Delegate everything except the company field check."""
        return getattr(self.meta, name)

    def has_field(self, fieldname: str) -> bool:
        """Report the company field as absent."""
        return fieldname != "company" and self.meta.has_field(fieldname)


def paystack_post_response(payload: dict, ok: bool = True, text: str = "") -> MagicMock:
    """Build a stubbed Paystack POST response."""
    response = MagicMock()
    response.ok = ok
    response.text = text
    response.json.return_value = payload
    return response


class TestIntegrationRequestFailure(PaystackTestCase):
    """log_integration_request swallows a failed audit write."""

    def test_write_failure_returns_none(self) -> None:
        """log_integration_request returns None when the write raises."""
        with patch(UTILS_REQUEST_LOG, side_effect=Exception("table is gone")):
            result = log_integration_request(status="Completed", url="webhook", request_data={"event": "x"})

        self.assertIsNone(result)


class TestGatewaySettingsFallbacks(PaystackTestCase):
    """get_company_row_settings falls back when its lookups fail."""

    def setUp(self) -> None:
        """Enable a Paystack gateway that carries its own webhook secret."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create(webhook_secret="wh_secret_abc")
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

    def test_unreadable_meta_yields_no_settings(self) -> None:
        """A doctype whose meta cannot be loaded resolves to no settings."""
        with patch(
            "frappe_paystack.utils.utils.frappe.get_meta",
            side_effect=Exception("no such doctype"),
        ):
            self.assertIsNone(get_company_row_settings(TEST_COMPANY))

    def test_company_scoped_lookup_needs_a_company(self) -> None:
        """A company-scoped gateway resolves to None without a company."""
        self.assertIsNone(get_company_row_settings(None))

    def test_gateway_without_a_company_field_is_resolved_globally(self) -> None:
        """A gateway doctype with no company field resolves the single enabled row."""
        original = frappe.get_meta

        def companyless_meta(doctype: str, *args: Any, **kwargs: Any) -> Any:
            meta = original(doctype, *args, **kwargs)
            if doctype == GATEWAY_DOCTYPE:
                return CompanylessMeta(meta)
            return meta

        with patch("frappe_paystack.utils.utils.frappe.get_meta", companyless_meta):
            settings = get_company_row_settings(None)

        self.assertIsNotNone(settings)
        self.assertEqual(settings["default_currency"], "NGN")

    def test_company_without_a_default_currency_falls_back_to_ngn(self) -> None:
        """A company with no default currency resolves to NGN."""
        original = frappe.db.get_value

        def blank_company_currency(doctype: str, *args: Any, **kwargs: Any) -> Any:
            if doctype == "Company":
                return None
            return original(doctype, *args, **kwargs)

        with patch.object(frappe.db, "get_value", side_effect=blank_company_currency):
            settings = get_company_row_settings(TEST_COMPANY)

        self.assertEqual(settings["default_currency"], "NGN")

    def test_currency_lookup_failure_falls_back_to_ngn(self) -> None:
        """A failing company lookup resolves to NGN."""
        original = frappe.db.get_value

        def failing_company_currency(doctype: str, *args: Any, **kwargs: Any) -> Any:
            if doctype == "Company":
                raise Exception("company table is locked")
            return original(doctype, *args, **kwargs)

        with patch.object(frappe.db, "get_value", side_effect=failing_company_currency):
            settings = get_company_row_settings(TEST_COMPANY)

        self.assertEqual(settings["default_currency"], "NGN")

    def test_unreadable_webhook_secret_falls_back_to_the_secret_key(self) -> None:
        """An undecryptable webhook secret falls back to the API secret key."""
        original = Document.get_password

        def failing_webhook_secret(
            doc: Any, fieldname: str = "password", raise_exception: bool = True
        ) -> Any:
            if fieldname == "webhook_secret":
                raise Exception("cannot decrypt")
            return original(doc, fieldname, raise_exception)

        with patch.object(Document, "get_password", failing_webhook_secret):
            settings = get_company_row_settings(TEST_COMPANY)

        self.assertEqual(settings["webhook_secret"], settings["secret_key"])


class TestSignatureResolution(PaystackTestCase):
    """resolve_settings_for_signature picks the gateway whose secret signs a payload."""

    payload = b'{"event":"charge.success"}'

    def setUp(self) -> None:
        """Enable a Paystack gateway for the test company."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

    def test_missing_signature_resolves_nothing(self) -> None:
        """An unsigned request resolves to None."""
        self.assertIsNone(resolve_settings_for_signature(self.payload, None))

    def test_correct_signature_resolves_the_gateway(self) -> None:
        """A payload signed with the gateway secret resolves that gateway."""
        signature = hmac_sha512(self.payload, SECRET_KEY)

        settings = resolve_settings_for_signature(self.payload, signature)

        self.assertIsNotNone(settings)
        self.assertEqual(settings["secret_key"], SECRET_KEY)

    def test_wrong_signature_resolves_nothing(self) -> None:
        """A payload signed with an unknown secret resolves to None."""
        signature = hmac_sha512(self.payload, "sk_live_not_ours")

        self.assertIsNone(resolve_settings_for_signature(self.payload, signature))

    def test_gateway_without_a_secret_is_skipped(self) -> None:
        """An enabled gateway that carries no secret key is passed over."""
        frappe.db.set_value(GATEWAY_DOCTYPE, self.gateway, "secret_key", "")
        frappe.clear_document_cache(GATEWAY_DOCTYPE, self.gateway)
        signature = hmac_sha512(self.payload, SECRET_KEY)

        self.assertIsNone(resolve_settings_for_signature(self.payload, signature))


class TestSignatureAmbiguity(PaystackTestCase):
    """Signature resolution when two companies share one signing secret."""

    payload = b'{"event":"settlement.success"}'

    def setUp(self) -> None:
        """Enable one gateway per company, both on the same Paystack secret."""
        super().setUp()

        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

        self.second = GatewaySettingFactory.create(
            gateway="Test Paystack Gateway 2", company=COMPANY_WITHOUT_PAYSTACK
        )
        self.addCleanup(GatewaySettingFactory.cleanup, self.second)

        self.signature = hmac_sha512(self.payload, SECRET_KEY)

    def ambiguity_reports(self) -> list:
        """Return this session's logs of a secret serving several companies."""
        return frappe.get_all(
            "Error Log",
            filters={
                "method": ["like", "%secret serves several companies%"],
                "creation": [">=", self.started_at],
            },
            pluck="error",
        )

    def test_a_shared_secret_resolves_nothing(self) -> None:
        """A signature matching two companies resolves to None."""
        self.assertIsNone(resolve_settings_for_signature(self.payload, self.signature))

    def test_the_companies_sharing_the_secret_are_named(self) -> None:
        """The Error Log names both companies sharing the secret."""
        resolve_settings_for_signature(self.payload, self.signature)

        self.assertEqual(
            self.ambiguity_reports(),
            [f"{TEST_COMPANY}, {COMPANY_WITHOUT_PAYSTACK}"],
        )

    def test_separating_the_secrets_resolves_the_signer_again(self) -> None:
        """Giving each gateway its own secret resolves the signing company."""
        setting = frappe.get_doc(GATEWAY_DOCTYPE, self.second)
        setting.secret_key = "sk_test_second"
        setting.flags.ignore_permissions = True
        setting.save()

        settings = resolve_settings_for_signature(self.payload, self.signature)

        self.assertEqual(settings["company"], TEST_COMPANY)


class TestCurrencyFallbacks(PaystackTestCase):
    """coalesce_currency walks its fallbacks in order."""

    def test_company_lookup_failure_falls_through_to_settings(self) -> None:
        """A failing company currency lookup falls through to the gateway default."""
        with patch(
            "frappe_paystack.utils.utils.get_company_currency",
            side_effect=Exception("no such company"),
        ):
            result = coalesce_currency(None, TEST_COMPANY, {"default_currency": "GHS"})

        self.assertEqual(result, "GHS")

    def test_company_lookup_failure_without_settings_falls_back_to_ngn(self) -> None:
        """With no settings to fall through to, the default currency is NGN."""
        with patch(
            "frappe_paystack.utils.utils.get_company_currency",
            side_effect=Exception("no such company"),
        ):
            self.assertEqual(coalesce_currency(None, TEST_COMPANY, None), "NGN")


class TestRequestPosCharge(PaystackTestCase):
    """request_pos_charge initialises a Paystack transaction for the POS."""

    def setUp(self) -> None:
        """Enable a Paystack gateway for the test company."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

    def charge(
        self,
        response: MagicMock,
        email: str = "buyer@example.com",
        currency: str = "NGN",
        amount: float = 1250.5,
    ) -> tuple:
        """Run a POS charge against a stubbed Paystack response."""
        with patch(UTILS_POST, return_value=response) as mock_post:
            data = request_pos_charge(
                company=TEST_COMPANY,
                email=email,
                amount=amount,
                currency=currency,
                reference="IR-POS-001",
            )
        return data, mock_post

    def test_successful_charge_returns_the_authorization_url(self) -> None:
        """A successful initialise returns the payload Paystack sent back."""
        response = paystack_post_response(
            {
                "status": True,
                "data": {"authorization_url": "https://checkout.paystack.com/x"},
            }
        )

        data, _ = self.charge(response)

        self.assertEqual(data["data"]["authorization_url"], "https://checkout.paystack.com/x")

    def test_charge_is_sent_in_minor_units(self) -> None:
        """The amount is converted to kobo and the currency normalised."""
        response = paystack_post_response({"status": True, "data": {}})

        _, mock_post = self.charge(response, currency="ngn")

        body = mock_post.call_args.kwargs["json"]
        self.assertEqual(body["amount"], 125050)
        self.assertEqual(body["currency"], "NGN")
        self.assertEqual(body["reference"], "IR-POS-001")
        self.assertEqual(body["email"], "buyer@example.com")

    def test_company_without_paystack_is_refused(self) -> None:
        """A company with no enabled gateway cannot start a charge."""
        with self.assertRaises(frappe.ValidationError):
            request_pos_charge(
                company=COMPANY_WITHOUT_PAYSTACK,
                email="buyer@example.com",
                amount=100,
                currency="NGN",
                reference="IR-POS-002",
            )

    def test_missing_email_is_refused(self) -> None:
        """An empty customer email is refused."""
        with self.assertRaises(frappe.ValidationError):
            request_pos_charge(
                company=TEST_COMPANY,
                email="",
                amount=100,
                currency="NGN",
                reference="IR-POS-003",
            )

    def test_unsupported_currency_is_refused(self) -> None:
        """A currency Paystack does not settle is refused before the call."""
        with self.assertRaises(frappe.ValidationError):
            request_pos_charge(
                company=TEST_COMPANY,
                email="buyer@example.com",
                amount=100,
                currency="EUR",
                reference="IR-POS-004",
            )

    def test_rejected_charge_reports_the_paystack_message(self) -> None:
        """A rejected initialise surfaces the message Paystack returned."""
        response = paystack_post_response({"status": False, "message": "Invalid key"}, ok=False)

        with patch(UTILS_POST, return_value=response):
            with self.assertRaises(frappe.ValidationError) as caught:
                request_pos_charge(
                    company=TEST_COMPANY,
                    email="buyer@example.com",
                    amount=100,
                    currency="NGN",
                    reference="IR-POS-005",
                )

        self.assertIn("Invalid key", str(caught.exception))

    def test_rejected_charge_without_a_message_reports_the_body(self) -> None:
        """A rejected initialise with no message reports the raw response body."""
        response = paystack_post_response({"status": False}, ok=True, text="bad body")

        with patch(UTILS_POST, return_value=response):
            with self.assertRaises(frappe.ValidationError) as caught:
                request_pos_charge(
                    company=TEST_COMPANY,
                    email="buyer@example.com",
                    amount=100,
                    currency="NGN",
                    reference="IR-POS-006",
                )

        self.assertIn("bad body", str(caught.exception))


class TestInitiateRefundNetworkFailure(PaystackTestCase):
    """initiate_refund records a failed Integration Request before throwing."""

    def setUp(self) -> None:
        """Enable a Paystack gateway for the test company."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

    def test_network_failure_is_logged_and_reported(self) -> None:
        """A transport error leaves a Failed Integration Request behind."""
        before = frappe.db.count("Integration Request")

        with patch(UTILS_POST, side_effect=requests.ConnectionError("timed out")):
            with self.assertRaises(frappe.ValidationError):
                initiate_refund(
                    transaction_id="ref_net",
                    amount=100,
                    currency="NGN",
                    company=TEST_COMPANY,
                )

        self.assertEqual(frappe.db.count("Integration Request"), before + 1)
        latest = frappe.get_last_doc("Integration Request")
        self.assertEqual(latest.status, "Failed")
        self.assertIn("timed out", latest.error)


class TestThrownExceptionText(PaystackTestCase):
    """A thrown Paystack message carries the exception's text, not its format() rendering."""

    def setUp(self) -> None:
        """Enable a Paystack gateway for the test company."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

    @patch("frappe_paystack.utils.utils.resolve_paystack_settings")
    def test_a_failed_verification_names_the_transport_error(self, mock_settings) -> None:
        """The verification failure reports the exception's own text."""
        mock_settings.return_value = {"secret_key": SECRET_KEY}

        doc = MagicMock()
        doc.company = TEST_COMPANY
        doc.name = "TEST-LOG-VERIFY-FORMAT"

        with patch(UTILS_GET, side_effect=MisformattedError("socket closed")):
            with self.assertRaises(frappe.ValidationError) as caught:
                validate_payment(doc)

        self.assertIn("socket closed", str(caught.exception))
        self.assertNotIn(FORMAT_MARKER, str(caught.exception))

    def test_a_failed_refund_names_the_transport_error(self) -> None:
        """The refund failure reports the exception's own text."""
        with patch(UTILS_POST, side_effect=MisformattedError("socket closed")):
            with self.assertRaises(frappe.ValidationError) as caught:
                initiate_refund(
                    transaction_id="ref_format",
                    amount=100,
                    currency="NGN",
                    company=TEST_COMPANY,
                )

        self.assertIn("socket closed", str(caught.exception))
        self.assertNotIn(FORMAT_MARKER, str(caught.exception))


class TestFailureReporting(PaystackTestCase):
    """record_failure turns a traceback into a message naming the Error Log."""

    def test_error_log_link_is_empty_without_a_record(self) -> None:
        """No Error Log name yields no link."""
        self.assertEqual(error_log_link(None), "")

    def test_error_log_link_points_at_the_desk_record(self) -> None:
        """A named Error Log becomes a desk anchor."""
        self.assertIn("/app/error-log/EL-1", error_log_link("EL-1"))

    def test_unnamed_error_log_still_yields_a_message(self) -> None:
        """When the Error Log cannot be written the message still names the failure."""
        with patch("frappe.log_error", return_value=None):
            message = record_failure("Refund failed")

        self.assertIn("Refund failed", message)
        self.assertIn("Error Log", message)

    def test_named_error_log_is_linked(self) -> None:
        """A written Error Log is linked from the message."""
        message = record_failure("Refund failed", "traceback body")

        self.assertIn("Refund failed", message)
        self.assertIn("/app/error-log/", message)


class TestPaystackResponseParsing(PaystackTestCase):
    """parse_paystack_response handles JSON and HTML gateway-error bodies."""

    def build_response(self, body: str, status_code: int = 502) -> MagicMock:
        """Build a response whose .json() raises the way requests does."""
        response = MagicMock()
        response.status_code = status_code
        response.text = body
        response.json.side_effect = ValueError("Expecting value")
        return response

    def test_json_body_is_returned(self) -> None:
        """A well-formed body is passed straight through."""
        response = MagicMock()
        response.json.return_value = {"status": True}

        self.assertEqual(parse_paystack_response(response), {"status": True})

    def test_html_body_raises_with_status_and_excerpt(self) -> None:
        """A non-JSON body raises with the HTTP status and a body excerpt."""
        response = self.build_response("<html>502 Bad Gateway</html>")

        with self.assertRaises(frappe.ValidationError) as ctx:
            parse_paystack_response(response)

        self.assertIn("502", str(ctx.exception))
        self.assertIn("Bad Gateway", str(ctx.exception))

    def test_empty_body_is_reported(self) -> None:
        """An empty body still produces an actionable message."""
        response = self.build_response("")

        with self.assertRaises(frappe.ValidationError):
            parse_paystack_response(response)

    def test_json_null_body_raises_with_status_and_excerpt(self) -> None:
        """A body decoding to null raises, naming the status and an excerpt."""
        response = MagicMock()
        response.status_code = 502
        response.text = "null"
        response.json.return_value = None

        with self.assertRaises(frappe.ValidationError) as ctx:
            parse_paystack_response(response)

        self.assertIn("502", str(ctx.exception))
        self.assertIn("null", str(ctx.exception))

    def test_json_list_body_raises_with_status_and_excerpt(self) -> None:
        """A body decoding to a JSON array raises, naming the status and an excerpt."""
        response = MagicMock()
        response.status_code = 503
        response.text = '["service unavailable"]'
        response.json.return_value = ["service unavailable"]

        with self.assertRaises(frappe.ValidationError) as ctx:
            parse_paystack_response(response)

        self.assertIn("503", str(ctx.exception))
        self.assertIn("service unavailable", str(ctx.exception))


class TestInitializeTransaction(PaystackTestCase):
    """initialize_transaction opens a Paystack-hosted checkout."""

    def setUp(self) -> None:
        """Enable a Paystack gateway for the test company."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

    def initialize(self, response: MagicMock, **kwargs) -> tuple:
        """Run an initialise against a stubbed Paystack response."""
        with patch(UTILS_POST, return_value=response) as mock_post:
            url = initialize_transaction(
                company=TEST_COMPANY,
                email="buyer@example.com",
                amount=1500,
                currency="NGN",
                reference="PSLOG-001",
                **kwargs,
            )
        return url, mock_post

    def test_authorization_url_is_returned(self) -> None:
        """The hosted checkout URL Paystack answers with is handed back."""
        response = paystack_post_response(
            {
                "status": True,
                "data": {"authorization_url": "https://checkout.paystack.com/abc"},
            }
        )

        url, _ = self.initialize(response)

        self.assertEqual(url, "https://checkout.paystack.com/abc")

    def test_metadata_and_callback_are_sent_when_given(self) -> None:
        """Metadata and callback_url are sent in the request body."""
        response = paystack_post_response({"status": True, "data": {"authorization_url": "https://x"}})

        _, mock_post = self.initialize(
            response,
            metadata={"reference": "PSLOG-001"},
            callback_url="https://site/paystack-checkout/PSLOG-001",
        )

        body = mock_post.call_args.kwargs["json"]
        self.assertEqual(body["metadata"], {"reference": "PSLOG-001"})
        self.assertEqual(body["callback_url"], "https://site/paystack-checkout/PSLOG-001")

    def test_neither_key_is_sent_when_omitted(self) -> None:
        """An initialise without metadata omits both keys from the body."""
        response = paystack_post_response({"status": True, "data": {"authorization_url": "https://x"}})

        _, mock_post = self.initialize(response)

        body = mock_post.call_args.kwargs["json"]
        self.assertNotIn("metadata", body)
        self.assertNotIn("callback_url", body)

    def test_a_response_without_a_url_is_refused(self) -> None:
        """A response carrying no authorization URL is refused."""
        response = paystack_post_response({"status": True, "data": {}})

        with patch(UTILS_POST, return_value=response):
            with self.assertRaises(frappe.ValidationError) as caught:
                initialize_transaction(
                    company=TEST_COMPANY,
                    email="buyer@example.com",
                    amount=100,
                    currency="NGN",
                    reference="PSLOG-002",
                )

        self.assertIn("checkout URL", str(caught.exception))


class TestChargeAuthorization(PaystackTestCase):
    """charge_authorization charges a card the customer already saved."""

    def setUp(self) -> None:
        """Enable a Paystack gateway for the test company."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

    def charge(self, response: MagicMock, **kwargs) -> tuple:
        """Run a saved-card charge against a stubbed Paystack response."""
        with patch(UTILS_POST, return_value=response) as mock_post:
            data = charge_authorization(
                company=TEST_COMPANY,
                email="buyer@example.com",
                amount=250,
                currency="NGN",
                reference="PSLOG-003",
                authorization_code="AUTH_secret",
                **kwargs,
            )
        return data, mock_post

    def test_transaction_data_is_returned(self) -> None:
        """The transaction object Paystack answers with is handed back."""
        response = paystack_post_response({"status": True, "data": {"status": "success", "id": 42}})

        data, _ = self.charge(response)

        self.assertEqual(data["status"], "success")

    def test_a_response_without_data_returns_an_empty_dict(self) -> None:
        """A response carrying no transaction returns an empty dict."""
        response = paystack_post_response({"status": True})

        data, _ = self.charge(response)

        self.assertEqual(data, {})

    def test_the_authorization_code_is_sent_to_paystack(self) -> None:
        """The authorization code is sent in the request body."""
        response = paystack_post_response({"status": True, "data": {}})

        _, mock_post = self.charge(response)

        self.assertEqual(mock_post.call_args.kwargs["json"]["authorization_code"], "AUTH_secret")

    def test_metadata_is_sent_when_given(self) -> None:
        """Metadata is sent in the request body."""
        response = paystack_post_response({"status": True, "data": {}})

        _, mock_post = self.charge(response, metadata={"reference": "PSLOG-003"})

        self.assertEqual(mock_post.call_args.kwargs["json"]["metadata"], {"reference": "PSLOG-003"})

    def test_the_authorization_code_is_never_logged(self) -> None:
        """The logged request body carries a redaction in place of the code."""
        response = paystack_post_response({"status": True, "data": {}})

        self.charge(response)

        logged = frappe.get_last_doc("Integration Request").data
        self.assertNotIn("AUTH_secret", logged)
        self.assertIn("***", logged)

    def test_the_code_paystack_sends_back_is_never_logged(self) -> None:
        """The logged response output carries a redaction in place of the code."""
        response = paystack_post_response(
            {
                "status": True,
                "data": {
                    "status": "success",
                    "authorization": {"authorization_code": "AUTH_returned"},
                },
            }
        )

        self.charge(response)

        logged = frappe.get_last_doc("Integration Request")
        self.assertNotIn("AUTH_returned", logged.output)
        self.assertIn("***", logged.output)

    def test_a_pos_charge_body_is_logged_unredacted(self) -> None:
        """A POS charge body carrying no card token is stored as sent."""
        response = paystack_post_response({"status": True, "data": {}})

        with patch(UTILS_POST, return_value=response):
            request_pos_charge(
                company=TEST_COMPANY,
                email="buyer@example.com",
                amount=100,
                currency="NGN",
                reference="IR-REDACT-001",
            )

        logged = frappe.get_last_doc("Integration Request").data
        self.assertIn("IR-REDACT-001", logged)
        self.assertNotIn("***", logged)


class TestQrCodes(PaystackTestCase):
    """Checkout links are rendered as scannable SVG."""

    def test_svg_encodes_the_text(self) -> None:
        """The QR renders as an SVG document."""
        svg = qr_svg("https://example.com/paystack-checkout/PSLOG-1")

        self.assertIn("<svg", svg)
        self.assertNotIn("\n", svg)

    def test_data_uri_is_base64_svg(self) -> None:
        """The data URI decodes back to the SVG it wraps."""
        uri = qr_data_uri("https://example.com/paystack-checkout/PSLOG-1")

        self.assertTrue(uri.startswith("data:image/svg+xml;base64,"))
        decoded = b64decode(uri.split(",", 1)[1]).decode()
        self.assertIn("<svg", decoded)

    def test_different_links_produce_different_codes(self) -> None:
        """Two different links render two different codes."""
        self.assertNotEqual(qr_data_uri("link-one"), qr_data_uri("link-two"))

    def test_empty_text_produces_no_image(self) -> None:
        """Empty text renders an empty data URI."""
        self.assertEqual(qr_data_uri(""), "")
