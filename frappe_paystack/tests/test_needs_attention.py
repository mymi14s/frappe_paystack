"""The Needs Attention terminal state a capture reaches when nothing can book it."""

import json
from typing import Any, Optional
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, flt, now_datetime, today

from frappe_paystack.api import process_charge_webhook_event
from frappe_paystack.frappe_paystack.doctype.paystack_payment_log.paystack_payment_log import (
    NEEDS_ATTENTION,
    PAYMENT_SWEEP,
    SETTLEMENT_MAX_ATTEMPTS,
    SETTLEMENT_RETRY_LOOKBACK_DAYS,
    VALID_STATUSES,
    abandon_expired_settlements,
    abandon_settlement,
    attempts_spent,
    backoff_minutes,
    complete_payment_from_log,
    create_payment_entry_from_log,
    drive_settlement_retry,
    expired_logs,
    is_completable,
    release_completion,
    retry_stuck_settlements,
    unsettled_logs,
)
from frappe_paystack.frappe_paystack.report.customer_paystack_volume import (
    customer_paystack_volume as volume_report,
)
from frappe_paystack.frappe_paystack.report.paystack_activity import paystack_activity as activity_report
from frappe_paystack.frappe_paystack.report.paystack_unsettled_payments import (
    paystack_unsettled_payments as unsettled_report,
)
from frappe_paystack.setup import DASHBOARD_CHARTS, NUMBER_CARDS
from frappe_paystack.tests.factories import (
    TEST_COMPANY,
    GatewaySettingFactory,
    PaymentLogFactory,
    POSInvoiceFactory,
    cleanup_doc,
)
from frappe_paystack.tests.test_base import PaystackTestCase
from frappe_paystack.utils.portal import PAYMENT_STATE_PROCESSING, download_payment_receipt, payment_state
from frappe_paystack.utils.reconciliation import RECONCILABLE_STATUSES, ReconciliationEngine
from frappe_paystack.utils.subscription import has_open_collection
from frappe_paystack.utils.sweep import clear_streak

PAYMENT_LOG = "Paystack Payment Log"
POS_INVOICE = "POS Invoice"
SALES_INVOICE = "Sales Invoice"
ERROR_LOG = "Error Log"
INTEGRATION_REQUEST = "Integration Request"

LOG_MODULE = "frappe_paystack.frappe_paystack.doctype.paystack_payment_log.paystack_payment_log"

CREATE_PAYMENT_ENTRY = f"{LOG_MODULE}.create_payment_entry_from_log"
DUE_LOGS = f"{LOG_MODULE}.due_logs"
EXPIRED_LOGS = f"{LOG_MODULE}.expired_logs"
VERIFIED_TRANSACTION = f"{LOG_MODULE}.verified_transaction"

OWNS_PAYMENT_LOG = "frappe_paystack.utils.portal.owns_payment_log"

# The card that counts the captures the app gave up on.
NEEDS_ATTENTION_CARD = "Paystack Needs Attention"

# The card that counts the captures still being retried.
AWAITING_SETTLEMENT_CARD = "Paystack Awaiting Settlement"

# The chart that sums what Paystack captured.
CAPTURED_CHART = "Paystack Payments Captured"

# The title every abandonment alert carries.
ALERT_TITLE = "Paystack settlement abandoned for log {0}"

# The cause a test hands the move.
TEST_CAUSE = "twelve attempts booked nothing."


def paystack_capture(amount: int = 100000, currency: str = "NGN", status: str = "success") -> dict:
    """Return a Paystack transaction payload, in the minor units it sends."""
    return {"id": 900003, "status": status, "currency": currency, "amount": amount}


class AbandonmentTestCase(PaystackTestCase):
    """Captures stuck at Processed, and what giving up on one writes."""

    def setUp(self) -> None:
        """Enable Paystack for the test company."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

    def stuck_log(self, days_old: int = 0, **fields: Any) -> str:
        """Insert a log at Processed with no Payment Entry, and return its name.

        days_old backdates creation, the column the lookback window reads.
        """
        name = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, name)
        self.addCleanup(clear_streak, PAYMENT_SWEEP, name)

        values = {
            "status": "Processed",
            "amount_paid": 1000,
            "currency_paid": "NGN",
            "payment_reference": name,
            "payment_date": today(),
        }
        values.update(fields)
        if days_old:
            values["creation"] = add_to_date(now_datetime(), days=-days_old)

        frappe.db.set_value(PAYMENT_LOG, name, values, update_modified=False)
        frappe.clear_document_cache(PAYMENT_LOG, name)
        frappe.db.commit()
        return name

    def abandoned_log(self, **fields: Any) -> str:
        """Insert a log already moved to Needs Attention."""
        return self.stuck_log(status=NEEDS_ATTENTION, **fields)

    def pos_log(self, **fields: Any) -> str:
        """Insert a POS tender at Processed with no Payment Entry."""
        invoice = POSInvoiceFactory.create()
        self.addCleanup(POSInvoiceFactory.cleanup, invoice)

        name = PaymentLogFactory.create(linked_doctype=POS_INVOICE, linked_docname=invoice, status="Pending")
        self.addCleanup(PaymentLogFactory.cleanup, name)

        frappe.db.set_value(PAYMENT_LOG, name, {"status": "Processed", **fields}, update_modified=False)
        frappe.clear_document_cache(PAYMENT_LOG, name)
        frappe.db.commit()
        return name

    def stamp(self, name: str, retry_count: int) -> None:
        """Write how many attempts a log has served."""
        frappe.db.set_value(PAYMENT_LOG, name, "retry_count", retry_count, update_modified=False)
        frappe.db.commit()

    def field(self, name: str, fieldname: str) -> Any:
        """Return one field of a log."""
        return frappe.db.get_value(PAYMENT_LOG, name, fieldname)

    def log_doc(self, name: str) -> Any:
        """Return a Payment Log read fresh off the row."""
        frappe.clear_document_cache(PAYMENT_LOG, name)
        return frappe.get_doc(PAYMENT_LOG, name)

    def alerts_for(self, name: str) -> list:
        """Return the abandonment alerts filed against a log."""
        return frappe.get_all(
            ERROR_LOG,
            filters={
                "reference_doctype": PAYMENT_LOG,
                "reference_name": name,
                "method": ["like", "Paystack settlement abandoned%"],
            },
            fields=["name", "method", "error"],
        )


class TestStatusVocabulary(PaystackTestCase):
    """Needs Attention is a status of its own, and the doctype offers it."""

    def options(self) -> list:
        """Return the Select options the status field carries."""
        return frappe.get_meta(PAYMENT_LOG).get_field("status").options.split("\n")

    def test_the_status_is_valid(self) -> None:
        """Needs Attention is in VALID_STATUSES."""
        self.assertIn(NEEDS_ATTENTION, VALID_STATUSES)

    def test_the_field_offers_exactly_the_valid_statuses(self) -> None:
        """The Select options are exactly VALID_STATUSES, in order."""
        self.assertEqual(self.options(), list(VALID_STATUSES))

    def test_failed_is_a_separate_status(self) -> None:
        """Failed remains its own option beside Needs Attention."""
        self.assertEqual(
            [status for status in self.options() if status in (NEEDS_ATTENTION, "Failed")],
            [NEEDS_ATTENTION, "Failed"],
        )


class TestSavingTheStatus(AbandonmentTestCase):
    """What the document does with a log carrying the new status."""

    def test_a_log_at_needs_attention_validates(self) -> None:
        """validate() accepts a log at Needs Attention."""
        log = self.log_doc(self.abandoned_log())

        self.assertIsNone(log.validate())

    def test_an_unknown_status_is_still_refused(self) -> None:
        """validate() still throws for a status outside the list."""
        log = self.log_doc(self.stuck_log())
        log.status = "Needs Attentions"

        with self.assertRaises(frappe.ValidationError):
            log.validate()

    def test_the_log_cannot_be_deleted(self) -> None:
        """on_trash refuses a log at Needs Attention."""
        log = self.log_doc(self.abandoned_log())

        with self.assertRaises(frappe.ValidationError):
            log.on_trash()

    def test_the_refusal_names_the_status(self) -> None:
        """The deletion refusal quotes the status it refused."""
        log = self.log_doc(self.abandoned_log())

        with self.assertRaises(frappe.ValidationError) as refused:
            log.on_trash()

        self.assertIn(
            f"Cannot delete a Payment Log at {NEEDS_ATTENTION}.",
            str(refused.exception),
        )

    def test_a_processed_log_is_still_undeletable(self) -> None:
        """on_trash still refuses a log at Processed."""
        log = self.log_doc(self.stuck_log())

        with self.assertRaises(frappe.ValidationError):
            log.on_trash()

    def test_a_pending_log_is_still_deletable(self) -> None:
        """on_trash passes for a Pending log carrying no Payment Entry."""
        name = PaymentLogFactory.create(status="Pending")
        self.addCleanup(PaymentLogFactory.cleanup, name)

        self.assertIsNone(self.log_doc(name).on_trash())

    def test_the_capture_is_no_longer_payable(self) -> None:
        """is_payable is false for a log at Needs Attention."""
        log = self.log_doc(self.abandoned_log())
        order = frappe.get_doc(log.linked_doctype, log.linked_docname)

        self.assertFalse(log.is_payable(order))

    def test_the_checkout_payload_refuses_payment(self) -> None:
        """The checkout payload reports the log as unpayable."""
        log = self.log_doc(self.abandoned_log())

        self.assertIs(log.get_data()["is_payable"], False)

    def test_the_checkout_payload_carries_the_status(self) -> None:
        """The checkout payload reports the status verbatim."""
        log = self.log_doc(self.abandoned_log())

        self.assertEqual(log.get_data()["status"], NEEDS_ATTENTION)


class TestExpiredSelection(AbandonmentTestCase):
    """Which captures the retry window has closed on."""

    def test_a_capture_past_the_window_is_selected(self) -> None:
        """A stuck capture older than the lookback is selected."""
        name = self.stuck_log(days_old=SETTLEMENT_RETRY_LOOKBACK_DAYS + 1)

        self.assertIn(name, expired_logs())

    def test_a_capture_inside_the_window_is_left_alone(self) -> None:
        """A stuck capture younger than the lookback is not selected."""
        name = self.stuck_log(days_old=SETTLEMENT_RETRY_LOOKBACK_DAYS - 2)

        self.assertNotIn(name, expired_logs())

    def test_the_sweep_still_offers_what_this_leaves(self) -> None:
        """A capture inside the window is the sweep's, not this pass's."""
        name = self.stuck_log(days_old=SETTLEMENT_RETRY_LOOKBACK_DAYS - 2)
        frappe.db.set_value(
            PAYMENT_LOG,
            name,
            "modified",
            add_to_date(now_datetime(), minutes=-60),
            update_modified=False,
        )

        self.assertIn(name, [row.name for row in unsettled_logs()])
        self.assertNotIn(name, expired_logs())

    def test_a_booked_capture_is_left_alone(self) -> None:
        """A capture carrying a Payment Entry is not selected."""
        name = self.stuck_log(
            days_old=SETTLEMENT_RETRY_LOOKBACK_DAYS + 1,
            payment_entry="ACC-PAY-ABANDON-0001",
        )

        self.assertNotIn(name, expired_logs())

    def test_a_capture_that_already_moved_is_left_alone(self) -> None:
        """A capture already at Needs Attention is not selected again."""
        name = self.abandoned_log(days_old=SETTLEMENT_RETRY_LOOKBACK_DAYS + 1)

        self.assertNotIn(name, expired_logs())

    def test_a_cancelled_capture_is_left_alone(self) -> None:
        """A cancelled log is not selected."""
        name = self.stuck_log(days_old=SETTLEMENT_RETRY_LOOKBACK_DAYS + 1, docstatus=2)

        self.assertNotIn(name, expired_logs())

    def test_a_pos_tender_is_left_alone(self) -> None:
        """A POS tender past the window is not selected."""
        name = self.pos_log(creation=add_to_date(now_datetime(), days=-(SETTLEMENT_RETRY_LOOKBACK_DAYS + 1)))

        self.assertNotIn(name, expired_logs())

    def test_the_pass_takes_one_batch_at_a_time(self) -> None:
        """The selection stops at the batch size the sweep's queue allows."""
        self.stuck_log(days_old=SETTLEMENT_RETRY_LOOKBACK_DAYS + 2)
        self.stuck_log(days_old=SETTLEMENT_RETRY_LOOKBACK_DAYS + 1)

        with patch(f"{LOG_MODULE}.SETTLEMENT_RETRY_LIMIT", 1):
            self.assertEqual(len(expired_logs()), 1)

    def test_the_oldest_capture_leads_the_batch(self) -> None:
        """The batch takes the captures that have waited longest."""
        oldest = self.stuck_log(days_old=SETTLEMENT_RETRY_LOOKBACK_DAYS + 2)
        self.stuck_log(days_old=SETTLEMENT_RETRY_LOOKBACK_DAYS + 1)

        with patch(f"{LOG_MODULE}.SETTLEMENT_RETRY_LIMIT", 1):
            self.assertEqual(expired_logs(), [oldest])


class TestAttemptsSpent(AbandonmentTestCase):
    """Whether a stuck capture has used the attempts it is allowed."""

    def test_a_capture_short_of_the_cap_has_attempts_left(self) -> None:
        """One attempt short of the cap is not spent."""
        name = self.stuck_log()
        self.stamp(name, SETTLEMENT_MAX_ATTEMPTS - 1)

        self.assertFalse(attempts_spent(name))

    def test_a_capture_at_the_cap_is_spent(self) -> None:
        """The cap itself is spent."""
        name = self.stuck_log()
        self.stamp(name, SETTLEMENT_MAX_ATTEMPTS)

        self.assertTrue(attempts_spent(name))

    def test_a_capture_past_the_cap_is_spent(self) -> None:
        """A count past the cap is spent."""
        name = self.stuck_log()
        self.stamp(name, SETTLEMENT_MAX_ATTEMPTS + 5)

        self.assertTrue(attempts_spent(name))

    def test_a_capture_never_attempted_is_not_spent(self) -> None:
        """A log carrying no attempt is not spent."""
        self.assertFalse(attempts_spent(self.stuck_log()))

    def test_a_booked_capture_is_not_spent(self) -> None:
        """A log carrying a Payment Entry is never spent."""
        name = self.stuck_log(payment_entry="ACC-PAY-ABANDON-0002")
        self.stamp(name, SETTLEMENT_MAX_ATTEMPTS)

        self.assertFalse(attempts_spent(name))

    def test_a_capture_that_already_moved_is_not_spent(self) -> None:
        """A log already at Needs Attention is not spent again."""
        name = self.abandoned_log()
        self.stamp(name, SETTLEMENT_MAX_ATTEMPTS)

        self.assertFalse(attempts_spent(name))

    def test_a_pos_tender_is_never_spent(self) -> None:
        """A POS tender at the cap is not spent."""
        name = self.pos_log(retry_count=SETTLEMENT_MAX_ATTEMPTS)

        self.assertFalse(attempts_spent(name))

    def test_the_attempts_run_out_inside_the_window(self) -> None:
        """The waits the cap buys add up to less than the lookback window."""
        waits = sum(backoff_minutes(count) for count in range(1, SETTLEMENT_MAX_ATTEMPTS))

        self.assertLess(waits, SETTLEMENT_RETRY_LOOKBACK_DAYS * 24 * 60)


class TestAbandonSettlement(AbandonmentTestCase):
    """What moving one capture to Needs Attention writes."""

    def test_the_status_is_written(self) -> None:
        """The log is left at Needs Attention."""
        name = self.stuck_log()

        abandon_settlement(name, TEST_CAUSE)

        self.assertEqual(self.field(name, "status"), NEEDS_ATTENTION)

    def test_the_status_survives_a_rollback(self) -> None:
        """A rollback leaves the status in place."""
        name = self.stuck_log()

        abandon_settlement(name, TEST_CAUSE)
        frappe.db.rollback()

        self.assertEqual(self.field(name, "status"), NEEDS_ATTENTION)

    def test_the_timestamp_is_left_alone(self) -> None:
        """Moving a log does not move its modified time."""
        name = self.stuck_log()
        modified = self.field(name, "modified")

        abandon_settlement(name, TEST_CAUSE)

        self.assertEqual(self.field(name, "modified"), modified)

    def test_one_alert_is_raised(self) -> None:
        """Exactly one Error Log is filed for the log."""
        name = self.stuck_log()

        abandon_settlement(name, TEST_CAUSE)

        self.assertEqual(len(self.alerts_for(name)), 1)

    def test_the_alert_names_the_log(self) -> None:
        """The alert title names the log it gave up on."""
        name = self.stuck_log()

        abandon_settlement(name, TEST_CAUSE)

        self.assertEqual(self.alerts_for(name)[0].method, ALERT_TITLE.format(name))

    def test_the_alert_states_the_cause(self) -> None:
        """The alert body opens with the cause it was given."""
        name = self.stuck_log()

        abandon_settlement(name, TEST_CAUSE)

        self.assertEqual(
            self.alerts_for(name)[0].error,
            f"{TEST_CAUSE} Last recorded error: none recorded.",
        )

    def test_the_alert_quotes_the_last_error(self) -> None:
        """The alert body quotes the error the log last recorded."""
        name = self.stuck_log(errors="Suspense account is missing.")

        abandon_settlement(name, TEST_CAUSE)

        self.assertEqual(
            self.alerts_for(name)[0].error,
            f"{TEST_CAUSE} Last recorded error: Suspense account is missing.",
        )

    def test_the_alert_is_filed_against_the_log(self) -> None:
        """The alert carries the payment log as its reference."""
        name = self.stuck_log()

        abandon_settlement(name, TEST_CAUSE)

        self.assertEqual(
            frappe.db.get_value(
                ERROR_LOG,
                self.alerts_for(name)[0].name,
                ["reference_doctype", "reference_name"],
            ),
            (PAYMENT_LOG, name),
        )


class TestAbandonExpiredSettlements(AbandonmentTestCase):
    """The pass that gives up on everything the window has closed on."""

    def test_an_expired_capture_is_moved(self) -> None:
        """A capture past the window is left at Needs Attention."""
        name = self.stuck_log(days_old=SETTLEMENT_RETRY_LOOKBACK_DAYS + 1)

        abandon_expired_settlements()

        self.assertEqual(self.field(name, "status"), NEEDS_ATTENTION)

    def test_the_pass_names_what_it_moved(self) -> None:
        """The pass returns the names it moved."""
        name = self.stuck_log(days_old=SETTLEMENT_RETRY_LOOKBACK_DAYS + 1)

        self.assertIn(name, abandon_expired_settlements())

    def test_a_capture_inside_the_window_is_left_alone(self) -> None:
        """A capture inside the window stays at Processed."""
        name = self.stuck_log(days_old=SETTLEMENT_RETRY_LOOKBACK_DAYS - 2)

        abandon_expired_settlements()

        self.assertEqual(self.field(name, "status"), "Processed")

    def test_the_alert_states_the_window(self) -> None:
        """The alert states the window the capture outlived."""
        name = self.stuck_log(days_old=SETTLEMENT_RETRY_LOOKBACK_DAYS + 1)

        abandon_expired_settlements()

        self.assertEqual(
            self.alerts_for(name)[0].error,
            f"No Payment Entry was booked within {SETTLEMENT_RETRY_LOOKBACK_DAYS} "
            "days of this capture. Last recorded error: none recorded.",
        )

    def test_a_second_pass_raises_no_second_alert(self) -> None:
        """Running the pass twice leaves one alert on the log."""
        name = self.stuck_log(days_old=SETTLEMENT_RETRY_LOOKBACK_DAYS + 1)

        abandon_expired_settlements()
        abandon_expired_settlements()

        self.assertEqual(len(self.alerts_for(name)), 1)

    def test_a_second_pass_moves_nothing(self) -> None:
        """The second pass finds the log already moved."""
        name = self.stuck_log(days_old=SETTLEMENT_RETRY_LOOKBACK_DAYS + 1)
        abandon_expired_settlements()

        self.assertNotIn(name, abandon_expired_settlements())

    def test_an_empty_pass_returns_nothing(self) -> None:
        """A pass with nothing expired returns an empty list."""
        self.stuck_log()

        with patch(EXPIRED_LOGS, return_value=[]):
            self.assertEqual(abandon_expired_settlements(), [])


class TestScheduledAbandonment(AbandonmentTestCase):
    """What one scheduled attempt does to a capture that has run out."""

    def test_the_last_attempt_moves_the_capture(self) -> None:
        """The attempt that reaches the cap leaves the log at Needs Attention."""
        name = self.stuck_log()
        self.stamp(name, SETTLEMENT_MAX_ATTEMPTS - 1)

        with patch(CREATE_PAYMENT_ENTRY):
            drive_settlement_retry(name)

        self.assertEqual(self.field(name, "status"), NEEDS_ATTENTION)

    def test_an_earlier_attempt_leaves_the_capture_alone(self) -> None:
        """An attempt two short of the cap leaves the log at Processed."""
        name = self.stuck_log()
        self.stamp(name, SETTLEMENT_MAX_ATTEMPTS - 2)

        with patch(CREATE_PAYMENT_ENTRY):
            drive_settlement_retry(name)

        self.assertEqual(self.field(name, "status"), "Processed")

    def test_an_earlier_attempt_raises_no_alert(self) -> None:
        """An attempt short of the cap files no alert."""
        name = self.stuck_log()
        self.stamp(name, SETTLEMENT_MAX_ATTEMPTS - 2)

        with patch(CREATE_PAYMENT_ENTRY):
            drive_settlement_retry(name)

        self.assertEqual(self.alerts_for(name), [])

    def test_the_last_attempt_alerts_once(self) -> None:
        """The attempt that gives up files one alert."""
        name = self.stuck_log()
        self.stamp(name, SETTLEMENT_MAX_ATTEMPTS - 1)

        with patch(CREATE_PAYMENT_ENTRY):
            drive_settlement_retry(name)

        self.assertEqual(len(self.alerts_for(name)), 1)

    def test_the_alert_states_the_attempts(self) -> None:
        """The alert states how many attempts booked nothing."""
        name = self.stuck_log(errors="Currency mismatch.")
        self.stamp(name, SETTLEMENT_MAX_ATTEMPTS - 1)

        with patch(CREATE_PAYMENT_ENTRY):
            drive_settlement_retry(name)

        self.assertEqual(
            self.alerts_for(name)[0].error,
            f"{SETTLEMENT_MAX_ATTEMPTS} settlement attempts booked nothing. "
            "Last recorded error: Currency mismatch.",
        )

    def test_an_attempt_that_books_raises_no_alert(self) -> None:
        """An attempt that books a Payment Entry leaves no alert."""
        name = self.stuck_log()
        self.stamp(name, SETTLEMENT_MAX_ATTEMPTS - 1)

        drive_settlement_retry(name)

        self.addCleanup(cleanup_doc, "Payment Entry", self.field(name, "payment_entry"))
        self.assertEqual(self.alerts_for(name), [])

    def test_an_attempt_that_books_leaves_the_log_completed(self) -> None:
        """An attempt that books leaves the log at Completed."""
        name = self.stuck_log()
        self.stamp(name, SETTLEMENT_MAX_ATTEMPTS - 1)

        drive_settlement_retry(name)

        self.addCleanup(cleanup_doc, "Payment Entry", self.field(name, "payment_entry"))
        self.assertEqual(self.field(name, "status"), "Completed")


class TestTheRetryLoopStops(AbandonmentTestCase):
    """A capture that has been given up on is never swept again."""

    def sweep_reports(self) -> int:
        """Return how many sweep reports this test filed."""
        return frappe.db.count(
            INTEGRATION_REQUEST,
            {"url": f"sweep:{PAYMENT_SWEEP}", "creation": [">=", self.started_at]},
        )

    def test_the_sweep_no_longer_offers_it(self) -> None:
        """unsettled_logs() drops a log at Needs Attention."""
        name = self.abandoned_log()
        frappe.db.set_value(
            PAYMENT_LOG,
            name,
            "modified",
            add_to_date(now_datetime(), minutes=-60),
            update_modified=False,
        )

        self.assertNotIn(name, [row.name for row in unsettled_logs()])

    def test_the_sweep_gives_up_before_it_retries(self) -> None:
        """The ten-minute sweep moves an expired capture on the same tick."""
        name = self.stuck_log(days_old=SETTLEMENT_RETRY_LOOKBACK_DAYS + 1)

        with patch(DUE_LOGS, return_value=[]):
            retry_stuck_settlements()

        self.assertEqual(self.field(name, "status"), NEEDS_ATTENTION)

    def test_two_ticks_leave_one_alert(self) -> None:
        """Two sweeps over an expired capture leave one alert."""
        name = self.stuck_log(days_old=SETTLEMENT_RETRY_LOOKBACK_DAYS + 1)

        with patch(DUE_LOGS, return_value=[]):
            retry_stuck_settlements()
            retry_stuck_settlements()

        self.assertEqual(len(self.alerts_for(name)), 1)

    def test_a_failing_expiry_pass_stops_the_tick(self) -> None:
        """A failing expiry pass files no sweep report."""
        with patch(EXPIRED_LOGS, side_effect=RuntimeError("db down")):
            retry_stuck_settlements()

        self.assertEqual(self.sweep_reports(), 0)

    def test_a_failing_expiry_pass_drives_nothing(self) -> None:
        """A failing expiry pass runs no settlement."""
        with patch(EXPIRED_LOGS, side_effect=RuntimeError("db down")):
            with patch(CREATE_PAYMENT_ENTRY) as settle:
                retry_stuck_settlements()

        self.assertEqual(settle.call_count, 0)


class TestManualCompletion(AbandonmentTestCase):
    """Complete Payment still re-drives a capture that was given up on."""

    def run_completion(self, name: str, transaction: Optional[dict]) -> Any:
        """Run the completion job against a fixed Paystack answer."""
        self.addCleanup(release_completion, name)
        with (
            patch(VERIFIED_TRANSACTION, return_value=transaction),
            patch("frappe.publish_realtime") as publish,
        ):
            complete_payment_from_log(name, "Administrator")

        return publish

    def test_the_capture_is_still_completable(self) -> None:
        """is_completable is true for a log at Needs Attention."""
        self.assertTrue(is_completable(self.log_doc(self.abandoned_log())))

    def test_the_settlement_still_runs_on_it(self) -> None:
        """create_payment_entry_from_log books a log at Needs Attention."""
        name = self.abandoned_log()

        create_payment_entry_from_log(name)

        self.addCleanup(cleanup_doc, "Payment Entry", self.field(name, "payment_entry"))
        self.assertIsNotNone(self.field(name, "payment_entry"))

    def test_a_successful_completion_clears_the_terminal_state(self) -> None:
        """A booked completion leaves the log at Completed."""
        name = self.abandoned_log()

        self.run_completion(name, paystack_capture())

        self.addCleanup(cleanup_doc, "Payment Entry", self.field(name, "payment_entry"))
        self.assertEqual(self.field(name, "status"), "Completed")

    def test_a_successful_completion_books_the_entry(self) -> None:
        """A booked completion records the Payment Entry on the log."""
        name = self.abandoned_log()

        self.run_completion(name, paystack_capture())

        self.addCleanup(cleanup_doc, "Payment Entry", self.field(name, "payment_entry"))
        self.assertIsNotNone(self.field(name, "payment_entry"))

    def test_a_refused_completion_keeps_the_terminal_state(self) -> None:
        """A completion Paystack contradicts leaves the log at Needs Attention."""
        name = self.abandoned_log()

        self.run_completion(name, paystack_capture(amount=90000))

        self.assertEqual(self.field(name, "status"), NEEDS_ATTENTION)

    def test_a_refused_completion_does_not_restart_the_sweep(self) -> None:
        """A refused completion leaves retry_count where it stood."""
        name = self.abandoned_log()
        self.stamp(name, SETTLEMENT_MAX_ATTEMPTS)

        self.run_completion(name, paystack_capture(amount=90000))

        self.assertEqual(self.field(name, "retry_count"), SETTLEMENT_MAX_ATTEMPTS)

    def test_a_refused_completion_records_the_discrepancy(self) -> None:
        """A refused completion writes what Paystack said to the log."""
        name = self.abandoned_log()

        self.run_completion(name, paystack_capture(amount=90000))

        self.assertEqual(
            self.field(name, "errors"),
            "Paystack captured 900.0 but this log records 1000.0.",
        )


class TestLaterWebhook(AbandonmentTestCase):
    """A charge webhook arriving late leaves a given-up capture as it stands."""

    def charge(self, name: str) -> None:
        """Deliver a charge.success naming a payment log."""
        process_charge_webhook_event(
            {
                "event": "charge.success",
                "data": {
                    "id": 900004,
                    "status": "success",
                    "amount": 100000,
                    "currency": "NGN",
                    "reference": name,
                    "metadata": {"reference": name},
                },
            }
        )

    def test_the_status_is_left_alone(self) -> None:
        """The webhook leaves the log at Needs Attention."""
        name = self.abandoned_log()

        self.charge(name)

        self.assertEqual(self.field(name, "status"), NEEDS_ATTENTION)

    def test_the_webhook_says_it_skipped_the_log(self) -> None:
        """The Integration Request records the status it found."""
        name = self.abandoned_log()

        self.charge(name)

        output = frappe.db.get_value(
            INTEGRATION_REQUEST,
            {"reference_docname": name, "url": "webhook"},
            "output",
        )
        self.assertEqual(frappe.parse_json(output), {"skipped": f"already {NEEDS_ATTENTION}"})


class TestReportVisibility(AbandonmentTestCase):
    """The reports that track money still sitting in suspense."""

    def unsettled(self) -> list:
        """Return the logs the unsettled payments report lists."""
        _columns, data = unsettled_report.execute({"company": TEST_COMPANY})
        return [row["name"] for row in data]

    def volume_for(self, customer: str) -> float:
        """Return what the customer volume report totals for a customer."""
        _columns, rows = volume_report.execute(frappe._dict({"company": TEST_COMPANY, "customer": customer}))
        return flt(rows[0].total) if rows else 0.0

    def customer_of(self, name: str) -> str:
        """Return the customer the log's invoice bills."""
        return frappe.db.get_value(SALES_INVOICE, self.field(name, "linked_docname"), "customer")

    def test_an_abandoned_capture_is_still_owed(self) -> None:
        """A log at Needs Attention is listed by the unsettled report."""
        self.assertIn(self.abandoned_log(), self.unsettled())

    def test_an_uncaptured_log_is_still_absent(self) -> None:
        """A Pending log stays off the unsettled report."""
        name = self.abandoned_log()
        frappe.db.set_value(PAYMENT_LOG, name, "status", "Pending")

        self.assertNotIn(name, self.unsettled())

    def test_the_row_reports_the_new_status(self) -> None:
        """The unsettled row carries Needs Attention in its status column."""
        name = self.abandoned_log()

        row = next(
            row for row in unsettled_report.execute({"company": TEST_COMPANY})[1] if row["name"] == name
        )
        self.assertEqual(row["status"], NEEDS_ATTENTION)

    def test_giving_up_leaves_the_customer_volume_untouched(self) -> None:
        """Moving a capture to Needs Attention keeps it in the customer's volume."""
        name = self.stuck_log()
        customer = self.customer_of(name)
        before = self.volume_for(customer)

        abandon_settlement(name, TEST_CAUSE)

        self.assertEqual(self.volume_for(customer), before)

    def test_a_failed_capture_is_not_in_the_customer_volume(self) -> None:
        """A Failed log is dropped from the customer's volume."""
        name = self.stuck_log()
        customer = self.customer_of(name)
        before = self.volume_for(customer)

        frappe.db.set_value(PAYMENT_LOG, name, "status", "Failed")

        self.assertEqual(self.volume_for(customer), before - 1000)

    def test_the_activity_report_grades_it_an_error(self) -> None:
        """A capture at Needs Attention is graded Error."""
        log = frappe._dict({"status": NEEDS_ATTENTION, "payment_entry": None})

        self.assertEqual(activity_report.payment_severity(log), activity_report.ERROR)

    def test_a_capture_still_being_retried_is_only_a_warning(self) -> None:
        """A capture at Processed with no entry is graded Warning."""
        log = frappe._dict({"status": "Processed", "payment_entry": None})

        self.assertEqual(activity_report.payment_severity(log), activity_report.WARNING)

    def test_the_reconciliation_pass_still_reads_it(self) -> None:
        """Needs Attention is one of the statuses reconciliation walks."""
        self.assertIn(NEEDS_ATTENTION, RECONCILABLE_STATUSES)


class TestReconciliationGrading(AbandonmentTestCase):
    """How reconciliation grades a capture the app gave up on."""

    def graded(self, name: str, status: str) -> tuple:
        """Compare a log against a Paystack transaction in a given state."""
        return ReconciliationEngine(TEST_COMPANY).compare(
            self.log_doc(name),
            {"status": status, "amount": 100000, "currency": "NGN"},
        )

    def test_a_capture_paystack_denies_is_a_mismatch(self) -> None:
        """Paystack reporting no capture against a given-up log is a Mismatch."""
        status, _reason, _paystack, _recorded = self.graded(self.abandoned_log(), "abandoned")

        self.assertEqual(status, "Mismatch")

    def test_the_same_answer_against_an_open_log_is_pending(self) -> None:
        """The same answer against a Pending log is graded Pending."""
        status, _reason, _paystack, _recorded = self.graded(self.stuck_log(status="Pending"), "abandoned")

        self.assertEqual(status, "Pending")

    def test_a_capture_paystack_confirms_is_a_mismatch(self) -> None:
        """A confirmed capture the ledger never booked is a Mismatch."""
        _status, reason, _paystack, _recorded = self.graded(self.abandoned_log(), "success")

        self.assertEqual(
            reason,
            f"Paystack captured this payment but the log is still '{NEEDS_ATTENTION}'",
        )


class TestCapturedChart(AbandonmentTestCase):
    """The workspace chart that sums what Paystack captured."""

    def charted(self, record: str) -> bool:
        """Report whether a log falls inside the rows the chart sums."""
        chart = next(chart for chart in DASHBOARD_CHARTS if chart["chart_name"] == CAPTURED_CHART)
        filters = json.loads(chart["filters_json"])
        filters.append([PAYMENT_LOG, "name", "=", record])

        return bool(frappe.get_list(PAYMENT_LOG, filters=filters, pluck="name", order_by=None))

    def test_an_abandoned_capture_is_still_plotted(self) -> None:
        """A log at Needs Attention is summed by the chart."""
        self.assertTrue(self.charted(self.abandoned_log()))

    def test_a_failed_payment_is_not_plotted(self) -> None:
        """A Failed log stays off the chart."""
        name = self.abandoned_log()
        frappe.db.set_value(PAYMENT_LOG, name, "status", "Failed")

        self.assertFalse(self.charted(name))


class TestHealthCard(AbandonmentTestCase):
    """The workspace card that counts the captures the app gave up on."""

    def card(self, name: str) -> dict:
        """Return the shipped definition of one card."""
        return next(card for card in NUMBER_CARDS if card["name"] == name)

    def counted(self, card_name: str, record: str) -> bool:
        """Report whether a record falls inside the rows a card counts."""
        card = self.card(card_name)
        filters = json.loads(card["filters_json"])
        filters.append([card["document_type"], "name", "=", record])

        return bool(frappe.get_list(card["document_type"], filters=filters, pluck="name", order_by=None))

    def test_an_abandoned_capture_is_counted(self) -> None:
        """A log at Needs Attention is counted by the card."""
        self.assertTrue(self.counted(NEEDS_ATTENTION_CARD, self.abandoned_log()))

    def test_a_capture_still_being_retried_is_not_counted(self) -> None:
        """A log at Processed stays off the card."""
        self.assertFalse(self.counted(NEEDS_ATTENTION_CARD, self.stuck_log()))

    def test_an_abandoned_capture_leaves_the_waiting_card(self) -> None:
        """A log at Needs Attention drops off the Awaiting Settlement card."""
        self.assertFalse(self.counted(AWAITING_SETTLEMENT_CARD, self.abandoned_log()))

    def test_the_card_counts_the_payment_log(self) -> None:
        """The card is a Payment Log count."""
        self.assertEqual(self.card(NEEDS_ATTENTION_CARD)["document_type"], PAYMENT_LOG)


class TestPortalState(AbandonmentTestCase):
    """What the customer's portal offers for a capture the merchant owes."""

    def test_the_document_reads_as_processing(self) -> None:
        """A log at Needs Attention keeps the document in the processing state."""
        name = self.abandoned_log()

        self.assertEqual(
            payment_state(SALES_INVOICE, self.field(name, "linked_docname")),
            PAYMENT_STATE_PROCESSING,
        )

    def test_a_receipt_is_still_issued(self) -> None:
        """The receipt endpoint accepts a log at Needs Attention."""
        name = self.abandoned_log()

        with patch(OWNS_PAYMENT_LOG, return_value=True), patch("frappe.get_print", return_value=b"%PDF-1.4"):
            download_payment_receipt(name)

        self.assertEqual(frappe.local.response.filename, f"{name}.pdf")

    def test_a_second_collection_is_refused(self) -> None:
        """A subscription invoice carrying the log is not charged again."""
        name = self.abandoned_log()

        self.assertTrue(has_open_collection(self.field(name, "linked_docname")))


class TestExpiryPatch(AbandonmentTestCase):
    """The migration that moves the logs already past the boundary."""

    def run_patch(self) -> None:
        """Run the patch the migration runs."""
        frappe.get_module("frappe_paystack.patches.v15_0.abandon_expired_settlements").execute()

    def test_the_patch_moves_an_expired_capture(self) -> None:
        """A capture past the window is moved by the patch."""
        name = self.stuck_log(days_old=SETTLEMENT_RETRY_LOOKBACK_DAYS + 1)

        self.run_patch()

        self.assertEqual(self.field(name, "status"), NEEDS_ATTENTION)

    def test_the_patch_leaves_a_recent_capture_alone(self) -> None:
        """A capture inside the window is untouched by the patch."""
        name = self.stuck_log(days_old=SETTLEMENT_RETRY_LOOKBACK_DAYS - 2)

        self.run_patch()

        self.assertEqual(self.field(name, "status"), "Processed")

    def test_the_patch_leaves_a_pos_tender_alone(self) -> None:
        """A POS tender past the window is untouched by the patch."""
        name = self.pos_log(creation=add_to_date(now_datetime(), days=-(SETTLEMENT_RETRY_LOOKBACK_DAYS + 1)))

        self.run_patch()

        self.assertEqual(self.field(name, "status"), "Processed")

    def test_a_second_run_raises_no_second_alert(self) -> None:
        """Running the patch twice leaves one alert on the log."""
        name = self.stuck_log(days_old=SETTLEMENT_RETRY_LOOKBACK_DAYS + 1)

        self.run_patch()
        self.run_patch()

        self.assertEqual(len(self.alerts_for(name)), 1)

    def test_the_patch_runs_after_the_model_sync(self) -> None:
        """The patch is registered under post_model_sync."""
        with open(frappe.get_app_path("frappe_paystack", "patches.txt"), encoding="utf-8") as registered:
            lines = registered.read().splitlines()

        self.assertIn(
            "frappe_paystack.patches.v15_0.abandon_expired_settlements",
            lines[lines.index("[post_model_sync]") :],
        )
