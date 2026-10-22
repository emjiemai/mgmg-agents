# 27 — Explanations and own start times (2026-10-22, the Boss)

**Asked:** the Boss, on the 20.10 brief's attendance block — «Аниқланг, ростан келмаган бўлса тушунтириш
берсин, AI сўрасин» — and on the list of missed reports — «AI улардан изоҳ олсин, тушунтириш хати ёзиб
беришсин!». And the owner: "some employees come at 10 a.m. — set different times".

**Code:** `integrations/org_bot/explanations.py`, `agents/explanations/agent.py` (08:00, right after the
brief), `ops_manager._take_explanation`, tables `explanations`, `attendance_starts`.

## Every morning

1. **Absent** — Verifix, yesterday: a working day, no arrival, nothing excusing it (sick leave, vacation,
   trip, day off), not «эркин график». The run of absent working days up to yesterday is found (a day off
   doesn't break it): «охирги 3 иш куни (19.10, 20.10, 21.10)». The Verifix person is matched to the bot's
   employee by name (every word of the bot name in the Verifix name, any order, Latin or Cyrillic); two
   alike or none → not asked (counted as «unmatched» in the log).
2. **Report** — yesterday's daily report never came (closed at 22:00 as missed). Someone absent is asked
   once — the absence covers the report.
3. The ask (friendly voice, «сиз», no emoji): «…Verifix бўйича кеча, 21.10 куни ишга келганингиз
   кўринмади, сабабини ёзиб юборсангиз, тушунтириш хатингиз раҳбариятга етказилади, келган бўлсангиз ҳам
   шуни ёзинг, раҳмат» — «келган бўлсангиз ҳам» because Verifix can miss a face (the Boss: «аниқланг»).
4. One ask per person, kind and day (`uq_explanation`). Never asked: the Director, the Garmin manager.
   A day off (/dam) yesterday: nobody asked.
5. First, the asks of the day before are summed up for the Director (everyone) and the Garmin manager
   (his team): «Сўралди: 4 · ёзди: 3 · ёзмади: 1 — Ёзмаганлар: …».

## The answer

Open for 36 hours. It is the explanation when it is:
- a **reply** to the ask — always;
- a **plain message** — not a command, no «?», no report owed at that moment (then it's the report), and
  the AI agrees it answers the ask (a work question isn't one; if the AI can't be reached, yes);
- a **file** with the «тушунтириш» button (shown while an ask waits): a voice / video as text
  (report_media), a photo (a sick note) as it is.

It goes as a card to the Director, and to the Garmin manager for his team (`report_calendar.TEAM_OF`):
«📝 Тушунтириш хати — name (role) · келмаган кунлар: … · «their words»», the file itself with it. The person
is thanked under their message. This is the one more exception to "no messages between employees and
the Director through the bot" (the Boss asked for these letters), like a task's proof.

## Own start time

Admin Bot **/grafik**: beside each name, **🕘** — a tap goes Verifix → 08:00 → 09:00 → 10:00 → 11:00 →
Verifix. With a time set, «кечикди» is counted from it (`attendance.records(starts=…)`): Галимов at 10:41
was 101 minutes late on Verifix's 09:00, 41 on his own 10:00. The brief, the bot's attendance answers and
the absences all use it.

## Settings

`EXPLANATIONS_ENABLED` (true). Verifix not connected → only the report asks.

## Tests

`scripts/selfcheck.py`, `test_explanations_and_start_times`, and the /grafik checks in `test_flexible_schedule`.
