"""Tests for the Payment Request backfill and the patch, job and status renames."""

from unittest.mock import patch

import frappe
from frappe.utils import flt, random_string

from frappe_paystack import hooks
from frappe_paystack.migration import (
    FORMER_JOB_HOME,
    FORMER_MANUAL_OVERRIDE,
    JOB_HOME,
    REMOVED_JOBS,
    RENAMED_JOBS,
    RENAMED_PATCHES,
    backfill_payment_requests,
    candidate_requests,
    drop_removed_scheduled_jobs,
    linkage_report,
    rename_manual_overrides,
    rename_patch_log_entries,
    rename_scheduled_jobs,
    revert_payment_request_backfill,
    unlinked_logs,
)
from frappe_paystack.patches.v15_0.backfill_payment_request import execute
from frappe_paystack.tests.factories import (
    TEST_COMPANY,
    GatewaySettingFactory,
    PaymentLogFactory,
    POSInvoiceFactory,
    SalesOrderFactory,
    cleanup_doc,
)
from frappe_paystack.tests.test_base import PaystackTestCase
from frappe_paystack.utils.payment_request import build_payment_request
from frappe_paystack.utils.reconciliation import MANUAL_OVERRIDE, RECONCILIATION_LOG

PAYMENT_LOG = "Paystack Payment Log"
PAYMENT_REQUEST = "Payment Request"
PATCH_LOG = "Patch Log"
SCHEDULED_JOB_TYPE = "Scheduled Job Type"

PATCH_MODULE = "frappe_paystack.patches.v15_0.backfill_payment_request"

# The directory the patch modules answered to before.
FORMER_PATCH_DIRECTORY = "frappe_paystack.patches.v_15"

# The patches that drive the renames.
RENAME_PATCHES = (
    "frappe_paystack.patches.v15_0.rename_patch_log",
    "frappe_paystack.patches.v15_0.rename_scheduled_jobs",
    "frappe_paystack.patches.v15_0.rename_manual_override",
)

# The patches written after the directory rename, which no former path names.
LATER_PATCHES = ("frappe_paystack.patches.v15_0.drop_weekly_reconciliation_report",)

# How many Sales Order names to walk when looking for an unreferenced one.
ORDER_ATTEMPTS = 5


def scheduled_methods() -> list:
    """Return every job path scheduler_events registers, cron entries included."""
    methods = []

    for events in hooks.scheduler_events.values():
        if isinstance(events, dict):
            for entry in events.values():
                methods.extend(entry)
            continue

        methods.extend(events)

    return methods


def listed_patches() -> list:
    """Return the patch paths patches.txt names, across both sections."""
    content = frappe.read_file(frappe.get_app_path("frappe_paystack", "patches.txt"))

    return [line.strip() for line in content.splitlines() if line.strip() and not line.startswith(("#", "["))]


class BackfillTestCase(PaystackTestCase):
    """Builds the pre-bridge state: a Payment Request with an unlinked log."""

    def setUp(self) -> None:
        """Enable Paystack so a Payment Request can carry the gateway."""
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

    def build_order(self, rate: float = 1000) -> str:
        """Create a submitted Sales Order nothing already bills.

        Names are handed out again once a test rolls back, so one can arrive
        carrying an earlier test's committed log or request.
        """
        for attempt in range(ORDER_ATTEMPTS):
            order = SalesOrderFactory.create(rate=rate)
            self.addCleanup(SalesOrderFactory.cleanup, order)

            if not self.already_billed(order):
                return order

        raise AssertionError(f"no unbilled Sales Order name in {attempt + 1} tries")

    def already_billed(self, order: str) -> bool:
        """Report whether a payment log or a Payment Request already names an order."""
        return bool(
            frappe.db.exists(PAYMENT_LOG, {"linked_docname": order})
            or frappe.db.exists(
                PAYMENT_REQUEST, {"reference_doctype": "Sales Order", "reference_name": order}
            )
        )

    def candidates_for(self, log: str) -> list:
        """Return the requests the backfill would weigh for a log."""
        return sorted(candidate_requests(frappe.get_doc(PAYMENT_LOG, log), set()))

    def build_request(self, order: str, amount: float = 1000) -> str:
        """Raise a submitted Paystack Payment Request that carries no log.

        Submitting one runs get_payment_url(), which raises a log pointing back
        at it. That log would claim the request, and a claimed request is no
        candidate, so it is dropped: the backfill is for pre-bridge requests.
        """
        doc = frappe.get_doc("Sales Order", order)
        request = build_payment_request(doc, amount, "buyer@example.com")
        self.addCleanup(cleanup_doc, PAYMENT_REQUEST, request.name)
        self.drop_bridge_logs(request.name)
        return request.name

    def drop_bridge_logs(self, request: str) -> None:
        """Delete the payment logs that submitting a Payment Request raised."""
        for log in frappe.get_all(PAYMENT_LOG, filters={"payment_request": request}, pluck="name"):
            frappe.delete_doc(PAYMENT_LOG, log, force=True, ignore_permissions=True)

    def build_log(self, order: str, amount: float = 1000, linked_doctype: str = "Sales Order") -> str:
        """Create a payment log that carries no Payment Request."""
        log = PaymentLogFactory.create(linked_doctype=linked_doctype, linked_docname=order, amount=amount)
        self.addCleanup(self.cleanup_log, log)
        return log

    def cleanup_log(self, log: str) -> None:
        """Delete a payment log, leaving its linked document to its factory."""
        if not frappe.db.exists(PAYMENT_LOG, log):
            return

        frappe.db.set_value(PAYMENT_LOG, log, {"status": "Pending", "payment_entry": None})
        cleanup_doc(PAYMENT_LOG, log)

    def request_on(self, log: str) -> str:
        """Return the Payment Request a log points at."""
        return frappe.db.get_value(PAYMENT_LOG, log, "payment_request")


class TestUnlinkedLogs(BackfillTestCase):
    """The selection only offers up logs the backfill can act on."""

    def test_a_log_without_a_request_is_selected(self) -> None:
        """A log carrying no Payment Request is selected."""
        log = self.build_log(self.build_order())

        self.assertIn(log, [row.name for row in unlinked_logs(TEST_COMPANY)])

    def test_a_log_that_already_carries_a_request_is_skipped(self) -> None:
        """A log that already carries a Payment Request is left out."""
        order = self.build_order()
        log = self.build_log(order)
        frappe.db.set_value(PAYMENT_LOG, log, "payment_request", self.build_request(order))

        self.assertNotIn(log, [row.name for row in unlinked_logs(TEST_COMPANY)])

    def test_a_pos_invoice_log_is_skipped(self) -> None:
        """A log billing a POS Invoice is left out of the selection."""
        invoice = POSInvoiceFactory.create()
        self.addCleanup(POSInvoiceFactory.cleanup, invoice)
        log = self.build_log(invoice, linked_doctype="POS Invoice")

        self.assertNotIn(log, [row.name for row in unlinked_logs(TEST_COMPANY)])

    def test_another_companys_logs_are_out_of_scope(self) -> None:
        """The selection is scoped to the company it is given."""
        self.build_log(self.build_order())

        self.assertEqual(unlinked_logs("Nonexistent Company"), [])


class TestCandidateRequests(BackfillTestCase):
    """Only an unambiguous, same-amount Paystack request is a candidate."""

    def test_a_request_for_the_same_amount_is_a_candidate(self) -> None:
        """A request for the same amount is offered as a candidate."""
        order = self.build_order()
        request = self.build_request(order)
        log = frappe.get_doc(PAYMENT_LOG, self.build_log(order))

        self.assertEqual(candidate_requests(log, set()), [request])

    def test_a_claimed_request_is_not_offered_twice(self) -> None:
        """A request already claimed in this run is left out of the candidates."""
        order = self.build_order()
        request = self.build_request(order)
        log = frappe.get_doc(PAYMENT_LOG, self.build_log(order))

        self.assertEqual(candidate_requests(log, {request}), [])

    def test_a_request_another_log_holds_is_not_a_candidate(self) -> None:
        """A request another log holds is left out of the candidates."""
        order = self.build_order()
        request = self.build_request(order)
        held = self.build_log(order)
        frappe.db.set_value(PAYMENT_LOG, held, "payment_request", request)
        log = frappe.get_doc(PAYMENT_LOG, self.build_log(order))

        self.assertEqual(candidate_requests(log, set()), [])

    def test_a_request_for_a_different_amount_is_not_a_candidate(self) -> None:
        """A request for a different amount is left out of the candidates."""
        order = self.build_order()
        self.build_request(order)
        log = frappe.get_doc(PAYMENT_LOG, self.build_log(order, amount=250))

        self.assertEqual(candidate_requests(log, set()), [])

    def test_a_log_with_no_amount_has_no_candidate(self) -> None:
        """A log with no amount has no candidates."""
        order = self.build_order()
        self.build_request(order)
        log = frappe.get_doc(PAYMENT_LOG, self.build_log(order))
        log.amount = 0
        log.amount_paid = 0

        self.assertEqual(candidate_requests(log, set()), [])

    def test_the_charged_amount_stands_in_for_a_missing_amount(self) -> None:
        """amount_paid carries the match when amount is zero."""
        order = self.build_order()
        request = self.build_request(order)
        log = frappe.get_doc(PAYMENT_LOG, self.build_log(order))
        log.amount = 0
        log.amount_paid = 1000

        self.assertEqual(candidate_requests(log, set()), [request])

    def test_a_request_for_another_gateway_is_not_a_candidate(self) -> None:
        """A request naming another gateway is left out of the candidates."""
        order = self.build_order()
        request = self.build_request(order)
        frappe.db.set_value(PAYMENT_REQUEST, request, "payment_gateway", "Bank Draft")
        log = frappe.get_doc(PAYMENT_LOG, self.build_log(order))

        self.assertEqual(candidate_requests(log, set()), [])


class TestBackfill(BackfillTestCase):
    """backfill_payment_requests links what it can and reports the rest."""

    def test_a_pre_bridge_log_is_linked_to_its_request(self) -> None:
        """A log predating the bridge is linked to the Payment Request billing it."""
        order = self.build_order()
        request = self.build_request(order)
        log = self.build_log(order)

        report = backfill_payment_requests(TEST_COMPANY)

        self.assertEqual(self.request_on(log), request)
        self.assertIn(log, report["linked"])

    def test_a_log_with_no_request_to_match_is_reported(self) -> None:
        """A log with no request to match is reported as unmatched."""
        log = self.build_log(self.build_order())

        report = backfill_payment_requests(TEST_COMPANY)

        self.assertIn(log, report["unmatched"])
        self.assertFalse(self.request_on(log))

    def test_two_matching_requests_leave_the_log_alone(self) -> None:
        """Two matching requests report the log as ambiguous and leave it unlinked."""
        order = self.build_order(rate=2000)
        first = self.build_request(order, 1000)
        second = self.build_request(order, 1000)
        log = self.build_log(order)

        # ERPNext folds the second request into the first once the order has
        # nothing left to bill, which would leave one candidate, not two.
        self.assertNotEqual(first, second, "the order carried one Payment Request, not two")
        self.assertEqual(self.candidates_for(log), sorted([first, second]))

        report = backfill_payment_requests(TEST_COMPANY)

        self.assertIn(log, report["ambiguous"])
        self.assertFalse(self.request_on(log))

    def test_one_request_is_never_given_to_two_logs(self) -> None:
        """One request is linked to exactly one of two competing logs."""
        order = self.build_order()
        self.build_request(order)
        first = self.build_log(order)
        second = self.build_log(order)

        backfill_payment_requests(TEST_COMPANY)

        linked = [name for name in (first, second) if self.request_on(name)]
        self.assertEqual(len(linked), 1)

    def test_re_running_the_backfill_changes_nothing(self) -> None:
        """A second run keeps the link and reports nothing newly linked."""
        order = self.build_order()
        request = self.build_request(order)
        log = self.build_log(order)

        backfill_payment_requests(TEST_COMPANY)
        second = backfill_payment_requests(TEST_COMPANY)

        self.assertEqual(self.request_on(log), request)
        self.assertNotIn(log, second["linked"])

    def test_a_dry_run_writes_nothing(self) -> None:
        """A dry run reports the link it would make and writes nothing."""
        order = self.build_order()
        request = self.build_request(order)
        log = self.build_log(order)

        # One candidate and no other, or the run has no link to report.
        self.assertEqual(self.candidates_for(log), [request])

        report = backfill_payment_requests(TEST_COMPANY, dry_run=True)

        self.assertIn(log, report["linked"])
        self.assertFalse(self.request_on(log))

    def test_the_retry_window_is_not_disturbed(self) -> None:
        """The backfill leaves the log's modified timestamp as it stands."""
        order = self.build_order()
        self.build_request(order)
        log = self.build_log(order)
        before = frappe.db.get_value(PAYMENT_LOG, log, "modified")

        backfill_payment_requests(TEST_COMPANY)

        self.assertEqual(frappe.db.get_value(PAYMENT_LOG, log, "modified"), before)

    def test_every_log_examined_is_accounted_for(self) -> None:
        """The examined count equals the linked, ambiguous and unmatched counts."""
        order = self.build_order()
        self.build_request(order)
        self.build_log(order)

        report = backfill_payment_requests(TEST_COMPANY)

        self.assertEqual(
            report["examined"],
            len(report["linked"]) + len(report["ambiguous"]) + len(report["unmatched"]),
        )


class TestLinkageReport(BackfillTestCase):
    """The counts a migration is validated against."""

    def test_an_unlinked_log_is_counted_as_unlinked(self) -> None:
        """An unlinked log is counted in the unlinked total."""
        self.build_log(self.build_order())

        report = linkage_report(TEST_COMPANY)

        self.assertEqual(report["billable_logs"], report["linked"] + report["unlinked"])
        self.assertGreaterEqual(report["unlinked"], 1)

    def test_the_backfill_moves_a_log_from_unlinked_to_linked(self) -> None:
        """The backfill raises the linked count and holds the billable total."""
        order = self.build_order()
        self.build_request(order)
        self.build_log(order)
        before = linkage_report(TEST_COMPANY)

        backfill_payment_requests(TEST_COMPANY)
        after = linkage_report(TEST_COMPANY)

        self.assertEqual(after["billable_logs"], before["billable_logs"])
        self.assertGreater(after["linked"], before["linked"])
        self.assertEqual(after["linked"] + after["unlinked"], before["linked"] + before["unlinked"])

    def test_the_report_covers_every_company_by_default(self) -> None:
        """The report with no company covers at least one company's total."""
        self.build_log(self.build_order())

        self.assertGreaterEqual(
            linkage_report()["billable_logs"], linkage_report(TEST_COMPANY)["billable_logs"]
        )


class TestRevertBackfill(BackfillTestCase):
    """The documented rollback for a backfill that linked the wrong request."""

    def test_a_backfilled_link_is_cleared(self) -> None:
        """Reverting clears the backfilled Payment Request from the log."""
        order = self.build_order()
        self.build_request(order)
        log = self.build_log(order)
        backfill_payment_requests(TEST_COMPANY)

        reverted = revert_payment_request_backfill([log])

        self.assertEqual(reverted, [log])
        self.assertFalse(self.request_on(log))

    def test_a_log_that_has_since_settled_is_left_alone(self) -> None:
        """A log that has since booked a Payment Entry keeps its link."""
        order = self.build_order()
        request = self.build_request(order)
        log = self.build_log(order)
        backfill_payment_requests(TEST_COMPANY)
        frappe.db.set_value(PAYMENT_LOG, log, "payment_entry", "ACC-PAY-TEST-0001")

        reverted = revert_payment_request_backfill([log])

        self.assertEqual(reverted, [])
        self.assertEqual(self.request_on(log), request)

    def test_a_log_that_no_longer_exists_is_ignored(self) -> None:
        """A log name that no longer exists is left out of the reverted list."""
        self.assertEqual(revert_payment_request_backfill(["PAYSTACK-LOG-GONE"]), [])


class TestBackfillPatch(BackfillTestCase):
    """The patch reports the linkage on either side of a stubbed backfill."""

    def test_the_patch_records_the_counts_around_the_backfill(self) -> None:
        """The patch takes a linkage report either side of one backfill call."""
        counts = {"billable_logs": 2, "linked": 1, "unlinked": 1}

        with patch(f"{PATCH_MODULE}.linkage_report", return_value=counts) as report:
            with patch(
                f"{PATCH_MODULE}.backfill_payment_requests",
                return_value={"examined": 0, "linked": [], "ambiguous": [], "unmatched": []},
            ) as backfill:
                execute()

        self.assertEqual(report.call_count, 2)
        backfill.assert_called_once_with()

    def test_the_patch_is_scoped_to_the_backfill(self) -> None:
        """The patch leaves the log's amount and its Payment Request untouched."""
        order = self.build_order()
        log = self.build_log(order)
        amount = flt(frappe.db.get_value(PAYMENT_LOG, log, "amount"))

        with patch(
            f"{PATCH_MODULE}.backfill_payment_requests",
            return_value={"examined": 0, "linked": [], "ambiguous": [], "unmatched": []},
        ):
            execute()

        self.assertEqual(flt(frappe.db.get_value(PAYMENT_LOG, log, "amount")), amount)
        self.assertFalse(self.request_on(log))


class TestPatchLogRename(PaystackTestCase):
    """The Patch Log follows a patch that moves to a new path."""

    def record(self, path: str) -> None:
        """Record a patch as applied, the way patch_handler does."""
        row = frappe.get_doc({"doctype": PATCH_LOG, "patch": path}).insert(ignore_permissions=True)
        self.addCleanup(frappe.delete_doc, PATCH_LOG, row.name, force=True)

    def applied(self, path: str) -> bool:
        """Return whether Frappe would treat this patch as already run."""
        return bool(frappe.db.exists(PATCH_LOG, {"patch": path}))

    def test_a_moved_patch_keeps_its_applied_state(self) -> None:
        """A moved patch carries its applied state to the new path."""
        old, new = self.pair()
        self.record(old)

        renamed = rename_patch_log_entries()

        self.assertEqual(renamed, [new])
        self.assertTrue(self.applied(new))
        self.assertFalse(self.applied(old))

    def test_a_patch_already_recorded_under_the_new_path_is_left_alone(self) -> None:
        """A patch recorded under both paths keeps both rows and renames nothing."""
        old, new = self.pair()
        self.record(old)
        self.record(new)

        self.assertEqual(rename_patch_log_entries(), [])
        self.assertTrue(self.applied(old))
        self.assertTrue(self.applied(new))

    def test_a_patch_that_never_ran_is_not_marked_applied(self) -> None:
        """A patch that never ran stays unrecorded under the new path."""
        _, new = self.pair()

        self.assertEqual(rename_patch_log_entries(), [])
        self.assertFalse(self.applied(new))

    def pair(self) -> tuple:
        """Return a throwaway old/new path pair, installed as the only rename."""
        suffix = random_string(8)
        old = f"frappe_paystack.patches.v0_0_0.moved_{suffix}"
        new = f"frappe_paystack.patches.v15_0.moved_{suffix}"
        patcher = patch.dict("frappe_paystack.migration.RENAMED_PATCHES", {old: new}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        return old, new


class TestPatchesTxt(PaystackTestCase):
    """Every line of patches.txt names something Frappe can run."""

    def test_every_listed_patch_resolves(self) -> None:
        """Every listed patch resolves to a callable execute."""
        for entry in listed_patches():
            self.assertTrue(callable(frappe.get_attr(f"{entry}.execute")), entry)

    def test_every_rename_target_is_listed(self) -> None:
        """Every rename target appears in patches.txt."""
        self.assertLessEqual(set(RENAMED_PATCHES.values()), set(listed_patches()))


class TestPatchDirectory(PaystackTestCase):
    """A patch that predates the directory rename is still reached by its old path."""

    def test_every_listed_patch_answers_to_the_former_directory(self) -> None:
        """A Patch Log row under the former directory names the patch it is now."""
        for entry in listed_patches():
            if entry in RENAME_PATCHES + LATER_PATCHES:
                continue

            with self.subTest(patch=entry):
                former = f"{FORMER_PATCH_DIRECTORY}.{entry.rsplit('.', 1)[-1]}"
                self.assertEqual(RENAMED_PATCHES.get(former), entry)


class TestScheduledJobRename(PaystackTestCase):
    """The scheduler follows a job whose module moves."""

    def registered(self, method: str) -> bool:
        """Return whether the scheduler holds a job under this method."""
        return bool(frappe.db.exists(SCHEDULED_JOB_TYPE, {"method": method}))

    def record(self, method: str) -> None:
        """Register a scheduled job the way sync_jobs does."""
        row = frappe.get_doc({"doctype": SCHEDULED_JOB_TYPE, "method": method, "frequency": "Daily"}).insert(
            ignore_permissions=True
        )
        self.addCleanup(frappe.delete_doc, SCHEDULED_JOB_TYPE, row.name, force=True)

    def pair(self) -> tuple:
        """Return a throwaway old/new method pair, installed as the only rename."""
        suffix = random_string(8)
        old = f"frappe_paystack.helpers.moved_{suffix}.collect"
        new = f"frappe_paystack.utils.moved_{suffix}.collect"
        patcher = patch.dict("frappe_paystack.migration.RENAMED_JOBS", {old: new}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        return old, new

    def test_a_moved_job_keeps_its_registration(self) -> None:
        """A registered job carries its row to the method it answers to now."""
        old, new = self.pair()
        self.record(old)

        self.assertEqual(rename_scheduled_jobs(), [new])
        self.assertTrue(self.registered(new))
        self.assertFalse(self.registered(old))

    def test_a_second_pass_writes_nothing(self) -> None:
        """A job already repointed is left alone by a later pass."""
        old, new = self.pair()
        self.record(old)
        rename_scheduled_jobs()

        self.assertEqual(rename_scheduled_jobs(), [])
        self.assertTrue(self.registered(new))

    def test_a_moved_job_keeps_the_row_it_was_registered_under(self) -> None:
        """The row keeps the name it was registered under."""
        old, _ = self.pair()
        self.record(old)
        before = frappe.db.get_value(SCHEDULED_JOB_TYPE, {"method": old}, "name")

        rename_scheduled_jobs()

        self.assertEqual(frappe.db.get_value(SCHEDULED_JOB_TYPE, {"method": old}, "name"), None)
        self.assertTrue(frappe.db.exists(SCHEDULED_JOB_TYPE, before))

    def test_a_job_that_was_never_registered_stays_unregistered(self) -> None:
        """An unregistered job is not created by the rename."""
        _, new = self.pair()

        self.assertEqual(rename_scheduled_jobs(), [])
        self.assertFalse(self.registered(new))

    def test_every_scheduled_hook_in_the_moved_package_is_covered(self) -> None:
        """Every scheduler entry under the moved package is a rename target."""
        for method in scheduled_methods():
            if not method.startswith(f"{JOB_HOME}."):
                continue

            with self.subTest(method=method):
                self.assertIn(method, set(RENAMED_JOBS.values()))


class TestScheduledJobRemoval(PaystackTestCase):
    """A job the app stopped scheduling loses the row that still names it."""

    def registered(self, method: str) -> bool:
        """Return whether the scheduler holds a job under this method."""
        return bool(frappe.db.exists(SCHEDULED_JOB_TYPE, {"method": method}))

    def record(self, method: str) -> None:
        """Register a scheduled job the way sync_jobs does."""
        row = frappe.get_doc({"doctype": SCHEDULED_JOB_TYPE, "method": method, "frequency": "Weekly"}).insert(
            ignore_permissions=True
        )
        self.addCleanup(cleanup_doc, SCHEDULED_JOB_TYPE, row.name)

    def dropped(self) -> str:
        """Return a throwaway method, installed as the only removal."""
        method = f"frappe_paystack.utils.gone_{random_string(8)}.report"
        patcher = patch("frappe_paystack.migration.REMOVED_JOBS", (method,))
        patcher.start()
        self.addCleanup(patcher.stop)
        return method

    def test_a_dropped_job_loses_its_row(self) -> None:
        """A registered job the app no longer schedules is deleted."""
        method = self.dropped()
        self.record(method)

        self.assertEqual(drop_removed_scheduled_jobs(), [method])
        self.assertFalse(self.registered(method))

    def test_a_second_pass_deletes_nothing(self) -> None:
        """A job already dropped is not reported again."""
        method = self.dropped()
        self.record(method)
        drop_removed_scheduled_jobs()

        self.assertEqual(drop_removed_scheduled_jobs(), [])

    def test_a_job_that_was_never_registered_is_left_alone(self) -> None:
        """An unregistered job reports nothing."""
        self.dropped()

        self.assertEqual(drop_removed_scheduled_jobs(), [])

    def test_the_weekly_report_is_named_under_every_path_it_carried(self) -> None:
        """Both packages the weekly report answered to are removal targets."""
        for home in (FORMER_JOB_HOME, JOB_HOME):
            with self.subTest(home=home):
                self.assertIn(
                    f"{home}.scheduled_jobs.generate_weekly_reconciliation_report",
                    REMOVED_JOBS,
                )

    def test_a_removed_job_is_no_longer_a_rename_target(self) -> None:
        """Nothing points a live row at a job the app dropped."""
        self.assertFalse(set(REMOVED_JOBS) & set(RENAMED_JOBS.values()))


class TestManualOverrideRename(PaystackTestCase):
    """A reconciliation a human settled keeps its status through the rename."""

    def setUp(self) -> None:
        """Reconcile one payment, so there is a row to move."""
        super().setUp()
        self.gateway_name = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway_name)

        self.log = PaymentLogFactory.create(status="Processed", amount=1000, amount_paid=1000)
        self.addCleanup(PaymentLogFactory.cleanup, self.log)

        row = frappe.get_doc(
            {
                "doctype": RECONCILIATION_LOG,
                "reconciliation_date": frappe.utils.today(),
                "payment_log": self.log,
                "company": TEST_COMPANY,
                "status": "Mismatch",
            }
        )
        row.flags.ignore_permissions = True
        row.insert()
        self.addCleanup(cleanup_doc, RECONCILIATION_LOG, row.name)
        self.reconciliation = row.name

    def force_status(self, status: str) -> None:
        """Write a status straight to the row, past the field's options."""
        frappe.db.set_value(RECONCILIATION_LOG, self.reconciliation, "status", status)
        frappe.clear_document_cache(RECONCILIATION_LOG, self.reconciliation)

    def status(self) -> str:
        """Return the status the reconciliation row carries."""
        return frappe.db.get_value(RECONCILIATION_LOG, self.reconciliation, "status")

    def test_a_row_under_the_former_status_is_moved(self) -> None:
        """A row carrying the former value ends up on the current one."""
        self.force_status(FORMER_MANUAL_OVERRIDE)

        self.assertEqual(rename_manual_overrides(), [self.reconciliation])
        self.assertEqual(self.status(), MANUAL_OVERRIDE)

    def test_a_row_already_moved_is_not_touched_again(self) -> None:
        """A second pass selects nothing and leaves the status alone."""
        self.force_status(MANUAL_OVERRIDE)

        self.assertEqual(rename_manual_overrides(), [])
        self.assertEqual(self.status(), MANUAL_OVERRIDE)

    def test_a_row_a_machine_set_is_left_alone(self) -> None:
        """A reconciliation no human overrode keeps its status."""
        self.assertEqual(rename_manual_overrides(), [])
        self.assertEqual(self.status(), "Mismatch")

    def test_the_field_offers_the_status_the_rename_writes(self) -> None:
        """The Select field lists the value the rows are moved to."""
        options = frappe.get_meta(RECONCILIATION_LOG).get_field("status").options

        self.assertIn(MANUAL_OVERRIDE, options.split("\n"))
