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
   A day off (/dam) yesterday: nobody asked. A day Verifix didn't record («no data», spec 11): nobody asked
   about it. Verifix unreachable: the report asks still go (2026-10-24 — the run used to crash).
4a. **Sent in the one morning message** (2026-10-24, spec 30): the agent leaves a note; agents/morning-notes sends
   each person one message — the bonus line and task reminders with it, this question last (a missed report's
   «no point» said inside it). That message is the prompt a reply answers.
5. First, the asks of the day before are summed up for the Director (everyone) and the Garmin manager
   (his team): «Сўралди: 4 · ёзди: 3 · ёзмади: 1 — Ёзмаганлар: …».

## The answer

Open for 36 hours. A **reply** to the morning message, or a **plain message** (not a command, no «?», no report
owed at that moment — then it's the report), is read by the AI (2026-10-24, `ops_manager._classify_answer`):
- **reason** — a reason, an excuse, an apology, «I did come / did send it» → the explanation (below);
- **report** — for a «report didn't come» question: the message IS the missing report → the late report for
  approval (spec 29), not an explanation card;
- **other** — about something else (the morning message may list tasks too, so a reply can be about one) → on to
  the report / the work AI. If the AI can't be reached: reason.

It is also the explanation when it is:
- a **file** with the «тушунтириш» button (shown while an ask waits): a voice / video as text
  (report_media), a photo (a sick note) as it is.

It goes as a card to the Director, and to the Garmin manager for his team (`report_calendar.TEAM_OF`):
«📝 Тушунтириш хати — name (role) · келмаган кунлар: … · «their words»», the file itself with it. The person
is thanked under their message. This is the one more exception to "no messages between employees and
the Director through the bot" (the Boss asked for these letters), like a task's proof.

## Verifix didn't record (2026-10-24, spec 11)

IT marks the day on Admin Bot `/verifix` (⛔): the unanswered «absent» asks covering it are closed
(`store.withdraw_absent_asks`, answer «[Verifix ишламади — савол бекор қилинди]»), each person gets an apology
(«кечирасиз, … жавоб бериш шарт эмас»), the Director is told; the next morning's summary doesn't count them as
«ёзмади». A question withdrawn before the morning message went out isn't asked in it.

## Excused, late report (2026-10-23, spec 29)

The card carries **⬜ Сабабли** (the Director; the Garmin manager for his team): the card's days turn grey. The
«report didn't come» ask carries **📝 Кечаги ҳисоботни юбориш**: the report then goes for ✅ / ❌ — approved = green.

## Own start time

Admin Bot **/grafik**: beside each name, **🕘** — a tap goes Verifix → 08:00 → 09:00 → 10:00 → 11:00 →
Verifix. With a time set, «кечикди» is counted from it (`attendance.records(starts=…)`): Галимов at 10:41
was 101 minutes late on Verifix's 09:00, 41 on his own 10:00. The brief, the bot's attendance answers and
the absences all use it.

## Settings

`EXPLANATIONS_ENABLED` (true). Verifix not connected → only the report asks.

## Tests

`scripts/selfcheck.py`, `test_explanations_and_start_times`, and the /grafik checks in `test_flexible_schedule`.
