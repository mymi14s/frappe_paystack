"""Tests for what a rejected webhook records: the throttle and forged signatures."""

from unittest.mock import patch

import frappe

from frappe_paystack.api import (
    WEBHOOK_RATE_WINDOW,
    count_in_window,
    global_rate_limit_exceeded,
    paystack_webhook,
    rate_limited_webhook,
    record_rate_limit_violation,
    record_signature_rejection,
    webhook_rate_key,
)
from frappe_paystack.tests.test_base import PaystackTestCase

INTEGRATION_REQUEST = "Integration Request"
ERROR_LOG = "Error Log"

ALERT_TITLE = "Paystack webhook rate limit: sustained violations"
SIGNATURE_ALERT_TITLE = "Paystack webhook: sustained signature rejections"

WINDOW_SCOPES = ("global", "violations", "signatures")


def clear_windows() -> None:
    """Drop this window's counters so a test starts from zero."""
    for scope in WINDOW_SCOPES:
        frappe.cache.delete(webhook_rate_key(scope))


class WebhookThrottleTestCase(PaystackTestCase):
    """Every test starts and ends with empty window counters."""

    def setUp(self) -> None:
        """Reset the shared redis counters around each test."""
        super().setUp()
        clear_windows()
        self.addCleanup(clear_windows)
        # Alerts outlive the rollback and are removed by name.
        self.addCleanup(self.remove_alerts)

    def throttle_requests(self) -> list:
        """Return the Integration Requests this test filed for a rejection."""
        return frappe.get_all(
            INTEGRATION_REQUEST,
            filters={"url": "webhook", "creation": [">=", self.started_at]},
            fields=["name", "error"],
        )

    def alerts(self, title: str = ALERT_TITLE) -> list:
        """Return the alerts raised since this test started."""
        return frappe.get_all(
            ERROR_LOG,
            filters={"method": title, "creation": [">=", self.started_at]},
            pluck="name",
        )

    def remove_alerts(self) -> None:
        """Delete only the alerts this test raised."""
        for title in (ALERT_TITLE, SIGNATURE_ALERT_TITLE):
            for name in self.alerts(title):
                frappe.delete_doc(ERROR_LOG, name, force=True, ignore_permissions=True)
        frappe.db.commit()


class TestWindowCounter(WebhookThrottleTestCase):
    """The counters are fixed one-minute windows keyed in redis."""

    def test_a_counter_starts_at_one(self) -> None:
        """The first request in a window counts one."""
        self.assertEqual(count_in_window("global"), 1)

    def test_a_counter_accumulates_within_one_window(self) -> None:
        """Counting twice in the same window reaches two."""
        count_in_window("global")

        self.assertEqual(count_in_window("global"), 2)

    def test_a_counter_expires_with_its_window(self) -> None:
        """The key is given the window as its time to live."""
        count_in_window("global")

        self.assertLessEqual(frappe.cache.ttl(webhook_rate_key("global")), WEBHOOK_RATE_WINDOW)

    def test_each_window_owns_its_key(self) -> None:
        """A key from the next window is a different key."""
        with patch("frappe_paystack.api.time") as clock:
            clock.time.return_value = 0
            first = webhook_rate_key("global")
            clock.time.return_value = WEBHOOK_RATE_WINDOW
            second = webhook_rate_key("global")

        self.assertNotEqual(first, second)

    def test_the_scopes_are_counted_apart(self) -> None:
        """Each scope carries a count of its own."""
        count_in_window("global")

        self.assertEqual(count_in_window("violations"), 1)


class TestGlobalCeiling(WebhookThrottleTestCase):
    """The app-wide cap on webhook traffic inside one window."""

    def test_traffic_under_the_ceiling_passes(self) -> None:
        """A single request passes the ceiling check."""
        self.assertFalse(global_rate_limit_exceeded())

    def test_the_ceiling_is_the_last_accepted_request(self) -> None:
        """The request at the limit passes and the one after it is rejected."""
        with patch("frappe_paystack.api.WEBHOOK_GLOBAL_LIMIT", 1):
            self.assertFalse(global_rate_limit_exceeded())
            self.assertTrue(global_rate_limit_exceeded())

    def test_a_flooded_webhook_is_rejected(self) -> None:
        """Past the ceiling the endpoint raises RateLimitExceededError."""
        with patch("frappe_paystack.api.WEBHOOK_GLOBAL_LIMIT", 0):
            with self.assertRaises(frappe.RateLimitExceededError):
                paystack_webhook()

    def test_a_flooded_webhook_never_reads_its_payload(self) -> None:
        """The rejection lands before the payload is read."""
        with (
            patch("frappe_paystack.api.WEBHOOK_GLOBAL_LIMIT", 0),
            patch("frappe_paystack.api.get_webhook_request_data") as mock_request,
        ):
            with self.assertRaises(frappe.RateLimitExceededError):
                paystack_webhook()

        mock_request.assert_not_called()


class TestViolationLogging(WebhookThrottleTestCase):
    """A throttled request leaves an audit row."""

    def test_a_global_rejection_is_logged(self) -> None:
        """The Integration Request names the ceiling that rejected it."""
        with patch("frappe_paystack.api.WEBHOOK_GLOBAL_LIMIT", 0):
            with self.assertRaises(frappe.RateLimitExceededError):
                paystack_webhook()

        errors = [row.error for row in self.throttle_requests()]
        self.assertTrue(any("(global)" in (error or "") for error in errors))

    def test_a_per_ip_rejection_is_logged(self) -> None:
        """A per-IP rejection is recorded as an Integration Request."""

        def throttled() -> None:
            raise frappe.RateLimitExceededError

        with self.assertRaises(frappe.RateLimitExceededError):
            rate_limited_webhook(throttled)()

        errors = [row.error for row in self.throttle_requests()]
        self.assertTrue(any("(ip)" in (error or "") for error in errors))

    def test_an_unthrottled_webhook_logs_no_violation(self) -> None:
        """A request that gets through leaves no rejection row."""
        rate_limited_webhook(lambda: None)()

        self.assertEqual(self.throttle_requests(), [])

    def test_the_source_ip_is_recorded(self) -> None:
        """A missing source IP is recorded as "IP unknown"."""
        record_rate_limit_violation("global", None)

        errors = [row.error for row in self.throttle_requests()]
        self.assertTrue(any("IP unknown" in (error or "") for error in errors))

    def test_a_known_source_ip_is_recorded(self) -> None:
        """The address that was throttled is on the audit row."""
        record_rate_limit_violation("ip", "203.0.113.9")

        errors = [row.error for row in self.throttle_requests()]
        self.assertTrue(any("203.0.113.9" in (error or "") for error in errors))


class TestViolationAlert(WebhookThrottleTestCase):
    """Sustained throttling raises one Error Log."""

    def test_one_violation_does_not_alert(self) -> None:
        """A single violation below the threshold raises no alert."""
        with patch("frappe_paystack.api.WEBHOOK_VIOLATION_ALERT", 2):
            record_rate_limit_violation("ip", "203.0.113.1")

        self.assertEqual(self.alerts(), [])

    def test_repeated_violations_alert_once(self) -> None:
        """Crossing the threshold raises one alert, however many follow."""
        with patch("frappe_paystack.api.WEBHOOK_VIOLATION_ALERT", 2):
            for attempt in range(4):
                record_rate_limit_violation("ip", f"203.0.113.{attempt}")

        self.assertEqual(len(self.alerts()), 1)

    def test_the_alert_outlives_the_rollback_that_follows_it(self) -> None:
        """The alert survives a rollback of the request that raised it."""
        with patch("frappe_paystack.api.WEBHOOK_VIOLATION_ALERT", 1):
            record_rate_limit_violation("ip", "203.0.113.7")
        frappe.db.rollback()

        self.assertEqual(len(self.alerts()), 1)


class TestSignatureRejection(WebhookThrottleTestCase):
    """A payload no gateway can have signed is audited, then alerted on."""

    def test_a_rejection_is_audited(self) -> None:
        """The Integration Request names the signature as the reason."""
        record_signature_rejection({"event": "charge.success"}, "203.0.113.20")

        errors = [row.error for row in self.throttle_requests()]
        self.assertIn("Invalid Paystack signature", errors)

    def test_one_rejection_does_not_alert(self) -> None:
        """A single rejection below the threshold raises no alert."""
        with patch("frappe_paystack.api.WEBHOOK_SIGNATURE_ALERT", 2):
            record_signature_rejection({}, "203.0.113.21")

        self.assertEqual(self.alerts(SIGNATURE_ALERT_TITLE), [])

    def test_repeated_rejections_alert_once(self) -> None:
        """Crossing the threshold raises one alert, however many follow."""
        with patch("frappe_paystack.api.WEBHOOK_SIGNATURE_ALERT", 2):
            for attempt in range(4):
                record_signature_rejection({}, f"203.0.113.{attempt}")

        self.assertEqual(len(self.alerts(SIGNATURE_ALERT_TITLE)), 1)

    def test_an_unknown_source_ip_is_named_as_such(self) -> None:
        """A rejection with no source IP reports "IP: unknown"."""
        with patch("frappe_paystack.api.WEBHOOK_SIGNATURE_ALERT", 1):
            record_signature_rejection({}, None)

        alert = frappe.get_doc(ERROR_LOG, self.alerts(SIGNATURE_ALERT_TITLE)[0])
        self.assertIn("IP: unknown", alert.error)

    def test_a_forged_webhook_reaches_the_recorder(self) -> None:
        """A forged webhook at the endpoint reaches the recorder and alerts."""
        with (
            patch("frappe_paystack.api.get_webhook_request_data") as mock_request,
            patch("frappe_paystack.api.resolve_settings_for_signature", return_value=None),
            patch("frappe_paystack.api.WEBHOOK_SIGNATURE_ALERT", 1),
        ):
            mock_request.return_value = ({}, b"{}", "forged", "203.0.113.22")
            with self.assertRaises(frappe.PermissionError):
                paystack_webhook()

        self.assertEqual(len(self.alerts(SIGNATURE_ALERT_TITLE)), 1)
