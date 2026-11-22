# Production HTML Simulator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Use transformed production HTML templates in the local simulator and correct observed UI differences with repeatable tests.

**Architecture:** Keep the existing SQLite protocol simulator. Extract reviewed inert HTML structures from the saved anonymous source and populate them with local JavaScript and fictional accounts. Serve fixed local assets under CSP; retain the browser adapter's existing guards.

**Tech Stack:** Python 3.11+, standard-library HTML parsing, existing HTTPX and Playwright, vanilla JavaScript and CSS.

**Spec:** `docs/superpowers/specs/2026-11-22-production-html-design.md`

## Global Constraints

- Python 3.11+; no new runtime dependencies.
- Exact loopback origin only; fixed resources under /webshop/resource/testshop/.
- No raw authenticated data, vendor runtime, external assets, or real payment.
- Fictional participants only; preserve production evidence requirements.

## Review Focus

- Missing or changed source templates must fail extraction instead of silently using invented markup (task 1).
- Selecting the child expansion control alone must never submit the parent (task 2).
- Independent balance and payment-method responses must not enable a stale payment action (task 2).
- Removing an item from another browser session or with the wrong product must leave reservations unchanged (task 3).
- An unguarded standalone browser must stay on the exact local origin and show real server state (task 4).

## Task 1: Source-derived inert templates

Files: `scripts/extract_shop_templates.py`, `uhkone/testshop_templates.html`, `tests/test_shop_templates.py`.

- [ ] Add failing extraction tests with synthetic source markers, missing templates, and unsafe attributes.
- [ ] Implement deterministic extraction from the saved anonymous source. Preserve reviewed DOM paths and template identifiers, replace dynamic values with named slots, record source hashes.
- [ ] Generate the checked-in inert fixture; run extraction tests and review the output for captured values and executable content.

## Task 2: Local rendering and observed state transitions

Files: `uhkone/testshop.html`, `uhkone/testshop.js`, `uhkone/testshop.css`.

- [ ] Add browser regressions for participant selection without writes, automatic cart opening, exact visible course/child/amount, pending status, and separate payment/balance renders.
- [ ] Render extracted templates from real local HTTP state. Keep the established R2 adapter surface.
- [ ] Add catalogue filters, source-derived forms and cart layout; keep a conspicuous simulator label.
- [ ] Use safe text insertion and exact local URLs. Disable duplicate submission while a write is pending.

## Task 3: Fixed resources and cart cleanup

Files: `uhkone/testshop.py`, `tests/test_testshop.py`, `pyproject.toml`, `scripts/check_public_tree.py`.

- [ ] Add failing HTTP tests for CSP, exact resource routes, and cart deletion scoped to session/item/product.
- [ ] Serve the three fixed assets with correct MIME types and self-only CSP.
- [ ] Implement the observed `cart/deleteCartItem` request for an unpaid local item; release that item's reservation atomically.
- [ ] Add only the new exact source paths to packaging and public-tree allowlists; run HTTP and public-tree tests.

## Task 4: Independent acceptance and final evidence

Files: `tests/test_testshop_browser.py`, `scripts/smoke_browser.py`, `docs/laadunvarmistus-2026-11-22.md`, `README.md`.

- [ ] Exercise standalone Chromium with off-origin requests recorded and aborted before transmission; assert no attempts.
- [ ] Exercise removal and reload; assert restored capacity and zero checkout/receipt writes.
- [ ] Extend the existing unpaid smoke to verify visible cart contents and pending state, then unchanged state on resume.
- [ ] Run the complete local suite, lint/type checks and public-tree review. Obtain independent code review.
- [ ] Publish the verified tree to existing draft PR #7 and require zero-skip Chromium CI at that exact commit before reporting success.
- [ ] Record confirmed differences, disproven hypotheses, and remaining production limits in the QA report.
