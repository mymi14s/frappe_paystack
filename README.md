# Frappe Paystack

Frappe Paystack is a payment gateway app for Frappe and ERPNext. It collects payments
through [Paystack](https://paystack.com) and books the resulting Payment Entries, refunds
and payouts into the ledger.

The app registers Paystack as a standard **Payment Gateway** through the `payments` app. It
is available wherever ERPNext already offers a gateway, and adds its own buttons to the desk,
the POS screen and the invoice print format.

![Checkout page](img/payment_page.png)

## Features

- Checkout page with an inline Paystack popup or a hosted redirect.
- **Pay now**, **Email payment link** and **Partial payment** buttons on Sales Invoice,
  Sales Order and Dunning.
- Webshop cart and customer portal checkout.
- POS collection by emailed link or Phone channel.
- Charging cards a customer has already used.
- Automatic collection of ERPNext Subscription invoices.
- QR codes and a payment link print format for Sales Invoice.
- Automatic booking of Payment Entries, refund reversals and settlement Journal Entries.
- Five reports, a monitoring workspace and a reconciliation page.
- Per company gateways, keys and accounts.

## Requirements

| | |
|---|---|
| Frappe | `>= 15.0.0` |
| ERPNext | `>= 15.0.0` |
| Payments | the `payments` app |
| Python | `>= 3.10, < 3.15` |

Supported charge currencies are **NGN**, **USD**, **GHS**, **ZAR** and **KES**.

## Install

```bash
bench get-app frappe_paystack https://github.com/mymi14s/frappe_paystack
bench --site your-site install-app frappe_paystack
bench --site your-site migrate
```

Installing creates the **Paystack** Mode of Payment, registers **Paystack** as a Payment
Gateway, and builds the number cards and charts of the **Paystack Dashboard** workspace.

## Configure

Open **Paystack Gateway Setting** and create one record per company. Only one enabled
gateway per company is allowed.

![Gateway setting](img/gateway.png)

| Field | Notes |
|---|---|
| **Gateway Name** | Names the record. |
| **Public Key**, **Secret Key** | From the [Paystack dashboard](https://dashboard.paystack.com/#/settings/developers). Validated against **Test Mode** on save. |
| **Webhook Secret** | Optional. Leave blank to verify webhooks with the secret key. |
| **Enabled** | Registers the gateway and creates its Payment Gateway Account. |
| **Test Mode** | Declares which key pair is in use. |
| **Company** | The company this gateway collects for. |
| **Currency** | Must equal the company's default currency. |
| **Suspense Account** | Required. Every capture is booked here at the gross amount. |
| **Mode of Payment** | Required. Put on each Payment Entry the app books. |
| **Checkout Mode** | `Inline` or `Hosted`. Defaults to `Inline`. |
| **Payment Link Validity (Hours)** | How long a checkout link accepts payment. `0`, the default, never expires. |
| **Allowed Webhook IPs** | One address or CIDR range per line, or comma separated. Empty accepts any source. |
| **Auto Refund on Credit Note** | See [Refunds](#refunds). |
| **Settlement Bank Account**, **Paystack Fee Account** | See [Settlement and fees](#settlement-and-fees). |
| **Auto Charge Subscription Invoices** | See [Subscriptions](#subscriptions). |

A minimal working setup:

```
Gateway Name ........ Paystack NGN
Company ............. Acme Trading Ltd
Currency ............ NGN
Public Key .......... pk_test_xxxxxxxxxxxxxxxxxxxx
Secret Key .......... sk_test_xxxxxxxxxxxxxxxxxxxx
Test Mode ........... yes
Suspense Account .... Paystack Suspense - ATL
Mode of Payment ..... Paystack
Checkout Mode ....... Inline
Enabled ............. yes
```

Add **Settlement Bank Account** and **Paystack Fee Account** to have payouts booked.

### Webhook

In the Paystack dashboard, set the webhook URL to:

```
https://your-site.com/api/method/frappe_paystack.api.paystack_webhook
```

Send at least `charge.success`, `charge.failed`, `refund.processed` and `refund.failed`.
Add `settlement.success` to have payouts booked.

Paystack's sending addresses can be pasted into **Allowed Webhook IPs**, one per line:

```
52.31.139.75
52.49.173.169
52.214.14.220
```

## Taking payment

Every route creates a **Paystack Payment Log** and a checkout page at
`/paystack-checkout/<log-name>`.

### Checkout page

The page shows what is being paid, the amount in the charge currency, and the exchange rate
when the document is billed in a different one. The **Pay** button follows **Checkout
Mode**:

- **Inline** opens the Paystack popup over the page.
- **Hosted** redirects to a Paystack hosted checkout URL. The URL is created once and reused.

The link is re-read server side before charging. An expired link, a settled document or a
cancelled one is refused.

### Desk buttons

Submitted Sales Invoices, Sales Orders and Dunnings that still owe money get a **Paystack**
button group when the gateway is enabled for the document's company.

| Button | What it does |
|---|---|
| **Pay now** | Creates the link and shows it in a dialog with a QR code, a copy button and an open button. |
| **Email payment link** | Creates the link and opens a pre-filled email to the customer's Email ID. |
| **Partial payment** | Asks for an amount, capped at the outstanding, then opens or emails the link. |
| **Charge saved card** | Charges a card the customer has already used. |

![Paystack button group](img/payment_button.png)

Sales Orders and Sales Invoices bill through ERPNext's **Payment Request**. A Dunning gets a
Paystack Payment Log of its own and the app books the Payment Entry directly.

### Webshop and customer portal

The Webshop cart's **Pay** button routes through Paystack with no extra configuration. The
app guards against a double click raising a second request, and hides the **Pay** button on a
portal or Webshop page whose payment is already in flight.

Signed-in customers get **`/my-payments`**: their unpaid invoices with a **Pay Now** button,
their last 20 Paystack payments with a downloadable PDF receipt, and any refunds raised
against them. A customer sees only logs raised against their own documents.

### POS

**Emailed link.** With a **Paystack** tender on the payment screen, the *Request for Payment*
button becomes **Send Payment Link**. It emails the customer a checkout link for the tendered
amount and freezes the till. When the money arrives the tender row is filled in, and POS's
own **Complete Order** is driven if the sale is fully paid. POS writes the ledger on Complete
Order.

**Phone channel.** With the Paystack Mode of Payment typed `Phone`, ERPNext's POS raises a
Payment Request and the gateway initialises a Paystack transaction. The cashier is shown the
resulting `authorization_url` to pass to the customer. The POS screen is released when the
charge settles or fails. Nothing is pushed to the customer's phone.

> **POS setup is manual.** Installing the app creates the **Paystack** Mode of Payment as
> type `General`, and POS payment will not work until it is wired up. Run
> `frappe_paystack.setup.setup_pos_payment_mode(company, suspense_account, channel)` from
> `bench console`, or make the equivalent changes by hand. Nothing in the desk or the
> installer calls it.

### Saved cards

When a card charge settles, Paystack returns a reusable authorization. If it is marked
reusable, is on the `card` channel and carries both a code and a signature, the app stores it
as a **Paystack Customer Authorization** against the customer. The record holds Paystack's
authorization code in an encrypted field, plus brand, bank, last four digits and expiry.

**Charge saved card** lists the customer's usable cards and charges the chosen one. Cards
that are inactive, non-reusable or past their expiry month are not offered. A decline marks
the log `Failed` and reports Paystack's message. A card that needs the customer present is
refused.

Charging a saved card requires the **System Manager** or **Accounts Manager** role.

### Subscriptions

ERPNext's **Subscription** owns the billing calendar and raises the Sales Invoice. Paystack
collects it. Pausing or cancelling the ERPNext Subscription stops the collection.

Tick **Auto Charge Subscription Invoices** and a daily job charges each subscriber's newest
usable saved card for their outstanding subscription invoices. The default is off, and
invoices are collected by hand with **Charge saved card**.

The job skips:

- an invoice no ERPNext Subscription raised,
- an invoice that already has a Paystack collection open or settled,
- an invoice more than 30 days old,
- a customer with no usable card on file.

It attempts at most 50 invoices per company per run. A declined card is recorded against its
own invoice and the batch carries on.

### QR codes and print format

The **Paystack Invoice with Payment Link** print format for Sales Invoice prints the invoice,
then a *Pay online* panel with the checkout link and a QR code when the document has an open,
unexpired link.

The same two Jinja helpers are available in your own print formats:

```jinja
{% set link = paystack_payment_link(doc) %}
{% if link %}
  <a href="{{ link }}">{{ link }}</a>
  <img src="{{ paystack_payment_qr(doc) }}">
{% endif %}
```

Both return an empty string when there is no open link.

## After payment

### Webhook handling

`frappe_paystack.api.paystack_webhook` accepts guest requests.

| Control | Behaviour |
|---|---|
| Signature | HMAC-SHA512 over the raw body, compared in constant time. The signature selects which company's gateway the event belongs to. An event no enabled gateway's secret verifies is rejected before any handler runs. |
| Source IP | Checked against **Allowed Webhook IPs** after the signature. |
| Per-IP rate limit | 100 requests per minute, answered with HTTP 429. |
| App-wide ceiling | 1000 requests per minute across every source. |
| Company scoping | A Payment Log, Refund Log or Settlement belonging to another company is refused. |
| Duplicate events | Deduplicated on the Paystack transaction id. |
| Audit | Every rejection files an Integration Request with url `webhook`. 10 throttled requests, or 10 rejected signatures, within a minute raises one Error Log. |

A correctly signed event is answered **200 whatever happens next**. A settlement that fails is
recovered by the retry sweep.

### Booking the payment

On `charge.success` the Payment Log is stamped with the amount, currency, Paystack fee,
transaction id and payment date, and moves to `Processed`. Then:

- **Backed by a Payment Request** (Sales Order, Sales Invoice, Webshop cart): the app calls
  ERPNext's `PaymentRequest.set_as_paid()`, which creates and submits the Payment Entry. For a
  Shopping Cart Sales Order, the same call raises and submits the Sales Invoice and allocates
  the advance to it. The log moves to `Completed`.
- **No Payment Request** (Dunning, manual links): the app builds the Payment Entry against the
  suspense account, with the gateway's Mode of Payment, the Paystack reference as the reference
  number, and the amount allocated in the receivable's own currency.
- **POS Invoice**: the log is marked `Completed` and POS writes the ledger.

If the capture and what the Payment Request bills differ by more than 0.01, nothing is booked
and the mismatch is written to the log's **Errors** field.

![Completed payment](img/completed.png)

### Retry sweep

Captures stuck at `Processed` with no Payment Entry are re-driven **every ten minutes**. Each
log serves an exponential wait between attempts: 10 minutes after the first attempt, doubling
up to 8 times, capped at 24 hours.

A log moves to **`Needs Attention`** when either 12 scheduled attempts have booked nothing, or
7 days have passed since the capture. `Needs Attention` means the money was captured but
nothing booked it. It is counted by a number card, listed by **Paystack Unsettled Payments**,
and raises an Error Log naming the last recorded failure.

### Complete Payment

A Payment Log at `Processed` or `Needs Attention` with no Payment Entry shows a **Complete
Payment** button under **Actions**. It verifies the capture against Paystack first, and refuses
to book anything if Paystack reports a different amount, a different currency, a non-capture, or
no transaction. Otherwise it drives the settlement once and reports the Payment Entry back to the
form.

Only one completion of a log runs at a time. The button requires **System Manager** or
**Accounts Manager**.

The same Actions menu offers **Verify Transaction** and **Open in Paystack**.

### When a webhook never arrives

The log stays at `Pending` with no transaction id. The checkout sends the log's own name as the
Paystack reference, and reconciliation looks the payment up by it. Run **Paystack
Reconciliation** from the desk, or wait for the daily pass.

## Refunds

Refund amounts are held in the currency Paystack **charged**.

**By hand.** Open the Paystack Payment Log and use **Actions > Refund**. The button appears on a
`Completed` or `Partially Refunded` log, and the dialog caps the amount at
`Amount Paid - Total Refunded`. Refunding requires **System Manager** or **Accounts Manager**.

**On a credit note.** Tick **Auto Refund on Credit Note**. Submitting a return Sales Invoice then
refunds the original Paystack payment for the credit note's total, capped at whatever earlier
refunds left.

Each refund creates a **Paystack Refund Log**, a draft-only record that cannot be submitted or
cancelled. On `refund.processed` the log:

1. books a reversal Payment Entry, a negative allocation against a credit note, otherwise an
   unallocated payment out of the suspense account,
2. updates the payment's **Total Refunded** and moves it to `Partially Refunded` or `Refunded`,
3. emails the customer a **Paystack Refund Receipt** PDF.

Partial refunds can be repeated up to the full captured amount. Anything beyond that is refused
on validation.

Cancelling a credit note does not reverse the Paystack refund. Cancel the reversal Payment Entry
by hand.

## Settlement and fees

Every capture books the gross into the suspense account. Paystack pays out net of its fees.

A `settlement.success` webhook records a **Paystack Settlement** carrying Paystack's gross, fees,
deductions and net, then raises one Journal Entry:

| Account | Debit | Credit |
|---|---|---|
| Settlement Bank Account | net | |
| Paystack Fee Account | fees | |
| Suspense Account | deductions | gross |

A zero fee gets no row, and so does a zero deduction. The fee is recognised at payout.

Once the entry is posted, a background job walks the payout's transactions at Paystack and stamps
each matching Payment Log with the payout that cleared it and the fee Paystack kept.

A payout is recorded but not booked when:

- the gateway has no Suspense Account, Settlement Bank Account or Paystack Fee Account set,
- Paystack paid out in a currency the company does not book in,
- the gross, fees, deductions and net do not add up within 0.01, or
- the Journal Entry itself fails.

The reason is written to the settlement's **Errors** field, and the payout is retried hourly for
30 days.

## Reports and monitoring

### Workspace

**Paystack Dashboard** carries eight number cards:

| | |
|---|---|
| Paystack Awaiting Settlement | Paystack Needs Attention |
| Paystack Failed Payments | Paystack Payouts Not Booked |
| Paystack Reconciliation Mismatches | Paystack Refunds Pending |
| Paystack Failed API Calls | Paystack Errors Unreviewed |

Plus two charts: **Paystack Payments Captured** (daily, last month) and **Paystack Payments by
Status**.

### Reports

| Report | What it shows |
|---|---|
| **Paystack Activity** | One chronological feed of captures, refunds, payouts and API calls, newest first, each graded `Error`, `Warning` or `Info`. Filter by company, date range, severity or source. |
| **Paystack Transactions** | Transactions read live from the Paystack API for a chosen gateway. Filter by customer id, status, date range and amount. Paged. |
| **Customer Paystack Volume** | Total captured per customer, per company. |
| **Paystack Unsettled Payments** | Captures no payout has claimed, each with Paystack's fee and how many days it has waited. |
| **Paystack Settlements vs Ledger** | Each payout's Paystack gross, fees, deductions and net beside what its Journal Entry left in the ledger, plus the captures naming that payout. The **Discrepancy** column says why a payout does not tie out. Tick **Only Payouts That Do Not Tie Out** to filter. |

All five are readable by **System Manager**, **Accounts Manager** and **Accounts User**.

### Reconciliation page

**Paystack Reconciliation** shows reconciled, mismatch and pending counts and the most recent
reconciliation records. **Run Reconciliation** compares each payment against the Paystack
transaction behind it and writes a **Paystack Reconciliation Log** with `Reconciled`, `Mismatch`
or `Pending`. The page requires **System Manager** or **Accounts Manager**.

A mismatch is resolved on the **Paystack Reconciliation Log** itself: **Resolve Mismatch** asks
how it was settled and marks the row **Manual Override**, which every automated pass then leaves
alone.

### Scheduled jobs

| Frequency | Job |
|---|---|
| Every 10 minutes | Re-drive captures that produced no Payment Entry |
| Hourly | Re-drive payouts that produced no Journal Entry |
| Daily | Reconcile the last two days; collect subscription invoices |

## Security

- **No card numbers.** Only Paystack authorization codes are stored, in an encrypted `Password`
  field. They are redacted out of every Integration Request and stored API response.
- **Per-company routing.** Each company has its own gateway, keys and suspense account. If one
  signing secret verifies for two companies, the event is dropped and an Error Log raised.
- **Roles that move money.** Refunding, charging a saved card and **Complete Payment** require
  **System Manager** or **Accounts Manager**.
- **Document permissions.** Creating a payment link, fetching a QR code and reading a customer's
  email each check the caller's read permission on the underlying document or Customer.
- **Rate limits.** The hosted checkout endpoint allows 30 calls per IP per minute, and
  `run_reconciliation` 5 per minute. Webhook limits are in [Webhook handling](#webhook-handling).
- **Audit trail.** Every outbound Paystack call files an Integration Request under the service
  name `Paystack`.

## Development

### Python tests

Always name the app:

```bash
bench --site your-site run-tests --app frappe_paystack
```

> **Never run `bench run-tests` without `--app frappe_paystack`.** `erpnext` and `payments` both
> register a `before_tests` hook that empties the entire Item Price table with raw SQL, leaving no
> `Deleted Document` rows. It also resets the Stock Settings and Selling Settings defaults. This
> has already destroyed data on a real bench. Never run it bare, and never with `--app erpnext` or
> `--app payments`, against a site whose data you care about.

`--app frappe_paystack` resolves this app's own hook,
`frappe_paystack.tests.session_setup.before_tests`. It deletes nothing.

Two side effects are inherent to `bench run-tests` on any site: Frappe installs ERPNext's `_Test*`
fixture records and never removes them, and the scheduler is disabled for the duration of the run.

### JavaScript tests

Vitest, over `js-tests/`:

```bash
cd apps/frappe_paystack
npm install
npm test            # or: npx vitest run
npm run test:watch
```

### Browser tests

Cypress specs live in `cypress/integration/` and drive the real POS, Webshop and portal screens.
They need a development server (`bench start`, developer mode on).

```bash
cd apps/frappe_paystack
npx cypress run     # or: npx cypress open
```

The base URL defaults to `http://localhost:8000`. Each spec creates its own fixtures and puts back
what it changed on teardown.

### Coverage

Run and report from `sites/`:

```bash
cd sites
../env/bin/python -m coverage run --rcfile=../apps/frappe_paystack/.coveragerc \
    -m frappe.utils.bench_helper frappe --site your-site run-tests --app frappe_paystack \
  && ../env/bin/python -m coverage report --rcfile=../apps/frappe_paystack/.coveragerc
```

Branch coverage is on and the report fails under 100%.

### Continuous integration locally

`docker/docker-compose.yml` runs the GitHub Actions jobs on your machine, against your working
tree, without touching any bench on the host.

```bash
cd apps/frappe_paystack
docker compose -f docker/docker-compose.yml run --rm quality      # lint and types
docker compose -f docker/docker-compose.yml run --rm security     # semgrep and pip-audit
docker compose -f docker/docker-compose.yml run --rm server-v15   # Python 3.12, frappe version-15
docker compose -f docker/docker-compose.yml run --rm server-v16   # Python 3.14, frappe version-16
```

See [docker/README.md](docker/README.md) for what each service covers and where it differs from
GitHub.

## Troubleshooting

| Symptom | Where to look |
|---|---|
| "This payment link can no longer be paid" | The log is settled, the link has passed **Payment Link Validity (Hours)**, or the document was cancelled or settled |
| Payment captured but nothing booked | The Payment Log's **Errors** field and the Error Log it names |
| Log stuck at **Needs Attention** | Fix the cause, then use **Actions > Complete Payment** |
| "No Payment Gateway Account is set up" | Save the Paystack Gateway Setting enabled, with a suspense account |
| Webhooks rejected | Integration Requests with url `webhook` carry the reason |
| Saved card declined | The Payment Log's **Errors** field names the Error Log |
| Payout recorded but no Journal Entry | The Paystack Settlement's **Errors** field says why |
| Suspense account will not clear | **Paystack Unsettled Payments** lists the captures still in it |
| Bank statement does not match Paystack | **Paystack Settlements vs Ledger**, with **Only Payouts That Do Not Tie Out** ticked |
| One secret serving two companies | An Error Log titled *one signing secret serves several companies*. Give each company its own key pair |

## What the app adds

**Doctypes**: Paystack Gateway Setting, Paystack Payment Log, Paystack Refund Log, Paystack
Reconciliation Log, Paystack Settlement, Paystack Customer Authorization.

**Print formats**: Paystack Invoice with Payment Link (Sales Invoice), Paystack Payment Receipt
(Paystack Payment Log), Paystack Refund Receipt (Paystack Refund Log).

**Web pages**: `/paystack-checkout/<reference>`, `/my-payments`.

## Support

Issues, feature requests and contributions:
[github.com/mymi14s/frappe_paystack](https://github.com/mymi14s/frappe_paystack).

Test in Paystack Test Mode before going live.

## License

MIT
