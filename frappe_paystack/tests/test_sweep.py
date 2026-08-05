"""Tests for the reporting traces left by the retry sweeps."""

from unittest.mock import patch

import frappe

from frappe_paystack.frappe_paystack.doctype.paystack_payment_log.paystack_payment_log import (
    PAYMENT_SWEEP,
    retry_stuck_settlements,
)
from frappe_paystack.tests.factories import PaymentLogFactory, SettlementFactory
from frappe_paystack.tests.test_base import PaystackTestCase
from frappe_paystack.utils.settlement import SETTLEMENT_SWEEP, retry_unposted_settlements
from frappe_paystack.utils.sweep import (
    SWEEP_ESCALATION,
    SWEEP_STREAK_TTL,
    clear_streak,
    record_streak,
    run_sweep,
    streak_key,
)

DUE_LOGS = "frappe_paystack.frappe_paystack.doctype.paystack_payment_log.paystack_payment_log.due_logs"

UNPOSTED_SETTLEMENTS = "frappe_paystack.utils.settlement.unposted_settlements"

SETTLEMENT = "Paystack Settlement"
INTEGRATION_REQUEST = "Integration Request"
ERROR_LOG = "Error Log"

JOURNAL_ENTRY = "journal_entry"


class SweepTestCase(PaystackTestCase):
    """One payout to sweep, under a sweep name unique to each test."""

    def setUp(self) -> None:
        """Raise an unposted payout and a sweep name unique to this test."""
        super().setUp()
        self.sweep = f"test-{frappe.generate_hash(length=10)}"
        self.payout = SettlementFactory.create()
        self.addCleanup(SettlementFactory.cleanup, self.payout)
        self.addCleanup(clear_streak, self.sweep, self.payout)
        self.addCleanup(self.remove_escalations)
        self.driven = []

    def resolve(self, name: str) -> None:
        """Stand in for a sweep step that books the payout."""
        self.driven.append(name)
        frappe.db.set_value(SETTLEMENT, name, JOURNAL_ENTRY, "JE-SWEEP-TEST")

    def leave_stuck(self, name: str) -> None:
        """Stand in for a sweep step that changes nothing."""
        self.driven.append(name)

    def sweep_payout(self, drive, names: list) -> None:
        """Run one sweep over the given rows."""
        run_sweep(self.sweep, SETTLEMENT, JOURNAL_ENTRY, names, drive)

    def requests(self) -> list:
        """Return the Integration Requests this sweep filed, oldest first."""
        return frappe.get_all(
            INTEGRATION_REQUEST,
            filters={"url": f"sweep:{self.sweep}"},
            fields=["name", "status", "error", "output"],
            order_by="creation asc",
        )

    def escalations(self) -> list:
        """Return the Error Logs this sweep raised."""
        return frappe.get_all(
            ERROR_LOG,
            filters={"method": ["like", f"Paystack {self.sweep} sweep:%"]},
            fields=["name", "method", "error", "reference_doctype", "reference_name"],
        )

    def remove_escalations(self) -> None:
        """Delete this sweep's Error Logs."""
        for row in self.escalations():
            frappe.delete_doc(ERROR_LOG, row.name, force=True, ignore_permissions=True)
        frappe.db.commit()


class TestIdleSweep(SweepTestCase):
    """A sweep with no rows to drive."""

    def test_an_empty_sweep_drives_nothing(self) -> None:
        """An empty batch drives nothing."""
        self.sweep_payout(self.leave_stuck, names=[])

        self.assertEqual(self.driven, [])

    def test_an_empty_sweep_files_no_request(self) -> None:
        """An empty sweep files no Integration Request."""
        self.sweep_payout(self.leave_stuck, names=[])

        self.assertEqual(self.requests(), [])


class TestSweepReport(SweepTestCase):
    """Every sweep that had work files one row describing how it went."""

    def test_a_resolved_row_is_reported_completed(self) -> None:
        """A sweep that leaves nothing stuck is reported Completed."""
        self.sweep_payout(self.resolve, names=[self.payout])

        self.assertEqual([row.status for row in self.requests()], ["Completed"])

    def test_a_resolved_row_is_named_as_resolved(self) -> None:
        """The output names the row the sweep cleared."""
        self.sweep_payout(self.resolve, names=[self.payout])

        self.assertEqual(
            frappe.parse_json(self.requests()[0].output),
            {"resolved": [self.payout], "unresolved": []},
        )

    def test_a_resolved_sweep_records_no_failure(self) -> None:
        """A clean sweep records the error column as the JSON literal null."""
        self.sweep_payout(self.resolve, names=[self.payout])

        self.assertEqual(self.requests()[0].error, "null")

    def test_a_stuck_row_is_reported_failed(self) -> None:
        """A sweep that leaves the payout unposted is reported Failed."""
        self.sweep_payout(self.leave_stuck, names=[self.payout])

        self.assertEqual([row.status for row in self.requests()], ["Failed"])

    def test_the_failure_counts_and_names_what_is_left(self) -> None:
        """The failure message counts and names the rows left unresolved."""
        self.sweep_payout(self.leave_stuck, names=[self.payout])

        self.assertEqual(self.requests()[0].error, f"1 of 1 still unresolved: {self.payout}")

    def test_a_stuck_row_is_named_as_unresolved(self) -> None:
        """The output lists the stuck row under unresolved."""
        self.sweep_payout(self.leave_stuck, names=[self.payout])

        self.assertEqual(
            frappe.parse_json(self.requests()[0].output),
            {"resolved": [], "unresolved": [self.payout]},
        )

    def test_a_batch_files_one_request_not_one_per_row(self) -> None:
        """A batch of several rows files one Integration Request."""
        second = SettlementFactory.create()
        self.addCleanup(SettlementFactory.cleanup, second)
        self.addCleanup(clear_streak, self.sweep, second)

        self.sweep_payout(self.leave_stuck, names=[self.payout, second])

        self.assertEqual(len(self.requests()), 1)

    def test_a_mixed_batch_reports_both_sides(self) -> None:
        """A mixed batch reports the resolved and the unresolved row."""
        second = SettlementFactory.create()
        self.addCleanup(SettlementFactory.cleanup, second)
        self.addCleanup(clear_streak, self.sweep, second)

        def drive(name: str) -> None:
            """Book the first payout and leave the second where it is."""
            if name == self.payout:
                self.resolve(name)

        self.sweep_payout(drive, names=[self.payout, second])

        self.assertEqual(
            frappe.parse_json(self.requests()[0].output),
            {"resolved": [self.payout], "unresolved": [second]},
        )

    def test_every_row_is_driven(self) -> None:
        """Every row in the batch is driven, in order."""
        second = SettlementFactory.create()
        self.addCleanup(SettlementFactory.cleanup, second)
        self.addCleanup(clear_streak, self.sweep, second)

        self.sweep_payout(self.leave_stuck, names=[self.payout, second])

        self.assertEqual(self.driven, [self.payout, second])


class TestSweepStreak(SweepTestCase):
    """The count that decides when a stuck row becomes an alert."""

    def test_the_first_failure_starts_the_count(self) -> None:
        """A row's first stuck sweep records a streak of one."""
        self.assertEqual(record_streak(self.sweep, self.payout), 1)

    def test_consecutive_failures_accumulate(self) -> None:
        """Consecutive stuck sweeps increment the streak."""
        record_streak(self.sweep, self.payout)

        self.assertEqual(record_streak(self.sweep, self.payout), 2)

    def test_the_count_is_given_a_lifetime(self) -> None:
        """The streak key carries a TTL within SWEEP_STREAK_TTL."""
        record_streak(self.sweep, self.payout)

        ttl = frappe.cache.ttl(streak_key(self.sweep, self.payout))
        self.assertTrue(0 < ttl <= SWEEP_STREAK_TTL, f"ttl was {ttl}")

    def test_a_later_failure_does_not_restart_the_lifetime(self) -> None:
        """A later failure leaves the key's remaining TTL where it is."""
        record_streak(self.sweep, self.payout)
        frappe.cache.expire(streak_key(self.sweep, self.payout), 60)
        record_streak(self.sweep, self.payout)

        self.assertLessEqual(frappe.cache.ttl(streak_key(self.sweep, self.payout)), 60)

    def test_a_resolved_row_forgets_its_count(self) -> None:
        """A row that clears starts again at one the next time it sticks."""
        for _attempt in range(SWEEP_ESCALATION - 1):
            self.sweep_payout(self.leave_stuck, names=[self.payout])

        self.sweep_payout(self.resolve, names=[self.payout])

        self.assertEqual(record_streak(self.sweep, self.payout), 1)


class TestSweepEscalation(SweepTestCase):
    """One Error Log per row that never resolves."""

    def stick(self, sweeps: int) -> None:
        """Run the sweep the given number of times, resolving nothing."""
        for _attempt in range(sweeps):
            self.sweep_payout(self.leave_stuck, names=[self.payout])

    def test_a_row_below_the_threshold_is_not_escalated(self) -> None:
        """A streak short of SWEEP_ESCALATION raises no Error Log."""
        self.stick(SWEEP_ESCALATION - 1)

        self.assertEqual(self.escalations(), [])

    def test_a_row_that_reaches_the_threshold_is_escalated(self) -> None:
        """A streak reaching SWEEP_ESCALATION raises one Error Log."""
        self.stick(SWEEP_ESCALATION)

        self.assertEqual(len(self.escalations()), 1)

    def test_a_row_that_stays_stuck_is_not_escalated_again(self) -> None:
        """Sweeps past the threshold hold the Error Log count at one."""
        self.stick(SWEEP_ESCALATION + 4)

        self.assertEqual(len(self.escalations()), 1)

    def test_the_alert_names_the_row_and_the_count(self) -> None:
        """The alert title names the row and the attempt count."""
        self.stick(SWEEP_ESCALATION)

        self.assertEqual(
            self.escalations()[0].method,
            f"Paystack {self.sweep} sweep: {self.payout} still unresolved "
            f"after {SWEEP_ESCALATION} attempts",
        )

    def test_the_alert_is_filed_against_the_row(self) -> None:
        """The alert's reference pair points at the payout."""
        self.stick(SWEEP_ESCALATION)

        alert = self.escalations()[0]
        self.assertEqual((alert.reference_doctype, alert.reference_name), (SETTLEMENT, self.payout))

    def test_the_alert_quotes_the_error_the_row_recorded(self) -> None:
        """The alert body quotes the errors field the row recorded."""
        frappe.db.set_value(SETTLEMENT, self.payout, "errors", "Settlement Bank Account is not set")

        self.stick(SWEEP_ESCALATION)

        self.assertEqual(self.escalations()[0].error, "Settlement Bank Account is not set")

    def test_a_row_with_no_error_of_its_own_still_says_so(self) -> None:
        """A row with an empty errors field gets a body saying so."""
        self.stick(SWEEP_ESCALATION)

        self.assertEqual(
            self.escalations()[0].error,
            f"{SETTLEMENT} {self.payout} reports no error of its own, " "so the cause is elsewhere.",
        )


class TestWiredSweeps(SweepTestCase):
    """The two scheduled jobs report through run_sweep."""

    def sweep_requests(self, sweep: str) -> list:
        """Return the Integration Requests a named sweep filed in this test."""
        return frappe.get_all(
            INTEGRATION_REQUEST,
            filters={
                "url": f"sweep:{sweep}",
                "creation": [">=", self.started_at],
            },
            fields=["name", "status", "data", "output"],
        )

    def attempted(self, request: dict) -> list:
        """Return the rows a sweep's Integration Request says it drove."""
        return frappe.parse_json(request.data)["attempted"]

    def failures(self, title: str) -> list:
        """Return the Error Logs filed under a title in this test."""
        return frappe.get_all(
            ERROR_LOG,
            filters={"method": title, "creation": [">=", self.started_at]},
            pluck="name",
        )

    def test_the_settlement_sweep_files_one_report(self) -> None:
        """A sweep that had work files exactly one Integration Request."""
        self.addCleanup(clear_streak, SETTLEMENT_SWEEP, self.payout)

        retry_unposted_settlements()

        self.assertEqual(len(self.sweep_requests(SETTLEMENT_SWEEP)), 1)

    def test_the_settlement_sweep_names_the_payout_it_drove(self) -> None:
        """The settlement sweep's report names the payout it drove."""
        self.addCleanup(clear_streak, SETTLEMENT_SWEEP, self.payout)

        retry_unposted_settlements()

        self.assertIn(self.payout, self.attempted(self.sweep_requests(SETTLEMENT_SWEEP)[0]))

    def test_the_payment_sweep_files_one_report(self) -> None:
        """The payment sweep files exactly one Integration Request."""
        log = PaymentLogFactory.create()
        self.addCleanup(PaymentLogFactory.cleanup, log)
        self.addCleanup(clear_streak, PAYMENT_SWEEP, log)

        with patch(DUE_LOGS, return_value=[log]):
            retry_stuck_settlements()

        self.assertEqual(len(self.sweep_requests(PAYMENT_SWEEP)), 1)

    def test_the_payment_sweep_names_the_log_it_drove(self) -> None:
        """The payment sweep's report names the log it drove."""
        log = PaymentLogFactory.create()
        self.addCleanup(PaymentLogFactory.cleanup, log)
        self.addCleanup(clear_streak, PAYMENT_SWEEP, log)

        with patch(DUE_LOGS, return_value=[log]):
            retry_stuck_settlements()

        self.assertIn(log, self.attempted(self.sweep_requests(PAYMENT_SWEEP)[0]))

    def test_a_lookup_failure_still_reports_nothing(self) -> None:
        """A failing row lookup files no Integration Request."""
        with patch(DUE_LOGS, side_effect=RuntimeError("db down")):
            retry_stuck_settlements()

        self.assertEqual(self.sweep_requests(PAYMENT_SWEEP), [])

    def test_a_payment_lookup_failure_names_the_payment_sweep(self) -> None:
        """The payment sweep's lookup failure is filed under its own name."""
        with patch(DUE_LOGS, side_effect=RuntimeError("db down")):
            retry_stuck_settlements()

        self.assertTrue(self.failures(f"Paystack {PAYMENT_SWEEP} sweep: lookup failed"))

    def test_a_settlement_lookup_failure_names_the_settlement_sweep(self) -> None:
        """The settlement sweep's lookup failure is filed under its own name."""
        with patch(UNPOSTED_SETTLEMENTS, side_effect=RuntimeError("db down")):
            retry_unposted_settlements()

        self.assertTrue(self.failures(f"Paystack {SETTLEMENT_SWEEP} sweep: lookup failed"))

    def test_the_two_sweeps_file_a_lookup_failure_apart(self) -> None:
        """Neither sweep's lookup failure can be read as the other's."""
        with patch(DUE_LOGS, side_effect=RuntimeError("db down")):
            retry_stuck_settlements()

        self.assertEqual(self.failures(f"Paystack {SETTLEMENT_SWEEP} sweep: lookup failed"), [])

    def test_the_settlement_sweep_grades_on_the_journal_entry(self) -> None:
        """The settlement sweep grades a payout on its journal_entry."""
        self.addCleanup(clear_streak, SETTLEMENT_SWEEP, self.payout)

        retry_unposted_settlements()

        report = frappe.parse_json(self.sweep_requests(SETTLEMENT_SWEEP)[0].output)
        self.assertIn(self.payout, report["unresolved"])

    def test_the_payment_sweep_grades_on_the_payment_entry(self) -> None:
        """The payment sweep grades a log on its Payment Entry."""
        log = PaymentLogFactory.create()
        self.addCleanup(PaymentLogFactory.cleanup, log)
        self.addCleanup(clear_streak, PAYMENT_SWEEP, log)

        with patch(DUE_LOGS, return_value=[log]):
            retry_stuck_settlements()

        report = frappe.parse_json(self.sweep_requests(PAYMENT_SWEEP)[0].output)
        self.assertEqual(report["unresolved"], [log])
