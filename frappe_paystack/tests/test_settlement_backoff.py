"""Tests for the wait a stuck capture serves between two scheduled settlement attempts."""

from typing import Any, Optional
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime, today

from frappe_paystack import hooks
from frappe_paystack.frappe_paystack.doctype.paystack_payment_log.paystack_payment_log import (
    PAYMENT_SWEEP,
    SETTLEMENT_BACKOFF_CAP_MINUTES,
    SETTLEMENT_BACKOFF_MINUTES,
    backoff_minutes,
    clear_retries,
    complete_payment,
    complete_payment_from_log,
    due_logs,
    hold_completion,
    record_retry,
    release_completion,
    retry_due,
    retry_stuck_settlements,
    unsettled_logs,
)
from frappe_paystack.migration import seed_payment_log_retries, stuck_payment_logs
from frappe_paystack.tests.factories import GatewaySettingFactory, PaymentLogFactory, cleanup_doc
from frappe_paystack.tests.test_base import PaystackTestCase
from frappe_paystack.utils.sweep import clear_streak

PAYMENT_LOG = "Paystack Payment Log"

LOG_MODULE = "frappe_paystack.frappe_paystack.doctype.paystack_payment_log.paystack_payment_log"

CREATE_PAYMENT_ENTRY = f"{LOG_MODULE}.create_payment_entry_from_log"
DUE_LOGS = f"{LOG_MODULE}.due_logs"
UNSETTLED_LOGS = f"{LOG_MODULE}.unsettled_logs"
VERIFIED_TRANSACTION = f"{LOG_MODULE}.verified_transaction"

STUCK_PAYMENT_LOGS = "frappe_paystack.migration.stuck_payment_logs"

# The path the ten-minute cron entry registers.
RETRY_JOB = (
    "frappe_paystack.frappe_paystack.doctype.paystack_payment_log"
    ".paystack_payment_log.retry_stuck_settlements"
)

# The path the hourly sweep keeps.
PAYOUT_RETRY_JOB = "frappe_paystack.utils.settlement.retry_unposted_settlements"

TEN_MINUTES = "*/10 * * * *"


def paystack_capture(amount: int = 100000, currency: str = "NGN", status: str = "success") -> dict:
    """Return a Paystack transaction payload, in the minor units it sends."""
    return {"id": 900002, "status": status, "currency": currency, "amount": amount}


class BackoffTestCase(PaystackTestCase):
    """A gateway to settle through and captures stuck at Processed."""

    def setUp(self) -> None:
        """Enable Paystack for the test company."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

    def stuck_log(self, idle_minutes: int = 60) -> str:
        """Insert a log stuck at Processed with no Payment Entry, aged past the settling grace."""
        name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, name)
        self.addCleanup(clear_streak, PAYMENT_SWEEP, name)

        frappe.db.set_value(
            PAYMENT_LOG,
            name,
            {
                "status": "Processed",
                "amount_paid": 1000,
                "currency_paid": "NGN",
                "payment_reference": f"ref-{name}",
                "payment_date": today(),
            },
            update_modified=False,
        )
        frappe.db.set_value(
            PAYMENT_LOG,
            name,
            "modified",
            add_to_date(now_datetime(), minutes=-idle_minutes),
            update_modified=False,
        )
        frappe.clear_document_cache(PAYMENT_LOG, name)
        frappe.db.commit()
        return name

    def stamp(self, name: str, retry_count: int, minutes_ago: int) -> None:
        """Write how many attempts a log has served and when the last one ran."""
        frappe.db.set_value(
            PAYMENT_LOG,
            name,
            {
                "retry_count": retry_count,
                "last_retry": add_to_date(now_datetime(), minutes=-minutes_ago),
            },
            update_modified=False,
        )
        frappe.clear_document_cache(PAYMENT_LOG, name)
        frappe.db.commit()

    def row(self, name: str) -> Any:
        """Return the back-off columns of a log, as the selection reads them."""
        return frappe.db.get_value(PAYMENT_LOG, name, ["name", "retry_count", "last_retry"], as_dict=True)

    def field(self, name: str, fieldname: str) -> Any:
        """Return one field of a log."""
        return frappe.db.get_value(PAYMENT_LOG, name, fieldname)

    def sweep(self, name: str) -> None:
        """Run the ten-minute sweep over one named log."""
        with patch(DUE_LOGS, return_value=[name]):
            retry_stuck_settlements()

    def select(self, name: str) -> list:
        """Run the real selection over one log's row."""
        with patch(UNSETTLED_LOGS, return_value=[self.row(name)]):
            return due_logs()


class TestBackoffCurve(PaystackTestCase):
    """The wait a log owes after a given number of attempts."""

    def test_a_log_never_attempted_owes_nothing(self) -> None:
        """A retry count of zero owes no wait."""
        self.assertEqual(backoff_minutes(0), 0)

    def test_an_empty_count_owes_nothing(self) -> None:
        """A retry count of None owes no wait."""
        self.assertEqual(backoff_minutes(None), 0)

    def test_the_first_attempt_buys_ten_minutes(self) -> None:
        """One attempt owes ten minutes."""
        self.assertEqual(backoff_minutes(1), 10)

    def test_the_wait_doubles_with_each_attempt(self) -> None:
        """The wait doubles across the first eight attempts."""
        self.assertEqual(
            [backoff_minutes(count) for count in range(1, 9)],
            [10, 20, 40, 80, 160, 320, 640, 1280],
        )

    def test_the_wait_flattens_at_a_day(self) -> None:
        """The ninth attempt owes a day."""
        self.assertEqual(backoff_minutes(9), 1440)

    def test_a_log_that_never_settles_still_owes_only_a_day(self) -> None:
        """A count far past the cap owes a day."""
        self.assertEqual(backoff_minutes(500), 1440)

    def test_the_curve_starts_at_the_configured_step(self) -> None:
        """The first wait is SETTLEMENT_BACKOFF_MINUTES."""
        self.assertEqual(backoff_minutes(1), SETTLEMENT_BACKOFF_MINUTES)

    def test_the_curve_stops_at_the_configured_cap(self) -> None:
        """The flat wait is SETTLEMENT_BACKOFF_CAP_MINUTES."""
        self.assertEqual(backoff_minutes(500), SETTLEMENT_BACKOFF_CAP_MINUTES)


class TestRetryDue(BackoffTestCase):
    """Whether a log's wait has run out."""

    def test_a_log_never_attempted_is_due(self) -> None:
        """A log carrying no attempt is due."""
        self.assertTrue(retry_due(self.row(self.stuck_log())))

    def test_a_count_carrying_no_stamp_is_due(self) -> None:
        """A log counting four attempts but carrying no stamp is due."""
        name = self.stuck_log()
        frappe.db.set_value(
            PAYMENT_LOG,
            name,
            {"retry_count": 4, "last_retry": None},
            update_modified=False,
        )

        self.assertTrue(retry_due(self.row(name)))

    def test_a_log_inside_its_first_wait_is_not_due(self) -> None:
        """A log attempted five minutes ago, owing ten, is not due."""
        name = self.stuck_log()
        self.stamp(name, retry_count=1, minutes_ago=5)

        self.assertFalse(retry_due(self.row(name)))

    def test_a_log_past_its_first_wait_is_due(self) -> None:
        """A log attempted eleven minutes ago, owing ten, is due."""
        name = self.stuck_log()
        self.stamp(name, retry_count=1, minutes_ago=11)

        self.assertTrue(retry_due(self.row(name)))

    def test_a_longer_count_buys_a_longer_wait(self) -> None:
        """A log on its fourth attempt, owing eighty minutes, waits out thirty."""
        name = self.stuck_log()
        self.stamp(name, retry_count=4, minutes_ago=30)

        self.assertFalse(retry_due(self.row(name)))

    def test_a_log_past_the_longer_wait_is_due(self) -> None:
        """A log on its fourth attempt is due ninety minutes on."""
        name = self.stuck_log()
        self.stamp(name, retry_count=4, minutes_ago=90)

        self.assertTrue(retry_due(self.row(name)))

    def test_a_structurally_broken_log_waits_a_day(self) -> None:
        """A log on its twentieth attempt is not due twelve hours on."""
        name = self.stuck_log()
        self.stamp(name, retry_count=20, minutes_ago=12 * 60)

        self.assertFalse(retry_due(self.row(name)))

    def test_a_structurally_broken_log_comes_back_after_a_day(self) -> None:
        """A log on its twentieth attempt is due twenty-five hours on."""
        name = self.stuck_log()
        self.stamp(name, retry_count=20, minutes_ago=25 * 60)

        self.assertTrue(retry_due(self.row(name)))


class TestDueSelection(BackoffTestCase):
    """Which stuck logs a scheduled sweep picks up."""

    def test_a_stuck_log_is_offered(self) -> None:
        """A log past the settling grace is offered to the sweep."""
        self.assertIn(self.stuck_log(), [row.name for row in unsettled_logs()])

    def test_the_offer_carries_the_count(self) -> None:
        """The selection reads retry_count off the row."""
        name = self.stuck_log()
        self.stamp(name, retry_count=3, minutes_ago=5)

        row = next(row for row in unsettled_logs() if row.name == name)
        self.assertEqual(row.retry_count, 3)

    def test_the_offer_carries_the_stamp(self) -> None:
        """The selection reads last_retry off the row."""
        name = self.stuck_log()
        self.stamp(name, retry_count=3, minutes_ago=5)

        row = next(row for row in unsettled_logs() if row.name == name)
        self.assertEqual(get_datetime(row.last_retry), get_datetime(self.field(name, "last_retry")))

    def test_an_untried_log_sorts_ahead_of_a_tried_one(self) -> None:
        """A log carrying no attempt comes before one already attempted."""
        tried = self.stuck_log()
        self.stamp(tried, retry_count=1, minutes_ago=30)
        untried = self.stuck_log()

        names = [row.name for row in unsettled_logs()]
        self.assertLess(names.index(untried), names.index(tried))

    def test_a_log_never_attempted_is_swept(self) -> None:
        """A log carrying no attempt is handed to the sweep."""
        name = self.stuck_log()

        self.assertEqual(self.select(name), [name])

    def test_a_log_inside_its_wait_is_held_back(self) -> None:
        """A log still inside its back-off is not handed to the sweep."""
        name = self.stuck_log()
        self.stamp(name, retry_count=1, minutes_ago=5)

        self.assertEqual(self.select(name), [])

    def test_a_log_past_its_wait_is_swept(self) -> None:
        """A log past its back-off is handed to the sweep."""
        name = self.stuck_log()
        self.stamp(name, retry_count=1, minutes_ago=11)

        self.assertEqual(self.select(name), [name])


class TestRetryStamp(BackoffTestCase):
    """The attempt count and stamp the sweep writes on a log."""

    def test_the_first_attempt_counts_one(self) -> None:
        """A first attempt leaves retry_count at one."""
        name = self.stuck_log()

        record_retry(name)

        self.assertEqual(self.field(name, "retry_count"), 1)

    def test_attempts_accumulate(self) -> None:
        """A second attempt leaves retry_count at two."""
        name = self.stuck_log()

        record_retry(name)
        record_retry(name)

        self.assertEqual(self.field(name, "retry_count"), 2)

    def test_the_attempt_is_stamped_with_the_time(self) -> None:
        """An attempt stamps last_retry within a minute of now."""
        name = self.stuck_log()

        record_retry(name)

        gap = get_datetime(self.field(name, "last_retry")) - now_datetime()
        self.assertLess(abs(gap.total_seconds()), 60)

    def test_the_count_survives_a_rollback(self) -> None:
        """The stamped attempt is still on the log after a rollback."""
        name = self.stuck_log()

        record_retry(name)
        frappe.db.rollback()

        self.assertEqual(self.field(name, "retry_count"), 1)

    def test_the_stamp_survives_a_rollback(self) -> None:
        """The stamped time is still on the log after a rollback."""
        name = self.stuck_log()

        record_retry(name)
        stamped = self.field(name, "last_retry")
        frappe.db.rollback()

        self.assertEqual(self.field(name, "last_retry"), stamped)

    def test_the_stamp_leaves_the_timestamp_alone(self) -> None:
        """Counting an attempt does not move the log's modified time."""
        name = self.stuck_log()
        modified = self.field(name, "modified")

        record_retry(name)

        self.assertEqual(self.field(name, "modified"), modified)

    def test_clearing_puts_the_count_back_to_zero(self) -> None:
        """Clearing a log's retries leaves retry_count at zero."""
        name = self.stuck_log()
        record_retry(name)

        clear_retries(name)

        self.assertEqual(self.field(name, "retry_count"), 0)

    def test_clearing_empties_the_stamp(self) -> None:
        """Clearing a log's retries empties last_retry."""
        name = self.stuck_log()
        record_retry(name)

        clear_retries(name)

        self.assertIsNone(self.field(name, "last_retry"))


class TestScheduledAttempt(BackoffTestCase):
    """What one scheduled attempt does to a log."""

    def test_the_sweep_counts_its_attempt(self) -> None:
        """A swept log that stays stuck carries a retry_count of one."""
        name = self.stuck_log()

        with patch(CREATE_PAYMENT_ENTRY):
            self.sweep(name)

        self.assertEqual(self.field(name, "retry_count"), 1)

    def test_the_sweep_stamps_its_attempt(self) -> None:
        """A swept log that stays stuck carries a last_retry."""
        name = self.stuck_log()

        with patch(CREATE_PAYMENT_ENTRY):
            self.sweep(name)

        self.assertIsNotNone(self.field(name, "last_retry"))

    def test_the_attempt_is_counted_before_it_runs(self) -> None:
        """The settlement finds the attempt already counted against the log."""
        name = self.stuck_log()
        seen = []

        def note(payment_log_name: str) -> None:
            """Record the count the log carries while the settlement runs."""
            seen.append(frappe.db.get_value(PAYMENT_LOG, payment_log_name, "retry_count"))

        with patch(CREATE_PAYMENT_ENTRY, side_effect=note):
            self.sweep(name)

        self.assertEqual(seen, [1])

    def test_a_settlement_that_blows_up_still_owes_the_wait(self) -> None:
        """A settlement that raises leaves the attempt counted on the log."""
        name = self.stuck_log()

        with patch(CREATE_PAYMENT_ENTRY, side_effect=RuntimeError("ledger is away")):
            with self.assertRaises(RuntimeError):
                self.sweep(name)

        self.assertEqual(self.field(name, "retry_count"), 1)

    def test_a_booked_log_forgets_its_count(self) -> None:
        """A sweep that books the Payment Entry puts retry_count back to zero."""
        name = self.stuck_log()

        self.sweep(name)

        self.addCleanup(cleanup_doc, "Payment Entry", self.field(name, "payment_entry"))
        self.assertEqual(self.field(name, "retry_count"), 0)

    def test_a_booked_log_forgets_its_stamp(self) -> None:
        """A sweep that books the Payment Entry empties last_retry."""
        name = self.stuck_log()

        self.sweep(name)

        self.addCleanup(cleanup_doc, "Payment Entry", self.field(name, "payment_entry"))
        self.assertIsNone(self.field(name, "last_retry"))

    def test_a_log_inside_its_wait_is_not_attempted(self) -> None:
        """A log still inside its back-off runs the settlement zero times."""
        name = self.stuck_log()
        self.stamp(name, retry_count=1, minutes_ago=5)

        with patch(CREATE_PAYMENT_ENTRY) as settle:
            with patch(UNSETTLED_LOGS, return_value=[self.row(name)]):
                retry_stuck_settlements()

        self.assertEqual(settle.call_count, 0)

    def test_a_log_inside_its_wait_keeps_its_count(self) -> None:
        """A log held back by its back-off is not counted again."""
        name = self.stuck_log()
        self.stamp(name, retry_count=1, minutes_ago=5)

        with patch(CREATE_PAYMENT_ENTRY):
            with patch(UNSETTLED_LOGS, return_value=[self.row(name)]):
                retry_stuck_settlements()

        self.assertEqual(self.field(name, "retry_count"), 1)

    def test_a_log_past_its_wait_is_attempted(self) -> None:
        """A log past its back-off runs the settlement once."""
        name = self.stuck_log()
        self.stamp(name, retry_count=1, minutes_ago=11)

        with patch(CREATE_PAYMENT_ENTRY) as settle:
            with patch(UNSETTLED_LOGS, return_value=[self.row(name)]):
                retry_stuck_settlements()

        self.assertEqual(settle.call_count, 1)

    def test_a_log_past_its_wait_is_counted_again(self) -> None:
        """A log past its back-off moves on to its second attempt."""
        name = self.stuck_log()
        self.stamp(name, retry_count=1, minutes_ago=11)

        with patch(CREATE_PAYMENT_ENTRY):
            with patch(UNSETTLED_LOGS, return_value=[self.row(name)]):
                retry_stuck_settlements()

        self.assertEqual(self.field(name, "retry_count"), 2)

    def test_a_held_back_sweep_files_no_report(self) -> None:
        """A sweep with every log held back files no Integration Request."""
        name = self.stuck_log()
        self.stamp(name, retry_count=1, minutes_ago=5)

        with patch(UNSETTLED_LOGS, return_value=[self.row(name)]):
            retry_stuck_settlements()

        self.assertEqual(
            frappe.db.count(
                "Integration Request",
                {"url": f"sweep:{PAYMENT_SWEEP}", "creation": [">=", self.started_at]},
            ),
            0,
        )


class TestCompletionInFlight(BackoffTestCase):
    """The scheduled sweep leaves a capture a manual completion has claimed."""

    def test_a_claimed_log_is_not_offered(self) -> None:
        """A capture with a completion in flight is left out of the selection."""
        name = self.stuck_log()
        hold_completion(name)
        self.addCleanup(release_completion, name)

        self.assertEqual(self.select(name), [])

    def test_a_log_nothing_holds_is_offered(self) -> None:
        """A capture no completion holds is offered to the sweep."""
        name = self.stuck_log()

        self.assertEqual(self.select(name), [name])

    def test_a_released_log_is_offered_again(self) -> None:
        """A capture whose completion has finished is back in the selection."""
        name = self.stuck_log()
        hold_completion(name)
        release_completion(name)

        self.assertEqual(self.select(name), [name])


class TestManualCompletionBypass(BackoffTestCase):
    """Complete Payment ignores the wait the scheduled sweep serves."""

    def run_completion(self, name: str, transaction: Optional[dict]) -> Any:
        """Run the completion job against a fixed Paystack answer."""
        with (
            patch(VERIFIED_TRANSACTION, return_value=transaction),
            patch("frappe.publish_realtime") as publish,
        ):
            complete_payment_from_log(name, "Administrator")

        return publish

    def test_a_log_deep_in_its_wait_is_still_booked(self) -> None:
        """A log a minute into a day-long wait still books a Payment Entry."""
        name = self.stuck_log()
        self.stamp(name, retry_count=20, minutes_ago=1)

        self.run_completion(name, paystack_capture())

        self.addCleanup(cleanup_doc, "Payment Entry", self.field(name, "payment_entry"))
        self.assertEqual(self.field(name, "status"), "Completed")

    def test_a_manual_attempt_is_not_counted(self) -> None:
        """A completion by hand leaves retry_count where it stood."""
        name = self.stuck_log()
        self.stamp(name, retry_count=3, minutes_ago=1)

        with patch(CREATE_PAYMENT_ENTRY):
            self.run_completion(name, paystack_capture())

        self.assertEqual(self.field(name, "retry_count"), 3)

    def test_a_manual_attempt_leaves_the_stamp_alone(self) -> None:
        """A completion by hand leaves last_retry where it stood."""
        name = self.stuck_log()
        self.stamp(name, retry_count=3, minutes_ago=1)
        stamped = self.field(name, "last_retry")

        with patch(CREATE_PAYMENT_ENTRY):
            self.run_completion(name, paystack_capture())

        self.assertEqual(self.field(name, "last_retry"), stamped)

    def test_a_manual_attempt_still_asks_paystack(self) -> None:
        """A capture Paystack reports differently books nothing."""
        name = self.stuck_log()
        self.stamp(name, retry_count=20, minutes_ago=1)

        self.run_completion(name, paystack_capture(amount=90000))

        self.assertIsNone(self.field(name, "payment_entry"))

    def test_a_manual_attempt_records_what_paystack_said(self) -> None:
        """A refused completion writes the discrepancy to the log."""
        name = self.stuck_log()
        self.stamp(name, retry_count=20, minutes_ago=1)

        self.run_completion(name, paystack_capture(amount=90000))

        self.assertEqual(
            self.field(name, "errors"),
            "Paystack captured 900.0 but this log records 1000.0.",
        )

    def test_a_manual_attempt_still_respects_the_claim(self) -> None:
        """A second press while one runs raises ValidationError."""
        name = self.stuck_log()
        self.addCleanup(release_completion, name)
        self.stamp(name, retry_count=20, minutes_ago=1)

        with patch("frappe.enqueue"):
            complete_payment(name)

            with self.assertRaises(frappe.ValidationError):
                complete_payment(name)


class TestRetryRegistration(PaystackTestCase):
    """Where the settlement retry is registered with the scheduler."""

    def test_the_retry_runs_every_ten_minutes(self) -> None:
        """The ten-minute cron entry carries the settlement retry alone."""
        self.assertEqual(hooks.scheduler_events["cron"][TEN_MINUTES], [RETRY_JOB])

    def test_the_cron_entry_is_the_only_one(self) -> None:
        """The ten-minute entry is the only cron entry the app registers."""
        self.assertEqual(list(hooks.scheduler_events["cron"]), [TEN_MINUTES])

    def test_the_hourly_sweep_keeps_the_payout_retry_alone(self) -> None:
        """The hourly sweep carries the payout retry and nothing else."""
        self.assertEqual(hooks.scheduler_events["hourly_long"], [PAYOUT_RETRY_JOB])

    def test_the_cron_entry_names_a_function_that_exists(self) -> None:
        """The registered path resolves to retry_stuck_settlements."""
        self.assertIs(frappe.get_attr(RETRY_JOB), retry_stuck_settlements)


class TestRetrySeedPatch(BackoffTestCase):
    """The patch that puts logs already stuck onto the back-off."""

    def seed_row(self, name: str) -> Any:
        """Return the columns the seed reads off a log."""
        return frappe.db.get_value(PAYMENT_LOG, name, ["name", "modified"], as_dict=True)

    def test_a_stuck_log_is_offered_to_the_seed(self) -> None:
        """A log stuck at Processed with no stamp is offered to the seed."""
        name = self.stuck_log()

        self.assertIn(name, [log.name for log in stuck_payment_logs()])

    def test_a_stamped_log_is_left_out(self) -> None:
        """A log already carrying a stamp is left out of the seed."""
        name = self.stuck_log()
        self.stamp(name, retry_count=1, minutes_ago=5)

        self.assertNotIn(name, [log.name for log in stuck_payment_logs()])

    def test_a_booked_log_is_left_out(self) -> None:
        """A log that has booked its Payment Entry is left out of the seed."""
        name = self.stuck_log()
        frappe.db.set_value(PAYMENT_LOG, name, "payment_entry", "ACC-PAY-TEST-0001")

        self.assertNotIn(name, [log.name for log in stuck_payment_logs()])

    def test_the_seed_counts_one_attempt(self) -> None:
        """A seeded log carries a retry_count of one."""
        name = self.stuck_log()

        with patch(STUCK_PAYMENT_LOGS, return_value=[self.seed_row(name)]):
            seed_payment_log_retries()

        self.assertEqual(self.field(name, "retry_count"), 1)

    def test_the_seed_stamps_the_last_write(self) -> None:
        """A seeded log carries its own modified time as last_retry."""
        name = self.stuck_log()
        modified = self.field(name, "modified")

        with patch(STUCK_PAYMENT_LOGS, return_value=[self.seed_row(name)]):
            seed_payment_log_retries()

        self.assertEqual(self.field(name, "last_retry"), modified)

    def test_the_seed_names_what_it_stamped(self) -> None:
        """The seed returns the names it stamped."""
        name = self.stuck_log()

        with patch(STUCK_PAYMENT_LOGS, return_value=[self.seed_row(name)]):
            self.assertEqual(seed_payment_log_retries(), [name])

    def test_the_seed_leaves_the_timestamp_alone(self) -> None:
        """Seeding a log does not move its modified time."""
        name = self.stuck_log()
        modified = self.field(name, "modified")

        with patch(STUCK_PAYMENT_LOGS, return_value=[self.seed_row(name)]):
            seed_payment_log_retries()

        self.assertEqual(self.field(name, "modified"), modified)
