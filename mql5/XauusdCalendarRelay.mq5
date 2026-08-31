//+------------------------------------------------------------------+
//| XauusdCalendarRelay.mq5                                          |
//|                                                                  |
//| Relays the MT5 terminal's built-in economic calendar to a file    |
//| the Python bridge reads.                                          |
//|                                                                   |
//| WHY THIS EXISTS                                                   |
//| The MetaTrader5 Python package does not expose the calendar on    |
//| every build, but MQL5 always does. The terminal's calendar is     |
//| free, already installed, and on the broker's own clock — which    |
//| makes it the right primary source. See                            |
//| docs/architecture/04-data-sources.md section 5.                    |
//|                                                                   |
//| INSTALL                                                           |
//|   1. Copy to MQL5/Experts/ in your terminal's data folder         |
//|   2. Attach to any chart (it does not trade)                      |
//|   3. Allow DLL imports is NOT required                            |
//|   4. Set the engine's calendar provider to read the output file   |
//|                                                                   |
//| It writes MQL5/Files/xauusd_calendar.json every RefreshMinutes.   |
//+------------------------------------------------------------------+
#property copyright "XAUUSD Trading System"
#property version   "1.00"
#property strict

input int    RefreshMinutes  = 30;                       // how often to rewrite the file
input int    LookBackHours   = 12;                       // include recent releases too
input int    LookAheadHours  = 72;                       // and the upcoming schedule
input string OutputFile      = "xauusd_calendar.json";   // under MQL5/Files/
input bool   USDOnly         = false;                    // false: let Python filter

datetime g_last_write = 0;

//+------------------------------------------------------------------+
int OnInit()
  {
   WriteCalendar();
   EventSetTimer(60);
   Print("XauusdCalendarRelay started, writing ", OutputFile,
         " every ", RefreshMinutes, " minutes");
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason) { EventKillTimer(); }

void OnTimer()
  {
   if(TimeCurrent() - g_last_write >= RefreshMinutes * 60)
      WriteCalendar();
  }

//+------------------------------------------------------------------+
//| Escape a string for JSON. Event names contain quotes and slashes. |
//+------------------------------------------------------------------+
string JsonEscape(const string s)
  {
   string out = "";
   int n = StringLen(s);
   for(int i = 0; i < n; i++)
     {
      ushort c = StringGetCharacter(s, i);
      if(c == '"')       out += "\\\"";
      else if(c == '\\') out += "\\\\";
      else if(c == '\n') out += "\\n";
      else if(c == '\r') out += "\\r";
      else if(c == '\t') out += "\\t";
      else if(c < 32)    out += " ";
      else               out += ShortToString(c);
     }
   return out;
  }

//+------------------------------------------------------------------+
void WriteCalendar()
  {
   datetime from = TimeCurrent() - LookBackHours  * 3600;
   datetime to   = TimeCurrent() + LookAheadHours * 3600;

   MqlCalendarValue values[];
   int count = CalendarValueHistory(values, from, to, NULL, NULL);
   if(count < 0)
     {
      Print("CalendarValueHistory failed, error ", GetLastError(),
            " — the terminal may not have calendar data for this account");
      return;
     }

   int handle = FileOpen(OutputFile, FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(handle == INVALID_HANDLE)
     {
      Print("cannot open ", OutputFile, ", error ", GetLastError());
      return;
     }

   FileWriteString(handle, "{\n");
   FileWriteString(handle, "  \"generated_at\": " + IntegerToString((long)TimeGMT()) + ",\n");
   FileWriteString(handle, "  \"server_time\": "  + IntegerToString((long)TimeCurrent()) + ",\n");
   FileWriteString(handle, "  \"events\": [\n");

   int written = 0;
   for(int i = 0; i < count; i++)
     {
      MqlCalendarEvent event;
      if(!CalendarEventById(values[i].event_id, event))
         continue;

      MqlCalendarCountry country;
      string currency = "";
      if(CalendarCountryById(event.country_id, country))
         currency = country.currency;

      if(USDOnly && currency != "USD")
         continue;

      if(written > 0)
         FileWriteString(handle, ",\n");

      // Times are the terminal's; Python converts using the measured broker offset.
      string row = StringFormat(
         "    {\"event_id\": %I64u, \"time\": %I64d, \"name\": \"%s\", "
         "\"currency\": \"%s\", \"importance\": %d, \"sector\": %d",
         values[i].event_id, (long)values[i].time,
         JsonEscape(event.name), currency, (int)event.importance, (int)event.sector);

      // Actual/forecast/previous are LONG_MIN when not yet released. Emit null so the
      // Python side cannot mistake a sentinel for a real value.
      row += ", \"actual\": "   + (values[i].HasActualValue()   ? DoubleToString(values[i].GetActualValue(), 4)   : "null");
      row += ", \"forecast\": " + (values[i].HasForecastValue() ? DoubleToString(values[i].GetForecastValue(), 4) : "null");
      row += ", \"previous\": " + (values[i].HasPreviousValue() ? DoubleToString(values[i].GetPreviousValue(), 4) : "null");
      row += "}";

      FileWriteString(handle, row);
      written++;
     }

   FileWriteString(handle, "\n  ]\n}\n");
   FileClose(handle);
   g_last_write = TimeCurrent();
   Print("wrote ", written, " calendar events to ", OutputFile);
  }
//+------------------------------------------------------------------+
