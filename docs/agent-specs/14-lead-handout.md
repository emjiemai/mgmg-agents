# B2B lead tracker — back 2026-10-28 (the Boss)

> Stopped 2026-10-07 by the Director; **back on 2026-10-28 with the Boss's own design**: «each lead found on the
> internet gets a number (Hilton 33) and goes to a sales person; the AI follows lead 33 — did you contact? which
> day? sold — how much, what? not sold — why? — and sends me the analytics: 40 leads → 3 sold, 37 not; why: the
> lead was old, the seller didn't contact, contacted late; lost opportunities 300 mln». «Менга ҳам келсин.»
> «Вақтинча stop / start қилиш керак» → stop / start on /lidlar (the Director in OPS Manager, the admin in Admin Bot).

**Code:** `integrations/org_bot/leads.py` (rules, wording, the amount, the statistics — pure),
`integrations/org_bot/lead_tracker.py` (sends, buttons, typed answers, /lidlar, the PDF, the switch),
`agents/lead-handout/agent.py` (08:00), `integrations/reports/templates/leads.html` (the table),
PULSE: `integrations/pulse/data.py` `leads()`, `/pulse/api/leads`, the «Лидлар» card.
**Runs:** 08:00 in the morning job right after the Lead Agent; **15:00–15:25 inside the API's 5-minute round**
(`lead_tracker.tick` — no daytime cron); Mondays 08:00 the statistics.
**Stored in:** `leads` (the sheet, copied; `leads.id` = the lead's number), `lead_assignments` (+ `contacted_on`,
`sold_amount`, `sold_currency`, `closed_by`), `lead_checkins` (+ status `not_contacted`), `lead_handout_switch`.
**Switch:** `lead_handout_switch` — **no row = stopped**: nothing goes out until someone presses ▶️ on /lidlar.
`BOTS_FROZEN` and a day off (/dam) stop it too.

## The day

| Time | What happens |
| ---- | ------------ |
| 08:00 | The Lead Agent fills the sheet; it is copied into `leads`. Each B2B Sotuv person at work today gets **one** lead card: «🎯 Лид №33 Hilton», a two-sentence Uzbek summary (AI, written once), place · stage · priority, contact, source link. Newest lead first (the owner, 02.10). The Boss gets one message: «№33 Hilton → Абдукарим Зунунов», one line per lead. |
| 15:00 | Leads never contacted in **7 days** are closed as «алоқага чиқилмади» (the seller is told). Every open lead of everyone at work is asked about, one message each. |
| Mon 08:00 | The Boss: the 30-day statistics (/lidlar's message) and the PDF table — whether the tracker is on or off, when any lead was given in the period. |

## The 15:00 questions (buttons on one message, edited in place)

1. Not contacted yet → **Мижоз билан боғландингизми?** ✅ Ҳа, бугун · ✅ Ҳа, кеча · ❌ Ҳали йўқ.
   «Ҳали йўқ» → asked again tomorrow (the message says in how many days it closes).
2. Contacted (now or on an earlier day) → **Натижаси қандай?** 💰 Сотилди · ❌ Сотилмади · ⏳ Жараёнда.
   * **Сотилди** → the lead is done; the bot asks «Нима ва қанчага сотилди?» — typed in one message
     («2 та кир ювиш машинаси, 120 млн сўм»). The amount is read from it (млн / млрд / минг / сўм / $); a bare
     «120» or words with a number but no clear sum are asked again — never guessed; words with no number go to
     the work AI as usual. Saved → **the Boss is told at once** («💰 Лид №33 Hilton сотилди — Абдукарим: 120 млн сўм»).
   * **Сотилмади** → why, as buttons: лид эскирган · эҳтиёжи йўқ · бошқадан олди · нархи тўғри келмади ·
     жавоб бермади · бошқа сабаб (→ typed, kept as «бошқа сабаб: …», counted as «бошқа сабаб»).
   * **Жараёнда** → asked again tomorrow.
3. One answer per lead per day; only the seller's own buttons work; the typed answer is taken for 3 hours, never
   from a reply to another message or a question.

## The Boss's statistics

For the leads given in the period (30 days on /lidlar and Mondays; 7 / 30 / 90 in PULSE):
given · contacted (average days from given to contacted) · **late** = contacted after the next day, or not
contacted and already past it · sold (count, sum in so'm, + $ apart) · not sold by reason · open ·
**lost opportunity ≈ not sold × the average so'm sale so far** (none before the first sale — never guessed),
and the part of it from leads never contacted or contacted late · the same per seller.

* **/lidlar** (the Director, ☰ menu): that message + ⏸ Тўхтатиш / ▶️ Бошлаш + 📄 Жадвал (PDF).
* **📄 the PDF table** (A4): five figures on top, the reasons, a sellers table, then every lead — №, company,
  seller, given, contacted (date · days, red when late), state (green sold, red not sold), amount / reason.
* **PULSE** (the Director only — the Garmin manager's view is his team, the endpoint answers 404 to him): a
  «Лидлар» card under the board: six figures, the reasons as bars, the sellers, every lead.
* The analyst's `lidlar` source: the same figures and every lead, for questions in words.

## Stop / start

* The Director: /lidlar → ⏸ / ▶️; IT is told (no figures).
* The admin: Admin Bot **/lidlar** → on or off only (who set it, when) — never leads, people or sums (the
  Director's order of 07.10); the Director is told.
* Stopped: no hand-out, no 15:00 questions; the data and the statistics stay. Never started = stopped.

## KPI

Unchanged: the 15:00 questions asked / answered that day count in «Жараён», closed leads in the finished work
(`store.lead_kpi_rows`, `kpi_score`). «Ҳали йўқ» is an answer.

## B2B sellers

☰ menu **/lidlar** — their own open leads (№, company, given, contacted). Old buttons from before 07.10 (`ld:` /
`lc:`) still just come off.

## History: how it worked the first time (2026-09-30 – 2026-10-07)

**Code:** `agents/lead-handout/agent.py` (sends), `integrations/org_bot/leads.py`
(the rules and wording), `integrations/org_bot/ops_manager.py` (taps, typed answers)
**Runs:** 08:00 in `mgmg-morning-agents`, right after the Lead Agent; 15:00 in
`mgmg-team-cheer` (the daytime job, `run_morning_agents.py --daytime`)
**Stored in:** `leads` (the sheet, copied), `lead_assignments`, `lead_checkins`
**Switch:** `LEAD_HANDOUT_ENABLED` (default `true`)

Asked for by the owner on 2026-09-30: the Lead Agent (F2) collects leads;
every morning each B2B sales person gets one; at 15:00 the bot asks how it's
going with three buttons, asks a question about the result, and KPI counts it.

## The day

| Time | What happens |
| ---- | ------------ |
| 08:00 | The Lead Agent runs; its sheet is copied into `leads`. Each B2B Sotuv person at work today (Saturday/Sunday: weekend workers only) gets **one** lead card. |
| 15:00 | For **every lead still open** — today's and older ones in progress — one friendly line: "Алишер ака, «Hyatt Regency» лиди қандай кетяпти, 3-кун? 🙂" with **жараёнда · рад этилди · бажарилди**. |
| after a tap | **жараёнда** → "кейинги қадамингиз нима ва қачон?" (typed answer kept as a note). **рад этилди** → reason buttons: эҳтиёжи йўқ · боғланиб бўлмади · бошқадан олишган · бошқа сабаб. **бажарилди** → result buttons: учрашув бўлди · таклиф юборилди · шартнома тузилди · бошқа натижа. "бошқа…" asks them to write it. |

## Rules (the owner's decisions, 2026-09-30)

- **Which lead:** always the **newest first — "from the end" of the sheet**
  (2026-10-02: a fresh lead negotiates best and leaves the most time): by the
  day it was added, then the later row first; priority only breaks a tie.
  Older unworked leads top up a thin morning the same way. One lead goes to
  one person, once. When the leads run out, the rest get none that day.
- **In progress stays:** an open lead is asked about every day at 15:00 until
  it's dismissed or done, and a new lead still comes every morning — open
  leads can pile up, by design.
- **Every track to B2B Sotuv:** Londry equipment, Londry service and the
  Garmin/Tanita sponsorship track.
- **KPI inside the existing parts** (`kpi_score.py`): 15:00 questions answered
  the same day count in 🔄 Жараён together with on-time reports; closed leads
  (dismissed or done) count in 📦 Иш ҳажми; a Director's goal that mentions
  "лид" ("10 та лидни бажариш") is filled from the leads marked done when no
  result was entered. Weights unchanged.

## The card

The Lead Agent writes English, so the card's summary is written once per lead
by the AI in Uzbek Cyrillic (kept in `leads.brief`); an AI answer that isn't
mostly Cyrillic is thrown away and the card says "Батафсил маълумот манбада."
The card shows the place (Cyrillic), the stage and priority in Uzbek, the
contact if the lead has one, and a link to the source. It's work, so it keeps
a card's format; the 15:00 question is one friendly line (`tone.py`).

## The typed answer

The follow-up answer is the person's next message within 90 minutes, or a
Telegram Reply to the question at any time. If the 16:00 report ask came
after the question, a plain message is taken as the report instead (a Reply
still goes to the lead).

## The Director

"lidlar qanday?", "Alisher lidi nima bo'ldi?", "qaysi lidlar rad etildi?" —
the `lidlar` agent in OPS Manager Bot answers from the last 30 days: who has
which lead, where it stands, the reason or result, the latest note. The
leads the Lead Agent *found* stay with `lead_agent`.

## People edit the sheet (2026-10-02)

Sales people may get edit access to the leads Google Sheet — share it from
Google Sheets (Share → their email → Editor); the bot can't share files. So
every reader and writer finds columns **by their header** (`leads.column_order`,
`sheet_records`, `row_for_sheet`): moving a column or adding your own column
doesn't break the hand-out, the Lead Agent's dedupe or its new rows. Don't
rename the header cells — if most of them aren't recognised, the bot falls
back to the original column order.

## Runbook

```bash
python agents/lead-handout/agent.py --morning --dry-run    # who would get what (copies the sheet, sends nothing)
python agents/lead-handout/agent.py --checkin --force      # ask about open leads now
```

- **Pause:** `LEAD_HANDOUT_ENABLED=false` in `mgmg-shared` (`BOTS_FROZEN` stops it too).
- **No leads handed out:** the sheet needs rows (the Lead Agent writes it only
  with `AGENT_WRITES_ENABLED=true`), and B2B Sotuv needs registered people.
