"""The workspace number cards, and the alert titles the Error Log card counts."""

import ast
import json
import os

import frappe

import frappe_paystack
from frappe_paystack.setup import ERROR_TITLE_PREFIX, NUMBER_CARDS
from frappe_paystack.tests.factories import PaymentLogFactory, SettlementFactory
from frappe_paystack.tests.test_base import PaystackTestCase
from frappe_paystack.utils import log_integration_request

ERROR_LOG = "Error Log"
INTEGRATION_REQUEST = "Integration Request"
SETTLEMENT = "Paystack Settlement"
PAYMENT_LOG = "Paystack Payment Log"

FAILED_CALLS_CARD = "Paystack Failed API Calls"
UNREVIEWED_ERRORS_CARD = "Paystack Errors Unreviewed"
UNBOOKED_PAYOUTS_CARD = "Paystack Payouts Not Booked"
AWAITING_SETTLEMENT_CARD = "Paystack Awaiting Settlement"

# The calls that put an Error Log on the record they trace.
ERROR_CALLS = ("log_error_for", "record_failure", "log_error")

# Package folders that hold no production code.
UNSCANNED = ("tests", "patches")


def called_name(node: ast.AST) -> str:
    """Return the bare name of whatever a Call node calls."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def literal_title(node: ast.AST) -> str:
    """Return the fixed text an Error Log title starts with, or nothing."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value

    if isinstance(node, ast.JoinedStr) and node.values:
        return literal_title(node.values[0])

    if isinstance(node, ast.Call) and called_name(node.func) == "_" and node.args:
        return literal_title(node.args[0])

    return ""


def call_title(node: ast.Call) -> str:
    """Return the title an Error Log call was given, keyword or positional."""
    for keyword in node.keywords:
        if keyword.arg == "title":
            return literal_title(keyword.value)

    return literal_title(node.args[0]) if node.args else ""


def source_files() -> list:
    """Return the app's production python sources."""
    root = os.path.dirname(frappe_paystack.__file__)

    paths = []
    for folder, subfolders, files in os.walk(root):
        subfolders[:] = [name for name in subfolders if name not in UNSCANNED]
        paths.extend(os.path.join(folder, name) for name in files if name.endswith(".py"))
    return sorted(paths)


def raised_titles() -> list:
    """Return every literal Error Log title the app raises, with its source."""
    titles = []

    for path in source_files():
        with open(path, encoding="utf-8") as source:
            tree = ast.parse(source.read(), filename=path)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if called_name(node.func) not in ERROR_CALLS:
                continue

            title = call_title(node)
            if title:
                titles.append((path, node.lineno, title))

    return titles


class NumberCardTestCase(PaystackTestCase):
    """Runs a card's filters the way the desk runs them."""

    def setUp(self) -> None:
        """Track the Error Logs raised here."""
        super().setUp()
        self.alerts = []
        self.addCleanup(self.remove_alerts)

    def card(self, name: str) -> dict:
        """Return the shipped definition of one card."""
        return next(card for card in NUMBER_CARDS if card["name"] == name)

    def counted(self, card_name: str, record: str) -> bool:
        """Report whether a record falls inside the rows a card counts."""
        card = self.card(card_name)
        filters = json.loads(card["filters_json"])
        filters.append([card["document_type"], "name", "=", record])

        return bool(frappe.get_list(card["document_type"], filters=filters, pluck="name", order_by=None))

    def alert(self, title: str, seen: int = 0) -> str:
        """Raise an Error Log with the given title and seen flag."""
        error_log = frappe.get_doc(
            {
                "doctype": ERROR_LOG,
                "method": title,
                "error": "raised by a health card test",
                "seen": seen,
            }
        )
        error_log.flags.ignore_permissions = True
        error_log.insert()
        self.alerts.append(error_log.name)
        return error_log.name

    def remove_alerts(self) -> None:
        """Delete only the Error Logs this test raised."""
        for name in self.alerts:
            frappe.delete_doc(ERROR_LOG, name, force=True, ignore_permissions=True)
        frappe.db.commit()

    def foreign_request(self) -> str:
        """File a failed Integration Request that belongs to another service."""
        request = frappe.get_doc(
            {
                "doctype": INTEGRATION_REQUEST,
                "integration_request_service": "Razorpay",
                "status": "Failed",
                "url": "https://api.razorpay.com/charge",
                "error": "not ours",
            }
        )
        request.flags.ignore_permissions = True
        request.insert()
        self.track_doc(INTEGRATION_REQUEST, request.name)
        return request.name


class TestFailedApiCallsCard(NumberCardTestCase):
    """The Integration Requests behind a forged webhook or a refused call."""

    def test_a_failed_paystack_call_is_counted(self) -> None:
        """A failed Paystack Integration Request is counted."""
        request = log_integration_request(
            status="Failed",
            url="webhook",
            request_data={},
            error="Invalid Paystack signature",
        )

        self.assertTrue(self.counted(FAILED_CALLS_CARD, request))

    def test_a_successful_paystack_call_is_not_counted(self) -> None:
        """A completed Paystack Integration Request stays off the card."""
        request = log_integration_request(status="Completed", url="webhook", request_data={})

        self.assertFalse(self.counted(FAILED_CALLS_CARD, request))

    def test_another_services_failure_is_not_counted(self) -> None:
        """A failed request from another service stays off the card."""
        request = self.foreign_request()

        self.assertFalse(self.counted(FAILED_CALLS_CARD, request))


class TestUnreviewedErrorsCard(NumberCardTestCase):
    """The alerts this app raises, counted only while nobody has read them."""

    def test_an_unread_paystack_alert_is_counted(self) -> None:
        """An unread Paystack alert is counted."""
        alert = self.alert("Paystack webhook: sustained signature rejections")

        self.assertTrue(self.counted(UNREVIEWED_ERRORS_CARD, alert))

    def test_a_read_alert_drops_off_the_card(self) -> None:
        """An alert with seen set drops off the card."""
        alert = self.alert("Paystack webhook: sustained signature rejections", seen=1)

        self.assertFalse(self.counted(UNREVIEWED_ERRORS_CARD, alert))

    def test_another_apps_error_is_not_counted(self) -> None:
        """An Error Log from another app stays off the card."""
        alert = self.alert("Scheduler job failed")

        self.assertFalse(self.counted(UNREVIEWED_ERRORS_CARD, alert))


class TestUnbookedPayoutsCard(NumberCardTestCase):
    """Payouts Paystack has paid out that the ledger has not taken up."""

    def test_a_payout_with_no_journal_entry_is_counted(self) -> None:
        """A payout with no journal entry is counted."""
        payout = SettlementFactory.create()
        self.addCleanup(SettlementFactory.cleanup, payout)

        self.assertTrue(self.counted(UNBOOKED_PAYOUTS_CARD, payout))

    def test_a_booked_payout_is_not_counted(self) -> None:
        """A payout carrying a journal entry drops off the card."""
        payout = SettlementFactory.create()
        self.addCleanup(SettlementFactory.cleanup, payout)
        frappe.db.set_value(SETTLEMENT, payout, "journal_entry", "JE-CARD-TEST")

        self.assertFalse(self.counted(UNBOOKED_PAYOUTS_CARD, payout))


class TestStuckPaymentsAreAlreadySurfaced(NumberCardTestCase):
    """The Awaiting Settlement card counts Processed logs with no Payment Entry."""

    def test_the_waiting_card_already_counts_a_stuck_log(self) -> None:
        """A Processed log with no Payment Entry is counted by the card."""
        log = PaymentLogFactory.create()
        self.addCleanup(PaymentLogFactory.cleanup, log)
        frappe.db.set_value(PAYMENT_LOG, log, "status", "Processed")

        self.assertTrue(self.counted(AWAITING_SETTLEMENT_CARD, log))

    def test_a_booked_log_leaves_the_waiting_card(self) -> None:
        """A Processed log carrying a Payment Entry drops off the card."""
        log = PaymentLogFactory.create()
        self.addCleanup(PaymentLogFactory.cleanup, log)
        frappe.db.set_value(
            PAYMENT_LOG,
            log,
            {"status": "Processed", "payment_entry": "PE-CARD-TEST"},
        )

        self.assertFalse(self.counted(AWAITING_SETTLEMENT_CARD, log))


class TestErrorTitleConvention(PaystackTestCase):
    """The unreviewed-errors card matches alert titles by prefix."""

    def test_every_alert_the_app_raises_carries_the_prefix(self) -> None:
        """Every literal alert title in the app opens with the card's prefix."""
        for path, lineno, title in raised_titles():
            with self.subTest(source=f"{os.path.basename(path)}:{lineno}"):
                self.assertEqual(title[: len(ERROR_TITLE_PREFIX)], ERROR_TITLE_PREFIX)

    def test_the_scan_reaches_the_titles_it_is_guarding(self) -> None:
        """The scan finds at least 25 alert titles."""
        self.assertGreaterEqual(len(raised_titles()), 25)

    def test_the_scan_skips_the_folders_that_raise_nothing(self) -> None:
        """The scan covers no folder listed in UNSCANNED."""
        scanned = {os.path.basename(os.path.dirname(path)) for path in source_files()}

        self.assertEqual(scanned & set(UNSCANNED), set())
