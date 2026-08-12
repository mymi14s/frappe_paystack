"""The Paystack Activity report: four tables read as one chronology."""

from unittest.mock import patch

import frappe
from frappe.utils import add_days, today

from frappe_paystack.frappe_paystack.report.paystack_activity import paystack_activity as activity
from frappe_paystack.tests.factories import (
    PaymentLogFactory,
    RefundLogFactory,
    SettlementFactory,
    cleanup_doc,
)
from frappe_paystack.tests.test_base import TEST_COMPANY, PaystackTestCase
from frappe_paystack.utils import log_integration_request

PAYMENT_LOG = "Paystack Payment Log"
REFUND_LOG = "Paystack Refund Log"
SETTLEMENT = "Paystack Settlement"
INTEGRATION_REQUEST = "Integration Request"

OTHER_COMPANY = "_Test Company 1"


class ActivityTestCase(PaystackTestCase):
    """Runs the report over a window that holds only what the test made."""

    def feed(self, **filters) -> dict:
        """Return the feed keyed by the doctype and record each row names."""
        filters.setdefault("company", TEST_COMPANY)
        filters.setdefault("from_date", add_days(today(), -1))
        filters.setdefault("to_date", today())

        _columns, rows = activity.execute(filters)
        return {(row["source_doctype"], row["record"]): row for row in rows}

    def row_for(self, doctype: str, name: str, **filters) -> dict:
        """Return the one feed row that names a record."""
        row = self.feed(**filters).get((doctype, name))
        self.assertIsNotNone(row, f"{doctype} {name} is missing from the feed")
        return row

    def capture(self, **overrides) -> str:
        """Raise a Payment Log the feed should carry."""
        name = PaymentLogFactory.create(**overrides)
        self.addCleanup(PaymentLogFactory.cleanup, name)
        return name

    def payout(self, **overrides) -> str:
        """Raise a Paystack Settlement the feed should carry."""
        name = SettlementFactory.create(**overrides)
        self.addCleanup(SettlementFactory.cleanup, name)
        return name

    def api_call(self, **overrides) -> str:
        """File a Paystack Integration Request the feed should carry."""
        overrides.setdefault("status", "Completed")
        overrides.setdefault("url", "https://api.paystack.co/transaction")
        overrides.setdefault("request_data", {"probe": 1})
        return log_integration_request(**overrides)


class TestActivityColumns(PaystackTestCase):
    """The grid resolves each row back to the record behind it."""

    def test_the_record_column_is_a_dynamic_link_on_the_source_doctype(self) -> None:
        """The record column is a Dynamic Link keyed on source_doctype."""
        columns = {column["fieldname"]: column for column in activity.get_columns()}

        self.assertEqual(
            (columns["record"]["fieldtype"], columns["record"]["options"]),
            ("Dynamic Link", "source_doctype"),
        )

    def test_the_doctype_the_link_points_at_is_a_column_of_its_own(self) -> None:
        """source_doctype is a column of the report."""
        fieldnames = [column["fieldname"] for column in activity.get_columns()]

        self.assertIn("source_doctype", fieldnames)

    def test_the_amount_is_formatted_in_the_row_currency(self) -> None:
        """The amount column takes its symbol from the row's currency field."""
        columns = {column["fieldname"]: column for column in activity.get_columns()}

        self.assertEqual(columns["amount"]["options"], "currency")


class TestActivityWindow(ActivityTestCase):
    """creation is a Datetime and the filters are Dates."""

    def test_a_row_logged_today_is_inside_a_window_ending_today(self) -> None:
        """A row logged today falls inside a window ending today."""
        log = self.capture()

        self.assertIn((PAYMENT_LOG, log), self.feed(to_date=today()))

    def test_an_open_ended_window_still_reaches_back(self) -> None:
        """A row is carried when only from_date is set."""
        log = self.capture()

        self.assertIn((PAYMENT_LOG, log), self.feed(from_date=add_days(today(), -1), to_date=None))

    def test_a_window_with_no_start_still_closes_at_the_end(self) -> None:
        """A row is carried when only to_date is set."""
        log = self.capture()

        self.assertIn((PAYMENT_LOG, log), self.feed(from_date=None, to_date=today()))

    def test_a_feed_with_no_dates_carries_everything(self) -> None:
        """A row is carried when neither date is set."""
        log = self.capture()

        self.assertIn((PAYMENT_LOG, log), self.feed(from_date=None, to_date=None))

    def test_a_row_after_the_window_is_left_out(self) -> None:
        """A row later than the window is left out."""
        log = self.capture()

        self.assertNotIn(
            (PAYMENT_LOG, log),
            self.feed(from_date=add_days(today(), -3), to_date=add_days(today(), -2)),
        )

    def test_a_row_before_the_window_is_left_out(self) -> None:
        """A row earlier than the window is left out."""
        log = self.capture()

        self.assertNotIn(
            (PAYMENT_LOG, log),
            self.feed(from_date=add_days(today(), 1), to_date=add_days(today(), 2)),
        )

    def test_the_report_runs_before_any_filter_is_set(self) -> None:
        """The report executes with no filters and carries the row."""
        log = self.capture()

        _columns, rows = activity.execute()

        self.assertIn(log, [row["record"] for row in rows])


class TestActivityPayments(ActivityTestCase):
    """Captures, and the grading of one that is still awaiting its booking."""

    def test_a_capture_is_carried_with_its_amount(self) -> None:
        """A capture's row carries its amount."""
        log = self.capture(amount=2500, amount_paid=2500)

        self.assertEqual(self.row_for(PAYMENT_LOG, log)["amount"], 2500.0)

    def test_a_capture_names_its_own_doctype(self) -> None:
        """A capture's row names Payment as its source."""
        log = self.capture()

        self.assertEqual(self.row_for(PAYMENT_LOG, log)["source"], "Payment")

    def test_a_pending_capture_is_information(self) -> None:
        """A Pending capture is graded Info."""
        log = self.capture(status="Pending")

        self.assertEqual(self.row_for(PAYMENT_LOG, log)["severity"], "Info")

    def test_a_failed_capture_is_an_error(self) -> None:
        """A Failed capture is graded Error."""
        log = self.capture()
        frappe.db.set_value(PAYMENT_LOG, log, "status", "Failed")

        self.assertEqual(self.row_for(PAYMENT_LOG, log)["severity"], "Error")

    def test_money_taken_but_not_booked_is_a_warning(self) -> None:
        """A Processed capture with no Payment Entry is graded Warning."""
        log = self.capture()
        frappe.db.set_value(PAYMENT_LOG, log, "status", "Processed")

        self.assertEqual(self.row_for(PAYMENT_LOG, log)["severity"], "Warning")

    def test_money_taken_and_booked_is_information(self) -> None:
        """A Processed capture with a Payment Entry is graded Info."""
        log = self.capture()
        frappe.db.set_value(
            PAYMENT_LOG,
            log,
            {"status": "Processed", "payment_entry": "PE-ACTIVITY-TEST"},
        )

        self.assertEqual(self.row_for(PAYMENT_LOG, log)["severity"], "Info")

    def test_a_capture_that_failed_says_why(self) -> None:
        """A capture's detail carries its error, whitespace collapsed."""
        log = self.capture()
        frappe.db.set_value(PAYMENT_LOG, log, "errors", "Insufficient\n  funds")

        self.assertEqual(self.row_for(PAYMENT_LOG, log)["detail"], "Insufficient funds")

    def test_a_healthy_capture_names_the_document_it_paid(self) -> None:
        """A capture with no error details the document it paid."""
        log = self.capture()
        invoice = frappe.db.get_value(PAYMENT_LOG, log, "linked_docname")

        self.assertEqual(self.row_for(PAYMENT_LOG, log)["detail"], f"Sales Invoice {invoice}")


class TestActivityRefunds(ActivityTestCase):
    """Refunds: their amount, their grading and their detail line."""

    def setUp(self) -> None:
        """Raise a captured payment every refund here can be taken from."""
        super().setUp()
        self.paid = PaymentLogFactory.create_completed(amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, self.paid)

    def refund(self, **overrides) -> str:
        """Raise a Refund Log the feed should carry."""
        name = RefundLogFactory.create_bypass_validate(self.paid, **overrides)
        self.addCleanup(RefundLogFactory.cleanup, name)
        return name

    def test_a_refund_is_carried_with_its_amount(self) -> None:
        """A refund's row carries its refund amount."""
        refund = self.refund(refund_amount=250)

        self.assertEqual(self.row_for(REFUND_LOG, refund)["amount"], 250.0)

    def test_a_pending_refund_is_a_warning(self) -> None:
        """A Pending refund is graded Warning."""
        refund = self.refund(status="Pending")

        self.assertEqual(self.row_for(REFUND_LOG, refund)["severity"], "Warning")

    def test_a_failed_refund_is_an_error(self) -> None:
        """A Failed refund is graded Error."""
        refund = self.refund(status="Failed")

        self.assertEqual(self.row_for(REFUND_LOG, refund)["severity"], "Error")

    def test_a_completed_refund_is_information(self) -> None:
        """A Completed refund is graded Info."""
        refund = self.refund(status="Completed")

        self.assertEqual(self.row_for(REFUND_LOG, refund)["severity"], "Info")

    def test_a_failed_refund_says_why(self) -> None:
        """A failed refund's detail carries its error."""
        refund = self.refund(status="Failed")
        frappe.db.set_value(REFUND_LOG, refund, "errors", "Refund window closed")

        self.assertEqual(self.row_for(REFUND_LOG, refund)["detail"], "Refund window closed")

    def test_a_healthy_refund_gives_the_reason_it_was_raised(self) -> None:
        """A refund with no error details its reason, whitespace collapsed."""
        refund = self.refund()
        frappe.db.set_value(REFUND_LOG, refund, "refund_reason", "Wrong  size")

        self.assertEqual(self.row_for(REFUND_LOG, refund)["detail"], "Wrong size")

    def test_a_refund_with_nothing_to_say_names_its_capture(self) -> None:
        """A refund with no error and no reason details the capture it reverses."""
        refund = self.refund()

        self.assertEqual(self.row_for(REFUND_LOG, refund)["detail"], self.paid)


class TestActivityPayouts(ActivityTestCase):
    """Payouts, graded on the ledger behind them."""

    def test_a_payout_is_carried_with_its_net(self) -> None:
        """A payout's row carries its net amount."""
        payout = self.payout(net_amount=985)

        self.assertEqual(self.row_for(SETTLEMENT, payout)["amount"], 985.0)

    def test_an_unbooked_payout_is_a_warning(self) -> None:
        """A payout with no journal entry is graded Warning."""
        payout = self.payout()

        self.assertEqual(self.row_for(SETTLEMENT, payout)["severity"], "Warning")

    def test_a_booked_payout_is_information(self) -> None:
        """A payout with a journal entry is graded Info."""
        payout = self.payout()
        frappe.db.set_value(SETTLEMENT, payout, "journal_entry", "JE-ACTIVITY-TEST")

        self.assertEqual(self.row_for(SETTLEMENT, payout)["severity"], "Info")

    def test_a_failed_payout_is_an_error(self) -> None:
        """A Failed payout is graded Error."""
        payout = self.payout(status="Failed")

        self.assertEqual(self.row_for(SETTLEMENT, payout)["severity"], "Error")

    def test_a_payout_that_refused_to_post_is_an_error(self) -> None:
        """A payout carrying an error is graded Error."""
        payout = self.payout()
        frappe.db.set_value(SETTLEMENT, payout, "errors", "Fee account is not set")

        self.assertEqual(self.row_for(SETTLEMENT, payout)["severity"], "Error")

    def test_a_payout_that_refused_to_post_says_why(self) -> None:
        """A payout's detail carries its error."""
        payout = self.payout()
        frappe.db.set_value(SETTLEMENT, payout, "errors", "Fee account is not set")

        self.assertEqual(self.row_for(SETTLEMENT, payout)["detail"], "Fee account is not set")

    def test_a_booked_payout_names_its_journal_entry(self) -> None:
        """A booked payout details its journal entry."""
        payout = self.payout()
        frappe.db.set_value(SETTLEMENT, payout, "journal_entry", "JE-ACTIVITY-TEST")

        self.assertEqual(self.row_for(SETTLEMENT, payout)["detail"], "JE-ACTIVITY-TEST")

    def test_a_payout_with_no_entry_and_no_error_says_it_is_unposted(self) -> None:
        """A payout with no entry and no error details that it is unposted."""
        payout = self.payout()

        self.assertEqual(
            self.row_for(SETTLEMENT, payout)["detail"],
            "Not posted: no journal entry cleared this payout.",
        )


class TestActivityApiCalls(ActivityTestCase):
    """The Integration Requests behind the other rows, unattributed ones included."""

    def test_a_paystack_call_is_carried(self) -> None:
        """An Integration Request's row names API Call as its source."""
        request = self.api_call()

        self.assertEqual(self.row_for(INTEGRATION_REQUEST, request)["source"], "API Call")

    def test_a_failed_call_is_an_error(self) -> None:
        """A Failed Integration Request is graded Error."""
        request = self.api_call(status="Failed", error="Invalid Paystack signature")

        self.assertEqual(self.row_for(INTEGRATION_REQUEST, request)["severity"], "Error")

    def test_a_completed_call_is_information(self) -> None:
        """A Completed Integration Request is graded Info."""
        request = self.api_call()

        self.assertEqual(self.row_for(INTEGRATION_REQUEST, request)["severity"], "Info")

    def test_a_failed_call_shows_what_paystack_said(self) -> None:
        """A failed call's detail carries the error Paystack returned."""
        request = self.api_call(status="Failed", error="Invalid Paystack signature")

        self.assertEqual(
            self.row_for(INTEGRATION_REQUEST, request)["detail"],
            "Invalid Paystack signature",
        )

    def test_a_successful_call_shows_the_endpoint_not_a_null(self) -> None:
        """A successful call's detail carries its endpoint URL."""
        request = self.api_call(url="https://api.paystack.co/refund")

        self.assertEqual(
            self.row_for(INTEGRATION_REQUEST, request)["detail"],
            "https://api.paystack.co/refund",
        )

    def test_a_call_carries_no_amount(self) -> None:
        """An API call's row carries a null amount."""
        request = self.api_call()

        self.assertIsNone(self.row_for(INTEGRATION_REQUEST, request)["amount"])

    def test_a_call_naming_a_paystack_record_takes_its_company(self) -> None:
        """A call naming a Paystack record takes that record's company."""
        log = self.capture()
        request = self.api_call(reference_doctype=PAYMENT_LOG, reference_docname=log)

        self.assertEqual(self.row_for(INTEGRATION_REQUEST, request)["company"], TEST_COMPANY)

    def test_a_call_naming_nothing_has_no_company(self) -> None:
        """A call naming no record has a null company."""
        request = self.api_call(status="Failed", error="Invalid Paystack signature")

        self.assertIsNone(self.row_for(INTEGRATION_REQUEST, request)["company"])

    def test_an_unattributable_call_survives_a_company_filter(self) -> None:
        """A call with no company is carried under a company filter."""
        request = self.api_call(status="Failed", error="Invalid Paystack signature")

        self.assertIn((INTEGRATION_REQUEST, request), self.feed(company=TEST_COMPANY))

    def test_another_tenants_call_is_filtered_out(self) -> None:
        """A call carrying one company is left out of another company's feed."""
        log = self.capture()
        request = self.api_call(reference_doctype=PAYMENT_LOG, reference_docname=log)

        self.assertNotIn((INTEGRATION_REQUEST, request), self.feed(company=OTHER_COMPANY))

    def test_a_call_naming_a_foreign_doctype_is_not_looked_up(self) -> None:
        """A call naming a doctype outside Paystack has a null company."""
        request = self.api_call(reference_doctype="Company", reference_docname=TEST_COMPANY)

        self.assertIsNone(self.row_for(INTEGRATION_REQUEST, request)["company"])


class TestActivityCompanyFilter(ActivityTestCase):
    """Each tenant reads its own captures, refunds and payouts."""

    def test_a_capture_from_another_company_is_left_out(self) -> None:
        """A capture is left out of another company's feed."""
        log = self.capture()

        self.assertNotIn((PAYMENT_LOG, log), self.feed(company=OTHER_COMPANY))

    def test_no_company_filter_reads_every_tenant(self) -> None:
        """An unset company filter carries every company's rows."""
        log = self.capture()

        self.assertIn((PAYMENT_LOG, log), self.feed(company=None))


class TestActivityCompanyScope(ActivityTestCase):
    """A caller held to one company reads only that company's feed."""

    def setUp(self) -> None:
        """Raise a capture and an API call, then restrict the caller."""
        super().setUp()
        self.log = self.capture()
        self.request = self.api_call(reference_doctype=PAYMENT_LOG, reference_docname=self.log)
        self.addCleanup(cleanup_doc, INTEGRATION_REQUEST, self.request)

        self.become_restricted_accountant()

    def test_a_company_the_caller_may_not_read_is_refused(self) -> None:
        """Naming another company throws before any row is read."""
        with self.assertRaises(frappe.PermissionError):
            self.feed(company=TEST_COMPANY)

    def test_an_unnamed_company_reads_only_the_permitted_ones(self) -> None:
        """A feed asking for no company in particular still leaves others out."""
        self.assertNotIn((PAYMENT_LOG, self.log), self.feed(company=None))

    def test_an_api_call_of_another_company_is_left_out(self) -> None:
        """An API call filed against another company's capture is left out."""
        self.assertNotIn((INTEGRATION_REQUEST, self.request), self.feed(company=None))


class TestActivitySourceFilter(ActivityTestCase):
    """Narrowing the feed to the one table a question is about."""

    def test_a_source_filter_keeps_its_own_rows(self) -> None:
        """The Payouts filter keeps settlement rows."""
        payout = self.payout()

        self.assertIn((SETTLEMENT, payout), self.feed(source="Payouts"))

    def test_a_source_filter_drops_the_others(self) -> None:
        """The Payouts filter drops payment log rows."""
        log = self.capture()

        self.assertNotIn((PAYMENT_LOG, log), self.feed(source="Payouts"))

    def test_no_source_filter_merges_every_table(self) -> None:
        """An unset source filter merges every table."""
        log = self.capture()
        payout = self.payout()

        feed = self.feed(source=None)
        self.assertEqual([(PAYMENT_LOG, log) in feed, (SETTLEMENT, payout) in feed], [True, True])


class TestActivitySeverityFilter(ActivityTestCase):
    """Reading only the rows that need somebody."""

    def test_a_severity_filter_keeps_its_own_grade(self) -> None:
        """The Error filter keeps rows graded Error."""
        payout = self.payout(status="Failed")

        self.assertIn((SETTLEMENT, payout), self.feed(severity="Error"))

    def test_a_severity_filter_drops_the_other_grades(self) -> None:
        """The Error filter drops rows graded Info."""
        log = self.capture(status="Pending")

        self.assertNotIn((PAYMENT_LOG, log), self.feed(severity="Error"))

    def test_no_severity_filter_carries_every_grade(self) -> None:
        """An unset severity filter carries every grade."""
        log = self.capture(status="Pending")

        self.assertIn((PAYMENT_LOG, log), self.feed(severity=None))


class TestActivityOrdering(ActivityTestCase):
    """One chronology, newest first, whichever table a row came from."""

    def test_the_feed_reads_newest_first(self) -> None:
        """The rows come back ordered by timestamp, newest first."""
        _columns, rows = activity.execute({"company": TEST_COMPANY, "from_date": add_days(today(), -1)})

        self.assertEqual(
            [row["timestamp"] for row in rows],
            sorted((row["timestamp"] for row in rows), reverse=True),
        )

    def test_a_payout_and_a_capture_share_one_ordering(self) -> None:
        """The payout was made after the capture, so it reads above it."""
        log = self.capture()
        payout = self.payout()

        _columns, rows = activity.execute({"company": TEST_COMPANY, "from_date": add_days(today(), -1)})
        records = [row["record"] for row in rows]

        self.assertLess(records.index(payout), records.index(log))

    def test_the_feed_is_cut_rather_than_returned_whole(self) -> None:
        """The feed returns at most ACTIVITY_LIMIT rows."""
        self.capture()
        self.payout()

        with patch.object(activity, "ACTIVITY_LIMIT", 1):
            _columns, rows = activity.execute({"company": TEST_COMPANY, "from_date": add_days(today(), -1)})

        self.assertEqual(len(rows), 1)
