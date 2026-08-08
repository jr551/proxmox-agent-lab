## What changed

## Failure mode or use case

## Safety boundary

- [ ] No lease, ownership, audit, expiry, cleanup, or shutdown invariant is weakened.
- [ ] No credentials, addresses, VMIDs, captures, journals, or site-specific values are included.
- [ ] Host changes and live-memory mutations remain explicitly gated.

## Verification

- [ ] Warning-clean unit tests
- [ ] Secret scan
- [ ] Packaging/install check, if applicable
- [ ] Real disposable hardware, if applicable

Describe exactly what was observed and what remains unverified.
