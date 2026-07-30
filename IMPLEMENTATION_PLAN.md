# Frappe Paystack - Phased Implementation Plan

> **Goal**: Transform `frappe_paystack` from a minimal, self-contained integration into a production-grade, fully-featured Paystack payment gateway for Frappe/ERPNext - covering refunds/returns, standard `payments` app integration, Webshop support, subscriptions, fee accounting, security hardening, and analytics.

This plan is organized into **7 phases**, ordered by dependency and business priority. Each phase is independently shippable.

---

## Phase Overview

| Phase | Title | Priority | Est. Effort | Dependencies |
|-------|-------|----------|-------------|--------------|
| **1** | Security, Reliability & Bug Fixes | P0 - Critical | Medium | None |
| **2** | Returns, Refunds & Reversals | P0 - Critical | Large | None (standalone) |
| **3** | Standard `payments` App Integration | P1 - High | Large | None |
| **4** | Payment Entry & Ledger Correctness | P1 - High | Medium | Phase 3 |
| **5** | Ecommerce / Webshop & Customer Portal | P2 - Medium | Medium | Phase 3 |
| **6** | Subscriptions, Fees & Settlement | P2 - Medium | Large | Phase 3, 4 |
| **7** | Analytics, Automation & Polish | P3 - Low | Medium | All prior |

---

## Phase 1 - Security, Reliability & Bug Fixes (P0)

> **Rationale**: Before adding features, the existing code must be safe and reliable in production. Several current patterns risk duplicate payments, permission escalation, and silent failures.

### 1.1 Webhook Idempotency & Duplicate Prevention
**Problem**: Webhook retries can create duplicate Payment Entries. The `transaction_id` field exists but isn't checked for uniqueness before processing.

**Tasks**:
- [ ] Add a unique index on `transaction_id` in `Paystack Payment Log` (schema change via doctype JSON).
- [ ] In `process_webhook_event()`, check if a log with this `transaction_id` already has status `Processed`/`Completed` → skip and return `201` (idempotent).
- [ ] Add an `idempotency_key` field (Paystack reference) and guard against re-processing.
- [ ] Unit test: send the same webhook twice → only one Payment Entry created.

**Files**: `api.py`, `paystack_payment_log.json`, `paystack_payment_log.py`, tests.

### 1.2 Integration Request Logging
**Problem**: No audit trail of webhook payloads or API calls. Standard Frappe integrations log everything to `Integration Request`.

**Tasks**:
- [ ] Create a helper `log_integration_request(status, url, request_data, response_data, error=None)` in `utils.py`.
- [ ] Wrap every Paystack API call (`validate_payment`, refund API, settlement API) and every webhook receipt with `Integration Request` creation.
- [ ] Link the `Integration Request` to the `Paystack Payment Log` via a `reference_doctype`/`reference_docname` field.

**Files**: `utils.py`, `api.py`, `paystack_payment_log.py`.

### 1.3 Remove `developer_mode` Signature Bypass
**Problem**: `paystack_webhook()` skips signature verification when `developer_mode` is on - dangerous if left enabled in production.

**Tasks**:
- [ ] Add a `test_mode` checkbox to `Paystack Gateway Setting` (separate from Frappe's developer mode).
- [ ] Always verify the signature. In test mode, allow Paystack **test** keys but still verify.
- [ ] Remove the `if frappe.local.conf.developer_mode: process_webhook_event(data); return` block.
- [ ] Log unverified webhook attempts to `Integration Request` with status `Failed`.

**Files**: `api.py`, `paystack_gateway_setting.json`, `paystack_gateway_setting.py`.

### 1.4 Separate Webhook Secret
**Problem**: The webhook signature is verified using `secret_key`. Paystack supports a dedicated webhook secret.

**Tasks**:
- [ ] Add `webhook_secret` (Password) field to `Paystack Gateway Setting`.
- [ ] Fall back to `secret_key` if `webhook_secret` is empty (backward compatibility).
- [ ] Update `resolve_paystack_settings()` to return `webhook_secret`.
- [ ] Update `paystack_webhook()` to use `webhook_secret` for verification.

**Files**: `paystack_gateway_setting.json`, `utils.py`, `api.py`.

### 1.5 Remove `frappe.set_user("administrator")` in `on_update()`
**Problem**: User-switching is fragile and a security smell. It can leave the session in an elevated state if an exception occurs before `frappe.set_user("Guest")`.

**Tasks**:
- [ ] Remove `frappe.set_user("administrator")` / `frappe.set_user("Guest")`.
- [ ] Rely on `ignore_permissions=True` (already used) for PE creation.
- [ ] Wrap PE creation in a `try/finally` that ensures no session state leaks.
- [ ] Consider running PE creation in a background job (`frappe.enqueue`) to avoid webhook timeout.

**Files**: `paystack_payment_log.py`.

### 1.6 IP Allowlisting for Webhooks
**Problem**: No source IP validation - any server can hit the webhook endpoint.

**Tasks**:
- [ ] Add `allowed_webhook_ips` (Small Text, one per line) to `Paystack Gateway Setting`.
- [ ] In `paystack_webhook()`, validate `frappe.local.request_ip` against the allowlist if configured.
- [ ] Ship default Paystack IP ranges as a comment/placeholder.

**Files**: `paystack_gateway_setting.json`, `api.py`.

### 1.7 Fix `on_trash()` Over-Restriction
**Problem**: `on_trash()` blocks ALL deletion, including Pending/Failed logs that created no Payment Entry.

**Tasks**:
- [ ] Allow deletion of logs with status `Pending` or `Failed` and no `payment_entry`.
- [ ] Block deletion of `Processed`/`Completed` logs (or require cancellation of the linked PE first).
- [ ] Add a clear error message explaining why deletion is blocked.

**Files**: `paystack_payment_log.py`.

### 1.8 Fix Currency Inconsistency
**Problem**: `utils.py` lists 5 supported currencies (`NGN, USD, GHS, ZAR, KES`), but `paystack_gateway_setting.py` lists only 4 (`NGN, GHS, ZAR, USD`).

**Tasks**:
- [ ] Unify the supported currencies list in a single constant (`SUPPORTED_CURRENCIES` in `utils.py`).
- [ ] Import it in `paystack_gateway_setting.py` instead of hardcoding.
- [ ] Add `KES` (Kenyan Shilling) to the settings doctype's validation.

**Files**: `utils.py`, `paystack_gateway_setting.py`.

### 1.9 Dead Code Cleanup
**Problem**: `www/paystack-checkout/index.py` has an unused `get_payment_request()` function. `paystack_gateway_setting.py` has a `get_payment_url()` that returns a legacy URL.

**Tasks**:
- [ ] Remove or clearly mark `get_payment_request()` as deprecated.
- [ ] Remove the legacy `get_payment_url()` or repurpose it for Phase 3.
- [ ] Remove the trailing `document.querySelector("paymentBTN")` line in `paystack-checkout/index.js`.

**Files**: `www/paystack-checkout/index.py`, `paystack_gateway_setting.py`, `public/js/paystack-checkout/index.js`.

### Phase 1 Deliverables
- Hardened webhook with idempotency, IP allowlisting, separate secret, full audit logging.
- No permission escalation in PE creation.
- Clean, consistent currency handling.
- Integration Request audit trail for every API interaction.

### Phase 1 Testing
- Unit tests for signature verification (valid, invalid, missing).
- Unit test for idempotency (duplicate webhook → single PE).
- Unit test for IP allowlisting.
- Integration test: full webhook → PE creation flow with mocked Paystack API.

---

## Phase 2 - Returns, Refunds & Reversals (P0)

> **Rationale**: Currently impossible to refund a Paystack payment or reverse the ledger when a credit note is issued. This is a critical business requirement.

### 2.1 New Doctype: `Paystack Refund Log`
**Purpose**: Track every refund initiated from ERPNext, mirroring the Payment Log pattern.

**Fields**:
- `payment_log` (Link → `Paystack Payment Log`, required)
- `company` (Link → Company, fetched)
- `linked_doctype` / `linked_docname` (the credit note or original invoice)
- `transaction_id` (from the original payment)
- `refund_amount` (Currency)
- `currency` (Data)
- `status` (Select: Pending → Processed → Completed → Failed)
- `refund_reference` (Data - Paystack refund ID)
- `refund_reason` (Small Text)
- `reversal_payment_entry` (Link → Payment Entry)
- `raw_response` (Text/JSON)
- `errors` (Small Text)
- `created_by`, `creation` (audit)

**Tasks**:
- [ ] Create doctype JSON + Python controller.
- [ ] `validate()`: ensure `refund_amount` ≤ original `amount_paid`, currency matches, original log is `Completed`.
- [ ] `on_update()`: when status = `Processed`, create a **reversal Payment Entry** (`payment_type=Pay`):
  - `paid_from = suspense_account`, `paid_to = debit_to` (receivable)
  - `references` → the credit note (`is_return=1` Sales Invoice)
  - `paid_amount = refund_amount`
  - Submit, link `reversal_payment_entry`.
- [ ] `on_trash()`: block if `reversal_payment_entry` exists.

**Files**: New `frappe_paystack/doctype/paystack_refund_log/` directory.

### 2.2 Paystack Refund API Integration
**Purpose**: Actually call Paystack to refund money to the customer's card.

**Tasks**:
- [ ] Add `initiate_refund(transaction_id, amount, currency, reason, merchant_note)` to `utils.py`.
- [ ] Calls `POST https://api.paystack.co/transaction/refund/{transaction_id}` with `Authorization: Bearer {secret_key}`.
- [ ] Body: `{"amount": amount_in_kobo, "merchant_note": reason}`.
- [ ] Parse response → return `{status, reference, amount, currency}`.
- [ ] Log to `Integration Request`.

**Files**: `utils.py`.

### 2.3 Credit Note → Auto-Refund Hook
**Purpose**: When a credit note is submitted against a Paystack-paid invoice, automatically initiate a refund.

**Tasks**:
- [ ] Add `doc_events` hook in `hooks.py`:
  ```python
  doc_events = {
      "Sales Invoice": {
          "on_submit": "frappe_paystack.hooks.sales_invoice_on_submit",
          "on_cancel": "frappe_paystack.hooks.sales_invoice_on_cancel",
      }
  }
  ```
- [ ] Create `hooks.py` (app-level, not the framework `hooks.py`) with `sales_invoice_on_submit(doc, method)`:
  - If `doc.is_return == 1` and `doc.return_against` is set:
    - Find `Paystack Payment Log` where `linked_docname = doc.return_against` and `status = Completed`.
    - If found and `refund_amount > 0`:
      - Create `Paystack Refund Log` (Pending).
      - Call `initiate_refund()`.
      - Update Refund Log status based on API response.
- [ ] Add a setting `auto_refund_on_credit_note` (Check, default 0) to `Paystack Gateway Setting` - opt-in, not forced.

**Files**: `hooks.py` (framework), new `frappe_paystack/hooks.py`, `paystack_gateway_setting.json`.

### 2.4 Refund Webhook Handling
**Purpose**: Paystack sends `refund.processed` (and `refund.failed`) webhook events.

**Tasks**:
- [ ] Extend `process_webhook_event()` to detect event type:
  - `charge.success` → existing payment flow.
  - `refund.processed` → update Refund Log status = `Processed` → trigger `on_update()` (creates reversal PE).
  - `refund.failed` → update Refund Log status = `Failed`, log error.
- [ ] Map the refund webhook's `transaction.id` to the original Payment Log's `transaction_id`, then find the Refund Log.

**Files**: `api.py`, `paystack_refund_log.py`.

### 2.5 Manual Refund Button on Payment Log
**Purpose**: Allow manual refunds without requiring a credit note.

**Tasks**:
- [ ] Add client JS button on `Paystack Payment Log` form (when `status=Completed` and `transaction_id` set):
  - "Refund" → dialog with `amount`, `reason`, `merchant_note`.
  - Calls a whitelisted method `frappe_paystack.api.initiate_refund_from_log(log_name, amount, reason)`.
- [ ] Server method creates Refund Log + calls Paystack API.

**Files**: `paystack_payment_log.js`, `api.py`.

### 2.6 Partial Refunds
**Purpose**: Credit notes may be for less than the full payment.

**Tasks**:
- [ ] In the credit note hook, compute `refund_amount = min(credit_note_grand_total, original_amount_paid)`.
- [ ] If credit note > amount paid, refund only `amount_paid`; the remaining credit note outstanding is handled by ERPNext normally (store credit / future allocation).
- [ ] Track `total_refunded` on the Payment Log (sum of all Refund Logs) to prevent over-refunding.

**Files**: `paystack_payment_log.py`, `paystack_refund_log.py`, `paystack_payment_log.json` (add `total_refunded` field).

### 2.7 Refund Receipt Email
**Purpose**: Notify the customer when a refund is processed.

**Tasks**:
- [ ] On Refund Log `Completed`, email the customer a refund receipt (PDF print format).
- [ ] Create a `Paystack Refund Receipt` print format.

**Files**: `paystack_refund_log.py`, new print format.

### Phase 2 Deliverables
- `Paystack Refund Log` doctype with full lifecycle.
- Paystack Refund API integration.
- Automatic refund on credit note submission (opt-in).
- Manual refund button.
- Partial refund support with over-refund prevention.
- Refund webhook handling.
- Reversal Payment Entry creation with correct GL posting.

### Phase 2 Testing
- Unit test: `initiate_refund()` with mocked API.
- Integration test: create invoice → pay via Paystack (mocked webhook) → create credit note → assert Refund Log created + reversal PE created + GL reversed.
- Test: partial refund (credit note < payment).
- Test: over-refund prevention (second refund exceeds total).
- Test: manual refund button flow.

---

## Phase 3 - Standard `payments` App Integration (P1)

> **Rationale**: The app currently bypasses Frappe's entire payment infrastructure. Integrating with the `payments` app unlocks the standard Payment Gateway selector, Payment Request lifecycle, and Webshop support.

### 3.1 Register Paystack as a `Payment Gateway`
**Tasks**:
- [ ] Add `after_install` hook in `hooks.py`:
  ```python
  after_install = "frappe_paystack.setup.after_install"
  ```
- [ ] Create `setup.py` with `after_install()`:
  - Create a `Payment Gateway` record (name="Paystack").
  - Create a `Mode of Payment` "Paystack" (already in fixtures, ensure it exists).
- [ ] Add a migration patch for existing installs.

**Files**: `hooks.py`, new `setup.py`, `patches.txt`.

### 3.2 Implement `PaystackController` (Gateway Controller)
**Purpose**: Implement the standard controller interface so `get_payment_gateway_controller("Paystack")` returns it.

**Tasks**:
- [ ] Create `frappe_paystack/payment_gateway.py` with class `PaystackController`:
  ```python
  class PaystackController:
      def validate_transaction_currency(self, currency): ...
      def validate_minimum_transaction_amount(self, currency, amount): ...
      def get_payment_url(self, **kwargs) -> str: ...
      def on_payment_request_submission(self, pr) -> bool: ...
      def request_for_payment(self, **kwargs): ...  # Phone channel (optional)
  ```
- [ ] `get_payment_url()`: create a `Paystack Payment Log` from the Payment Request, return `{site_url}/paystack-checkout/{log.name}`.
- [ ] Register the controller so `get_payment_gateway_controller("Paystack")` finds it (via the `payment_gateway` hook or by naming convention).

**Files**: new `frappe_paystack/payment_gateway.py`, `hooks.py`.

### 3.3 Auto-Create `Payment Gateway Account`
**Tasks**:
- [ ] In `after_install()` and in `Paystack Gateway Setting.validate()` (when enabled):
  - Create/update a `Payment Gateway Account`:
    - `payment_gateway = "Paystack"`
    - `payment_account = suspense_account`
    - `currency`, `company`, `is_default = 1`
- [ ] Hook into `payment_gateway_enabled` if the `payments` app exposes it.

**Files**: `setup.py`, `paystack_gateway_setting.py`.

### 3.4 Bridge Payment Request ↔ Paystack Payment Log
**Purpose**: When a standard Payment Request is created for Paystack, link it to the Payment Log.

**Tasks**:
- [ ] In `PaystackController.get_payment_url()`, create the Payment Log and store the Payment Request name on it (new field `payment_request` on Payment Log).
- [ ] On webhook success, in addition to the existing flow, call `PaymentRequest.set_as_paid()` (or update its status) so the standard lifecycle works.
- [ ] Set `payment_request` on the Payment Entry reference row.

**Files**: `payment_gateway.py`, `api.py`, `paystack_payment_log.json` (add `payment_request` field).

### 3.5 Standard "Make Payment Request" Button Support
**Purpose**: ERPNext's built-in "Make Payment Request" on Sales Invoice / Sales Order should now offer Paystack.

**Tasks**:
- [ ] Verify the `Payment Gateway Account` appears in the gateway selector.
- [ ] Test that `make_payment_request()` → `get_payment_url()` → Paystack checkout works end-to-end.
- [ ] Optionally keep the custom `doctype_js` buttons as a shortcut, but route them through the standard Payment Request flow.

**Files**: `sales_invoice.js`, `sales_order.js` (refactor to use Payment Request).

### Phase 3 Deliverables
- Paystack registered as a standard `Payment Gateway`.
- `PaystackController` implementing the full controller interface.
- Auto-created `Payment Gateway Account`.
- Payment Request lifecycle integration.
- Paystack appears in all standard gateway selectors.

### Phase 3 Testing
- Test: `get_payment_gateway_controller("Paystack")` returns `PaystackController`.
- Test: create Payment Request for a Sales Invoice with Paystack → checkout URL generated.
- Test: webhook → Payment Request status = Paid.
- Test: `Payment Gateway Account` auto-created on settings save.

---

## Phase 4 - Payment Entry & Ledger Correctness (P1)

> **Rationale**: The manual Payment Entry construction in `on_update()` misses edge cases (payment terms, advances, deductions, exchange gain/loss). Using ERPNext's factory ensures correctness.

### 4.1 Use `get_payment_entry()` Factory
**Tasks**:
- [ ] Replace manual PE construction in `PaystackPaymentLog.on_update()` with:
  ```python
  from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
  pe = get_payment_entry(self.linked_doctype, self.linked_docname,
                         bank_account=suspense_account,
                         created_from_payment_request=True)
  pe.reference_no = self.payment_reference
  pe.reference_date = self.payment_date
  pe.mode_of_payment = settings.mode_of_payment
  pe.save(ignore_permissions=True)
  pe.submit()
  ```
- [ ] This handles: payment terms splitting, advance allocation, deductions, exchange rates, and correct GL posting automatically.

**Files**: `paystack_payment_log.py`.

### 4.2 Link Payment Entry to Payment Request
**Tasks**:
- [ ] Set `payment_request` on the PE reference rows (from the Payment Log's `payment_request` field).
- [ ] Ensure `update_payment_requests()` runs on PE submit to mark the PR as Paid.

**Files**: `paystack_payment_log.py`.

### 4.3 Multi-Currency Exchange Rate Handling
**Problem**: Currently uses `inv.conversion_rate` which may be stale. Paystack's actual conversion may differ.

**Tasks**:
- [ ] Fetch the real exchange rate from the Paystack transaction payload (`data.exchange_rate` or compute from `amount` / `amount_paid` in different currencies).
- [ ] Set `source_exchange_rate` and `target_exchange_rate` on the PE from the Paystack rate.
- [ ] Trigger `make_exchange_gain_loss_journal()` if there's a rate difference.

**Files**: `paystack_payment_log.py`, `api.py`.

### 4.4 Dunning Payment Support
**Purpose**: Allow Paystack payments against Dunning documents (which wrap Sales Invoices + interest/fees).

**Tasks**:
- [ ] Add "Dunning" to the supported `linked_doctype` values.
- [ ] In `create_payment_link()`, handle Dunning: `amount = dunning.grand_total`.
- [ ] In PE creation, use `get_payment_entry("Dunning", dunning_name)` which appends invoice references + a deduction for dunning interest.

**Files**: `api.py`, `paystack_payment_log.py`, `sales_invoice.js` (add button to Dunning form via `doctype_js`).

### 4.5 POS Integration
**Purpose**: Use Paystack as a payment method at the POS counter.

**Tasks**:
- [ ] Add Paystack as a `Mode of Payment` usable in POS Sales Invoice `payments` table.
- [ ] On POS submit, if a Paystack payment mode is used, initiate an inline checkout (or assume the merchant uses Paystack's POS terminal and just records the reference).
- [ ] Validate the transaction via `validate_payment()` before allowing submit.

**Files**: `paystack_gateway_setting.py`, new POS JS hook, `sales_invoice.py` (validation hook).

### Phase 4 Deliverables
- Correct Payment Entry creation via the standard factory.
- Proper Payment Request linkage and status updates.
- Accurate multi-currency handling with FX gain/loss.
- Dunning payment support.
- POS payment mode support.

### Phase 4 Testing
- Test: PE created via `get_payment_entry()` has correct references, GL, and outstanding update.
- Test: multi-currency invoice → PE exchange rate from Paystack → FX journal created.
- Test: Dunning payment link → PE with interest deduction.
- Test: POS invoice with Paystack mode → transaction verified.

---

## Phase 5 - Ecommerce / Webshop & Customer Portal (P2)

> **Rationale**: Once Phase 3 is done, Webshop can use Paystack. This phase adds ecommerce-specific features and expands the customer portal.

### 5.1 Webshop Checkout Integration
**Tasks**:
- [ ] Verify Webshop's `make_payment_request(order_type="Shopping Cart")` routes to Paystack (depends on Phase 3).
- [ ] Test the full flow: Shopping Cart → Sales Order → Payment Request → Paystack checkout → webhook → `set_as_paid()` → Payment Entry + Sales Invoice auto-created.
- [ ] Handle `on_payment_authorized(status)` redirect to `/orders/{name}` or `payment_success_url`.

**Files**: `payment_gateway.py`, `www/paystack-checkout/index.py`.

### 5.2 Hosted Checkout Option
**Purpose**: Offer Paystack's hosted checkout page as an alternative to the inline `PaystackPop` (better for mobile, simpler).

**Tasks**:
- [ ] Add `checkout_mode` (Select: Inline / Hosted) to `Paystack Gateway Setting`.
- [ ] If Hosted: call Paystack's `POST /transaction/initialize` to get an `authorization_url`, redirect the customer there instead of rendering the inline page.
- [ ] The hosted callback redirects back to `/paystack-checkout/{reference}` for confirmation.

**Files**: `paystack_gateway_setting.json`, `payment_gateway.py`, `api.py`, `www/paystack-checkout/index.py`.

### 5.3 Expand Customer Portal (`/my-payments`)
**Tasks**:
- [ ] Show all invoices (paid + unpaid), not just unpaid.
- [ ] Add a "Refunds" tab showing `Paystack Refund Log` entries.
- [ ] Add downloadable PDF receipts for completed payments.
- [ ] Add a "Saved Cards" section (depends on §5.5).
- [ ] Add search/filter by date range and status.

**Files**: `www/my-payments/index.py`, `www/my-payments/index.html`.

### 5.4 Payment Link Expiry
**Purpose**: Payment links shouldn't be valid forever.

**Tasks**:
- [ ] Add `expires_at` (Datetime) to `Paystack Payment Log`, set to `now + expiry_hours` (configurable in settings, default 24h).
- [ ] In `validate_payment_link()` and the checkout page, refuse to load if expired.
- [ ] Show a clear "Link Expired" message with a button to request a new link.

**Files**: `paystack_payment_log.json`, `api.py`, `www/paystack-checkout/index.py`, `www/paystack-checkout/index.html`.

### 5.5 Saved Cards / Customer Authorizations
**Purpose**: Paystack returns a reusable `authorization` token for cards. Store it for repeat payments.

**Tasks**:
- [ ] New doctype `Paystack Customer Authorization`:
  - `customer` (Link), `authorization_code` (Data), `card_type`, `last4`, `exp_month`, `exp_year`, `bank`, `channel`, `is_active`.
- [ ] On webhook `charge.success`, if `data.authorization.reusable == true`, save the authorization.
- [ ] On the checkout page, if the customer has saved authorizations, show "Pay with saved card ••••{last4}".
- [ ] To charge a saved card: call `POST /transaction/charge_authorization` with the `authorization_code`.

**Files**: New `frappe_paystack/doctype/paystack_customer_authorization/`, `api.py`, `www/paystack-checkout/index.html`, `www/paystack-checkout/index.js`.

### 5.6 QR Code Payment Links
**Purpose**: Useful for printed/emailed invoices.

**Tasks**:
- [ ] Generate a QR code for the payment link (using a JS library or server-side `qrcode` Python package).
- [ ] Display it on the checkout page and in the "Send Payment Link" dialog.
- [ ] Add it to the invoice print format (§5.7).

**Files**: `www/paystack-checkout/index.html`, `sales_invoice.js`.

### 5.7 Invoice Print Format with Paystack Link
**Tasks**:
- [ ] Create a custom Print Format for Sales Invoice that embeds:
  - The Paystack checkout link (as a clickable URL + QR code).
  - Payment status indicator.
- [ ] Make it selectable in the Invoice print dialog.

**Files**: New print format (via `Print Format` doctype or HTML template).

### Phase 5 Deliverables
- Full Webshop checkout via Paystack.
- Hosted checkout option.
- Rich customer portal with refunds, receipts, saved cards.
- Payment link expiry.
- Saved card / tokenized payments.
- QR code + print format integration.

### Phase 5 Testing
- E2E test: Shopping Cart → Sales Order → Paystack → Sales Invoice + PE created.
- Test: hosted checkout redirect and callback.
- Test: expired link refused.
- Test: saved card charge via `charge_authorization`.
- Test: portal shows correct data for a logged-in customer.

---

## Phase 6 - Subscriptions, Fees & Settlement (P2)

> **Rationale**: Paystack supports subscriptions and provides fee/settlement data. This phase adds recurring revenue support and proper financial reconciliation.

### 6.1 Paystack Plans & Subscriptions
**Purpose**: Map ERPNext `Subscription` / `Subscription Plan` to Paystack Plans and Subscriptions.

**Tasks**:
- [ ] New doctype `Paystack Plan`:
  - `subscription_plan` (Link → ERPNext Subscription Plan), `paystack_plan_code` (Data), `amount`, `interval`, `currency`, `is_active`.
- [ ] On ERPNext Subscription Plan creation, call `POST /plan` to create a Paystack Plan.
- [ ] On ERPNext Subscription activation:
  - Call `POST /subscription` with customer email + plan code.
  - Store `subscription_code` and `email_token` on the ERPNext Subscription (custom fields).
- [ ] On ERPNext Subscription cancellation: `POST /subscription/{code}/disable`.

**Files**: New `frappe_paystack/doctype/paystack_plan/`, `hooks.py` (doc_events on Subscription), custom fields on Subscription.

### 6.2 Recurring Charge Webhook → Auto-Invoice
**Purpose**: When Paystack charges a recurring subscription, auto-create the Sales Invoice + Payment Entry.

**Tasks**:
- [ ] Handle `charge.success` webhooks where `data.plan` is set (recurring charge):
  - Find the ERPNext Subscription by `subscription_code`.
  - Call `Subscription.create_invoice()` to generate a Sales Invoice.
  - Create Payment Entry from the webhook (same as normal flow).
  - Update Subscription status.
- [ ] Handle `subscription.disable` webhook → mark ERPNext Subscription as cancelled.

**Files**: `api.py`, `paystack_payment_log.py`.

### 6.3 Fee Accounting
**Purpose**: Track Paystack fees as a separate expense instead of netting into the suspense account.

**Tasks**:
- [ ] Add fields to `Paystack Gateway Setting`: `paystack_fee_account` (Link → Account, expense), `fee_tax_account` (optional).
- [ ] On Payment Entry creation from a Paystack log:
  - Read `data.fees` from the webhook payload (in kobo).
  - Add a **deduction row** to the PE: `account = paystack_fee_account`, `amount = fees`, `description = "Paystack transaction fee"`.
  - GL: DR fee expense, CR suspense (net deposit = amount - fees).
- [ ] If `fee_tax_account` is set, split the fee into fee + VAT portions.

**Files**: `paystack_gateway_setting.json`, `paystack_payment_log.py`, `api.py`.

### 6.4 Settlement Reconciliation
**Purpose**: Paystack settles funds to your bank account in batches. Reconcile these against the ledger.

**Tasks**:
- [ ] New doctype `Paystack Settlement`:
  - `settlement_id` (Data), `settlement_date`, `total_amount`, `total_fee`, `total_credit`, `currency`, `status`, `raw_response`.
- [ ] Handle `settlement.processed` webhook → create `Paystack Settlement` record.
- [ ] Reconciliation tool (a page or report):
  - Match settlement amounts against completed Payment Entries.
  - Post a transfer JE: DR `default_bank_account`, CR `suspense_account` for the settled amount.
  - Flag any Payment Entries not yet settled.
- [ ] Add `wallet_clearing_account` and `default_bank_account` to settings.

**Files**: New `frappe_paystack/doctype/paystack_settlement/`, `api.py`, new reconciliation report.

### 6.5 Auto-Conversion for Foreign Currency
**Purpose**: The `enable_auto_conversion` placeholder in settings should do something.

**Tasks**:
- [ ] If `enable_auto_conversion = 1` and invoice currency ≠ company currency:
  - Use Paystack's conversion rate (from the transaction response).
  - Post the Payment Entry in the transaction currency.
  - Trigger `make_exchange_gain_loss_journal()` for any rate difference vs the invoice rate.

**Files**: `paystack_gateway_setting.json`, `paystack_payment_log.py`.

### Phase 6 Deliverables
- Paystack Plans & Subscriptions mapped to ERPNext Subscriptions.
- Auto-invoice creation on recurring charges.
- Fee accounting with separate expense + VAT.
- Settlement reconciliation with bank transfer posting.
- Foreign currency auto-conversion with FX gain/loss.

### Phase 6 Testing
- Test: create Subscription Plan → Paystack Plan created.
- Test: recurring charge webhook → Sales Invoice + PE auto-created.
- Test: fee deduction in PE → GL correct.
- Test: settlement webhook → Settlement record + reconciliation JE.
- Test: multi-currency with auto-conversion → FX journal created.

---

## Phase 7 - Analytics, Automation & Polish (P3)

> **Rationale**: Visibility, automation, and quality-of-life improvements once the core is solid.

### 7.1 Dashboard Charts & Number Cards
**Tasks**:
- [ ] Add charts to the `Paystack Dashboard` workspace:
  - Total volume (time series: today/week/month).
  - Success rate (pie: success vs failed).
  - Refund volume (time series).
  - Fees paid (time series).
  - Settlement pending vs settled.
- [ ] Add number cards: "Processed This Month", "Pending Settlement", "Failed (needs attention)", "Total Refunds".

**Files**: `paystack_dashboard.json`, new chart doctypes.

### 7.2 Reconciliation Report
**Purpose**: Compare Paystack Payment Logs (Completed) against Payment Entries.

**Tasks**:
- [ ] New report `Paystack Reconciliation`:
  - Columns: Payment Log, linked invoice, log amount, PE name, PE amount, difference, status.
  - Flag logs where PE wasn't created or amounts mismatch.
  - Filter by date range, company, status.

**Files**: New `frappe_paystack/report/paystack_reconciliation/`.

### 7.3 Settlement vs Ledger Report
**Purpose**: Compare Paystack settlement batches against bank deposits.

**Tasks**:
- [ ] New report comparing `Paystack Settlement` totals against GL entries on the `default_bank_account` for the same period.
- [ ] Flag discrepancies.

**Files**: New report.

### 7.4 Email Notifications
**Tasks**:
- [ ] On Payment Entry creation (payment success): auto-email PDF receipt to customer.
- [ ] On payment failure: email the accounts team.
- [ ] On invalid webhook signature: email the system administrator (security alert).
- [ ] On large payments (configurable threshold): notify management.
- [ ] Add notification templates for each.

**Files**: `paystack_payment_log.py`, new Notification doctypes.

### 7.5 Payment Reminders (Scheduled Job)
**Purpose**: Automatically send Paystack payment links for overdue invoices.

**Tasks**:
- [ ] Scheduled job (`hooks.py` → `scheduler_events`):
  - Find Sales Invoices with `status in ["Overdue", "Unpaid"]` and `outstanding_amount > 0`.
  - If no active Paystack Payment Log exists, create one and email the link.
  - Configurable: reminder frequency, max reminders per invoice, template.
- [ ] Integrate with ERPNext Dunning (optional): if a Dunning exists, send the Dunning amount link.

**Files**: `hooks.py`, new `frappe_paystack/tasks/payment_reminders.py`.

### 7.6 Audit Trail Enhancement
**Tasks**:
- [ ] Track on Payment Log / Refund Log: `initiated_by`, `initiated_ip`, `processed_at`, `processed_by`.
- [ ] Add a timeline view (via Frappe's `timeline` feature or a custom HTML field) showing all events: link created, checkout opened, payment received, PE created, refund initiated, etc.

**Files**: `paystack_payment_log.json`, `paystack_refund_log.json`.

### 7.7 PII Masking in Raw Response
**Tasks**:
- [ ] Mask card numbers, CVVs, and sensitive customer data in `raw_response` before storing.
- [ ] Keep only: last 4 digits, card type, bank, channel, authorization code (if reusable).
- [ ] Add a setting `mask_pii_in_logs` (default 1).

**Files**: `api.py`, `paystack_payment_log.py`, `paystack_gateway_setting.json`.

### 7.8 Comprehensive Test Suite
**Tasks**:
- [ ] Mock Paystack API using `unittest.mock` or `responses` library.
- [ ] Test coverage:
  - Link creation (SINV, SO, Dunning).
  - Webhook processing (charge.success, refund.processed, refund.failed, settlement.processed, subscription events).
  - PE creation via `get_payment_entry()`.
  - Refund flow (full, partial, over-refund prevention).
  - Reversal PE creation.
  - Currency conversion + FX journal.
  - Idempotency (duplicate webhooks).
  - Signature verification (valid, invalid, missing).
  - Saved card charge.
  - Settlement reconciliation.
- [ ] Add to CI (`.github/workflows/`).

**Files**: `tests/`, `.github/workflows/`.

### 7.9 Documentation
**Tasks**:
- [ ] Update README with: setup guide, webhook configuration, refund flow, subscription setup, fee accounting, settlement reconciliation.
- [ ] Add inline docstrings to all public methods.
- [ ] Create a `docs/` folder with architecture diagrams (Mermaid).
- [ ] Document the migration path from the old self-contained pattern to the `payments` app pattern.

**Files**: `README.md`, new `docs/` directory.

### Phase 7 Deliverables
- Rich dashboard with charts and number cards.
- Reconciliation and settlement reports.
- Automated email notifications and payment reminders.
- Full audit trail.
- PII masking.
- Comprehensive test suite with CI.
- Complete documentation.

---

## Dependency Graph

```
Phase 1 (Security) ──────────────────────────────────────┐
                                                          │
Phase 2 (Returns/Refunds) ─────────────────────────────── │ ─→ Phase 7 (Analytics)
                                                          │
Phase 3 (payments app) ──→ Phase 4 (PE/Ledger) ──→ Phase 5 (Webshop/Portal)
                    │                                       │
                    └──→ Phase 6 (Subscriptions/Fees) ──────┘
```

- **Phase 1 & 2** can proceed in parallel (no dependencies).
- **Phase 3** is the architectural pivot - Phases 4, 5, 6 all depend on it.
- **Phase 7** depends on all prior phases being substantially complete.

---

## Migration Considerations

- **Schema changes** (new fields, new doctypes) require `bench migrate`.
- **Phase 3** changes the payment flow - existing Payment Logs created before the migration should still work. Add a patch to backfill `Payment Gateway` and `Payment Gateway Account` records.
- **Phase 4** changes PE creation from manual to factory - test with existing data to ensure no inconsistencies.
- **Phase 6** adds new doctypes - no migration of existing data needed.
- All schema changes should be in `[post_model_sync]` in `patches.txt` unless they affect the doctype model itself.

---

## Settings Additions Summary

New fields on `Paystack Gateway Setting` across all phases:

| Field | Phase | Purpose |
|-------|-------|---------|
| `test_mode` | 1 | Separate from developer mode |
| `webhook_secret` | 1 | Dedicated webhook signing secret |
| `allowed_webhook_ips` | 1 | IP allowlist |
| `auto_refund_on_credit_note` | 2 | Auto-refund on credit note |
| `checkout_mode` | 5 | Inline vs Hosted |
| `link_expiry_hours` | 5 | Payment link expiry |
| `paystack_fee_account` | 6 | Fee expense account |
| `fee_tax_account` | 6 | Fee VAT account |
| `wallet_clearing_account` | 6 | Settlement transit |
| `default_bank_account` | 6 | Final settlement bank |
| `enable_auto_conversion` | 6 | FX auto-conversion (already placeholder) |
| `mask_pii_in_logs` | 7 | PII masking toggle |
| `large_payment_threshold` | 7 | Notification threshold |

---

## New Doctypes Summary

| Doctype | Phase | Purpose |
|---------|-------|---------|
| `Paystack Refund Log` | 2 | Track refunds + reversal PEs |
| `Paystack Customer Authorization` | 5 | Saved cards / tokenized payments |
| `Paystack Plan` | 6 | Map ERPNext Subscription Plan → Paystack Plan |
| `Paystack Settlement` | 6 | Settlement batch tracking + reconciliation |

---

## New Reports Summary

| Report | Phase | Purpose |
|--------|-------|---------|
| `Paystack Reconciliation` | 7 | Payment Log vs Payment Entry matching |
| `Paystack Settlement vs Ledger` | 7 | Settlement vs bank deposit comparison |

*(Existing reports: `Paystack Transactions`, `Customer Paystack Volume` - keep and enhance)*

---

## New Hooks Summary

| Hook | Phase | Purpose |
|------|-------|---------|
| `after_install` | 3 | Auto-create Payment Gateway + Account |
| `doc_events` (Sales Invoice on_submit/on_cancel) | 2 | Auto-refund on credit note |
| `doc_events` (Subscription Plan) | 6 | Create Paystack Plan |
| `doc_events` (Subscription) | 6 | Create/cancel Paystack Subscription |
| `scheduler_events` | 7 | Payment reminders |
| `payment_gateway_enabled` | 3 | Auto-create Payment Gateway Account |

---

This plan is a living document - adjust priorities and scope as implementation progresses. Each phase is designed to be independently shippable, so you can stop after any phase and still have a working, improved app.