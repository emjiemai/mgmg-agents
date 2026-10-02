# Lead hand-out — one lead a morning, "how is it going?" at 15:00

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

- **Which lead:** today's new leads first, then the best older leads nobody has
  been given (priority, then confidence, then newest). One lead goes to one
  person, once. When the leads run out, the rest get none that day.
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
