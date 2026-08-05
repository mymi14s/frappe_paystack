"""Reconciliation: the engine, its API, and the jobs that drive it."""

from typing import Any
from unittest.mock import MagicMock, patch

import frappe
from frappe.utils import add_days, today

from frappe_paystack import hooks
from frappe_paystack.tests.factories import (
    TEST_COMPANY,
    GatewaySettingFactory,
    PaymentLogFactory,
    cleanup_user,
)
from frappe_paystack.tests.test_base import PaystackTestCase
from frappe_paystack.utils import resolve_paystack_settings
from frappe_paystack.utils.reconciliation import PAYMENT_LOG, RECONCILIATION_LOG, ReconciliationEngine
from frappe_paystack.utils.reconciliation_api import (
    apply_company_filter,
    check_reconciliation_permission,
    get_reconciliation_report,
    get_reconciliation_stats,
    mark_manual_override,
    permitted_companies,
    reconcile_payment,
    run_reconciliation,
)
from frappe_paystack.utils.scheduled_jobs import (
    DAILY_LOOKBACK_DAYS,
    paystack_companies,
    run_daily_reconciliation,
)

ACCOUNTANT_EMAIL = "paystack-accountant@example.com"

USER_PERMISSION = "User Permission"

GATEWAY_SETTING = "Paystack Gateway Setting"

# A company the test company's payments do not belong to.
OTHER_COMPANY = "_Test Company 1"


def failing_lookup(doctype: str) -> Any:
    """Return a get_all that raises for one doctype and passes the rest through."""
    original = frappe.get_all

    def lookup(target: str, *args: Any, **kwargs: Any) -> Any:
        if target == doctype:
            raise RuntimeError(f"{target} is unreadable")
        return original(target, *args, **kwargs)

    return lookup


def paystack_tx(amount=100000, status="success", currency="NGN") -> dict:
    """Build a Paystack transaction payload (amount in minor units)."""
    return {"amount": amount, "status": status, "currency": currency}


class PaystackReconciliationTestCase(PaystackTestCase):
    """Base case that provides an enabled Paystack gateway for the test company."""

    def setUp(self) -> None:
        """Create an enabled gateway setting."""
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

    def make_log(self, **kwargs) -> str:
        """Create a Completed payment log wired up for cleanup."""
        log_name = PaymentLogFactory.create_completed(**kwargs)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        self.addCleanup(self.cleanup_reconciliation_log, log_name)
        return log_name

    def log_without_transaction_id(self, status: str = "Pending", amount_paid: float = 0) -> str:
        """Create a payment log carrying no transaction_id."""
        log_name = PaymentLogFactory.create(status=status, amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        self.addCleanup(self.cleanup_reconciliation_log, log_name)

        frappe.db.set_value(
            PAYMENT_LOG,
            log_name,
            {"transaction_id": None, "amount_paid": amount_paid},
        )
        frappe.clear_document_cache(PAYMENT_LOG, log_name)
        return log_name


class TestReconciliationEngine(PaystackReconciliationTestCase):
    """ReconciliationEngine."""

    def setUp(self) -> None:
        """Build the engine after the gateway setting exists."""
        super().setUp()
        self.engine = ReconciliationEngine(TEST_COMPANY)

    def test_matching_amounts_are_reconciled(self) -> None:
        """A matching amount, currency and status reconciles cleanly."""
        log_name = self.make_log(amount=1000, amount_paid=1000, currency="NGN")

        with patch.object(self.engine, "fetch_transaction") as mock_fetch:
            mock_fetch.return_value = paystack_tx(amount=100000)
            result = self.engine.reconcile_payment(log_name)

        self.assertEqual(result["status"], "Reconciled")
        self.assertTrue(frappe.db.exists("Paystack Reconciliation Log", log_name))

    def test_reconciliation_log_records_company(self) -> None:
        """The reconciliation log is stamped with the company for scoping."""
        log_name = self.make_log(amount=1000, amount_paid=1000)

        with patch.object(self.engine, "fetch_transaction") as mock_fetch:
            mock_fetch.return_value = paystack_tx(amount=100000)
            self.engine.reconcile_payment(log_name)

        self.assertEqual(
            frappe.db.get_value("Paystack Reconciliation Log", log_name, "company"),
            TEST_COMPANY,
        )

    def test_mismatched_amounts_flagged(self) -> None:
        """A differing amount is flagged as Mismatch."""
        log_name = self.make_log(amount=1000, amount_paid=1000)

        with patch.object(self.engine, "fetch_transaction") as mock_fetch:
            mock_fetch.return_value = paystack_tx(amount=95000)
            result = self.engine.reconcile_payment(log_name)

        self.assertEqual(result["status"], "Mismatch")

    def test_a_mismatch_records_the_gap_the_report_reads(self) -> None:
        """The reconciliation the engine writes carries the difference between the amounts."""
        log_name = self.make_log(amount=1000, amount_paid=1000)

        with patch.object(self.engine, "fetch_transaction") as mock_fetch:
            mock_fetch.return_value = paystack_tx(amount=90000)
            self.engine.reconcile_payment(log_name)

        self.assertAlmostEqual(
            frappe.db.get_value(RECONCILIATION_LOG, log_name, "difference"),
            -100.0,
            places=2,
        )

    def test_the_report_carries_the_gap_the_engine_wrote(self) -> None:
        """The reconciliation report reads back the difference the engine recorded."""
        log_name = self.make_log(amount=1000, amount_paid=1000)

        with patch.object(self.engine, "fetch_transaction") as mock_fetch:
            mock_fetch.return_value = paystack_tx(amount=90000)
            self.engine.reconcile_payment(log_name)

        row = next(row for row in get_reconciliation_report() if row["payment_log"] == log_name)
        self.assertAlmostEqual(row["difference"], -100.0, places=2)

    def test_failed_paystack_status_is_not_reconciled(self) -> None:
        """A failed Paystack status is flagged as Mismatch."""
        log_name = self.make_log(amount=1000, amount_paid=1000)

        with patch.object(self.engine, "fetch_transaction") as mock_fetch:
            mock_fetch.return_value = paystack_tx(amount=100000, status="failed")
            result = self.engine.reconcile_payment(log_name)

        self.assertEqual(result["status"], "Mismatch")
        self.assertIn("failed", result["reason"])

    def test_currency_mismatch_is_flagged(self) -> None:
        """A differing currency is flagged as Mismatch."""
        log_name = self.make_log(amount=1000, amount_paid=1000, currency="NGN")

        with patch.object(self.engine, "fetch_transaction") as mock_fetch:
            mock_fetch.return_value = paystack_tx(amount=100000, currency="USD")
            result = self.engine.reconcile_payment(log_name)

        self.assertEqual(result["status"], "Mismatch")
        self.assertIn("Currency", result["reason"])

    def test_refunded_amount_is_accounted_for(self) -> None:
        """Reconciliation compares against the amount net of refunds."""
        log_name = self.make_log(amount=1000, amount_paid=1000)
        frappe.db.set_value("Paystack Payment Log", log_name, "total_refunded", 400)

        with patch.object(self.engine, "fetch_transaction") as mock_fetch:
            mock_fetch.return_value = paystack_tx(amount=60000)
            result = self.engine.reconcile_payment(log_name)

        self.assertEqual(result["status"], "Reconciled")

    def test_uncompleted_log_is_flagged(self) -> None:
        """A captured payment on a Pending log is flagged as Mismatch."""
        log_name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        self.addCleanup(self.cleanup_reconciliation_log, log_name)

        with patch.object(self.engine, "fetch_transaction") as mock_fetch:
            mock_fetch.return_value = paystack_tx(amount=0)
            result = self.engine.reconcile_payment(log_name)

        self.assertEqual(result["status"], "Mismatch")

    def test_missing_transaction_id_falls_back_to_the_reference(self) -> None:
        """A log with no transaction ID is verified by its reference."""
        log_name = self.log_without_transaction_id(status="Completed", amount_paid=1000)

        with patch.object(self.engine, "verify_reference") as mock_verify:
            mock_verify.return_value = paystack_tx(amount=100000)
            result = self.engine.reconcile_payment(log_name)

        mock_verify.assert_called_once_with(log_name)
        self.assertEqual(result["status"], "Reconciled")

    def test_api_error_leaves_status_pending(self) -> None:
        """A Paystack lookup failure records Pending."""
        log_name = self.make_log(amount=1000, amount_paid=1000)

        with patch.object(self.engine, "fetch_transaction") as mock_fetch:
            mock_fetch.side_effect = Exception("API timeout")
            result = self.engine.reconcile_payment(log_name)

        self.assertEqual(result["status"], "Pending")

    def test_reconcile_nonexistent_log_raises(self) -> None:
        """Reconciling an unknown payment log raises."""
        with self.assertRaises(frappe.DoesNotExistError):
            self.engine.reconcile_payment("NONEXISTENT-LOG")

    def test_a_second_pass_rewrites_the_same_reconciliation(self) -> None:
        """A second pass updates the one reconciliation row for the payment."""
        log_name = self.make_log(amount=1000, amount_paid=1000)

        with patch.object(self.engine, "fetch_transaction") as mock_fetch:
            mock_fetch.return_value = paystack_tx(amount=95000)
            first = self.engine.reconcile_payment(log_name)

            mock_fetch.return_value = paystack_tx(amount=100000)
            second = self.engine.reconcile_payment(log_name)

        self.assertEqual(first["status"], "Mismatch")
        self.assertEqual(second["status"], "Reconciled")
        self.assertEqual(frappe.db.count(RECONCILIATION_LOG, {"payment_log": log_name}), 1)

    def test_manual_override_is_not_clobbered(self) -> None:
        """An automated pass skips a Manual Override reconciliation."""
        log_name = self.make_log(amount=1000, amount_paid=1000)

        with patch.object(self.engine, "fetch_transaction") as mock_fetch:
            mock_fetch.return_value = paystack_tx(amount=95000)
            self.engine.reconcile_payment(log_name)

        mark_manual_override(log_name, notes="Verified against bank statement")

        with patch.object(self.engine, "fetch_transaction") as mock_fetch:
            mock_fetch.return_value = paystack_tx(amount=95000)
            result = self.engine.reconcile_payment(log_name)

        self.assertEqual(result["status"], "Skipped")
        self.assertEqual(
            frappe.db.get_value("Paystack Reconciliation Log", log_name, "status"),
            "Manual Override",
        )

    def test_batch_isolates_per_payment_failures(self) -> None:
        """A batch tallies a per-payment failure and reconciles the rest."""
        self.make_log(amount=1000, amount_paid=1000)
        bad = self.make_log(amount=1000, amount_paid=1000)

        def flaky(transaction_id, payment_log=None):
            bad_tx = frappe.db.get_value("Paystack Payment Log", bad, "transaction_id")
            if transaction_id == bad_tx:
                raise RuntimeError("boom")
            return paystack_tx(amount=100000)

        with patch.object(self.engine, "write_reconciliation_log") as mock_write:
            mock_write.side_effect = lambda name, *a, **kw: (
                (_ for _ in ()).throw(RuntimeError("write failed"))
                if name == bad
                else {"payment_log": name, "status": "Reconciled", "reason": None}
            )
            with patch.object(self.engine, "fetch_transaction", side_effect=flaky):
                result = self.engine.run_full_reconciliation(add_days(today(), -30), today())

        self.assertGreaterEqual(result["errored"], 1)
        self.assertGreaterEqual(result["reconciled"], 1)
        self.assertEqual(
            result["total"],
            result["reconciled"] + result["mismatched"] + result["pending"] + result["errored"],
        )

    def test_run_full_reconciliation_counts_add_up(self) -> None:
        """Batch totals equal the sum of the per-status tallies."""
        self.make_log(amount_paid=500)
        self.make_log(amount_paid=1000)

        with patch.object(self.engine, "fetch_transaction") as mock_fetch:
            mock_fetch.return_value = paystack_tx(amount=50000)
            result = self.engine.run_full_reconciliation(add_days(today(), -30), today())

        self.assertGreater(result["total"], 0)
        self.assertEqual(
            result["total"],
            result["reconciled"] + result["mismatched"] + result["pending"] + result["errored"],
        )

    def test_fetch_transaction_returns_data(self) -> None:
        """A successful Paystack response returns the data payload."""
        with patch("requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                **{"json.return_value": {"status": True, "data": paystack_tx(50000)}}
            )
            result = self.engine.fetch_transaction("12345")

        self.assertEqual(result["amount"], 50000)

    def test_fetch_transaction_rejects_unsuccessful_response(self) -> None:
        """An unsuccessful Paystack response raises."""
        with patch("requests.get") as mock_get:
            mock_get.return_value = MagicMock(**{"json.return_value": {"status": False, "message": "nope"}})
            with self.assertRaises(ValueError):
                self.engine.fetch_transaction("12345")


class TestLostWebhookRecovery(PaystackReconciliationTestCase):
    """Reconciling a payment log that carries no transaction_id."""

    def setUp(self) -> None:
        """Build the engine after the gateway setting exists."""
        super().setUp()
        self.engine = ReconciliationEngine(TEST_COMPANY)

    def test_a_captured_payment_is_reported_as_a_mismatch(self) -> None:
        """A captured payment whose log is still Pending is flagged as Mismatch."""
        log_name = self.log_without_transaction_id()

        with patch.object(self.engine, "verify_reference", return_value=paystack_tx(amount=100000)):
            result = self.engine.reconcile_payment(log_name)

        self.assertEqual(result["status"], "Mismatch")
        self.assertIn("Pending", result["reason"])

    def test_an_unpaid_log_is_not_a_mismatch(self) -> None:
        """A log with no Paystack transaction stays Pending."""
        log_name = self.log_without_transaction_id()

        with patch.object(self.engine, "verify_reference", return_value=None):
            result = self.engine.reconcile_payment(log_name)

        self.assertEqual(result["status"], "Pending")
        self.assertIn("nothing was paid", result["reason"])

    def test_an_abandoned_checkout_is_not_a_mismatch(self) -> None:
        """An abandoned transaction leaves the log Pending."""
        log_name = self.log_without_transaction_id()

        with patch.object(
            self.engine,
            "verify_reference",
            return_value=paystack_tx(amount=100000, status="abandoned"),
        ):
            result = self.engine.reconcile_payment(log_name)

        self.assertEqual(result["status"], "Pending")
        self.assertIn("abandoned", result["reason"])

    def test_a_settled_log_paystack_never_captured_is_a_mismatch(self) -> None:
        """A completed log whose Paystack transaction failed is a Mismatch."""
        log_name = self.make_log(amount=1000, amount_paid=1000)

        with patch.object(
            self.engine,
            "fetch_transaction",
            return_value=paystack_tx(status="failed"),
        ):
            result = self.engine.reconcile_payment(log_name)

        self.assertEqual(result["status"], "Mismatch")


class TestTransactionLookupLogging(PaystackReconciliationTestCase):
    """Every Paystack call the engine makes is filed as an Integration Request."""

    def setUp(self) -> None:
        """Build the engine after the gateway setting exists."""
        super().setUp()
        self.engine = ReconciliationEngine(TEST_COMPANY)

    def paystack_response(self, status_code: int, payload: dict) -> MagicMock:
        """Build a stand-in for a requests response."""
        return MagicMock(
            status_code=status_code,
            ok=status_code < 400,
            **{"json.return_value": payload},
        )

    def request_exists(self, log_name: str, status: str) -> bool:
        """Report whether an Integration Request was filed against a log."""
        return bool(
            frappe.db.exists(
                "Integration Request",
                {
                    "reference_doctype": PAYMENT_LOG,
                    "reference_docname": log_name,
                    "status": status,
                },
            )
        )

    def test_a_verify_lookup_is_logged(self) -> None:
        """Verifying by reference files a request against the payment log."""
        log_name = self.log_without_transaction_id(status="Completed", amount_paid=1000)

        with patch(
            "requests.get",
            return_value=self.paystack_response(200, {"status": True, "data": paystack_tx(amount=100000)}),
        ):
            result = self.engine.reconcile_payment(log_name)

        self.assertEqual(result["status"], "Reconciled")
        self.assertTrue(self.request_exists(log_name, "Completed"))

    def test_the_verify_endpoint_is_called_with_the_log_name(self) -> None:
        """The verify endpoint is called with the payment log name."""
        log_name = self.log_without_transaction_id()

        with patch(
            "requests.get",
            return_value=self.paystack_response(200, {"status": True, "data": paystack_tx(amount=100000)}),
        ) as mock_get:
            self.engine.reconcile_payment(log_name)

        self.assertIn(f"/transaction/verify/{log_name}", mock_get.call_args.args[0])

    def test_an_unknown_reference_reads_as_unpaid(self) -> None:
        """A 404 from verify leaves the log Pending."""
        log_name = self.log_without_transaction_id()

        with patch(
            "requests.get",
            return_value=self.paystack_response(
                404, {"status": False, "message": "Transaction reference not found"}
            ),
        ):
            result = self.engine.reconcile_payment(log_name)

        self.assertEqual(result["status"], "Pending")
        self.assertIn("nothing was paid", result["reason"])

    def test_a_failed_lookup_is_logged(self) -> None:
        """An unreachable Paystack files a Failed request and stays Pending."""
        log_name = self.make_log(amount=1000, amount_paid=1000)

        with patch("requests.get", side_effect=Exception("connection reset")):
            result = self.engine.reconcile_payment(log_name)

        self.assertEqual(result["status"], "Pending")
        self.assertTrue(self.request_exists(log_name, "Failed"))


class TestReconciliationEngineGuards(PaystackTestCase):
    """Company and settings guards, without a gateway present."""

    def test_engine_requires_a_company(self) -> None:
        """Constructing the engine with no company raises."""
        with patch.object(frappe.defaults, "get_user_default", return_value=None):
            with self.assertRaises(frappe.ValidationError):
                ReconciliationEngine(None)

    def test_engine_requires_paystack_enabled(self) -> None:
        """A company with no enabled gateway is refused."""
        with self.assertRaises(frappe.ValidationError):
            ReconciliationEngine(TEST_COMPANY)

    def test_settings_are_not_leaked_across_companies(self) -> None:
        """Settings resolve for the gateway's own company only."""
        gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, gateway)

        self.assertIsNotNone(resolve_paystack_settings(TEST_COMPANY))
        self.assertIsNone(resolve_paystack_settings(None))


class TestReconciliationAPI(PaystackReconciliationTestCase):
    """The reconciliation API endpoints."""

    def test_reconcile_payment_endpoint(self) -> None:
        """The endpoint reconciles and reports the resulting status."""
        log_name = self.make_log(amount_paid=1000)

        with patch(
            "frappe_paystack.utils.reconciliation.ReconciliationEngine.fetch_transaction"
        ) as mock_fetch:
            mock_fetch.return_value = paystack_tx(amount=100000)
            result = reconcile_payment(log_name)

        self.assertEqual(result["status"], "Reconciled")

    def test_reconcile_payment_nonexistent_log(self) -> None:
        """An unknown payment log is rejected."""
        with self.assertRaises(frappe.DoesNotExistError):
            reconcile_payment("NONEXISTENT-LOG")

    def test_run_reconciliation_endpoint(self) -> None:
        """The batch endpoint forwards the date range to the engine."""
        with patch("frappe_paystack.utils.reconciliation_api.ReconciliationEngine") as mock_engine_class:
            mock_engine = MagicMock()
            mock_engine.run_full_reconciliation.return_value = {"total": 3}
            mock_engine_class.return_value = mock_engine

            result = run_reconciliation(add_days(today(), -7), today(), company=TEST_COMPANY)

        self.assertEqual(result["total"], 3)
        mock_engine.run_full_reconciliation.assert_called_once_with(add_days(today(), -7), today())

    def test_a_batch_naming_no_company_reaches_the_engine(self) -> None:
        """With no company named, the engine resolves the caller's own."""
        with patch("frappe_paystack.utils.reconciliation_api.ReconciliationEngine") as mock_engine_class:
            mock_engine = MagicMock()
            mock_engine.run_full_reconciliation.return_value = {"total": 0}
            mock_engine_class.return_value = mock_engine

            run_reconciliation()

        mock_engine_class.assert_called_once_with(None)

    def test_get_reconciliation_report_filters_by_status(self) -> None:
        """The report returns only rows in the requested status."""
        log_name = self.make_log(amount_paid=1000)

        with patch(
            "frappe_paystack.utils.reconciliation.ReconciliationEngine.fetch_transaction"
        ) as mock_fetch:
            mock_fetch.return_value = paystack_tx(amount=100000)
            reconcile_payment(log_name)

        report = get_reconciliation_report(status="Reconciled", limit=10)
        self.assertIn(log_name, [row["payment_log"] for row in report])

        self.assertNotIn(
            log_name,
            [row["payment_log"] for row in get_reconciliation_report(status="Mismatch")],
        )

    def test_get_reconciliation_report_caps_limit(self) -> None:
        """A large limit is clamped to at most 500 rows."""
        self.assertLessEqual(len(get_reconciliation_report(limit=10_000_000)), 500)

    def test_get_reconciliation_stats(self) -> None:
        """Stats include a total across the status buckets."""
        log_name = self.make_log(amount_paid=1000)

        with patch(
            "frappe_paystack.utils.reconciliation.ReconciliationEngine.fetch_transaction"
        ) as mock_fetch:
            mock_fetch.return_value = paystack_tx(amount=100000)
            reconcile_payment(log_name)

        stats = get_reconciliation_stats()
        self.assertIn("total", stats)
        self.assertGreaterEqual(stats["total"], 1)

    def test_mark_manual_override_requires_a_note(self) -> None:
        """An override without justification is refused."""
        log_name = self.make_log(amount_paid=1000)

        with patch(
            "frappe_paystack.utils.reconciliation.ReconciliationEngine.fetch_transaction"
        ) as mock_fetch:
            mock_fetch.return_value = paystack_tx(amount=100000)
            reconcile_payment(log_name)

        with self.assertRaises(frappe.ValidationError):
            mark_manual_override(log_name, notes="")


class TestReconciliationPermissions(PaystackReconciliationTestCase):
    """Endpoints refuse users holding no reconciliation role."""

    def setUp(self) -> None:
        """Create and switch to an unprivileged user."""
        super().setUp()
        self.user = self.create_limited_user()
        frappe.set_user(self.user)
        self.addCleanup(frappe.set_user, "Administrator")

    def create_limited_user(self) -> str:
        """Create a user holding no Paystack-related role."""
        email = "paystack-nobody@example.com"
        if not frappe.db.exists("User", email):
            user = frappe.get_doc(
                {
                    "doctype": "User",
                    "email": email,
                    "first_name": "Paystack",
                    "last_name": "Nobody",
                    "send_welcome_email": 0,
                    "roles": [{"role": "Blogger"}],
                }
            )
            user.flags.ignore_permissions = True
            user.insert()
            frappe.db.commit()
        self.addCleanup(cleanup_user, email)
        return email

    def test_reconcile_payment_is_refused(self) -> None:
        """An unprivileged user cannot trigger reconciliation."""
        with self.assertRaises(frappe.PermissionError):
            reconcile_payment("any-log")

    def test_run_reconciliation_is_refused(self) -> None:
        """An unprivileged user cannot start a batch run."""
        with self.assertRaises(frappe.PermissionError):
            run_reconciliation()

    def test_manual_override_is_refused(self) -> None:
        """An unprivileged user cannot override a reconciliation."""
        with self.assertRaises(frappe.PermissionError):
            mark_manual_override("any-log", notes="nope")

    def test_reports_are_refused(self) -> None:
        """An unprivileged user cannot read reconciliation reports."""
        with self.assertRaises(frappe.PermissionError):
            get_reconciliation_report()
        with self.assertRaises(frappe.PermissionError):
            get_reconciliation_stats()


class TestReconciliationCompanyScope(PaystackReconciliationTestCase):
    """The role opens the endpoints; a company permission narrows them."""

    def setUp(self) -> None:
        """Reconcile a payment, then restrict the caller to another company."""
        super().setUp()
        self.log_name = self.make_log(amount=1000, amount_paid=1000)
        self.reconciled = self.write_reconciliation()

        self.user = self.become_accountant()
        self.restrict_to_other_company(self.user)

    def write_reconciliation(self) -> str:
        """Record a Mismatch reconciliation for the fixture payment."""
        log = frappe.new_doc(RECONCILIATION_LOG)
        log.payment_log = self.log_name
        log.company = TEST_COMPANY
        log.reconciliation_date = today()
        log.status = "Mismatch"
        log.flags.ignore_permissions = True
        log.insert()
        return log.name

    def become_accountant(self) -> str:
        """Create an Accounts Manager and switch to it."""
        if not frappe.db.exists("User", ACCOUNTANT_EMAIL):
            user = frappe.get_doc(
                {
                    "doctype": "User",
                    "email": ACCOUNTANT_EMAIL,
                    "first_name": "Paystack Accountant",
                    "send_welcome_email": 0,
                    "roles": [{"role": "Accounts Manager"}],
                }
            )
            user.flags.ignore_permissions = True
            user.insert()
            frappe.db.commit()

        self.addCleanup(cleanup_user, ACCOUNTANT_EMAIL)
        self.addCleanup(frappe.set_user, "Administrator")
        return ACCOUNTANT_EMAIL

    def restrict_to_other_company(self, user: str) -> None:
        """Give a user a permission for a company the fixture payment falls outside of."""
        permission = frappe.get_doc(
            {
                "doctype": USER_PERMISSION,
                "user": user,
                "allow": "Company",
                "for_value": OTHER_COMPANY,
            }
        )
        permission.flags.ignore_permissions = True
        permission.insert()
        self.addCleanup(
            frappe.delete_doc,
            USER_PERMISSION,
            permission.name,
            force=True,
            ignore_permissions=True,
        )
        frappe.db.commit()
        frappe.clear_cache(user=user)
        frappe.set_user(user)

    def test_another_companys_payment_is_not_reconciled(self) -> None:
        """reconcile_payment refuses a payment outside the caller's company."""
        with self.assertRaises(frappe.PermissionError):
            reconcile_payment(self.log_name)

    def test_another_companys_batch_is_refused(self) -> None:
        """run_reconciliation refuses a company the caller may not read."""
        with self.assertRaises(frappe.PermissionError):
            run_reconciliation(company=TEST_COMPANY)

    def test_the_company_fallen_back_to_is_judged_too(self) -> None:
        """A batch run naming no company is judged on the one it falls back to."""
        with patch(
            "frappe.defaults.get_user_default",
            side_effect=lambda key, *args, **kwargs: TEST_COMPANY if key == "company" else None,
        ):
            with self.assertRaises(frappe.PermissionError):
                run_reconciliation()

    def test_another_companys_mismatch_cannot_be_overridden(self) -> None:
        """mark_manual_override refuses a reconciliation outside the company."""
        with self.assertRaises(frappe.PermissionError):
            mark_manual_override(self.reconciled, notes="not mine to silence")

    def test_a_refused_override_leaves_the_status_alone(self) -> None:
        """The mismatch the override was refused still reads as a mismatch."""
        with self.assertRaises(frappe.PermissionError):
            mark_manual_override(self.reconciled, notes="not mine to silence")

        frappe.set_user("Administrator")
        self.assertEqual(
            frappe.db.get_value(RECONCILIATION_LOG, self.reconciled, "status"),
            "Mismatch",
        )


class TestReconciliationValidation(PaystackReconciliationTestCase):
    """Paystack Reconciliation Log validation."""

    def build_log(self, log_name: str, **kwargs) -> object:
        """Build an unsaved reconciliation log for a payment."""
        doc = frappe.new_doc("Paystack Reconciliation Log")
        doc.payment_log = log_name
        doc.company = TEST_COMPANY
        doc.reconciliation_date = today()
        doc.flags.ignore_permissions = True
        doc.update(kwargs)
        return doc

    def test_negative_amounts_rejected(self) -> None:
        """Negative amounts are refused."""
        log_name = self.make_log(amount_paid=1000)
        doc = self.build_log(log_name, paystack_amount=-100, frappe_amount=100, status="Mismatch")

        with self.assertRaises(frappe.ValidationError):
            doc.insert()

    def test_reconciled_with_difference_rejected(self) -> None:
        """A Reconciled record must actually balance."""
        log_name = self.make_log(amount_paid=1000)
        doc = self.build_log(log_name, paystack_amount=1000, frappe_amount=900, status="Reconciled")

        with self.assertRaises(frappe.ValidationError):
            doc.insert()

    def test_small_difference_accepted(self) -> None:
        """A sub-tolerance difference still counts as Reconciled."""
        log_name = self.make_log(amount_paid=1000)
        doc = self.build_log(
            log_name,
            paystack_amount=1000.001,
            frappe_amount=1000.0,
            status="Reconciled",
        )
        doc.insert()

        self.assertEqual(doc.status, "Reconciled")

    def test_difference_is_computed_on_save(self) -> None:
        """The difference field is computed on save."""
        log_name = self.make_log(amount_paid=1000)
        doc = self.build_log(
            log_name,
            paystack_amount=900,
            frappe_amount=1000,
            difference=0,
            status="Mismatch",
        )
        doc.insert()

        self.assertAlmostEqual(doc.difference, -100.0, places=2)


class ReconciliationPathTestCase(PaystackReconciliationTestCase):
    """Shared gateway and payment log fixtures."""

    def setUp(self) -> None:
        """Expose the gateway setting name as self.gateway."""
        super().setUp()
        self.gateway = self.gateway_name


class TestBatchTallies(ReconciliationPathTestCase):
    """run_full_reconciliation tallies every per-payment outcome."""

    def setUp(self) -> None:
        """Build the engine after the gateway setting exists."""
        super().setUp()
        self.engine = ReconciliationEngine(TEST_COMPANY)

    def test_overridden_payments_are_skipped_and_errors_stay_pending(self) -> None:
        """A manually overridden payment is skipped; an unreachable one stays Pending."""
        overridden = self.make_log(amount=1000, amount_paid=1000)
        unreachable = self.make_log(amount=1000, amount_paid=1000)

        with patch.object(
            self.engine,
            "fetch_transaction",
            return_value={"amount": 100000, "status": "success", "currency": "NGN"},
        ):
            self.engine.reconcile_payment(overridden)
        mark_manual_override(overridden, notes="Matched against the bank statement")

        with patch.object(self.engine, "fetch_transaction", side_effect=Exception("gateway down")):
            result = self.engine.run_full_reconciliation(add_days(today(), -1), today())

        self.assertGreaterEqual(result["pending"], 1)
        self.assertEqual(
            frappe.db.get_value(RECONCILIATION_LOG, overridden, "status"),
            "Manual Override",
        )
        self.assertEqual(frappe.db.get_value(RECONCILIATION_LOG, unreachable, "status"), "Pending")


class TestPermittedRoles(ReconciliationPathTestCase):
    """check_reconciliation_permission admits users holding a reconciliation role."""

    def create_accountant(self) -> str:
        """Create a user holding the Accounts Manager role."""
        if not frappe.db.exists("User", ACCOUNTANT_EMAIL):
            user = frappe.get_doc(
                {
                    "doctype": "User",
                    "email": ACCOUNTANT_EMAIL,
                    "first_name": "Paystack",
                    "last_name": "Accountant",
                    "send_welcome_email": 0,
                    "roles": [{"role": "Accounts Manager"}],
                }
            )
            user.flags.ignore_permissions = True
            user.insert()
            frappe.db.commit()
        self.addCleanup(cleanup_user, ACCOUNTANT_EMAIL)
        return ACCOUNTANT_EMAIL

    def test_accounts_manager_is_admitted(self) -> None:
        """A non-Administrator holding Accounts Manager passes the permission check."""
        user = self.create_accountant()
        frappe.set_user(user)
        self.addCleanup(frappe.set_user, "Administrator")

        self.assertIsNone(check_reconciliation_permission())


class TestCompanyScopedReports(ReconciliationPathTestCase):
    """Reports are narrowed to the companies a user is permitted to see."""

    def user_permissions(self, companies: list) -> Any:
        """Patch the user permission lookup to grant the given companies."""
        return patch.object(
            frappe.defaults,
            "get_user_permissions",
            return_value={"Company": [{"doc": company} for company in companies]},
        )

    def test_permitted_companies_are_listed(self) -> None:
        """The permitted company list is read off the user permissions."""
        with self.user_permissions([TEST_COMPANY]):
            self.assertEqual(permitted_companies(), [TEST_COMPANY])

    def test_no_user_permissions_leaves_the_filters_alone(self) -> None:
        """Without company permissions the filters are left untouched."""
        with self.user_permissions([]):
            self.assertEqual(apply_company_filter({"status": "Reconciled"}), {"status": "Reconciled"})

    def test_filters_are_narrowed_to_permitted_companies(self) -> None:
        """A permitted company list becomes an "in" filter on company."""
        with self.user_permissions([TEST_COMPANY]):
            filters = apply_company_filter({})

        self.assertEqual(filters, {"company": ["in", [TEST_COMPANY]]})

    def reconcile_one(self, company: str = TEST_COMPANY) -> str:
        """Record a reconciliation for a payment booked to a company."""
        log_name = self.make_log(amount=1000, amount_paid=1000)
        if company != TEST_COMPANY:
            frappe.db.set_value(PAYMENT_LOG, log_name, "company", company, update_modified=False)

        engine = ReconciliationEngine(TEST_COMPANY)
        engine.company = company
        with patch.object(
            engine,
            "fetch_transaction",
            return_value={"amount": 100000, "status": "success", "currency": "NGN"},
        ):
            engine.reconcile_payment(log_name)

        self.addCleanup(self.cleanup_reconciliation_log, log_name)
        return log_name

    def test_report_is_scoped_to_permitted_companies(self) -> None:
        """A report run under company permissions returns only those companies."""
        self.reconcile_one()
        self.reconcile_one(OTHER_COMPANY)

        with self.user_permissions([TEST_COMPANY]):
            rows = get_reconciliation_report()

        self.assertTrue(rows)
        self.assertEqual({row["company"] for row in rows}, {TEST_COMPANY})

    def test_stats_are_scoped_to_permitted_companies(self) -> None:
        """Reconciliation counts respect the same company permissions."""
        permitted = self.reconcile_one()
        self.reconcile_one(OTHER_COMPANY)

        with self.user_permissions([TEST_COMPANY]):
            stats = get_reconciliation_stats(days=7)

        self.assertEqual(stats["total"], sum(v for k, v in stats.items() if k != "total"))
        self.assertEqual(
            stats["total"],
            frappe.db.count(RECONCILIATION_LOG, {"company": TEST_COMPANY, "name": permitted}),
        )


class TestManualOverrideGuards(ReconciliationPathTestCase):
    """mark_manual_override needs an existing reconciliation to override."""

    def test_unreconciled_payment_cannot_be_overridden(self) -> None:
        """Overriding a payment with no reconciliation row raises."""
        log_name = self.make_log(amount=1000, amount_paid=1000)

        with self.assertRaises(frappe.DoesNotExistError):
            mark_manual_override(log_name, notes="Nothing to override yet")


class TestScheduledJobRegistration(PaystackTestCase):
    """The scheduler_events hooks entries and the queues they sit on."""

    def registered_methods(self) -> list:
        """Return every job path scheduler_events registers, cron entries included."""
        methods = []

        for events in hooks.scheduler_events.values():
            if isinstance(events, dict):
                for entry in events.values():
                    methods.extend(entry)
                continue

            methods.extend(events)

        return methods

    def test_scheduler_events_resolve(self) -> None:
        """Every registered scheduled job path is importable."""
        methods = self.registered_methods()
        self.assertTrue(methods)

        for method in methods:
            with self.subTest(method=method):
                self.assertTrue(callable(frappe.get_attr(method)))

    def test_reconciliation_runs_on_a_long_queue(self) -> None:
        """run_daily_reconciliation is registered on the daily_long queue."""
        daily_long = hooks.scheduler_events.get("daily_long", [])
        self.assertIn("frappe_paystack.utils.scheduled_jobs.run_daily_reconciliation", daily_long)

    def test_the_weekly_report_is_not_scheduled(self) -> None:
        """No frequency schedules the weekly reconciliation summary."""
        self.assertNotIn(
            "frappe_paystack.utils.scheduled_jobs.generate_weekly_reconciliation_report",
            self.registered_methods(),
        )


class TestPaystackCompanies(PaystackTestCase):
    """Company discovery for the scheduled jobs."""

    def test_returns_only_enabled_gateways(self) -> None:
        """A company appears once its gateway is enabled."""
        self.assertNotIn(TEST_COMPANY, paystack_companies())

        gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, gateway)

        self.assertIn(TEST_COMPANY, paystack_companies())

    def test_returns_unique_sorted_companies(self) -> None:
        """The list is de-duplicated."""
        gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, gateway)

        companies = paystack_companies()
        self.assertEqual(len(companies), len(set(companies)))


class TestDailyReconciliationJob(PaystackTestCase):
    """run_daily_reconciliation behaviour."""

    def setUp(self) -> None:
        """Enable Paystack for the test company."""
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

    def test_reconciles_recent_payments(self) -> None:
        """The job reconciles each enabled company's recent payments."""
        log_name = PaymentLogFactory.create_completed(amount=1000, amount_paid=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log_name)
        self.addCleanup(self.cleanup_reconciliation_log, log_name)

        with (
            patch(
                "frappe_paystack.utils.scheduled_jobs.paystack_companies",
                return_value=[TEST_COMPANY],
            ),
            patch(
                "frappe_paystack.utils.reconciliation.ReconciliationEngine.fetch_transaction"
            ) as mock_fetch,
        ):
            mock_fetch.return_value = {
                "amount": 100000,
                "status": "success",
                "currency": "NGN",
            }
            run_daily_reconciliation()

        self.assertEqual(
            frappe.db.get_value("Paystack Reconciliation Log", log_name, "status"),
            "Reconciled",
        )

    def test_one_company_failing_does_not_raise(self) -> None:
        """An engine failure is logged and the job returns."""
        with patch("frappe_paystack.utils.scheduled_jobs.ReconciliationEngine") as mock_engine:
            mock_engine.side_effect = RuntimeError("gateway exploded")
            run_daily_reconciliation()

    def test_disabled_company_is_skipped(self) -> None:
        """A company whose gateway has been removed is left out of the run."""
        GatewaySettingFactory.cleanup(self.gateway_name)

        with patch("frappe_paystack.utils.scheduled_jobs.ReconciliationEngine") as mock_engine:
            run_daily_reconciliation()

        reconciled = [call.args[0] for call in mock_engine.call_args_list if call.args]
        self.assertNotIn(TEST_COMPANY, reconciled)

    def test_a_company_lookup_failure_does_not_raise(self) -> None:
        """A failing gateway query is logged and the job returns."""
        with patch("frappe.get_all", side_effect=failing_lookup(GATEWAY_SETTING)):
            self.assertIsNone(run_daily_reconciliation())

    def test_the_pass_reads_the_recent_window(self) -> None:
        """Each company is reconciled over the daily lookback, ending today."""
        with (
            patch(
                "frappe_paystack.utils.scheduled_jobs.paystack_companies",
                return_value=[TEST_COMPANY],
            ),
            patch(
                "frappe_paystack.utils.reconciliation.ReconciliationEngine.run_full_reconciliation"
            ) as mock_run,
        ):
            run_daily_reconciliation()

        mock_run.assert_called_once_with(add_days(today(), -DAILY_LOOKBACK_DAYS), today())
