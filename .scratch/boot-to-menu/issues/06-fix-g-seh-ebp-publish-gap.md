# Fix the g_seh_ebp publish gap on ABI_CALL sites

Status: open
Type: task
Blocked by: 01

## Question

`ebp` is the only register modelled as a per-function local rather than
threaded through the lifter globally. It is published to `g_seh_ebp` on 563
tail-jumps but on 0 of 1624 `ABI_CALL` sites — a lifter-wide gap, not a
single-function bug. This is one of the three bug *classes* identified in
the 2026-08-07 session summary (alongside deferred-flag miscompiles and
generic esp stubs) but, unlike those two, this class has not yet had even
one instance measured — it's a known gap, not yet a confirmed win.

First establish whether this gap is actually load-bearing anywhere reachable
(a fragment that inherits `ebp` via `g_seh_ebp` across an `ABI_CALL` boundary
would read a stale value) before deciding whether to publish at every
`ABI_CALL` site (broad, mechanical, but touches 1624 sites) or only at sites
proven to matter. Ledger-check first (`mcp__OddityRecomp__ledger`,
action=check) in case this was already probed and refuted.

## Answer

<!-- filled on resolution -->
