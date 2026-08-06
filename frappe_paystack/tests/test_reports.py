"""The Paystack query reports."""

from typing import Optional
from unittest.mock import MagicMock, patch

import frappe
import requests

from frappe_paystack.frappe_paystack.report.customer_paystack_volume import customer_paystack_volume
from frappe_paystack.frappe_paystack.report.paystack_settlements_vs_ledger import (
    paystack_settlements_vs_ledger,
)
from frappe_paystack.frappe_paystack.report.paystack_transactions import paystack_transactions
from frappe_paystack.frappe_paystack.report.paystack_unsettled_payments import paystack_unsettled_payments
from frappe_paystack.tests.factories import (
    TEST_COMPANY,
    TEST_CUSTOMER,
    GatewaySettingFactory,
    PaymentLogFactory,
    SalesInvoiceFactory,
)
from frappe_paystack.tests.test_base import FORMAT_MARKER, MisformattedError, PaystackTestCase

# Roles every Paystack report is held to.
PAYSTACK_REPORT_ROLES = ("System Manager", "Accounts Manager", "Accounts User")

# The doctype each report selects from.
PAYSTACK_REPORT_DOCTYPES = {
    "Customer Paystack Volume": "Paystack Payment Log",
    "Paystack Activity": "Paystack Payment Log",
    "Paystack Settlements vs Ledger": "Paystack Settlement",
    "Paystack Transactions": "Paystack Payment Log",
    "Paystack Unsettled Payments": "Paystack Payment Log",
}

PAYSTACK_REPORTS = tuple(PAYSTACK_REPORT_DOCTYPES)

TRANSACTIONS_MODULE = "frappe_paystack.frappe_paystack.report.paystack_transactions.paystack_transactions"
VOLUME_MODULE = "frappe_paystack.frappe_paystack.report.customer_paystack_volume.customer_paystack_volume"

TRANSACTIONS_REQUESTS = f"{TRANSACTIONS_MODULE}.requests.get"


def paystack_response(transactions: list, status: bool = True) -> MagicMock:
    """Build a stubbed Paystack /transaction list response."""
    response = MagicMock()
    response.ok = True
    response.raise_for_status.return_value = None
    response.json.return_value = {"status": status, "data": transactions}
    return response


class TransactionsReportTestCase(PaystackTestCase):
    """Shared gateway fixture for the Paystack Transactions report."""

    def setUp(self) -> None:
        """Enable a Paystack gateway so the report can read its secret key."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

    def run_report(self, transactions: list, filters: Optional[dict] = None, status: bool = True) -> tuple:
        """Execute the report against a stubbed Paystack response."""
        payload = frappe._dict({"gateway": self.gateway, **(filters or {})})
        with patch(
            TRANSACTIONS_REQUESTS,
            return_value=paystack_response(transactions, status=status),
        ) as mock_get:
            columns, data = paystack_transactions.execute(payload)
        self.last_request = mock_get
        return columns, data


class TestPaystackTransactionsColumns(PaystackTestCase):
    """get_columns describes the transaction grid."""

    def test_every_column_has_a_fieldname_and_label(self) -> None:
        """Each column definition carries a label, fieldname and fieldtype."""
        for column in paystack_transactions.get_columns():
            with self.subTest(column=column.get("fieldname")):
                self.assertTrue(column["label"])
                self.assertTrue(column["fieldname"])
                self.assertTrue(column["fieldtype"])

    def test_docname_is_a_dynamic_link_on_the_doctype_column(self) -> None:
        """The docname column resolves through the reference_doctype column."""
        columns = {c["fieldname"]: c for c in paystack_transactions.get_columns()}

        self.assertEqual(columns["reference_docname"]["fieldtype"], "Dynamic Link")
        self.assertEqual(columns["reference_docname"]["options"], "reference_doctype")


class TestPaystackTransactionsData(TransactionsReportTestCase):
    """get_data flattens the Paystack transaction list."""

    def test_execute_returns_columns_and_rows(self) -> None:
        """execute pairs the column definitions with the fetched rows."""
        columns, data = self.run_report([{"id": 1, "amount": 50000, "customer": {"email": "a@b.com"}}])

        self.assertEqual(len(columns), len(paystack_transactions.get_columns()))
        self.assertEqual(len(data), 1)

    def test_amount_is_converted_from_minor_units(self) -> None:
        """Kobo amounts are reported in major units."""
        _, data = self.run_report([{"id": 1, "amount": 250050}])

        self.assertEqual(data[0]["amount"], 2500.50)

    def test_customer_email_is_lifted_to_the_row(self) -> None:
        """The nested customer email becomes a top-level column."""
        _, data = self.run_report([{"id": 1, "amount": 100, "customer": {"email": "buyer@example.com"}}])

        self.assertEqual(data[0]["email"], "buyer@example.com")

    def test_missing_customer_yields_no_email(self) -> None:
        """A transaction without a customer object reports an empty email."""
        _, data = self.run_report([{"id": 1, "amount": 100}])

        self.assertIsNone(data[0]["email"])

    def test_metadata_reference_becomes_the_payment_log_column(self) -> None:
        """A metadata reference is surfaced as reference_log."""
        _, data = self.run_report(
            [
                {
                    "id": 1,
                    "amount": 100,
                    "reference": "psref_1",
                    "metadata": {"reference": "PAY-LOG-001", "custom": "x"},
                }
            ]
        )

        self.assertEqual(data[0]["reference_log"], "PAY-LOG-001")
        self.assertEqual(data[0]["reference"], "psref_1")
        self.assertEqual(data[0]["custom"], "x")

    def test_metadata_without_reference_keeps_the_paystack_reference(self) -> None:
        """Metadata carrying no reference leaves reference_log empty."""
        _, data = self.run_report(
            [
                {
                    "id": 1,
                    "amount": 100,
                    "reference": "psref_2",
                    "metadata": {"custom": "y"},
                }
            ]
        )

        self.assertIsNone(data[0]["reference_log"])
        self.assertEqual(data[0]["reference"], "psref_2")

    def test_empty_string_metadata_is_treated_as_absent(self) -> None:
        """An empty-string metadata is treated as absent."""
        _, data = self.run_report([{"id": 1, "amount": 100, "reference": "psref_3", "metadata": ""}])

        self.assertNotIn("metadata", data[0])
        self.assertEqual(data[0]["reference"], "psref_3")

    def test_non_dict_metadata_is_dropped(self) -> None:
        """A metadata string is discarded."""
        _, data = self.run_report([{"id": 1, "amount": 100, "metadata": "free form note"}])

        self.assertNotIn("metadata", data[0])
        self.assertNotIn("reference_log", data[0])

    def test_authorization_and_log_blobs_are_stripped(self) -> None:
        """Card authorization, log and source blobs are stripped from the row."""
        _, data = self.run_report(
            [
                {
                    "id": 1,
                    "amount": 100,
                    "authorization": {"card_type": "visa"},
                    "log": {"history": []},
                    "source": {"type": "api"},
                }
            ]
        )

        for field in ("authorization", "log", "source"):
            with self.subTest(field=field):
                self.assertNotIn(field, data[0])

    def test_transaction_without_authorization_is_reported(self) -> None:
        """A non-card transaction omits authorization and is still reported."""
        _, data = self.run_report([{"id": 7, "amount": 100, "channel": "bank"}])

        self.assertEqual(data[0]["id"], 7)
        self.assertEqual(data[0]["channel"], "bank")

    def test_unsuccessful_response_yields_no_rows(self) -> None:
        """A response with status false produces an empty result set."""
        _, data = self.run_report([{"id": 1, "amount": 100}], status=False)

        self.assertEqual(data, [])

    def test_empty_transaction_list_yields_no_rows(self) -> None:
        """A successful but empty response produces an empty result set."""
        _, data = self.run_report([])

        self.assertEqual(data, [])


class TestPaystackTransactionsFilters(TransactionsReportTestCase):
    """Report filters are forwarded as Paystack query parameters."""

    def sent_params(self) -> dict:
        """Return the params of the last stubbed Paystack request."""
        return self.last_request.call_args.kwargs["params"]

    def test_no_filters_sends_no_parameters(self) -> None:
        """A run with only the gateway sends an empty parameter set."""
        self.run_report([])

        self.assertEqual(self.sent_params(), {})

    def test_every_filter_is_forwarded(self) -> None:
        """Each supported filter maps onto its Paystack query parameter."""
        self.run_report(
            [],
            filters={
                "per_page": 50,
                "page": 2,
                "customer": 991,
                "terminalid": "TERM-1",
                "status": "success",
                "from_date": "2026-01-01",
                "to_date": "2026-01-31",
                "amount": 1500,
            },
        )

        self.assertEqual(
            self.sent_params(),
            {
                "perPage": 50,
                "page": 2,
                "customer": 991,
                "terminalid": "TERM-1",
                "status": "success",
                "from": "2026-01-01",
                "to": "2026-01-31",
                "amount": 1500,
            },
        )

    def test_api_error_is_reported_to_the_user(self) -> None:
        """A failed Paystack call surfaces as a validation error."""
        with patch(TRANSACTIONS_REQUESTS, side_effect=requests.ConnectionError("down")):
            with self.assertRaises(frappe.ValidationError):
                paystack_transactions.execute(frappe._dict({"gateway": self.gateway}))

    def test_api_error_names_the_transport_error(self) -> None:
        """The report failure reports the exception's own text."""
        with patch(TRANSACTIONS_REQUESTS, side_effect=MisformattedError("socket closed")):
            with self.assertRaises(frappe.ValidationError) as caught:
                paystack_transactions.execute(frappe._dict({"gateway": self.gateway}))

        self.assertIn("socket closed", str(caught.exception))
        self.assertNotIn(FORMAT_MARKER, str(caught.exception))

    def test_http_error_is_reported_to_the_user(self) -> None:
        """A non-2xx Paystack response surfaces as a validation error."""
        response = MagicMock()
        response.raise_for_status.side_effect = requests.HTTPError("401")

        with patch(TRANSACTIONS_REQUESTS, return_value=response):
            with self.assertRaises(frappe.ValidationError):
                paystack_transactions.execute(frappe._dict({"gateway": self.gateway}))


class TestPaystackTransactionsFilterShapes(TransactionsReportTestCase):
    """The transactions report reaches its permission check whatever shape the filters take."""

    def gated_run(self, *filters: dict) -> MagicMock:
        """Run the report with the permission check, the secret and the API call stubbed."""
        gate = patch(f"{TRANSACTIONS_MODULE}.check_company_permission")
        secret = patch(f"{TRANSACTIONS_MODULE}.get_gateway_secret", return_value="sk_test")
        api = patch(TRANSACTIONS_REQUESTS, return_value=paystack_response([]))

        with gate as checked:
            with secret, api:
                paystack_transactions.execute(*filters)

        return checked

    def test_absent_filters_reach_the_permission_check(self) -> None:
        """A run with no filters argument checks permission for no company."""
        checked = self.gated_run()

        checked.assert_called_once_with(None)

    def test_empty_filters_reach_the_permission_check(self) -> None:
        """An empty filter set checks permission for no company."""
        checked = self.gated_run({})

        checked.assert_called_once_with(None)

    def test_plain_dict_filters_reach_the_permission_check(self) -> None:
        """A plain dict names the gateway's company to the permission check."""
        checked = self.gated_run({"gateway": self.gateway})

        checked.assert_called_once_with(TEST_COMPANY)

    def test_frappe_dict_filters_reach_the_permission_check(self) -> None:
        """A frappe._dict names the gateway's company to the permission check."""
        checked = self.gated_run(frappe._dict({"gateway": self.gateway}))

        checked.assert_called_once_with(TEST_COMPANY)

    def test_no_gateway_named_resolves_no_company(self) -> None:
        """A run naming no gateway resolves to no company instead of an arbitrary one."""
        self.assertIsNone(paystack_transactions.gateway_company(None))

    def test_no_gateway_named_never_reaches_paystack(self) -> None:
        """A run naming no gateway resolves no secret key and calls nothing."""
        with patch(TRANSACTIONS_REQUESTS) as mock_get:
            with self.assertRaises(frappe.DoesNotExistError):
                paystack_transactions.execute({})

        mock_get.assert_not_called()

    def test_a_plain_dict_still_refuses_another_companys_gateway(self) -> None:
        """A plain dict naming another company's gateway is refused before the Paystack call."""
        self.become_restricted_accountant()

        with patch(TRANSACTIONS_REQUESTS) as mock_get:
            with self.assertRaises(frappe.PermissionError):
                paystack_transactions.execute({"gateway": self.gateway})

        mock_get.assert_not_called()


class TestCustomerPaystackVolume(PaystackTestCase):
    """The customer volume report totals settled Paystack payments."""

    def setUp(self) -> None:
        """Create a completed payment log to aggregate."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)
        self.log_name = PaymentLogFactory.create_completed(amount=1500)
        self.addCleanup(PaymentLogFactory.cleanup, self.log_name)

    def rows_for(self, filters: dict) -> list:
        """Run the report and return its data rows."""
        return customer_paystack_volume.execute(frappe._dict(filters))[1]

    def test_columns_describe_customer_company_and_total(self) -> None:
        """The report exposes customer, company, total and currency columns."""
        columns, _ = customer_paystack_volume.execute(frappe._dict({"company": TEST_COMPANY}))

        self.assertEqual(
            [column["fieldname"] for column in columns],
            ["customer", "company", "total", "currency"],
        )

    def test_completed_payment_is_totalled_for_the_customer(self) -> None:
        """A Completed payment log contributes its amount to the customer total."""
        rows = self.rows_for({"company": TEST_COMPANY})

        totals = {row["customer"]: row["total"] for row in rows}
        self.assertIn(TEST_CUSTOMER, totals)
        self.assertGreaterEqual(totals[TEST_CUSTOMER], 1500)

    def test_currency_is_reported(self) -> None:
        """The aggregated row carries the charge currency."""
        rows = self.rows_for({"company": TEST_COMPANY})

        row = next(row for row in rows if row["customer"] == TEST_CUSTOMER)
        self.assertEqual(row["currency"], "NGN")

    def test_customer_filter_narrows_the_result(self) -> None:
        """Filtering by customer restricts the aggregation to that customer."""
        rows = self.rows_for({"company": TEST_COMPANY, "customer": TEST_CUSTOMER})

        self.assertTrue(rows)
        self.assertEqual({row["customer"] for row in rows}, {TEST_CUSTOMER})

    def test_unknown_customer_filter_yields_nothing(self) -> None:
        """A customer with no Paystack payments produces no rows."""
        rows = self.rows_for({"company": TEST_COMPANY, "customer": "_Nonexistent Customer"})

        self.assertEqual(rows, [])

    def test_other_company_is_excluded(self) -> None:
        """Another company's filter produces no rows."""
        rows = self.rows_for({"company": "_Test Company 2"})

        self.assertEqual(rows, [])

    def test_pending_payment_is_not_counted(self) -> None:
        """Only Processed and Completed logs contribute to the totals."""
        invoice = SalesInvoiceFactory.create(rate=777)
        self.addCleanup(SalesInvoiceFactory.cleanup, invoice)
        pending = PaymentLogFactory.create(linked_docname=invoice, amount=777, status="Pending")
        self.addCleanup(PaymentLogFactory.cleanup, pending)

        rows = self.rows_for({"company": TEST_COMPANY, "customer": TEST_CUSTOMER})

        self.assertNotIn(777, [row["total"] for row in rows])


class TestCustomerPaystackVolumeFilterShapes(PaystackTestCase):
    """The volume report reaches its permission check whatever shape the filters take."""

    def gated_run(self, *filters: dict) -> MagicMock:
        """Run the report with the permission check stubbed."""
        with patch(f"{VOLUME_MODULE}.check_company_permission") as checked:
            customer_paystack_volume.execute(*filters)

        return checked

    def test_absent_filters_reach_the_permission_check(self) -> None:
        """A run with no filters argument checks permission for no company."""
        checked = self.gated_run()

        checked.assert_called_once_with(None)

    def test_empty_filters_reach_the_permission_check(self) -> None:
        """An empty filter set checks permission for no company."""
        checked = self.gated_run({})

        checked.assert_called_once_with(None)

    def test_plain_dict_filters_reach_the_permission_check(self) -> None:
        """A plain dict names its company to the permission check."""
        checked = self.gated_run({"company": TEST_COMPANY})

        checked.assert_called_once_with(TEST_COMPANY)

    def test_frappe_dict_filters_reach_the_permission_check(self) -> None:
        """A frappe._dict names its company to the permission check."""
        checked = self.gated_run(frappe._dict({"company": TEST_COMPANY}))

        checked.assert_called_once_with(TEST_COMPANY)

    def test_a_plain_dict_narrows_the_query_to_its_company(self) -> None:
        """A plain dict scopes the aggregation to the company it names."""
        rows = customer_paystack_volume.execute({"company": "_Test Company 2"})[1]

        self.assertEqual(rows, [])

    def test_a_plain_dict_still_refuses_an_unreadable_company(self) -> None:
        """A plain dict naming a company the caller may not read is refused."""
        self.become_restricted_accountant()

        with self.assertRaises(frappe.PermissionError):
            customer_paystack_volume.execute({"company": TEST_COMPANY})


class TestReportCompanyScope(PaystackTestCase):
    """Each report holds the caller to the companies they may read."""

    def setUp(self) -> None:
        """Enable a gateway for the test company, then restrict the caller."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

        self.become_restricted_accountant()

    def test_the_transactions_report_refuses_another_gateway(self) -> None:
        """A gateway of another company is refused before the Paystack call."""
        with patch(TRANSACTIONS_REQUESTS) as mock_get:
            with self.assertRaises(frappe.PermissionError):
                paystack_transactions.execute(frappe._dict({"gateway": self.gateway}))

        mock_get.assert_not_called()

    def test_the_volume_report_refuses_another_company(self) -> None:
        """Customer Paystack Volume refuses a company the caller may not read."""
        with self.assertRaises(frappe.PermissionError):
            customer_paystack_volume.execute(frappe._dict({"company": TEST_COMPANY}))

    def test_the_unsettled_report_refuses_another_company(self) -> None:
        """Paystack Unsettled Payments refuses a company the caller may not read."""
        with self.assertRaises(frappe.PermissionError):
            paystack_unsettled_payments.execute({"company": TEST_COMPANY})

    def test_the_settlements_report_refuses_another_company(self) -> None:
        """Paystack Settlements vs Ledger refuses a company the caller may not read."""
        with self.assertRaises(frappe.PermissionError):
            paystack_settlements_vs_ledger.execute({"company": TEST_COMPANY})


class TestReportPermissions(PaystackTestCase):
    """Every Paystack report names its roles and the doctype it reads."""

    def roles_of(self, report: str) -> set:
        """Return the roles a report grants."""
        return set(frappe.get_all("Has Role", filters={"parent": report}, pluck="role"))

    def test_every_report_names_its_roles(self) -> None:
        """No Paystack report is left open to every role."""
        for report in PAYSTACK_REPORTS:
            with self.subTest(report=report):
                self.assertEqual(self.roles_of(report), set(PAYSTACK_REPORT_ROLES))

    def test_every_report_is_judged_on_what_it_reads(self) -> None:
        """Each report's ref_doctype is the doctype it selects from."""
        for report, doctype in PAYSTACK_REPORT_DOCTYPES.items():
            with self.subTest(report=report):
                self.assertEqual(frappe.db.get_value("Report", report, "ref_doctype"), doctype)
