# MQL5 helpers

Small terminal-side scripts for the things the Python binding cannot do.

## `XauusdCalendarRelay.mq5`

Relays the terminal's built-in economic calendar to
`MQL5/Files/xauusd_calendar.json`, which the Python side reads.

**Why:** the `MetaTrader5` Python package does not expose the calendar on every build,
but MQL5 always does. The terminal's calendar is free, already installed, and on the
broker's own clock — which makes it the right *primary* source, ahead of any commercial
API. See `docs/architecture/04-data-sources.md` §5 for the full provider chain.

**Install**

1. Copy to `MQL5/Experts/` in your terminal's data folder
   (File → Open Data Folder in MT5).
2. Compile in MetaEditor (F7).
3. Attach to any chart. It does not trade and places no orders.
4. Point the engine at the file:

```yaml
# config/demo.yaml
news:
  calendar_file: "C:/Users/<you>/AppData/Roaming/MetaQuotes/Terminal/<hash>/MQL5/Files/xauusd_calendar.json"
```

**Notes**

- `actual` / `forecast` / `previous` are emitted as `null` when unreleased, never as a
  sentinel value — a sentinel mistaken for a real number is a direct route to a
  look-ahead bug.
- Times are the terminal's server times. Python converts them using the *measured*
  broker offset (`BrokerClock`), not a configured one.
- If the terminal has no calendar data for your account (some brokers disable it), the
  relay logs the error and the engine falls back to the next provider in the chain,
  ultimately the curated schedule. That fallback is logged loudly.
