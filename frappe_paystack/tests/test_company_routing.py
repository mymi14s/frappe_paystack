"""Routing of a collection to the gateway setting of the company it bills."""

from typing import Optional
from unittest.mock import patch

import frappe
from payments.utils import get_payment_gateway_controller

from frappe_paystack.frappe_paystack.doctype.paystack_gateway_setting.paystack_gateway_setting import (
    company_for_reference,
)
from frappe_paystack.patches.v15_0.route_payments_per_company import (
    repair_gateway_controller,
    restamp_open_checkouts,
)
from frappe_paystack.setup import create_payment_gateway_record, update_payment_gateway_controller
from frappe_paystack.tests.factories import (
    GatewaySettingFactory,
    PaymentLogFactory,
    SalesInvoiceFactory,
    cleanup_doc,
)
from frappe_paystack.tests.test_base import PaystackTestCase, restore_gateway_setting_enabled
from frappe_paystack.utils import resolve_paystack_settings

PATCH_MODULE = "frappe_paystack.patches.v15_0.route_payments_per_company"

GATEWAY_SETTING = "Paystack Gateway Setting"
PAYMENT_GATEWAY = "Payment Gateway"
PAYMENT_LOG = "Paystack Payment Log"
PAYMENT_REQUEST = "Payment Request"
SALES_INVOICE = "Sales Invoice"
PAYSTACK = "Paystack"

TEST_COMPANY = "_Test Company"

# The second company collecting through Paystack.
OTHER_COMPANY = "_Test Company 3"
OTHER_GATEWAY = "Other Paystack Gateway"

# A company that runs no Paystack Gateway Setting of its own.
UNSERVED_COMPANY = "_Test Company 4"

OTHER_SECRET = "sk_test_other"

POS_CHARGE_PATCH = (
    "frappe_paystack.frappe_paystack.doctype.paystack_gateway_setting"
    ".paystack_gateway_setting.request_pos_charge"
)


class RoutingTestCase(PaystackTestCase):
    """A Payment Request billing one company's invoice."""

    def build_request(self, company: str) -> str:
        """Create a Payment Request charging a company's invoice in NGN."""
        invoice = SalesInvoiceFactory.create(rate=1000, company=company)
        self.addCleanup(SalesInvoiceFactory.cleanup, invoice)

        request = frappe.get_doc(
            {
                "doctype": PAYMENT_REQUEST,
                "payment_gateway": PAYSTACK,
                "reference_doctype": SALES_INVOICE,
                "reference_name": invoice,
                "payment_request_type": "Inward",
                "company": company,
                "grand_total": 1000,
                "currency": "NGN",
                "email_to": "customer@example.com",
            }
        )
        request.flags.ignore_permissions = True
        request.flags.ignore_mandatory = True
        request.flags.ignore_validate = True
        request.insert()
        self.addCleanup(cleanup_doc, PAYMENT_REQUEST, request.name)

        return request.name


class TestReferenceCompany(PaystackTestCase):
    """The company a checkout books against comes off the document it bills."""

    def test_an_invoices_company_is_returned(self) -> None:
        """An invoice answers the company it books against."""
        invoice = SalesInvoiceFactory.create(rate=1000, company=OTHER_COMPANY)
        self.addCleanup(SalesInvoiceFactory.cleanup, invoice)

        self.assertEqual(company_for_reference(SALES_INVOICE, invoice), OTHER_COMPANY)

    def test_a_reference_that_is_not_named_has_no_company(self) -> None:
        """A reference missing its doctype or its name answers no company."""
        self.assertIsNone(company_for_reference(None, "SINV-0001"))
        self.assertIsNone(company_for_reference(SALES_INVOICE, None))

    def test_a_doctype_without_a_company_field_has_no_company(self) -> None:
        """A reference doctype carrying no company field answers no company."""
        self.assertIsNone(company_for_reference("User", "Administrator"))


class TestCrossCompanyRouting(RoutingTestCase):
    """A checkout resolves the gateway setting of the company it bills."""

    def setUp(self) -> None:
        """Enable Paystack for two companies, with the controller on the first."""
        super().setUp()

        self.other_gateway = GatewaySettingFactory.create(
            gateway=OTHER_GATEWAY, company=OTHER_COMPANY, secret_key=OTHER_SECRET
        )
        self.addCleanup(GatewaySettingFactory.cleanup, self.other_gateway)

        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

        # The setting saved last owns the shared controller.
        update_payment_gateway_controller(self.gateway)

    def checkout_log(self, company: str) -> str:
        """Return the Payment Log behind a checkout link for a company's invoice."""
        request = self.build_request(company)

        url = get_payment_gateway_controller(PAYSTACK).get_payment_url(
            amount=1000,
            currency="NGN",
            reference_doctype=PAYMENT_REQUEST,
            reference_docname=request,
        )
        log = url.rstrip("/").split("/")[-1]
        self.addCleanup(cleanup_doc, PAYMENT_LOG, log)

        return log

    def test_the_controller_is_one_pointer_for_the_whole_site(self) -> None:
        """The Payment Gateway controller names the setting saved last."""
        self.assertEqual(get_payment_gateway_controller(PAYSTACK).name, self.gateway)

    def test_a_second_companys_checkout_is_booked_to_that_company(self) -> None:
        """A checkout billing the second company stamps that company on the log."""
        log = self.checkout_log(OTHER_COMPANY)

        self.assertEqual(frappe.db.get_value(PAYMENT_LOG, log, "company"), OTHER_COMPANY)

    def test_a_second_companys_checkout_collects_on_its_own_secret(self) -> None:
        """The checkout charges through the second company's own Paystack secret."""
        log = self.checkout_log(OTHER_COMPANY)
        company = frappe.db.get_value(PAYMENT_LOG, log, "company")

        self.assertEqual(resolve_paystack_settings(company).get("secret_key"), OTHER_SECRET)

    def test_the_pointed_at_companys_checkout_is_booked_to_it(self) -> None:
        """A checkout billing the company the controller names books to it."""
        log = self.checkout_log(TEST_COMPANY)

        self.assertEqual(frappe.db.get_value(PAYMENT_LOG, log, "company"), TEST_COMPANY)

    def test_a_pos_charge_is_raised_on_the_billed_companys_account(self) -> None:
        """A POS charge for the second company is raised on that company."""
        request = self.build_request(OTHER_COMPANY)

        with patch(POS_CHARGE_PATCH) as charge:
            charge.return_value = {"status": True, "data": {"reference": "x"}}
            get_payment_gateway_controller(PAYSTACK).request_for_payment(
                reference_doctype=PAYMENT_REQUEST,
                reference_docname=request,
                request_amount=500,
                currency="NGN",
            )

        self.assertEqual(charge.call_args.kwargs["company"], OTHER_COMPANY)

    def test_a_company_running_no_gateway_is_refused(self) -> None:
        """A company with no enabled setting is refused, not routed elsewhere."""
        controller = get_payment_gateway_controller(PAYSTACK)

        with self.assertRaises(frappe.ValidationError):
            controller.for_company(UNSERVED_COMPANY)

    def test_an_unnamed_company_keeps_the_controller(self) -> None:
        """A reference carrying no company resolves the controller itself."""
        controller = get_payment_gateway_controller(PAYSTACK)

        self.assertEqual(controller.for_company(None).name, self.gateway)

    def test_the_controllers_own_company_resolves_the_controller(self) -> None:
        """The company the controller already serves resolves the controller."""
        controller = get_payment_gateway_controller(PAYSTACK)

        self.assertEqual(controller.for_company(TEST_COMPANY).name, self.gateway)

    def test_a_switched_off_controller_still_serves_its_own_company(self) -> None:
        """The controller serves the company it names even when switched off."""
        controller = get_payment_gateway_controller(PAYSTACK)
        self.addCleanup(restore_gateway_setting_enabled, self.gateway)
        frappe.db.set_value(GATEWAY_SETTING, self.gateway, "enabled", 0, update_modified=False)
        frappe.clear_document_cache(GATEWAY_SETTING, self.gateway)

        self.assertEqual(controller.for_company(TEST_COMPANY).name, self.gateway)


class TestRoutingPatch(RoutingTestCase):
    """The patch repairs routing on a site that already collects."""

    def setUp(self) -> None:
        """Enable Paystack for the test company and point the controller at it."""
        super().setUp()

        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

        create_payment_gateway_record()
        update_payment_gateway_controller(self.gateway)

    def controller(self) -> Optional[str]:
        """Return the setting the shared Payment Gateway points at."""
        return frappe.db.get_value(PAYMENT_GATEWAY, PAYSTACK, "gateway_controller")

    def build_log(self, company: str, request: str, status: str = "Pending") -> str:
        """Create a Payment Log booked to a company and carrying a request."""
        log = PaymentLogFactory.create(status=status, amount=1000, company=company)
        self.addCleanup(PaymentLogFactory.cleanup, log)

        frappe.db.set_value(PAYMENT_LOG, log, "payment_request", request)
        frappe.clear_document_cache(PAYMENT_LOG, log)

        return log

    def test_a_missing_payment_gateway_is_left_missing(self) -> None:
        """A site with no Payment Gateway record gets none from the patch."""
        frappe.delete_doc(PAYMENT_GATEWAY, PAYSTACK, force=True, ignore_permissions=True)

        repair_gateway_controller()

        self.assertFalse(frappe.db.exists(PAYMENT_GATEWAY, PAYSTACK))

    def test_a_controller_naming_a_deleted_setting_is_repointed(self) -> None:
        """A controller naming a setting that is gone moves to one that exists."""
        frappe.db.set_value(PAYMENT_GATEWAY, PAYSTACK, "gateway_controller", "Gone Paystack Gateway")
        frappe.clear_document_cache(PAYMENT_GATEWAY, PAYSTACK)

        repair_gateway_controller()

        self.assertTrue(frappe.db.get_value(GATEWAY_SETTING, self.controller(), "enabled"))

    def test_a_controller_naming_a_live_setting_is_left_alone(self) -> None:
        """A controller already naming a setting that exists is not moved."""
        disabled = GatewaySettingFactory.create(gateway=OTHER_GATEWAY, company=OTHER_COMPANY, enabled=False)
        self.addCleanup(GatewaySettingFactory.cleanup, disabled)

        frappe.db.set_value(PAYMENT_GATEWAY, PAYSTACK, "gateway_controller", disabled)
        frappe.clear_document_cache(PAYMENT_GATEWAY, PAYSTACK)

        repair_gateway_controller()

        self.assertEqual(self.controller(), disabled)

    def test_a_controller_with_no_setting_to_move_to_is_left_alone(self) -> None:
        """With no setting to take over, the controller keeps what it names."""
        frappe.db.set_value(PAYMENT_GATEWAY, PAYSTACK, "gateway_controller", "Gone Paystack Gateway")
        frappe.clear_document_cache(PAYMENT_GATEWAY, PAYSTACK)

        with patch(f"{PATCH_MODULE}.find_replacement_setting", return_value=None):
            repair_gateway_controller()

        self.assertEqual(self.controller(), "Gone Paystack Gateway")

    def test_an_open_checkout_moves_onto_the_company_it_bills(self) -> None:
        """A pending log booked to the wrong company moves to the billed one."""
        request = self.build_request(OTHER_COMPANY)
        log = self.build_log(TEST_COMPANY, request)

        self.assertIn(log, restamp_open_checkouts())
        self.assertEqual(frappe.db.get_value(PAYMENT_LOG, log, "company"), OTHER_COMPANY)

    def test_a_checkout_already_on_its_company_is_left_alone(self) -> None:
        """A pending log already booked to the billed company is not rewritten."""
        request = self.build_request(TEST_COMPANY)
        log = self.build_log(TEST_COMPANY, request)

        self.assertNotIn(log, restamp_open_checkouts())

    def test_a_request_naming_no_company_leaves_the_checkout_alone(self) -> None:
        """A Payment Request carrying no company leaves the log where it is."""
        request = self.build_request(OTHER_COMPANY)
        frappe.db.set_value(PAYMENT_REQUEST, request, "company", None)
        log = self.build_log(TEST_COMPANY, request)

        self.assertNotIn(log, restamp_open_checkouts())
        self.assertEqual(frappe.db.get_value(PAYMENT_LOG, log, "company"), TEST_COMPANY)

    def test_a_settled_checkout_is_left_alone(self) -> None:
        """A log that is no longer pending keeps the company it settled on."""
        request = self.build_request(OTHER_COMPANY)
        log = self.build_log(TEST_COMPANY, request, status="Completed")

        self.assertNotIn(log, restamp_open_checkouts())
        self.assertEqual(frappe.db.get_value(PAYMENT_LOG, log, "company"), TEST_COMPANY)

    def test_a_checkout_carrying_no_request_is_left_alone(self) -> None:
        """A pending log with no Payment Request keeps its company."""
        log = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log)

        self.assertNotIn(log, restamp_open_checkouts())
        self.assertEqual(frappe.db.get_value(PAYMENT_LOG, log, "company"), TEST_COMPANY)
