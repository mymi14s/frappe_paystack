"""Tests for settlement: the payout that clears suspense, and the fee that falls out of it."""

import json
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import frappe
from frappe.utils import add_days, flt, today
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from frappe_paystack.api import paystack_webhook, process_webhook_event
from frappe_paystack.frappe_paystack.report.paystack_settlements_vs_ledger import (
    paystack_settlements_vs_ledger as ledger_report,
)
from frappe_paystack.frappe_paystack.report.paystack_unsettled_payments import (
    paystack_unsettled_payments as unsettled_report,
)
from frappe_paystack.tests.factories import (
    TEST_COMPANY,
    GatewaySettingFactory,
    LedgerAccountFactory,
    PaymentLogFactory,
    SettlementFactory,
)
from frappe_paystack.tests.test_base import PaystackTestCase, marked_translation
from frappe_paystack.utils import hmac_sha512
from frappe_paystack.utils.settlement import (
    SETTLEMENT_DOCTYPE,
    TRANSACTION_PAGE_SIZE,
    build_settlement_entry,
    discard_draft_entry,
    enqueue_transaction_linkage,
    link_settled_payments,
    missing_settlement_accounts,
    post_settlement_entry,
    process_settlement_webhook_event,
    retry_unposted_settlements,
    settlement_gateway,
    settlement_transactions,
    unpostable_reason,
    unposted_settlements,
)

PAYMENT_LOG = "Paystack Payment Log"
GATEWAY_DOCTYPE = "Paystack Gateway Setting"

SETTLEMENT_MODULE = "frappe_paystack.utils.settlement"
GATEWAY_MODULE = "frappe_paystack.frappe_paystack.doctype.paystack_gateway_setting.paystack_gateway_setting"

LEDGER_REPORT_MODULE = (
    "frappe_paystack.frappe_paystack.report.paystack_settlements_vs_ledger.paystack_settlements_vs_ledger"
)

# Accounts the payout entry touches.
BANK_ACCOUNT = "Test Paystack Settlement Bank"
FEE_ACCOUNT = "Test Paystack Fees"
SUSPENSE_ACCOUNT = "Test Paystack Suspense"

# A documentation-only address.
WEBHOOK_CALLER_IP = "203.0.113.91"

# The second tenant these tests check a payout against.
OTHER_COMPANY = "_Test Company 2"


def payout_payload(
    settlement_id: str = "770001",
    event: str = "settlement.success",
    total_amount: int = 100000,
    total_fees: int = 1500,
    effective_amount: int = 98500,
    currency: Optional[str] = None,
    **extra: Any,
) -> dict:
    """Return a settlement webhook body shaped the way Paystack sends one."""
    return {
        "event": event,
        "data": {
            "id": settlement_id,
            "domain": "test",
            "status": "success",
            "currency": currency or frappe.db.get_value("Company", TEST_COMPANY, "default_currency"),
            "total_amount": total_amount,
            "total_fees": total_fees,
            "effective_amount": effective_amount,
            "settlement_date": "2026-07-30T09:00:00.000Z",
            **extra,
        },
    }


def unbalanced_entry(settlement: Any, gateway: Any) -> Any:
    """Return a payout entry whose rows do not add up."""
    entry = build_settlement_entry(settlement, gateway)
    entry.accounts[0].debit_in_account_currency = flt(entry.accounts[0].debit_in_account_currency) + 1
    return entry


def paystack_page(rows: list, ok: bool = True) -> MagicMock:
    """Return a stubbed requests response carrying one page of transactions."""
    response = MagicMock()
    response.ok = ok
    response.status_code = 200 if ok else 500
    response.json.return_value = {"status": True, "data": rows}
    return response


class SettlementTestCase(PaystackTestCase):
    """Base case with a gateway pointed at three distinguishable accounts."""

    def setUp(self) -> None:
        """Raise the ledger accounts, then the gateway that settles into them."""
        super().setUp()

        self.bank = self.ledger_account(BANK_ACCOUNT, "Asset", "Bank")
        self.fee = self.ledger_account(FEE_ACCOUNT, "Expense")
        self.suspense = self.ledger_account(SUSPENSE_ACCOUNT, "Asset", "Bank")

        self.gateway = GatewaySettingFactory.create()
        self.addCleanup(GatewaySettingFactory.cleanup, self.gateway)
        GatewaySettingFactory.configure_settlement(self.gateway, self.suspense, self.bank, self.fee)

    def ledger_account(self, account_name: str, root_type: str, account_type: Optional[str] = None) -> str:
        """Return a dedicated ledger account, removed once the test is done."""
        account = LedgerAccountFactory.create(account_name, root_type, account_type)
        self.addCleanup(LedgerAccountFactory.cleanup, account)
        return account

    def settlement(self, **kwargs: Any) -> Any:
        """Return a recorded payout, removed once the test is done."""
        name = SettlementFactory.create(**kwargs)
        self.addCleanup(SettlementFactory.cleanup, name)
        return frappe.get_doc(SETTLEMENT_DOCTYPE, name)

    def entries_for(self, settlement_id: str) -> int:
        """Count this test's journal entries for a payout."""
        return frappe.db.count(
            "Journal Entry",
            {"cheque_no": settlement_id, "creation": [">=", self.started_at]},
        )

    def receive_payout(self, payload: dict, company: Optional[str] = TEST_COMPANY) -> str:
        """Run a settlement webhook payload and return the payout id it named."""
        settlement_id = str((payload.get("data") or {}).get("id") or "")
        self.addCleanup(SettlementFactory.cleanup, settlement_id)

        with patch("frappe_paystack.utils.settlement.frappe.enqueue"):
            process_settlement_webhook_event(payload, company)

        return settlement_id


class TestSettlementWebhookRouting(SettlementTestCase):
    """The webhook router hands a payout to the settlement handler."""

    def test_a_settlement_success_event_is_recorded(self) -> None:
        """settlement.success reaches the settlement handler."""
        self.addCleanup(SettlementFactory.cleanup, "770101")
        process_webhook_event(payout_payload(settlement_id="770101"), {"company": TEST_COMPANY})

        self.assertTrue(frappe.db.exists(SETTLEMENT_DOCTYPE, "770101"))

    def test_a_settlement_processed_event_is_recorded(self) -> None:
        """settlement.processed reaches the settlement handler."""
        self.addCleanup(SettlementFactory.cleanup, "770102")
        process_webhook_event(
            payout_payload(settlement_id="770102", event="settlement.processed"),
            {"company": TEST_COMPANY},
        )

        self.assertTrue(frappe.db.exists(SETTLEMENT_DOCTYPE, "770102"))

    def test_a_charge_event_is_not_treated_as_a_payout(self) -> None:
        """A charge.success event leaves the settlement handler uncalled."""
        with patch("frappe_paystack.api.process_settlement_webhook_event") as mock_settlement:
            process_webhook_event({"event": "charge.success", "data": {}})

        mock_settlement.assert_not_called()

    def test_a_payout_without_settings_is_refused(self) -> None:
        """A payout delivered with no settings is not recorded."""
        self.addCleanup(SettlementFactory.cleanup, "770103")
        process_webhook_event(payout_payload(settlement_id="770103"))

        self.assertFalse(frappe.db.exists(SETTLEMENT_DOCTYPE, "770103"))

    def deliver_signed(self, settlement_id: str, **extra: Any) -> None:
        """Deliver a settlement event signed with the gateway's own secret."""
        payload = payout_payload(settlement_id=settlement_id, **extra)
        self.addCleanup(SettlementFactory.cleanup, settlement_id)
        body = json.dumps(payload).encode("utf-8")
        secret = frappe.get_doc(GATEWAY_DOCTYPE, self.gateway).get_password("secret_key")

        with (
            patch("frappe_paystack.api.get_webhook_request_data") as mock_request,
            patch("frappe_paystack.utils.settlement.frappe.enqueue"),
        ):
            mock_request.return_value = (
                payload,
                body,
                hmac_sha512(body, secret),
                WEBHOOK_CALLER_IP,
            )
            paystack_webhook()

    def test_the_signed_webhook_books_the_payout(self) -> None:
        """End to end: a signed settlement event reaches the ledger."""
        self.deliver_signed("770104")

        self.assertEqual(frappe.db.get_value(SETTLEMENT_DOCTYPE, "770104", "status"), "Processed")

    def test_the_payout_lands_in_the_ledger_of_the_gateway_that_signed_it(self) -> None:
        """The payout is booked against the company whose gateway signed it."""
        self.deliver_signed("770105")

        self.assertEqual(frappe.db.get_value(SETTLEMENT_DOCTYPE, "770105", "company"), TEST_COMPANY)

    def test_a_company_named_in_the_payload_does_not_override_the_signature(self) -> None:
        """A company field in the payload leaves the signing gateway's company."""
        self.deliver_signed("770106", company="_Test Company 2")

        self.assertEqual(frappe.db.get_value(SETTLEMENT_DOCTYPE, "770106", "company"), TEST_COMPANY)


class TestSettlementWebhookGuards(SettlementTestCase):
    """What the settlement handler refuses to record."""

    def integration_errors(self) -> list:
        """Return the errors filed on this session's webhook Integration Requests."""
        return frappe.get_all(
            "Integration Request",
            filters={"url": "webhook", "creation": [">=", self.started_at]},
            pluck="error",
        )

    def test_a_payout_without_an_id_is_refused(self) -> None:
        """A payout carrying no id is not recorded, and the filed request says so."""
        payload = payout_payload()
        payload["data"].pop("id")

        process_settlement_webhook_event(payload, TEST_COMPANY)

        self.assertEqual(frappe.db.count(SETTLEMENT_DOCTYPE, {"company": TEST_COMPANY}), 0)
        self.assertIn("carries no id", " ".join(self.integration_errors()))

    def test_a_payout_without_a_company_is_refused(self) -> None:
        """A payout with no company is not recorded, and the filed request says so."""
        self.receive_payout(payout_payload(settlement_id="770201"), company=None)

        self.assertFalse(frappe.db.exists(SETTLEMENT_DOCTYPE, "770201"))
        self.assertIn("No Paystack gateway company", " ".join(self.integration_errors()))

    def test_a_repeated_payout_is_recorded_once(self) -> None:
        """A repeated delivery keeps the first entry and leaves no Error Log."""
        self.receive_payout(payout_payload(settlement_id="770202"))
        first = frappe.db.get_value(SETTLEMENT_DOCTYPE, "770202", "journal_entry")

        process_settlement_webhook_event(payout_payload(settlement_id="770202"), TEST_COMPANY)

        self.assertEqual(frappe.db.get_value(SETTLEMENT_DOCTYPE, "770202", "journal_entry"), first)
        self.assertEqual(self.entries_for("770202"), 1)
        self.assertFalse(
            frappe.db.exists(
                "Error Log",
                {"reference_name": "770202", "creation": [">=", self.started_at]},
            )
        )

    def test_a_handler_failure_is_logged_not_raised(self) -> None:
        """A handler that raises returns None and files an Error Log."""
        with patch(
            "frappe_paystack.utils.settlement.record_settlement",
            side_effect=RuntimeError("boom"),
        ):
            self.assertIsNone(
                process_settlement_webhook_event(payout_payload(settlement_id="770203"), TEST_COMPANY)
            )

        self.assertTrue(
            frappe.db.exists("Error Log", {"method": ["like", "%settlement webhook failed: 770203%"]})
        )


class TestSettlementRecording(SettlementTestCase):
    """What a payout payload becomes on the record."""

    def recorded(self, **kwargs: Any) -> Any:
        """Receive a payout and return the record it wrote."""
        return frappe.get_doc(SETTLEMENT_DOCTYPE, self.receive_payout(payout_payload(**kwargs)))

    def test_amounts_are_read_out_of_minor_units(self) -> None:
        """Paystack reports kobo; the ledger takes naira."""
        settlement = self.recorded(settlement_id="770301")

        self.assertEqual(flt(settlement.gross_amount), 1000.0)
        self.assertEqual(flt(settlement.total_fees), 15.0)
        self.assertEqual(flt(settlement.net_amount), 985.0)

    def test_the_settlement_date_is_taken_from_the_payload(self) -> None:
        """The settlement date is the one the payload carries."""
        settlement = self.recorded(settlement_id="770302")

        self.assertEqual(str(settlement.settlement_date), "2026-07-30")

    def test_settled_at_is_used_when_the_payload_names_no_date(self) -> None:
        """settled_at supplies the settlement date when settlement_date is absent."""
        payload = payout_payload(settlement_id="770303")
        payload["data"].pop("settlement_date")
        payload["data"]["settled_at"] = "2026-07-29T09:00:00.000Z"

        self.receive_payout(payload)

        self.assertEqual(
            str(frappe.db.get_value(SETTLEMENT_DOCTYPE, "770303", "settlement_date")),
            "2026-07-29",
        )

    def test_a_payout_with_no_date_posts_today(self) -> None:
        """A payload carrying neither date records today."""
        payload = payout_payload(settlement_id="770304")
        payload["data"].pop("settlement_date")

        self.receive_payout(payload)

        self.assertEqual(
            str(frappe.db.get_value(SETTLEMENT_DOCTYPE, "770304", "settlement_date")),
            today(),
        )

    def test_the_payout_carries_its_integration_request(self) -> None:
        """The payout names the Integration Request its payload was filed on."""
        settlement = self.recorded(settlement_id="770305")

        self.assertTrue(settlement.integration_request)
        self.assertTrue(frappe.db.exists("Integration Request", settlement.integration_request))


class TestSettlementJournalEntry(SettlementTestCase):
    """The entry that empties suspense into the bank and the fee."""

    def test_the_gross_leaves_suspense_for_the_bank_and_the_fee(self) -> None:
        """The gross leaves suspense as the net to the bank and the fee to fees."""
        self.receive_payout(payout_payload(settlement_id="770401"))
        entry = frappe.db.get_value(SETTLEMENT_DOCTYPE, "770401", "journal_entry")

        ledger = self.gl_entries_by_account(entry)

        self.assertEqual(ledger[self.bank]["debit"], 985.0)
        self.assertEqual(ledger[self.fee]["debit"], 15.0)
        self.assertEqual(ledger[self.suspense]["credit"], 1000.0)

    def test_a_booked_payout_is_processed(self) -> None:
        """A payout that posted its entry is Processed and carries no errors."""
        self.receive_payout(payout_payload(settlement_id="770402"))

        self.assertEqual(frappe.db.get_value(SETTLEMENT_DOCTYPE, "770402", "status"), "Processed")
        self.assertFalse(frappe.db.get_value(SETTLEMENT_DOCTYPE, "770402", "errors"))

    def test_a_payout_without_fees_posts_no_fee_row(self) -> None:
        """A payout with no fees posts an entry with no fee row."""
        self.receive_payout(payout_payload(settlement_id="770403", total_fees=0, effective_amount=100000))
        entry = frappe.db.get_value(SETTLEMENT_DOCTYPE, "770403", "journal_entry")

        self.assertEqual(frappe.db.get_value(SETTLEMENT_DOCTYPE, "770403", "status"), "Processed")
        self.assertNotIn(self.fee, self.gl_entries_by_account(entry))

    def test_the_entry_names_the_payout(self) -> None:
        """The entry carries the payout id in cheque_no and in the remark."""
        self.receive_payout(payout_payload(settlement_id="770404"))
        entry = frappe.get_doc(
            "Journal Entry",
            frappe.db.get_value(SETTLEMENT_DOCTYPE, "770404", "journal_entry"),
        )

        self.assertEqual(entry.cheque_no, "770404")
        self.assertIn("770404", entry.user_remark)

    def test_posting_twice_books_one_entry(self) -> None:
        """Posting a booked payout returns its entry and adds no second one."""
        self.receive_payout(payout_payload(settlement_id="770405"))
        settlement = frappe.get_doc(SETTLEMENT_DOCTYPE, "770405")

        self.assertEqual(post_settlement_entry(settlement), settlement.journal_entry)
        self.assertEqual(self.entries_for("770405"), 1)


class TestSettlementDeductions(SettlementTestCase):
    """A payout Paystack held a refund back out of."""

    def refund_payout(self, settlement_id: str) -> str:
        """Receive a 1000 payout that funded a 200 refund and cost 15 in fees."""
        return self.receive_payout(
            payout_payload(
                settlement_id=settlement_id,
                total_amount=100000,
                total_fees=1500,
                deductions=20000,
                effective_amount=78500,
            )
        )

    def test_the_deduction_is_read_out_of_minor_units(self) -> None:
        """A deduction in kobo is recorded in naira."""
        self.refund_payout("771201")

        self.assertEqual(flt(frappe.db.get_value(SETTLEMENT_DOCTYPE, "771201", "deductions")), 200.0)

    def test_a_payout_that_funded_a_refund_is_booked(self) -> None:
        """A payout carrying a refund deduction is Processed and carries no errors."""
        self.refund_payout("771202")

        self.assertEqual(frappe.db.get_value(SETTLEMENT_DOCTYPE, "771202", "status"), "Processed")
        self.assertFalse(frappe.db.get_value(SETTLEMENT_DOCTYPE, "771202", "errors"))

    def test_the_bank_takes_only_what_paystack_paid(self) -> None:
        """The bank is debited the net paid, and the fee account the fee."""
        self.refund_payout("771203")
        ledger = self.gl_entries_by_account(
            frappe.db.get_value(SETTLEMENT_DOCTYPE, "771203", "journal_entry")
        )

        self.assertEqual(ledger[self.bank]["debit"], 785.0)
        self.assertEqual(ledger[self.fee]["debit"], 15.0)

    def test_the_deduction_goes_back_to_suspense(self) -> None:
        """Suspense is credited the gross less the deduction."""
        self.refund_payout("771204")
        suspense = self.gl_entries_by_account(
            frappe.db.get_value(SETTLEMENT_DOCTYPE, "771204", "journal_entry")
        )[self.suspense]

        self.assertEqual(suspense["credit"] - suspense["debit"], 800.0)

    def test_no_other_account_is_touched(self) -> None:
        """The entry touches the bank, the fee and the suspense accounts."""
        self.refund_payout("771205")
        ledger = self.gl_entries_by_account(
            frappe.db.get_value(SETTLEMENT_DOCTYPE, "771205", "journal_entry")
        )

        self.assertEqual(set(ledger), {self.bank, self.fee, self.suspense})

    def test_a_payout_with_no_deduction_posts_no_deduction_row(self) -> None:
        """A payout with no deduction carries one suspense row."""
        settlement = self.settlement(settlement_id="771206", deductions=0)
        entry = build_settlement_entry(settlement, settlement_gateway(TEST_COMPANY))

        self.assertEqual([row.account for row in entry.accounts].count(self.suspense), 1)

    def test_a_deduction_the_identity_does_not_close_is_not_booked(self) -> None:
        """A payout whose figures miss the net books nothing and names deductions."""
        settlement_id = self.receive_payout(
            payout_payload(
                settlement_id="771207",
                total_amount=100000,
                total_fees=1500,
                deductions=20000,
                effective_amount=90000,
            )
        )
        settlement = frappe.get_doc(SETTLEMENT_DOCTYPE, settlement_id)

        self.assertFalse(settlement.journal_entry)
        self.assertIn("in deductions", settlement.errors)

    def test_the_entry_balances_with_a_deduction(self) -> None:
        """Debits equal credits on an entry carrying a deduction."""
        settlement = self.settlement(settlement_id="771208", deductions=200.0, net_amount=785.0)
        entry = build_settlement_entry(settlement, settlement_gateway(TEST_COMPANY))

        debit = sum(flt(row.debit_in_account_currency) for row in entry.accounts)
        credit = sum(flt(row.credit_in_account_currency) for row in entry.accounts)
        self.assertEqual(debit, credit)


class TestSettlementRefusals(SettlementTestCase):
    """Payouts that are recorded without a journal entry."""

    def unbooked(self, **kwargs: Any) -> Any:
        """Receive a payout and return it, asserting nothing was posted."""
        settlement_id = self.receive_payout(payout_payload(**kwargs))
        settlement = frappe.get_doc(SETTLEMENT_DOCTYPE, settlement_id)

        self.assertFalse(settlement.journal_entry)
        return settlement

    def test_a_payout_in_another_currency_is_not_booked(self) -> None:
        """A payout in another currency names that currency in its errors."""
        settlement = self.unbooked(settlement_id="770501", currency="USD")

        self.assertIn("USD", settlement.errors)

    def test_a_payout_that_does_not_add_up_is_not_booked(self) -> None:
        """A payout whose gross less fees misses the net names fees in its errors."""
        settlement = self.unbooked(settlement_id="770502", effective_amount=90000)

        self.assertIn("in fees", settlement.errors)

    def test_a_payout_of_nothing_is_not_booked(self) -> None:
        """A payout of nothing names 'nothing settled' in its errors."""
        settlement = self.unbooked(settlement_id="770503", total_amount=0, total_fees=0, effective_amount=0)

        self.assertIn("nothing settled", settlement.errors)

    def test_a_gateway_missing_its_accounts_names_them(self) -> None:
        """The errors name both accounts the gateway is missing."""
        GatewaySettingFactory.configure_settlement(self.gateway, self.suspense, None, None)

        settlement = self.unbooked(settlement_id="770504")

        self.assertIn("Settlement Bank Account", settlement.errors)
        self.assertIn("Paystack Fee Account", settlement.errors)

    def test_the_missing_account_labels_are_translated(self) -> None:
        """Each account the gateway is missing is named through the translator."""
        GatewaySettingFactory.configure_settlement(self.gateway, self.suspense, None, None)
        gateway = frappe.get_doc(GATEWAY_DOCTYPE, self.gateway)

        with patch(f"{SETTLEMENT_MODULE}._", marked_translation):
            labels = missing_settlement_accounts(gateway)

        self.assertEqual(
            labels,
            [
                marked_translation("Settlement Bank Account"),
                marked_translation("Paystack Fee Account"),
            ],
        )

    def test_a_company_without_a_gateway_is_not_booked(self) -> None:
        """A payout for a company with no enabled gateway names 'not enabled'."""
        self.disable_company_gateways()

        settlement = self.unbooked(settlement_id="770505")

        self.assertIn("not enabled", settlement.errors)

    def test_a_failing_entry_marks_the_payout_failed(self) -> None:
        """A payout whose entry raises is Failed and names the Error Log."""
        settlement = self.settlement(settlement_id="770506")

        with patch(
            "frappe_paystack.utils.settlement.build_settlement_entry",
            side_effect=RuntimeError("chart of accounts"),
        ):
            self.assertIsNone(post_settlement_entry(settlement))

        self.assertEqual(frappe.db.get_value(SETTLEMENT_DOCTYPE, "770506", "status"), "Failed")
        self.assertIn("Error Log", frappe.db.get_value(SETTLEMENT_DOCTYPE, "770506", "errors"))

    def test_unpostable_reason_clears_once_the_gateway_is_complete(self) -> None:
        """unpostable_reason returns None once the gateway names all three accounts."""
        settlement = self.settlement(settlement_id="770507")

        self.assertIsNone(unpostable_reason(settlement, settlement_gateway(TEST_COMPANY)))


class TestSettlementDurability(SettlementTestCase):
    """What a payout leaves behind when the request it arrived on is rolled back."""

    def build_request(self) -> None:
        """Install a POST as the current Frappe request."""
        previous_request = getattr(frappe.local, "request", None)
        previous_ip = getattr(frappe.local, "request_ip", None)
        self.addCleanup(setattr, frappe.local, "request", previous_request)
        self.addCleanup(setattr, frappe.local, "request_ip", previous_ip)

        frappe.local.request = Request(
            EnvironBuilder(method="POST", content_type="application/json").get_environ()
        )
        frappe.local.request_ip = WEBHOOK_CALLER_IP

    def payout_through_a_request(self, settlement_id: str, **patches: Any) -> Any:
        """Receive a payout, then discard everything a rollback would discard."""
        self.build_request()
        frappe.db.commit()

        if patches:
            with patch("frappe_paystack.utils.settlement.build_settlement_entry", **patches):
                self.receive_payout(payout_payload(settlement_id=settlement_id))
        else:
            self.receive_payout(payout_payload(settlement_id=settlement_id))

        frappe.db.rollback()
        return frappe.get_doc(SETTLEMENT_DOCTYPE, settlement_id)

    def test_a_failed_entry_keeps_its_payout_and_its_reason(self) -> None:
        """A failed payout keeps its status and its reason through the rollback."""
        settlement = self.payout_through_a_request("770601", side_effect=RuntimeError("chart of accounts"))

        self.assertEqual(settlement.status, "Failed")
        self.assertIn("Error Log", settlement.errors)

    def test_a_failed_entry_leaves_no_draft_behind(self) -> None:
        """A failed payout is Failed and leaves no journal entry behind."""
        settlement = self.payout_through_a_request("770604", side_effect=unbalanced_entry)

        self.assertEqual(settlement.status, "Failed")
        self.assertEqual(self.entries_for("770604"), 0)

    def test_a_refused_payout_keeps_the_reason_it_was_refused(self) -> None:
        """A refused payout keeps its reason through the rollback."""
        GatewaySettingFactory.configure_settlement(self.gateway, self.suspense, None, None)

        settlement = self.payout_through_a_request("770602")

        self.assertIn("Settlement Bank Account", settlement.errors)

    def test_a_booked_payout_keeps_its_journal_entry(self) -> None:
        """A booked payout keeps its journal entry through the rollback."""
        settlement = self.payout_through_a_request("770603")

        self.assertTrue(settlement.journal_entry)
        self.assertTrue(frappe.db.exists("Journal Entry", settlement.journal_entry))


class TestSettlementRetrySweep(SettlementTestCase):
    """The hourly sweep that books payouts the webhook could not."""

    def test_a_stuck_payout_is_booked_once_the_accounts_are_set(self) -> None:
        """The sweep books a payout that arrived before the accounts were set."""
        GatewaySettingFactory.configure_settlement(self.gateway, self.suspense, None, None)
        settlement_id = self.receive_payout(payout_payload(settlement_id="770701"))

        GatewaySettingFactory.configure_settlement(self.gateway, self.suspense, self.bank, self.fee)
        retry_unposted_settlements()

        self.assertTrue(frappe.db.get_value(SETTLEMENT_DOCTYPE, settlement_id, "journal_entry"))

    def test_a_booked_payout_is_not_swept_again(self) -> None:
        """A posted payout is absent from the sweep's list."""
        self.receive_payout(payout_payload(settlement_id="770702"))

        self.assertNotIn("770702", unposted_settlements())

    def test_an_old_payout_is_left_to_a_human(self) -> None:
        """A payout older than the lookback is absent from the sweep's list."""
        settlement = self.settlement(settlement_id="770703")
        frappe.db.set_value(
            SETTLEMENT_DOCTYPE,
            settlement.name,
            "creation",
            add_days(today(), -60),
            update_modified=False,
        )

        self.assertNotIn("770703", unposted_settlements())

    def test_a_lookup_failure_is_logged_not_raised(self) -> None:
        """The sweep returns None when the lookup raises."""
        with patch(
            "frappe_paystack.utils.settlement.unposted_settlements",
            side_effect=RuntimeError("db down"),
        ):
            self.assertIsNone(retry_unposted_settlements())


class TestSettlementLinkage(SettlementTestCase):
    """Naming which captures a payout actually paid out."""

    def setUp(self) -> None:
        """Raise a captured payment log the payout can be matched against."""
        super().setUp()
        self.log = PaymentLogFactory.create_completed(amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, self.log)
        self.transaction_id = frappe.db.get_value(PAYMENT_LOG, self.log, "transaction_id")

    def transaction(self, **overrides: Any) -> dict:
        """Return a settlement transaction row the way Paystack sends one."""
        return {"id": self.transaction_id, "fees": 1500, **overrides}

    def walk(self, settlement: Any, *pages: list) -> int:
        """Run the linkage against a stubbed sequence of transaction pages."""
        with patch(
            "frappe_paystack.utils.settlement.requests.get",
            side_effect=[paystack_page(page) for page in pages],
        ):
            return link_settled_payments(settlement.name)

    def test_a_settled_capture_is_stamped_with_its_payout_and_fee(self) -> None:
        """A matched capture is stamped with its payout and the fee Paystack sent."""
        settlement = self.settlement(settlement_id="770801")

        self.assertEqual(self.walk(settlement, [self.transaction()]), 1)
        self.assertEqual(frappe.db.get_value(PAYMENT_LOG, self.log, "settlement"), settlement.name)
        self.assertEqual(flt(frappe.db.get_value(PAYMENT_LOG, self.log, "paystack_fee")), 15.0)

    def test_a_transaction_this_site_never_saw_is_skipped(self) -> None:
        """A transaction no payment log matches stamps nothing."""
        settlement = self.settlement(settlement_id="770802")

        self.assertEqual(self.walk(settlement, [self.transaction(id="99999999")]), 0)

    def test_a_transaction_without_an_id_is_skipped(self) -> None:
        """A transaction with no id stamps nothing, including a log with a blank id."""
        settlement = self.settlement(settlement_id="770803")
        idless = PaymentLogFactory.create_completed(amount=500)
        self.addCleanup(PaymentLogFactory.cleanup, idless)
        frappe.db.set_value(PAYMENT_LOG, idless, "transaction_id", "")

        self.assertEqual(self.walk(settlement, [self.transaction(id=None)]), 0)
        self.assertFalse(frappe.db.get_value(PAYMENT_LOG, idless, "settlement"))

    def test_a_short_page_ends_the_walk(self) -> None:
        """A page shorter than the page size ends the walk after one call."""
        settlement = self.settlement(settlement_id="770810")
        settings = {"secret_key": "sk_test_1"}

        with patch(
            "frappe_paystack.utils.settlement.requests.get",
            return_value=paystack_page([self.transaction()]),
        ) as mock_get:
            settlement_transactions(settings, settlement)

        self.assertEqual(mock_get.call_count, 1)

    def test_a_full_page_is_followed_by_another(self) -> None:
        """A full page is followed by a request for the next one."""
        settlement = self.settlement(settlement_id="770804")
        full_page = [{"id": f"9{index}", "fees": 0} for index in range(TRANSACTION_PAGE_SIZE)]

        self.assertEqual(self.walk(settlement, full_page, [self.transaction()]), 1)

    def test_the_page_cap_ends_the_walk(self) -> None:
        """The walk stops at the page cap."""
        settlement = self.settlement(settlement_id="770805")
        settings = {"secret_key": "sk_test_1"}

        with (
            patch("frappe_paystack.utils.settlement.MAX_TRANSACTION_PAGES", 2),
            patch("frappe_paystack.utils.settlement.TRANSACTION_PAGE_SIZE", 1),
            patch(
                "frappe_paystack.utils.settlement.requests.get",
                return_value=paystack_page([self.transaction()]),
            ) as mock_get,
        ):
            rows = settlement_transactions(settings, settlement)

        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(len(rows), 2)

    def test_a_settled_page_is_filed_without_the_cards_it_names(self) -> None:
        """The Integration Request filed for a page carries no authorization code."""
        settlement = self.settlement(settlement_id="770809")

        with patch(
            "frappe_paystack.utils.settlement.requests.get",
            return_value=paystack_page(
                [self.transaction(authorization={"authorization_code": "AUTH_settled"})]
            ),
        ):
            link_settled_payments(settlement.name)

        logged = frappe.get_all(
            "Integration Request",
            filters={"reference_docname": settlement.name},
            pluck="output",
        )

        self.assertTrue(logged)
        for output in logged:
            self.assertNotIn("AUTH_settled", output)

    def test_an_unreachable_page_ends_the_walk(self) -> None:
        """A request that raises ends the walk at zero and files a Failed request."""
        settlement = self.settlement(settlement_id="770806")

        with patch(
            "frappe_paystack.utils.settlement.requests.get",
            side_effect=RuntimeError("timeout"),
        ):
            self.assertEqual(link_settled_payments(settlement.name), 0)

        self.assertTrue(
            frappe.db.exists(
                "Integration Request",
                {"reference_docname": settlement.name, "status": "Failed"},
            )
        )

    def test_a_rejected_page_is_filed_as_failed(self) -> None:
        """A non-2xx page ends the walk at zero and files a Failed request."""
        settlement = self.settlement(settlement_id="770807")

        with patch(
            "frappe_paystack.utils.settlement.requests.get",
            return_value=paystack_page([], ok=False),
        ):
            self.assertEqual(link_settled_payments(settlement.name), 0)

        self.assertTrue(
            frappe.db.exists(
                "Integration Request",
                {"reference_docname": settlement.name, "status": "Failed"},
            )
        )

    def test_a_company_without_a_gateway_links_nothing(self) -> None:
        """The walk stamps nothing when the company has no enabled gateway."""
        settlement = self.settlement(settlement_id="770808")
        self.disable_company_gateways()

        self.assertEqual(link_settled_payments(settlement.name), 0)

    def test_a_capture_in_another_company_is_never_claimed(self) -> None:
        """A payment log in another company is left unstamped by this payout."""
        settlement = self.settlement(settlement_id="770811")
        foreign = PaymentLogFactory.create(company=OTHER_COMPANY, transaction_id="tx_other_company")
        self.addCleanup(PaymentLogFactory.cleanup, foreign)

        self.assertEqual(self.walk(settlement, [self.transaction(id="tx_other_company")]), 0)
        self.assertFalse(frappe.db.get_value(PAYMENT_LOG, foreign, "settlement"))

    def test_the_linkage_is_queued_off_the_webhook(self) -> None:
        """The linkage is queued with the settlement name."""
        settlement = self.settlement(settlement_id="770809")

        with patch("frappe_paystack.utils.settlement.frappe.enqueue") as mock_enqueue:
            enqueue_transaction_linkage(settlement.name)

        self.assertEqual(mock_enqueue.call_args.kwargs["settlement_name"], settlement.name)

    def test_the_linkage_job_is_named_by_the_supported_keyword(self) -> None:
        """The linkage job identifies itself with job_id."""
        settlement = self.settlement(settlement_id="770812")

        with patch("frappe_paystack.utils.settlement.frappe.enqueue") as mock_enqueue:
            enqueue_transaction_linkage(settlement.name)

        self.assertNotIn("job_name", mock_enqueue.call_args.kwargs)
        self.assertEqual(mock_enqueue.call_args.kwargs["job_id"], f"paystack-settlement-{settlement.name}")

    def test_the_linkage_job_is_deduplicated_on_the_payout(self) -> None:
        """The linkage lets the queue turn away a walk it already holds."""
        settlement = self.settlement(settlement_id="770813")

        with patch("frappe_paystack.utils.settlement.frappe.enqueue") as mock_enqueue:
            enqueue_transaction_linkage(settlement.name)

        self.assertTrue(mock_enqueue.call_args.kwargs["deduplicate"])

    def test_a_queue_with_room_takes_the_linkage(self) -> None:
        """A walk nothing already holds reaches the queue."""
        settlement = self.settlement(settlement_id="770814")

        with patch("frappe_paystack.utils.settlement.frappe.enqueue") as mock_enqueue:
            enqueue_transaction_linkage(settlement.name)

        self.assertEqual(mock_enqueue.call_count, 1)


class TestSettlementLinkageGating(SettlementTestCase):
    """Only a payout that reached the ledger claims the captures it paid."""

    def setUp(self) -> None:
        """Raise a captured payment log the payout would claim."""
        super().setUp()
        self.log = PaymentLogFactory.create_completed(amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, self.log)
        self.transaction_id = frappe.db.get_value(PAYMENT_LOG, self.log, "transaction_id")

    def deliver(self, settlement_id: str) -> None:
        """Receive a payout whose only transaction is this test's capture."""
        self.addCleanup(SettlementFactory.cleanup, settlement_id)

        with patch(
            "frappe_paystack.utils.settlement.requests.get",
            return_value=paystack_page([{"id": self.transaction_id, "fees": 1500}]),
        ):
            process_settlement_webhook_event(payout_payload(settlement_id=settlement_id), TEST_COMPANY)

    def unsettled(self) -> list:
        """Return the payment logs the unsettled report still lists."""
        _columns, data = unsettled_report.execute({"company": TEST_COMPANY})
        return [row.name for row in data]

    def test_a_booked_payout_claims_its_captures(self) -> None:
        """A booked payout stamps its capture, which drops off the unsettled report."""
        self.deliver("771301")

        self.assertEqual(frappe.db.get_value(PAYMENT_LOG, self.log, "settlement"), "771301")
        self.assertNotIn(self.log, self.unsettled())

    def test_a_refused_payout_leaves_its_captures_unsettled(self) -> None:
        """A refused payout stamps nothing, and its capture stays on the report."""
        GatewaySettingFactory.configure_settlement(self.gateway, self.suspense, None, None)

        self.deliver("771302")

        self.assertFalse(frappe.db.get_value(PAYMENT_LOG, self.log, "settlement"))
        self.assertIn(self.log, self.unsettled())

    def test_a_failed_payout_leaves_its_captures_unsettled(self) -> None:
        """A payout whose entry raises stamps nothing, and its capture stays listed."""
        with patch(
            "frappe_paystack.utils.settlement.build_settlement_entry",
            side_effect=RuntimeError("chart of accounts"),
        ):
            self.deliver("771303")

        self.assertFalse(frappe.db.get_value(PAYMENT_LOG, self.log, "settlement"))
        self.assertIn(self.log, self.unsettled())


class TestSettlementDraftCleanup(SettlementTestCase):
    """A journal entry that was inserted but never submitted is removed."""

    def test_an_entry_that_would_not_submit_is_removed(self) -> None:
        """An unbalanced draft leaves no journal entry, and the payout is Failed."""
        settlement = self.settlement(settlement_id="771401")

        with patch(
            "frappe_paystack.utils.settlement.build_settlement_entry",
            side_effect=unbalanced_entry,
        ):
            self.assertIsNone(post_settlement_entry(settlement))

        self.assertEqual(self.entries_for("771401"), 0)
        self.assertEqual(frappe.db.get_value(SETTLEMENT_DOCTYPE, "771401", "status"), "Failed")

    def test_the_hourly_sweep_accumulates_nothing(self) -> None:
        """Two sweeps over a stuck payout leave no journal entry."""
        self.settlement(settlement_id="771402")

        with patch(
            "frappe_paystack.utils.settlement.build_settlement_entry",
            side_effect=unbalanced_entry,
        ):
            retry_unposted_settlements()
            retry_unposted_settlements()

        self.assertEqual(self.entries_for("771402"), 0)

    def test_an_entry_that_never_reached_the_table_is_left_alone(self) -> None:
        """discard_draft_entry returns None for an unsaved entry."""
        settlement = self.settlement(settlement_id="771403")
        entry = build_settlement_entry(settlement, settlement_gateway(TEST_COMPANY))

        self.assertIsNone(discard_draft_entry(entry))

    def test_a_submitted_entry_is_never_discarded(self) -> None:
        """discard_draft_entry keeps a submitted entry."""
        self.receive_payout(payout_payload(settlement_id="771404"))
        entry = frappe.get_doc(
            "Journal Entry",
            frappe.db.get_value(SETTLEMENT_DOCTYPE, "771404", "journal_entry"),
        )

        discard_draft_entry(entry)

        self.assertTrue(frappe.db.exists("Journal Entry", entry.name))


class TestPaystackSettlementDoctype(SettlementTestCase):
    """The record itself."""

    def test_an_invalid_status_is_refused(self) -> None:
        """Saving an unlisted status raises ValidationError."""
        settlement = self.settlement(settlement_id="770901")
        settlement.status = "Nearly"

        with self.assertRaises(frappe.ValidationError):
            settlement.save()

    def test_the_currency_is_stored_uppercase(self) -> None:
        """A lowercase currency is stored uppercase."""
        settlement = self.settlement(settlement_id="770902", currency="ngn")

        self.assertEqual(settlement.currency, "NGN")

    def test_a_payout_with_no_currency_is_still_recorded(self) -> None:
        """A payout saves with an empty currency."""
        settlement = self.settlement(settlement_id="770905")
        settlement.currency = None
        settlement.save()

        self.assertFalse(settlement.currency)

    def test_a_booked_payout_cannot_be_deleted(self) -> None:
        """Deleting a payout that names a journal entry raises ValidationError."""
        self.receive_payout(payout_payload(settlement_id="770903"))

        with self.assertRaises(frappe.ValidationError):
            frappe.delete_doc(SETTLEMENT_DOCTYPE, "770903", ignore_permissions=True)

    def test_an_unbooked_payout_can_be_deleted(self) -> None:
        """A payout with no journal entry deletes."""
        settlement = self.settlement(settlement_id="770904")

        frappe.delete_doc(SETTLEMENT_DOCTYPE, settlement.name, ignore_permissions=True)

        self.assertFalse(frappe.db.exists(SETTLEMENT_DOCTYPE, "770904"))


class TestSettlementAccountValidation(SettlementTestCase):
    """A settlement account has to belong to the gateway's own company."""

    def test_an_account_from_another_company_is_refused(self) -> None:
        """Saving a fee account from another company raises ValidationError."""
        other = LedgerAccountFactory.create(
            "Test Paystack Foreign Fees", "Expense", company="_Test Company 2"
        )
        self.addCleanup(LedgerAccountFactory.cleanup, other)

        setting = frappe.get_doc(GATEWAY_DOCTYPE, self.gateway)
        setting.paystack_fee_account = other

        with self.assertRaises(frappe.ValidationError):
            setting.save()

    def test_a_suspense_account_from_another_company_is_refused(self) -> None:
        """Saving a suspense account from another company raises ValidationError."""
        other = LedgerAccountFactory.create(
            "Test Paystack Foreign Suspense", "Asset", "Bank", company="_Test Company 2"
        )
        self.addCleanup(LedgerAccountFactory.cleanup, other)

        setting = frappe.get_doc(GATEWAY_DOCTYPE, self.gateway)
        setting.suspense_account = other

        with self.assertRaises(frappe.ValidationError):
            setting.save()

    def test_a_foreign_account_refusal_names_a_translated_label(self) -> None:
        """The wrong-company refusal puts the account's label through the translator."""
        other = LedgerAccountFactory.create(
            "Test Paystack Foreign Bank", "Asset", "Bank", company="_Test Company 2"
        )
        self.addCleanup(LedgerAccountFactory.cleanup, other)

        setting = frappe.get_doc(GATEWAY_DOCTYPE, self.gateway)
        setting.settlement_bank_account = other

        with patch(f"{GATEWAY_MODULE}._", marked_translation):
            with self.assertRaises(frappe.ValidationError) as caught:
                setting.validate_settlement_accounts()

        self.assertIn(marked_translation("Settlement Bank Account"), str(caught.exception))

    def test_an_account_from_the_same_company_is_accepted(self) -> None:
        """An account from the gateway's own company saves."""
        setting = frappe.get_doc(GATEWAY_DOCTYPE, self.gateway)
        setting.paystack_fee_account = self.fee
        setting.save()

        self.assertEqual(
            frappe.db.get_value(GATEWAY_DOCTYPE, self.gateway, "paystack_fee_account"),
            self.fee,
        )

    def test_an_empty_account_is_accepted(self) -> None:
        """A gateway saves with its settlement accounts left blank."""
        setting = frappe.get_doc(GATEWAY_DOCTYPE, self.gateway)
        setting.settlement_bank_account = None
        setting.paystack_fee_account = None
        setting.save()

        self.assertFalse(frappe.db.get_value(GATEWAY_DOCTYPE, self.gateway, "paystack_fee_account"))


class TestChargeFeeCapture(SettlementTestCase):
    """What the charge webhook records about Paystack's cut."""

    def charge(self, log_name: str, fees: Optional[int]) -> None:
        """Run a charge webhook carrying, or omitting, a fee."""
        transaction = {
            "id": f"tx_fee_{log_name}",
            "reference": log_name,
            "status": "success",
            "amount": 100000,
            "currency": "NGN",
            "paid_at": "2026-07-30T10:00:00Z",
            "metadata": {"reference": log_name},
        }
        if fees is not None:
            transaction["fees"] = fees

        process_webhook_event({"event": "charge.success", "data": transaction})

    def test_the_fee_paystack_reported_is_recorded(self) -> None:
        """The fee a charge webhook reports is recorded on the payment log."""
        log = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log)

        self.charge(log, 1500)

        self.assertEqual(flt(frappe.db.get_value(PAYMENT_LOG, log, "paystack_fee")), 15.0)

    def test_a_charge_without_a_fee_records_nothing_owed(self) -> None:
        """A charge webhook carrying no fee records a fee of zero."""
        log = PaymentLogFactory.create(status="Pending", amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log)

        self.charge(log, None)

        self.assertEqual(flt(frappe.db.get_value(PAYMENT_LOG, log, "paystack_fee")), 0.0)


class TestUnsettledPaymentsReport(SettlementTestCase):
    """The list behind the suspense account's balance."""

    def setUp(self) -> None:
        """Raise a captured payment log no payout has claimed yet."""
        super().setUp()
        self.log = PaymentLogFactory.create_completed(amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, self.log)
        frappe.db.set_value(
            PAYMENT_LOG,
            self.log,
            {"payment_date": add_days(today(), -4), "paystack_fee": 15.0},
        )

    def rows(self, **filters: Any) -> list:
        """Run the report for the test company and return its rows."""
        _columns, data = unsettled_report.execute({"company": TEST_COMPANY, **filters})
        return data

    def names(self, **filters: Any) -> list:
        """Return the payment logs the report lists."""
        return [row.name for row in self.rows(**filters)]

    def test_a_captured_payment_awaiting_payout_is_listed(self) -> None:
        """A captured payment no payout has claimed is listed."""
        self.assertIn(self.log, self.names())

    def test_a_settled_payment_drops_off(self) -> None:
        """A payment stamped with a payout drops off the list."""
        settlement = self.settlement(settlement_id="771001")
        frappe.db.set_value(PAYMENT_LOG, self.log, "settlement", settlement.name)

        self.assertNotIn(self.log, self.names())

    def test_a_payment_that_was_never_captured_is_not_listed(self) -> None:
        """A Pending payment is absent from the list."""
        frappe.db.set_value(PAYMENT_LOG, self.log, "status", "Pending")

        self.assertNotIn(self.log, self.names())

    def test_the_wait_is_counted_from_the_capture(self) -> None:
        """days_waiting counts from the payment date, and the row carries the fee."""
        row = next(row for row in self.rows() if row.name == self.log)

        self.assertEqual(row.days_waiting, 4)
        self.assertEqual(flt(row.paystack_fee), 15.0)

    def test_a_capture_with_no_date_has_no_wait(self) -> None:
        """A capture with no payment date reports zero days waiting."""
        frappe.db.set_value(PAYMENT_LOG, self.log, "payment_date", None)

        row = next(row for row in self.rows() if row.name == self.log)

        self.assertEqual(row.days_waiting, 0)

    def test_the_date_filter_narrows_the_list(self) -> None:
        """A to_date before the capture drops it from the list."""
        self.assertNotIn(self.log, self.names(to_date=add_days(today(), -10)))

    def test_the_report_runs_without_filters(self) -> None:
        """execute() with no filters returns columns."""
        columns, _data = unsettled_report.execute()

        self.assertTrue(columns)


class TestSettlementEntryShape(SettlementTestCase):
    """The journal entry is built against the gateway's own accounts."""

    def test_every_row_carries_the_company_cost_center(self) -> None:
        """Every row of the entry carries a cost center."""
        settlement = self.settlement(settlement_id="771101")
        entry = build_settlement_entry(settlement, settlement_gateway(TEST_COMPANY))

        self.assertTrue(all(row.cost_center for row in entry.accounts))

    def test_the_entry_balances(self) -> None:
        """Debits equal credits on the entry."""
        settlement = self.settlement(settlement_id="771102")
        entry = build_settlement_entry(settlement, settlement_gateway(TEST_COMPANY))

        debit = sum(flt(row.debit_in_account_currency) for row in entry.accounts)
        credit = sum(flt(row.credit_in_account_currency) for row in entry.accounts)
        self.assertEqual(debit, credit)


class SettlementLedgerReportTestCase(SettlementTestCase):
    """Fixtures for the Paystack Settlements vs Ledger report."""

    def report_rows(self, **filters: Any) -> list:
        """Run the report over the test company, with any extra filters."""
        _columns, data = ledger_report.execute({"company": TEST_COMPANY, **filters})
        return data

    def row_for(self, settlement: str, **filters: Any) -> Optional[dict]:
        """Return one payout's row, or None when the report leaves it out."""
        return next(
            (row for row in self.report_rows(**filters) if row["settlement"] == settlement),
            None,
        )

    def booked(self, **kwargs: Any) -> Any:
        """Return a payout the poster has cleared into the ledger."""
        settlement = self.settlement(**kwargs)
        self.assertTrue(post_settlement_entry(settlement))
        settlement.reload()
        return settlement

    def capture(self, settlement: str, amount: float) -> str:
        """Return a captured payment log stamped with the payout that paid it."""
        log = PaymentLogFactory.create_completed(amount=amount)
        self.addCleanup(PaymentLogFactory.cleanup, log)
        frappe.db.set_value(PAYMENT_LOG, log, "settlement", settlement)
        return log

    def dated(self, settlement_id: str, days_ago: int) -> str:
        """Return a payout that landed a given number of days back."""
        settlement = self.settlement(settlement_id=settlement_id)
        frappe.db.set_value(
            SETTLEMENT_DOCTYPE,
            settlement.name,
            "settlement_date",
            add_days(today(), -days_ago),
            update_modified=False,
        )
        return settlement.name


class TestSettlementLedgerColumns(SettlementLedgerReportTestCase):
    """The grid describes itself, and every column it declares is populated."""

    def test_every_column_has_a_label_fieldname_and_fieldtype(self) -> None:
        """Every column declares a label, a fieldname and a fieldtype."""
        for column in ledger_report.get_columns():
            with self.subTest(column=column.get("fieldname")):
                self.assertTrue(column["label"])
                self.assertTrue(column["fieldname"])
                self.assertTrue(column["fieldtype"])

    def test_every_money_column_resolves_through_the_currency_column(self) -> None:
        """Every Currency column resolves through the row's currency field."""
        for column in ledger_report.get_columns():
            if column["fieldtype"] != "Currency":
                continue
            with self.subTest(column=column["fieldname"]):
                self.assertEqual(column["options"], "currency")

    def test_every_column_is_present_on_the_row(self) -> None:
        """Every declared column has a key on the row."""
        settlement = self.booked(settlement_id="771501")
        row = self.row_for(settlement.name)

        for column in ledger_report.get_columns():
            with self.subTest(column=column["fieldname"]):
                self.assertIn(column["fieldname"], row)

    def test_the_row_carries_nothing_the_grid_does_not_show(self) -> None:
        """The row carries exactly the fieldnames the grid declares."""
        settlement = self.booked(settlement_id="771502")
        row = self.row_for(settlement.name)

        self.assertEqual(set(row), {column["fieldname"] for column in ledger_report.get_columns()})


class TestSettlementLedgerAgreement(SettlementLedgerReportTestCase):
    """A payout whose entry says what Paystack said reports no discrepancy."""

    def test_a_booked_payout_ties_out(self) -> None:
        """A booked payout with its capture linked reports no discrepancy."""
        settlement = self.booked(settlement_id="771511")
        self.capture(settlement.name, 1000)

        row = self.row_for(settlement.name)

        self.assertEqual(row["discrepancy"], "")
        self.assertEqual(row["difference"], 0)

    def test_the_ledger_columns_are_read_off_the_journal_entry(self) -> None:
        """The booked figures and the journal entry are read off the posting."""
        settlement = self.booked(settlement_id="771512")
        self.capture(settlement.name, 1000)

        row = self.row_for(settlement.name)

        self.assertEqual(row["booked_gross"], 1000)
        self.assertEqual(row["booked_fee"], 15)
        self.assertEqual(row["booked_net"], 985)
        self.assertEqual(row["booked_deductions"], 0)
        self.assertEqual(row["journal_entry"], settlement.journal_entry)

    def test_a_deduction_reads_back_as_a_debit_to_suspense(self) -> None:
        """A payout with a deduction reports it booked and ties out."""
        settlement = self.booked(settlement_id="771513", deductions=50, net_amount=935)
        self.capture(settlement.name, 1000)

        row = self.row_for(settlement.name)

        self.assertEqual(row["booked_deductions"], 50)
        self.assertEqual(row["discrepancy"], "")

    def test_two_captures_are_counted_and_totalled_together(self) -> None:
        """Two captures naming a payout are counted and totalled together."""
        settlement = self.booked(settlement_id="771514")
        self.capture(settlement.name, 400)
        self.capture(settlement.name, 600)

        row = self.row_for(settlement.name)

        self.assertEqual(row["capture_count"], 2)
        self.assertEqual(row["captured_amount"], 1000)
        self.assertEqual(row["unlinked_amount"], 0)
        self.assertEqual(row["discrepancy"], "")


class TestSettlementLedgerDiscrepancies(SettlementLedgerReportTestCase):
    """Every way a payout can fail to tie out is named in one sentence."""

    def test_an_unposted_payout_reports_the_reason_it_was_refused(self) -> None:
        """An unposted payout reports the gateway setting behind its refusal."""
        GatewaySettingFactory.configure_settlement(self.gateway, self.suspense, None, None)
        settlement = self.settlement(settlement_id="771521")
        self.assertIsNone(post_settlement_entry(settlement))

        row = self.row_for(settlement.name)

        self.assertIn("Paystack Gateway Setting", row["discrepancy"])

    def test_a_payout_never_put_to_the_poster_still_reads_as_unposted(self) -> None:
        """A payout with no recorded reason reports the report's own sentence."""
        settlement = self.settlement(settlement_id="771522")

        row = self.row_for(settlement.name)

        self.assertEqual(row["discrepancy"], "Not posted: no journal entry cleared this payout.")

    def test_a_cancelled_journal_entry_reads_as_unbooked(self) -> None:
        """A cancelled entry reports as cancelled with zero booked figures."""
        settlement = self.booked(settlement_id="771523")
        entry = frappe.get_doc("Journal Entry", settlement.journal_entry)
        entry.flags.ignore_permissions = True
        entry.cancel()

        row = self.row_for(settlement.name)

        self.assertIn("cancelled", row["discrepancy"])
        self.assertEqual(row["booked_gross"], 0)
        self.assertEqual(row["booked_net"], 0)
        self.assertEqual(row["difference"], 985)

    def test_an_entry_posted_to_other_accounts_is_not_read_as_cancelled(self) -> None:
        """An entry on accounts the gateway no longer names reports a ledger gap."""
        settlement = self.booked(settlement_id="771524")
        GatewaySettingFactory.configure_settlement(
            self.gateway,
            self.ledger_account("Test Paystack Suspense Two", "Asset", "Bank"),
            self.ledger_account("Test Paystack Bank Two", "Asset", "Bank"),
            self.ledger_account("Test Paystack Fees Two", "Expense"),
        )

        row = self.row_for(settlement.name)

        self.assertNotIn("cancelled", row["discrepancy"])
        self.assertIn("ledger", row["discrepancy"])
        self.assertEqual(row["booked_net"], 0)

    def test_a_company_without_a_gateway_cannot_resolve_the_accounts(self) -> None:
        """A company with no enabled gateway reports 'not enabled'."""
        settlement = self.booked(settlement_id="771525")
        self.disable_company_gateways()

        row = self.row_for(settlement.name)

        self.assertIn("not enabled", row["discrepancy"])

    def test_a_gross_the_ledger_does_not_carry_is_named(self) -> None:
        """A gross the ledger does not carry is named in the discrepancy."""
        settlement = self.booked(settlement_id="771526")
        settlement.db_set("gross_amount", 1200, update_modified=False)
        self.capture(settlement.name, 1200)

        self.assertIn("gross", self.row_for(settlement.name)["discrepancy"])

    def test_a_fee_the_ledger_does_not_carry_is_named(self) -> None:
        """A fee the ledger does not carry is named in the discrepancy."""
        settlement = self.booked(settlement_id="771527")
        settlement.db_set("total_fees", 40, update_modified=False)
        self.capture(settlement.name, 1000)

        self.assertIn("fees", self.row_for(settlement.name)["discrepancy"])

    def test_a_deduction_the_ledger_does_not_carry_is_named(self) -> None:
        """A deduction the ledger does not carry is named in the discrepancy."""
        settlement = self.booked(settlement_id="771528")
        settlement.db_set("deductions", 60, update_modified=False)
        self.capture(settlement.name, 1000)

        self.assertIn("deductions", self.row_for(settlement.name)["discrepancy"])

    def test_a_net_the_bank_was_not_debited_is_named(self) -> None:
        """A net the bank was not debited is named, with the difference."""
        settlement = self.booked(settlement_id="771529")
        settlement.db_set("net_amount", 900, update_modified=False)
        self.capture(settlement.name, 1000)

        row = self.row_for(settlement.name)

        self.assertIn("net", row["discrepancy"])
        self.assertEqual(row["difference"], -85)

    def test_captures_short_of_the_gross_are_reported(self) -> None:
        """Captures short of the gross report the unlinked amount."""
        settlement = self.booked(settlement_id="771530")
        self.capture(settlement.name, 400)

        row = self.row_for(settlement.name)

        self.assertEqual(row["unlinked_amount"], 600)
        self.assertIn("Captures naming this payout", row["discrepancy"])

    def test_a_capture_another_tenant_holds_never_clears_this_payout(self) -> None:
        """A capture another company holds is left out of the count and the total."""
        settlement = self.booked(settlement_id="771532")
        foreign = PaymentLogFactory.create_completed(amount=1000, company=OTHER_COMPANY)
        self.addCleanup(PaymentLogFactory.cleanup, foreign)
        frappe.db.set_value(PAYMENT_LOG, foreign, "settlement", settlement.name)

        row = self.row_for(settlement.name)

        self.assertEqual(row["capture_count"], 0)
        self.assertEqual(row["unlinked_amount"], 1000)
        self.assertIn("Captures naming this payout", row["discrepancy"])

    def test_a_payout_no_capture_names_reports_its_whole_gross(self) -> None:
        """A payout no capture names reports its whole gross as unlinked."""
        settlement = self.booked(settlement_id="771531")

        row = self.row_for(settlement.name)

        self.assertEqual(row["capture_count"], 0)
        self.assertEqual(row["captured_amount"], 0)
        self.assertEqual(row["unlinked_amount"], 1000)


class TestSettlementLedgerFilters(SettlementLedgerReportTestCase):
    """The report answers for one company, one date range, one question."""

    def test_another_company_sees_none_of_these_payouts(self) -> None:
        """The report for another company leaves out these payouts."""
        settlement = self.booked(settlement_id="771541")

        self.assertIsNone(self.row_for(settlement.name, company=OTHER_COMPANY))

    def test_a_date_range_keeps_only_the_payouts_inside_it(self) -> None:
        """A closed range keeps the payout inside it and drops those outside."""
        inside = self.dated("771542", days_ago=3)
        older = self.dated("771543", days_ago=30)
        newer = self.dated("771552", days_ago=1)

        window = {"from_date": add_days(today(), -7), "to_date": add_days(today(), -2)}

        self.assertIsNotNone(self.row_for(inside, **window))
        self.assertIsNone(self.row_for(older, **window))
        self.assertIsNone(self.row_for(newer, **window))

    def test_a_start_date_alone_drops_everything_before_it(self) -> None:
        """from_date alone keeps the payouts on or after it."""
        recent = self.dated("771544", days_ago=2)
        old = self.dated("771545", days_ago=40)

        window = {"from_date": add_days(today(), -7)}

        self.assertIsNotNone(self.row_for(recent, **window))
        self.assertIsNone(self.row_for(old, **window))

    def test_an_end_date_alone_drops_everything_after_it(self) -> None:
        """to_date alone keeps the payouts on or before it."""
        recent = self.dated("771546", days_ago=2)
        old = self.dated("771547", days_ago=40)

        window = {"to_date": add_days(today(), -7)}

        self.assertIsNone(self.row_for(recent, **window))
        self.assertIsNotNone(self.row_for(old, **window))

    def test_no_date_range_reports_every_payout(self) -> None:
        """Omitting both dates reports every payout."""
        recent = self.dated("771548", days_ago=2)
        old = self.dated("771549", days_ago=400)

        self.assertIsNotNone(self.row_for(recent))
        self.assertIsNotNone(self.row_for(old))

    def test_only_discrepancies_drops_the_payouts_that_tie_out(self) -> None:
        """only_discrepancies keeps the payouts that do not tie out."""
        clean = self.booked(settlement_id="771550")
        self.capture(clean.name, 1000)
        broken = self.settlement(settlement_id="771551")

        self.assertIsNone(self.row_for(clean.name, only_discrepancies=1))
        self.assertIsNotNone(self.row_for(broken.name, only_discrepancies=1))

    def test_execute_without_filters_still_describes_the_grid(self) -> None:
        """execute() with no filters returns every column and no rows."""
        columns, data = ledger_report.execute()

        self.assertEqual(len(columns), len(ledger_report.get_columns()))
        self.assertEqual(data, [])


class TestSettlementLedgerEmptyRange(SettlementLedgerReportTestCase):
    """A range holding no payout is answered without a query."""

    def test_no_payouts_means_no_captures_are_looked_up(self) -> None:
        """capture_totals returns an empty mapping for an empty payout list."""
        log = PaymentLogFactory.create_completed(amount=1000)
        self.addCleanup(PaymentLogFactory.cleanup, log)

        self.assertEqual(ledger_report.capture_totals([], TEST_COMPANY), {})

    def test_nothing_posted_means_the_ledger_is_never_asked(self) -> None:
        """ledger_totals returns an empty mapping without querying."""
        gateway = frappe.get_doc(GATEWAY_DOCTYPE, self.gateway)

        with patch(f"{LEDGER_REPORT_MODULE}.frappe.get_all") as mock_get_all:
            self.assertEqual(ledger_report.ledger_totals([], gateway), {})

        mock_get_all.assert_not_called()
