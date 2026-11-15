# Reserve Without Payment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Reserve one exact course in the cart and stop before payment, via CLI, wizard, and matching offline demo.

**Architecture:** Carry an explicit no-payment intent through `BookingSpec`, its durable binding, the browser guard, and `run_booking`. Return an unpaid-held result after an authoritative cart snapshot; propagate it through the race and CLI. Add a demo mode that ends at the same conceptual point with zero simulated payments.

**Tech Stack:** Python 3.11+, pytest, Playwright, standalone HTML/JavaScript.

**Spec:** `docs/superpowers/specs/2026-11-15-reserve-without-payment-design.md`

## Global Constraints

- No `cart/externalpaymentstart` request in no-payment mode.
- A verified exact child/course/price cart line is unpaid success; no paid-confirmation claim.
- Preserve existing paid behavior and durable duplicate protection.

## Review Focus

- Restart with an existing unpaid cart line returns without another add.
- An uncertain add response is reconciled, not resent.
- A no-payment journal cannot silently become a paid run.
- A payment write is refused by the browser guard even if checkout is called directly.
- The demo shows zero payment requests and distinct unpaid wording.

### Task 1: Controller and browser boundary

**Files:** `uhkone/booking.py`, `uhkone/booking_browser.py`, `uhkone/multi_browser.py`, `tests/test_booking.py`, `tests/test_booking_browser.py`

- [x] Write failing tests for verified unpaid hold, restart, uncertain add, binding, and guard refusal.
- [x] Run focused tests and see failure.
- [x] Add `without_payment: bool = False` to `BookingSpec`; bind it durably; skip payment preflight; return `held_unpaid` only after exact cart verification; block checkout in guard.
- [x] Run focused tests and see pass.

### Task 2: Race and CLI

**Files:** `uhkone/multi.py`, `uhkone/cli.py`, `uhkone/wizard.py`, `tests/test_multi.py`, `tests/test_booking_cli.py`, `tests/test_wizard.py`

- [x] Write failing tests for CLI parsing, race result, wizard command, and unpaid result message.
- [x] Run focused tests and see failure.
- [x] Add `--ilman-maksua` to paid-booking commands; make payment type optional only in this mode; propagate flag and result; expose wizard choice.
- [x] Run focused tests and see pass.

### Task 3: Offline demo and documentation

**Files:** `docs/esimerkkiajo.html`, `README.md`

- [x] Add a no-payment control and end the demo after cart verification with zero payment requests.
- [x] Document the CLI usage and clarify the HTML is fictional and disconnected.
- [x] Verify demo behavior and run the full suite.
