"""Completing a capture by hand from the Payment Log form."""

from typing import Any, Optional
from unittest.mock import patch

import frappe

from frappe_paystack.frappe_paystack.doctype.paystack_payment_log.paystack_payment_log import (
    COMPLETE_PAYMENT_EVENT,
    COMPLETE_PAYMENT_JOB,
    PAYMENT_SWEEP,
    complete_payment,
    complete_payment_from_log,
    completion_key,
    hold_completion,
    is_completable,
    release_completion,
    verification_mismatch,
    verified_transaction,
)
from frappe_paystack.tests.factories import GatewaySettingFactory, PaymentLogFactory, cleanup_user
from frappe_paystack.tests.test_base import PaystackTestCase
from frappe_paystack.utils.sweep import clear_streak

PAYMENT_LOG = "Paystack Payment Log"
INTEGRATION_REQUEST = "Integration Request"
USER = "User"
USER_PERMISSION = "User Permission"

LOG_MODULE = "frappe_paystack.frappe_paystack.doctype.paystack_payment_log.paystack_payment_log"

VERIFIED_TRANSACTION = f"{LOG_MODULE}.verified_transaction"
CREATE_PAYMENT_ENTRY = f"{LOG_MODULE}.create_payment_entry_from_log"
LOOKUP_TRANSACTION = "frappe_paystack.utils.reconciliation.ReconciliationEngine.lookup_transaction"

# The path the desk form's button posts to.
COMPLETE_PAYMENT_METHOD = (
    "frappe_paystack.frappe_paystack.doctype.paystack_payment_log.paystack_payment_log.complete_payment"
)

# The company an Accounts Manager is restricted to.
OTHER_COMPANY = "_Test Company 1"

COMPLETER = "test.paystack.complete@example.com"


def paystack_capture(amount: int = 100000, currency: str = "NGN", status: str = "success") -> dict:
    """Return a Paystack transaction payload, in the minor units it sends."""
    return {"id": 900001, "status": status, "currency": currency, "amount": amount}


class CompletePaymentTestCase(PaystackTestCase):
    """A gateway to verify through and a capture awaiting settlement."""

    def setUp(self) -> None:
        """Enable Paystack for the test company."""
        super().setUp()
        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)

    def stuck_log(self, amount: float = 1000, **fields: Any) -> str:
        """Insert a log at Processed with no Payment Entry, and return its name."""
        name = PaymentLogFactory.create(amount=amount)
        values = {
            "status": "Processed",
            "amount_paid": amount,
            "currency_paid": "NGN",
            "payment_reference": name,
        }
        values.update(fields)

        frappe.db.set_value(PAYMENT_LOG, name, values, update_modified=False)
        frappe.clear_document_cache(PAYMENT_LOG, name)
        frappe.db.commit()

        self.addCleanup(PaymentLogFactory.cleanup, name)
        self.addCleanup(clear_streak, PAYMENT_SWEEP, name)
        self.addCleanup(release_completion, name)
        return name

    def log_doc(self, name: str) -> Any:
        """Return a Payment Log read fresh off the row."""
        frappe.clear_document_cache(PAYMENT_LOG, name)
        return frappe.get_doc(PAYMENT_LOG, name)

    def field(self, name: str, fieldname: str) -> Any:
        """Return one field of a Payment Log."""
        return frappe.db.get_value(PAYMENT_LOG, name, fieldname)

    def run_completion(self, name: str, transaction: Optional[dict], user: str = "Administrator") -> Any:
        """Run the background job against a fixed Paystack answer."""
        with (
            patch(VERIFIED_TRANSACTION, return_value=transaction),
            patch("frappe.publish_realtime") as publish,
        ):
            complete_payment_from_log(name, user)

        return publish

    def published(self, publish: Any) -> dict:
        """Return the payload the job pushed to the user."""
        return publish.call_args.args[1]


class TestCompletable(CompletePaymentTestCase):
    """Which logs still have a settlement outstanding."""

    def test_a_capture_nothing_booked_is_completable(self) -> None:
        """A Processed log with no Payment Entry is completable."""
        log = self.log_doc(self.stuck_log())

        self.assertTrue(is_completable(log))

    def test_a_capture_that_already_booked_is_not(self) -> None:
        """A log that already has a Payment Entry is not completable."""
        name = self.stuck_log()
        frappe.db.set_value(PAYMENT_LOG, name, "payment_entry", "ACC-PAY-TEST-0001")

        self.assertFalse(is_completable(self.log_doc(name)))

    def test_a_pending_log_is_not_completable(self) -> None:
        """A Pending log is not completable."""
        name = self.stuck_log()
        frappe.db.set_value(PAYMENT_LOG, name, "status", "Pending")

        self.assertFalse(is_completable(self.log_doc(name)))

    def test_a_completed_log_is_not_completable(self) -> None:
        """A Completed log is not completable."""
        name = self.stuck_log()
        frappe.db.set_value(PAYMENT_LOG, name, "status", "Completed")

        self.assertFalse(is_completable(self.log_doc(name)))

    def test_a_failed_log_is_not_completable(self) -> None:
        """A Failed log is not completable."""
        name = self.stuck_log()
        frappe.db.set_value(PAYMENT_LOG, name, "status", "Failed")

        self.assertFalse(is_completable(self.log_doc(name)))

    def test_a_refunded_log_is_not_completable(self) -> None:
        """A Refunded log is not completable."""
        name = self.stuck_log()
        frappe.db.set_value(PAYMENT_LOG, name, "status", "Refunded")

        self.assertFalse(is_completable(self.log_doc(name)))

    def test_a_partially_refunded_log_is_not_completable(self) -> None:
        """A Partially Refunded log is not completable."""
        name = self.stuck_log()
        frappe.db.set_value(PAYMENT_LOG, name, "status", "Partially Refunded")

        self.assertFalse(is_completable(self.log_doc(name)))


class TestCompletionLock(CompletePaymentTestCase):
    """The key that lets one completion of a log run at a time."""

    def test_the_first_claim_wins(self) -> None:
        """The first claim on a log's completion key succeeds."""
        name = self.stuck_log()

        self.assertTrue(hold_completion(name))

    def test_a_second_claim_on_the_same_log_loses(self) -> None:
        """A second claim on a held log fails."""
        name = self.stuck_log()
        hold_completion(name)

        self.assertFalse(hold_completion(name))

    def test_a_claim_on_another_log_is_unaffected(self) -> None:
        """A claim on one log leaves another log's key free to claim."""
        first = self.stuck_log()
        second = self.stuck_log()
        hold_completion(first)

        self.assertTrue(hold_completion(second))

    def test_releasing_lets_the_next_claim_through(self) -> None:
        """Releasing a log's key lets the next claim succeed."""
        name = self.stuck_log()
        hold_completion(name)
        release_completion(name)

        self.assertTrue(hold_completion(name))

    def test_the_claim_is_given_a_lifetime(self) -> None:
        """The completion key carries a TTL of at most 900 seconds."""
        name = self.stuck_log()
        hold_completion(name)

        ttl = frappe.cache.ttl(completion_key(name))
        self.assertTrue(0 < ttl <= 900, f"ttl was {ttl}")


class TestCompletePaymentPermission(CompletePaymentTestCase):
    """Who may put a Paystack capture into the ledger."""

    def become(self, roles: list) -> str:
        """Sign in as a fresh user holding the given roles."""
        user = frappe.get_doc(
            {
                "doctype": USER,
                "email": COMPLETER,
                "first_name": "Paystack Completer",
                "send_welcome_email": 0,
                "roles": [{"role": role} for role in roles],
            }
        )
        user.flags.ignore_permissions = True
        user.insert()
        frappe.db.commit()

        self.addCleanup(cleanup_user, COMPLETER)
        self.addCleanup(frappe.set_user, "Administrator")
        frappe.set_user(COMPLETER)
        return COMPLETER

    def restrict_to_other_company(self, user: str) -> None:
        """Give a user a company permission the stuck log falls outside of."""
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

    def test_a_user_who_can_only_read_the_log_may_not_book_it(self) -> None:
        """An Accounts User calling complete_payment raises PermissionError."""
        name = self.stuck_log()
        self.become(["Accounts User"])

        with self.assertRaises(frappe.PermissionError):
            complete_payment(name)

    def test_a_refused_role_queues_nothing(self) -> None:
        """A refused role leaves the enqueue count at zero."""
        name = self.stuck_log()
        self.become(["Accounts User"])

        with patch("frappe.enqueue") as enqueue:
            with self.assertRaises(frappe.PermissionError):
                complete_payment(name)

        self.assertEqual(enqueue.call_count, 0)

    def test_an_accounts_manager_may_book_it(self) -> None:
        """An Accounts Manager calling complete_payment gets the log name back."""
        name = self.stuck_log()
        self.become(["Accounts Manager"])

        with patch("frappe.enqueue"):
            self.assertEqual(complete_payment(name), name)

    def test_a_log_outside_the_users_company_is_refused(self) -> None:
        """A log outside the user's permitted company raises PermissionError."""
        name = self.stuck_log()
        user = self.become(["Accounts Manager"])
        self.restrict_to_other_company(user)

        with self.assertRaises(frappe.PermissionError):
            complete_payment(name)


class TestCompletePaymentWiring(CompletePaymentTestCase):
    """The names the desk form and the worker have to agree on."""

    def test_the_button_posts_to_a_method_that_exists(self) -> None:
        """The path the form posts to resolves to complete_payment."""
        self.assertIs(frappe.get_attr(COMPLETE_PAYMENT_METHOD), complete_payment)

    def test_the_method_is_reachable_from_the_desk(self) -> None:
        """complete_payment is whitelisted."""
        self.assertIn(complete_payment, frappe.whitelisted)

    def test_the_queued_job_names_a_function_that_exists(self) -> None:
        """The queued job path resolves to complete_payment_from_log."""
        self.assertIs(frappe.get_attr(COMPLETE_PAYMENT_JOB), complete_payment_from_log)

    def test_the_form_and_the_job_agree_on_the_event(self) -> None:
        """The completion event is 'paystack_payment_completed'."""
        self.assertEqual(COMPLETE_PAYMENT_EVENT, "paystack_payment_completed")


class TestCompletePaymentQueueing(CompletePaymentTestCase):
    """What the desk call does before it hands the work over."""

    def test_the_job_is_queued_for_the_log_on_screen(self) -> None:
        """The job is queued under the completion path, carrying the log name."""
        name = self.stuck_log()

        with patch("frappe.enqueue") as enqueue:
            complete_payment(name)

        self.assertEqual(enqueue.call_args.args, (COMPLETE_PAYMENT_JOB,))
        self.assertEqual(enqueue.call_args.kwargs["payment_log_name"], name)

    def test_the_job_carries_the_user_who_asked(self) -> None:
        """The queued job carries the calling session user."""
        name = self.stuck_log()

        with patch("frappe.enqueue") as enqueue:
            complete_payment(name)

        self.assertEqual(enqueue.call_args.kwargs["user"], frappe.session.user)

    def test_the_job_runs_off_the_request(self) -> None:
        """The job is queued on the long queue."""
        name = self.stuck_log()

        with patch("frappe.enqueue") as enqueue:
            complete_payment(name)

        self.assertEqual(enqueue.call_args.kwargs["queue"], "long")

    def test_the_job_is_named_by_the_supported_keyword(self) -> None:
        """The completion job identifies itself with job_id."""
        name = self.stuck_log()

        with patch("frappe.enqueue") as enqueue:
            complete_payment(name)

        self.assertNotIn("job_name", enqueue.call_args.kwargs)
        self.assertEqual(enqueue.call_args.kwargs["job_id"], f"paystack-complete-{name}")

    def test_the_job_is_deduplicated_on_the_log(self) -> None:
        """The completion lets the queue turn away a job it already holds."""
        name = self.stuck_log()

        with patch("frappe.enqueue") as enqueue:
            complete_payment(name)

        self.assertTrue(enqueue.call_args.kwargs["deduplicate"])

    def test_a_queue_with_room_takes_the_job(self) -> None:
        """A completion nothing already holds is queued and the log comes back."""
        name = self.stuck_log()

        with patch("frappe.enqueue") as enqueue:
            self.assertEqual(complete_payment(name), name)

        self.assertEqual(enqueue.call_count, 1)

    def test_a_job_the_queue_turns_away_is_reported(self) -> None:
        """A completion the queue already holds raises ValidationError."""
        name = self.stuck_log()

        with patch("frappe.enqueue", return_value=None):
            with self.assertRaises(frappe.ValidationError) as caught:
                complete_payment(name)

        self.assertIn("already queued", str(caught.exception))

    def test_a_job_the_queue_turns_away_hands_back_the_claim(self) -> None:
        """A completion the queue refuses leaves its key free to claim."""
        name = self.stuck_log()

        with patch("frappe.enqueue", return_value=None):
            with self.assertRaises(frappe.ValidationError):
                complete_payment(name)

        self.assertTrue(hold_completion(name))

    def test_a_settled_log_is_refused(self) -> None:
        """A Completed log raises ValidationError."""
        name = self.stuck_log()
        frappe.db.set_value(PAYMENT_LOG, name, "status", "Completed")

        with self.assertRaises(frappe.ValidationError):
            complete_payment(name)

    def test_a_settled_log_queues_nothing(self) -> None:
        """A Completed log leaves the enqueue count at zero."""
        name = self.stuck_log()
        frappe.db.set_value(PAYMENT_LOG, name, "status", "Completed")

        with patch("frappe.enqueue") as enqueue:
            with self.assertRaises(frappe.ValidationError):
                complete_payment(name)

        self.assertEqual(enqueue.call_count, 0)

    def test_a_settled_log_claims_no_lock(self) -> None:
        """A Completed log leaves its completion key free to claim."""
        name = self.stuck_log()
        frappe.db.set_value(PAYMENT_LOG, name, "status", "Completed")

        with self.assertRaises(frappe.ValidationError):
            complete_payment(name)

        self.assertTrue(hold_completion(name))

    def test_a_second_press_while_one_runs_is_refused(self) -> None:
        """A second call while the first job is outstanding raises ValidationError."""
        name = self.stuck_log()

        with patch("frappe.enqueue"):
            complete_payment(name)

            with self.assertRaises(frappe.ValidationError):
                complete_payment(name)

    def test_a_second_press_queues_no_second_job(self) -> None:
        """Two calls in a row leave the enqueue count at one."""
        name = self.stuck_log()

        with patch("frappe.enqueue") as enqueue:
            complete_payment(name)
            with self.assertRaises(frappe.ValidationError):
                complete_payment(name)

        self.assertEqual(enqueue.call_count, 1)


class TestVerificationRule(CompletePaymentTestCase):
    """What Paystack has to say before anything is booked."""

    def test_a_matching_capture_verifies(self) -> None:
        """A successful capture of the logged amount and currency verifies."""
        log = self.log_doc(self.stuck_log())

        self.assertIsNone(verification_mismatch(log, paystack_capture()))

    def test_a_reference_paystack_has_never_seen_is_refused(self) -> None:
        """A missing transaction reports the reference Paystack was asked for."""
        log = self.log_doc(self.stuck_log())

        self.assertEqual(
            verification_mismatch(log, None),
            f"Paystack has no transaction under reference {log.name}.",
        )

    def test_an_abandoned_transaction_is_refused(self) -> None:
        """An abandoned transaction reports the status Paystack gave."""
        log = self.log_doc(self.stuck_log())

        self.assertEqual(
            verification_mismatch(log, paystack_capture(status="abandoned")),
            "Paystack reports this transaction as abandoned, not a capture.",
        )

    def test_a_transaction_with_no_status_is_refused(self) -> None:
        """A transaction with an empty status reports it as unknown."""
        log = self.log_doc(self.stuck_log())

        self.assertEqual(
            verification_mismatch(log, paystack_capture(status="")),
            "Paystack reports this transaction as unknown, not a capture.",
        )

    def test_a_capture_in_another_currency_is_refused(self) -> None:
        """A capture in another currency reports both currencies."""
        log = self.log_doc(self.stuck_log())

        self.assertEqual(
            verification_mismatch(log, paystack_capture(currency="USD")),
            "Paystack settled in USD but this log records NGN.",
        )

    def test_a_capture_with_no_currency_is_refused(self) -> None:
        """A capture with an empty currency reports it as no currency."""
        log = self.log_doc(self.stuck_log())

        self.assertEqual(
            verification_mismatch(log, paystack_capture(currency="")),
            "Paystack settled in no currency but this log records NGN.",
        )

    def test_a_log_with_no_currency_at_all_is_refused(self) -> None:
        """A log holding no currency reports the settled currency against it."""
        name = self.stuck_log()
        frappe.db.set_value(PAYMENT_LOG, name, {"currency_paid": None, "currency": None})

        self.assertEqual(
            verification_mismatch(self.log_doc(name), paystack_capture()),
            "Paystack settled in NGN but this log records no currency.",
        )

    def test_the_charge_currency_outranks_the_billed_one(self) -> None:
        """The comparison uses currency_paid."""
        name = self.stuck_log()
        frappe.db.set_value(PAYMENT_LOG, name, {"currency_paid": "USD"})

        self.assertEqual(
            verification_mismatch(self.log_doc(name), paystack_capture(currency="USD")),
            None,
        )

    def test_a_short_capture_is_refused(self) -> None:
        """A capture below the logged amount reports both amounts."""
        log = self.log_doc(self.stuck_log())

        self.assertEqual(
            verification_mismatch(log, paystack_capture(amount=90000)),
            "Paystack captured 900.0 but this log records 1000.0.",
        )

    def test_an_over_capture_is_refused(self) -> None:
        """A capture above the logged amount reports both amounts."""
        log = self.log_doc(self.stuck_log())

        self.assertEqual(
            verification_mismatch(log, paystack_capture(amount=110000)),
            "Paystack captured 1100.0 but this log records 1000.0.",
        )

    def test_rounding_slack_still_verifies(self) -> None:
        """A one-kobo difference verifies."""
        log = self.log_doc(self.stuck_log())

        self.assertIsNone(verification_mismatch(log, paystack_capture(amount=100001)))

    def test_the_lookup_asks_paystack_about_this_log(self) -> None:
        """Verification passes the log to the reconciliation lookup."""
        log = self.log_doc(self.stuck_log())

        with patch(LOOKUP_TRANSACTION, return_value=paystack_capture()) as lookup:
            verified_transaction(log)

        self.assertEqual(lookup.call_args.args[0].name, log.name)


class TestCompletionJob(CompletePaymentTestCase):
    """What the background job books, records and reports."""

    def test_a_verified_capture_books_a_payment_entry(self) -> None:
        """A verified capture books a Payment Entry."""
        name = self.stuck_log()

        self.run_completion(name, paystack_capture())

        self.assertTrue(self.field(name, "payment_entry"))

    def test_a_verified_capture_completes_the_log(self) -> None:
        """A verified capture leaves the log at Completed."""
        name = self.stuck_log()

        self.run_completion(name, paystack_capture())

        self.assertEqual(self.field(name, "status"), "Completed")

    def test_the_booked_entry_is_reported_to_the_user(self) -> None:
        """A booked capture publishes the Payment Entry name and a booked message."""
        name = self.stuck_log()

        publish = self.run_completion(name, paystack_capture())

        self.assertEqual(
            self.published(publish),
            {
                "log": name,
                "booked": True,
                "payment_entry": self.field(name, "payment_entry"),
                "message": f"Payment Entry {self.field(name, 'payment_entry')} booked.",
            },
        )

    def test_the_outcome_reaches_the_user_who_asked(self) -> None:
        """The outcome is published to the user the job carries."""
        name = self.stuck_log()

        publish = self.run_completion(name, paystack_capture(), user=COMPLETER)

        self.assertEqual(publish.call_args.kwargs["user"], COMPLETER)

    def test_the_outcome_is_pushed_on_the_agreed_event(self) -> None:
        """The outcome is published on the paystack_payment_completed event."""
        name = self.stuck_log()

        publish = self.run_completion(name, paystack_capture())

        self.assertEqual(publish.call_args.args[0], "paystack_payment_completed")

    def test_the_sweep_files_its_usual_report(self) -> None:
        """A completion by hand files one sweep Integration Request."""
        name = self.stuck_log()

        self.run_completion(name, paystack_capture())

        self.assertEqual(
            frappe.db.count(
                INTEGRATION_REQUEST,
                {"url": f"sweep:{PAYMENT_SWEEP}", "creation": [">=", self.started_at]},
            ),
            1,
        )

    def test_a_disagreeing_paystack_books_nothing(self) -> None:
        """A mismatched capture leaves payment_entry empty."""
        name = self.stuck_log()

        self.run_completion(name, paystack_capture(amount=90000))

        self.assertFalse(self.field(name, "payment_entry"))

    def test_a_disagreeing_paystack_leaves_the_log_where_it_was(self) -> None:
        """A mismatched capture leaves the log at Processed."""
        name = self.stuck_log()

        self.run_completion(name, paystack_capture(amount=90000))

        self.assertEqual(self.field(name, "status"), "Processed")

    def test_a_disagreeing_paystack_records_the_discrepancy(self) -> None:
        """A mismatched capture writes the discrepancy to the log's errors field."""
        name = self.stuck_log()

        self.run_completion(name, paystack_capture(amount=90000))

        self.assertEqual(
            self.field(name, "errors"),
            "Paystack captured 900.0 but this log records 1000.0.",
        )

    def test_a_disagreeing_paystack_reports_the_discrepancy(self) -> None:
        """A mismatched capture publishes the discrepancy as the message."""
        name = self.stuck_log()

        publish = self.run_completion(name, paystack_capture(amount=90000))

        self.assertEqual(
            self.published(publish),
            {
                "log": name,
                "booked": False,
                "payment_entry": None,
                "message": "Paystack captured 900.0 but this log records 1000.0.",
            },
        )

    def test_a_recorded_discrepancy_survives_a_rollback(self) -> None:
        """The recorded discrepancy is still on the log after a rollback."""
        name = self.stuck_log()

        self.run_completion(name, paystack_capture(amount=90000))
        frappe.db.rollback()

        self.assertEqual(
            self.field(name, "errors"),
            "Paystack captured 900.0 but this log records 1000.0.",
        )

    def test_a_log_settled_since_the_press_is_not_booked_again(self) -> None:
        """A log carrying a Payment Entry runs the settlement zero times."""
        name = self.stuck_log()
        frappe.db.set_value(PAYMENT_LOG, name, "payment_entry", "ACC-PAY-TEST-0001")

        with patch(CREATE_PAYMENT_ENTRY) as settle:
            self.run_completion(name, paystack_capture())

        self.assertEqual(settle.call_count, 0)

    def test_a_log_settled_since_the_press_reports_what_booked_it(self) -> None:
        """A log carrying a Payment Entry publishes it as booked."""
        name = self.stuck_log()
        frappe.db.set_value(PAYMENT_LOG, name, "payment_entry", "ACC-PAY-TEST-0001")

        publish = self.run_completion(name, paystack_capture())

        self.assertEqual(
            self.published(publish),
            {
                "log": name,
                "booked": True,
                "payment_entry": "ACC-PAY-TEST-0001",
                "message": f"{name} has no outstanding settlement to complete.",
            },
        )

    def test_a_log_settled_since_the_press_asks_paystack_nothing(self) -> None:
        """A log carrying a Payment Entry runs the verification lookup zero times."""
        name = self.stuck_log()
        frappe.db.set_value(PAYMENT_LOG, name, "payment_entry", "ACC-PAY-TEST-0001")

        with patch(VERIFIED_TRANSACTION) as lookup, patch("frappe.publish_realtime"):
            complete_payment_from_log(name, "Administrator")

        self.assertEqual(lookup.call_count, 0)

    def test_a_settlement_that_books_nothing_quotes_the_logs_error(self) -> None:
        """A settlement that books nothing publishes the log's errors field."""
        name = self.stuck_log()

        def leave_stuck(payment_log_name: str) -> None:
            """Record a settlement failure on the log."""
            frappe.db.set_value(PAYMENT_LOG, payment_log_name, "errors", "No receivable")

        with patch(CREATE_PAYMENT_ENTRY, side_effect=leave_stuck):
            publish = self.run_completion(name, paystack_capture())

        self.assertEqual(self.published(publish)["message"], "No receivable")

    def test_a_settlement_that_books_nothing_silently_still_says_so(self) -> None:
        """A settlement that records nothing publishes a no-Payment-Entry message."""
        name = self.stuck_log()

        with patch(CREATE_PAYMENT_ENTRY):
            publish = self.run_completion(name, paystack_capture())

        self.assertEqual(
            self.published(publish),
            {
                "log": name,
                "booked": False,
                "payment_entry": None,
                "message": "Settlement produced no Payment Entry.",
            },
        )

    def test_a_lookup_that_blows_up_is_reported(self) -> None:
        """A lookup that raises publishes an unbooked outcome naming the log."""
        name = self.stuck_log()

        with (
            patch(VERIFIED_TRANSACTION, side_effect=RuntimeError("paystack unreachable")),
            patch("frappe.publish_realtime") as publish,
        ):
            complete_payment_from_log(name, "Administrator")

        self.assertFalse(self.published(publish)["booked"])
        self.assertIn(
            f"Paystack manual completion failed for log {name}",
            self.published(publish)["message"],
        )

    def test_a_lookup_that_blows_up_books_nothing(self) -> None:
        """A lookup that raises leaves payment_entry empty."""
        name = self.stuck_log()

        with (
            patch(VERIFIED_TRANSACTION, side_effect=RuntimeError("paystack unreachable")),
            patch("frappe.publish_realtime"),
        ):
            complete_payment_from_log(name, "Administrator")

        self.assertFalse(self.field(name, "payment_entry"))

    def test_a_failure_records_itself_on_the_log(self) -> None:
        """A lookup that raises writes the failure to the log's errors field."""
        name = self.stuck_log()

        with (
            patch(VERIFIED_TRANSACTION, side_effect=RuntimeError("paystack unreachable")),
            patch("frappe.publish_realtime"),
        ):
            complete_payment_from_log(name, "Administrator")

        self.assertIn(
            f"Paystack manual completion failed for log {name}",
            self.field(name, "errors"),
        )

    def test_a_booked_log_is_free_of_its_claim(self) -> None:
        """A booked log releases its completion key."""
        name = self.stuck_log()
        hold_completion(name)

        self.run_completion(name, paystack_capture())

        self.assertTrue(hold_completion(name))

    def test_a_refused_log_is_free_of_its_claim(self) -> None:
        """A refused log releases its completion key."""
        name = self.stuck_log()
        hold_completion(name)

        self.run_completion(name, paystack_capture(amount=90000))

        self.assertTrue(hold_completion(name))

    def test_a_failed_log_is_free_of_its_claim(self) -> None:
        """A log whose lookup raised releases its completion key."""
        name = self.stuck_log()
        hold_completion(name)

        with (
            patch(VERIFIED_TRANSACTION, side_effect=RuntimeError("paystack unreachable")),
            patch("frappe.publish_realtime"),
        ):
            complete_payment_from_log(name, "Administrator")

        self.assertTrue(hold_completion(name))
