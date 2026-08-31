# 06 — Decisions Needed Before Phase 1

None of these block approving the architecture. They do shape Phase 1–3, so answers now save
rework later. Where there is a sensible default I have named it, so silence is a valid answer.

## A. Environment

| # | Question | Default if unanswered |
|---|---|---|
| A1 | Which broker(s), and do you have a demo account ready? | Proceed generically; symbol discovery handles any broker. |
| A2 | Account currency — USD, or something else (ZAR, EUR, GBP)? | Assume USD, but build the FX-conversion path anyway (it is needed for correct sizing on a non-USD account). |
| A3 | Where will this run — do you already have a Windows VPS, or should the deployment guide assume you need one? | Assume you will provision one; local Windows works for Phases 1–13. |
| A4 | Approximate account size for Stage 6? | Affects whether a structural gold stop is even *sizeable* at 1% — with a ~$3–8 stop distance on gold, small accounts hit `volume_min` and the system will correctly refuse trades. Worth knowing early. |
| A5 | Alert channel — Telegram (recommended), email, or both? | Telegram + email. |

## B. Scope and appetite

| # | Question | Default |
|---|---|---|
| B1 | One position at a time, or allow concurrent positions within the total-risk cap? | One at a time initially. Concurrency multiplies correlation risk on a single instrument for very little gain. |
| B2 | Hold positions over the weekend? | No — gold gaps. Flatten before the Friday close by default, configurable. |
| B3 | Pending (limit) orders, or market-on-confirmation only? | Market-on-confirmation only for v1. Pending orders add a whole class of stale-order failure modes for a modest fill improvement. |
| B4 | Partial take-profits, or single full TP? | Build both; let validation decide. Partials frequently *reduce* expectancy on a min-2R system. |
| B5 | Are you also happy to run this as a signal-only/alerting system indefinitely if the validation gate is never met? | Assume yes. This is a real possible outcome and worth agreeing to now. |

## C. Data & keys

| # | Question | Default |
|---|---|---|
| C1 | FRED API key — will you create one? (free, 1 minute) | Required for yields/real yields. Assume yes. |
| C2 | Budget for a commercial economic calendar / news API? | Assume $0. Terminal-calendar relay + curated YAML fallback. |
| C3 | Anthropic API key for the news-assessment layer? | Assume yes but keep it optional — the rules-based classifier alone is a valid degraded mode. |
| C4 | Is any tick/history data you already own available (Tickstory, broker exports)? | Assume no; harvest broker + Dukascopy. |

## D. Risk specifics

| # | Question | Default |
|---|---|---|
| D1 | Is "drawdown" measured from period *starting equity* or from the period's *peak* equity? | Peak (high-water mark) — stricter, and the standard prop-firm definition. |
| D2 | Does the daily limit reset at broker midnight, NY midnight, or UTC midnight? | Broker server midnight, since that is what the broker's own daily bar and any funded-account rule use. |
| D3 | After a drawdown lockout, resume automatically at the period roll, or require manual clearance? | Auto-resume for daily; **manual clearance for weekly and monthly**. A weekly breach deserves a human looking at it. |
| D4 | Should A+ risk be capped lower than 2% during Stage 6/7? | Yes — a hard global cap (starting 0.25%) applied on top of class caps, raised only by explicit scaling steps. |

## E. Working style

| # | Question | Default |
|---|---|---|
| E1 | One PR per phase, or continuous commits to the feature branch? | Commit continuously to `claude/xauusd-trading-bot-mbvqdd`, and open a PR at each phase gate for review. |
| E2 | Do you want the detection specifications (`docs/specs/market_structure.md` etc.) reviewed by you before Phase 5 code is written? | Yes — recommended. The definitions are where your trading judgement matters most, and they are cheap to change on paper and expensive to change in code. |
| E3 | Do you want to see the dashboard earlier than Phase 12? | Ask and I will bring a read-only version forward to after Phase 7. |

---

## The one thing worth saying plainly

Section 3 of the brief was right to reject a fake 70% guarantee, and the gate in `05` implements
your requirement literally. It is worth being explicit about what that requirement implies:

A ≥70% win rate at a ≥1:2 average reward-to-risk is roughly a +1R expectancy per trade. Systems
that genuinely sustain that are rare, and typically do so by trading very rarely and by having a
real structural edge rather than a better indicator combination. So the honest expectation to hold
going in is:

- Most strategy variants will fail the gate. That is the gate doing its job, not wasted work.
- The path to passing usually runs through **more selectivity**, not more signals — which is
  exactly the operating principle you already set out.
- If a variant sails through on the first try, the first thing to do is hunt for a data leak. A
  mandatory leak hunt is built into the Phase 10 gate for that reason.
- The system is designed so that "nothing is approved for live" is a stable, safe, useful state:
  it still analyses, still journals, still shows you the rejection ledger, and still tells you
  exactly why it is not trading.

Capital preservation over profit, as specified.
