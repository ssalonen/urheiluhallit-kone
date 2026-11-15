# Reserve without payment

## Intent

Let a user exercise the real booking flow through the shopping cart without purchasing a course. The program selects only an approved course and existing child, reserves at most one actual place, verifies that the exact place is held in this browser session's cart, and stops before payment.

## Scope and constraints

- Add `--ilman-maksua` to `varaa` and `varaa-yksi`, and expose the same choice in `ohjattu`.
- Update the standalone `docs/esimerkkiajo.html` control-pane demo with a no-payment choice. Its fictional run stops with one unpaid item in the cart and zero payment requests. Keep its explicit offline-demo labeling; it does not operate the CLI.
- In this mode, no payment method or payment type is needed. The command must never send `cart/externalpaymentstart`, click a payment control, or enter a payment provider.
- Keep the existing course filters, price cap, opening time, participant matching, account lock, durable intent journal, and reconciliation after an uncertain add response.
- A verified held cart line is success for this mode, with an explicit message that it is unpaid and may expire. It is never reported as a paid or confirmed course place.
- Repeating the command with the same state and profile may verify the existing cart line, but cannot add a duplicate. A later paid run must not silently reuse or bypass the no-payment journal.
- Do not change the existing paid command's behavior. Local simulator and tests must cover both a normal cart hold and an uncertain add response, with zero payment writes.
- Keep all personal and browser state outside the public repository.

## Approaches

1. **Selected: flag on the existing booking commands.** Reuse the current arbitration, account lock, browser guard, and add reconciliation. Add a terminal `held_unpaid` result after authoritative cart verification. This keeps one reservation path and gives the user one clear option.
2. Separate reservation command. It could have a shorter interface but would duplicate the current one-course and multi-course workflow.
3. Stop at the existing `ilmoittaudu` preparation. That does not send or verify a cart reservation, so it does not cover the requested steps.

## Design

Represent no-payment intent in the immutable booking specification and durable state binding. The browser skips payment preflight. The controller returns once it verifies the exact child, course, and bounded price in the cart; its checkout path is unreachable in this mode. The multi-course runner treats that result as the selected winner and retains its one-course lease. The CLI and wizard report the unpaid cart state distinctly; the real CLI opens the guarded cart view until the user closes the browser. The browser guard refuses checkout writes for the no-payment specification as an independent backstop.

The HTML demo gets a clear run-goal control alongside its existing scenario and step controls. In no-payment mode, the timeline ends at the verified cart hold, marks the place as unpaid, and leaves the payment count and total at zero. Its paid example continues to work as before.
