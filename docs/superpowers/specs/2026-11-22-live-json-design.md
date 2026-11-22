# Production JSON unpaid-hold correction

## Intent and evidence

The user authorized repeating a real shop reservation without payment, preserving JSON, strengthening the simulator from evidence, and attempting the repository browser code without payment when feasible. The 2026-11-22 authenticated service-34 adult trial produced empty cart/reservations, one pending reservation, then empty cart/reservations after cleanup; free places changed 8 -> 7 -> 8 and reserved places 0 -> 1 -> 0. Full private cart and reservation bodies are saved outside Git. GET bodies were read as ordinary browser pages; no network log, HTTP metadata, or payment RPC was captured. Separate GET reads after cleanup returned cart/islocked as {"result":{"isLocked":false},"errors":[]} and cart/getreceiptinfo as {"result":false,"errors":[]}; these were not the adapter's POST requests and were not captured while the hold existed.

The course reservation has status 7 and a positive reservation ID, but no is_paid or receipt_event_id. The exact cart line has payment_event_id:null, bundle_price:null, item_id:0, quantity:1, price:3500, and matching course/account/product. Existing parsing recognizes held but fails unpaid_hold. A synthetic is_paid:0 would make the old path pass. The parser already handles null bundle price and zero item ID.

## Chosen correction

Use separate positive evidence for an observed unconfirmed cart hold in without-payment mode. Preserve reservation_paid=None when production omits its field. Require exactly one exact cart line, the matching unique status-7 reservation with positive ID, explicit null payment_event_id, no conflicting paid/receipt signal, known unlocked cart, valid approved price, and no other cart contents. Payment actions and confirmation continue to require their existing stricter evidence. Explicit non-null/malformed payment event data must not fall through to the legacy unpaid predicate.

Default simulator public course JSON should match the observed pending row shape and stop inventing is_paid and receipt_event_id. Keep SQLite paid state for invariants and stats. Isolate synthetic settlement proof in an explicitly loopback-only test read, consumed only for loopback paid test flows; never expose that proof as verified production semantics. Do not invent payment labels as a paid proof. Preserve current positive stable cart IDs unless evidence supports replacing their semantics, while adding a regression using the observed item_id:0. Populate reserved as well as free.

## Alternatives considered

Keeping invented fields would hide the demonstrated failure. Treating status 7 alone as payment evidence is too broad. A separate unpaid simulator mode would leave the default path unlike the captured shop. The chosen separate hold evidence plus explicit local settlement test contract preserves real unpaid behavior and payment safety.

## Constraints

No real payment, no terms acceptance, no production writes in code tests, no browser-control or OS-policy bypass. Production browser adapter remains limited to service 9 and linked children. Adult service 34 evidence is labeled as such. No private identities, account/reservation/sale/loading IDs, cookies, raw captures or browser profiles in Git. All new test identities and identifiers are fictional. Existing Python 3.11+ stack and dependencies remain unchanged. Work remains on fix/real-shop-correctness-audit and draft PR 7; no merge.

## Completion

Regression fails on old code and passes on corrected code. Missing, malformed, contradictory, duplicate, mismatched and locked evidence fails closed, and observed pending evidence cannot authorize payment. Default unpaid browser smoke exercises omitted is_paid and completes with zero checkout writes. Existing paid synthetic smoke stays passing and labels its proof as local. Full local tests, lint/type/public-tree checks and exact-head hosted Chromium CI pass. Report what the real browser tested and any remaining real automation blocker accurately.
