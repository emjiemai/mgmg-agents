# HARI — EMJIEM's AI HR assistant (Telegram)

Built from `HARI_AI_HR_Texnik_Topshiriq_UZ_ 04.11.2026.docx` (the spec) and
`HARI_150_Savol_Javob_IT_Qollanma_UZ.docx` (the 150 scenarios). Section numbers (§) below refer to the spec.
HARI is a separate bot from @mgmgopsmanagerbot (МИЯ). Bot: **@mgmHaribot**.
In the mgmg-agents repository it lives in its own folder, `hari/`, with its own requirements and tests:
run every command below from that folder. It shares no code, database or deployment with МИЯ.

Everything the bot says is Uzbek Cyrillic, with the polite «сиз» form. People who choose Latin see Latin.
Code, comments and docs are in English.

## What works now (phase 1, spec §26 weeks 5–8)

| Area | What HARI does | Spec |
| --- | --- | --- |
| Employee card | HR fills it in the bot (➕ Янги ходим, 8 steps). The same card sent again makes no second person (A01) | §5 |
| Identity | A one-time link (72 h) binds the person's own Telegram account. One account = one person. Chat text never gives a role | §3, §5, §23 |
| Access | Server-side checks for every document, step and request. A candidate or an unbound account sees nothing internal | §4 |
| Knowledge answers | Searches only documents this person may read (approved, in force, right role/department), Latin or Cyrillic. The model writes from those sources only; the server adds «Манба: title, version». No source → "тасдиқланган жавоб топилмади" + a button to send it to HR | §10, §12, §24, §30 |
| Safety rules | Pay, bank, other people's data, company finance, passwords, "I'm the director", prompt injection, complaints, legal disputes, personal crisis → fixed approved replies, never sent to the model | §23, HARI-008/042/086–097/126–129 |
| Output checks | Answer with an unknown source, the «сен» form or something that looks like a card/passport/token is stopped | §30 |
| No AI | No key, provider down or over limit: the approved excerpt with its source is shown instead (A12) | §28 |
| Onboarding | 90-day plan (day 1, 2–7, 8–15, 30, 60, 90). Read steps: the employee's «танишдим». Tasks/meetings: proof → the mentor/manager/HR accepts or returns with a reason. «Бажардим» is never acceptance | §9 |
| HR requests | 11 types, number HR-YYYY-MM-NNNN, routed (leave → manager, pay → accountant, legal → lawyer, …), 4 work-hour first-answer deadline. A complaint never goes to the person it's about; about HR → owner (A13). "Answered" and "closed" are separate | §10, §18, §32 |
| Timers | Morning onboarding reminder only on work days and hours, at most 2 planned messages a day (A07). Late first answers → handler + owner, once. Messages are saved before sending, so a crash neither loses nor doubles one (A09) | §10 |
| Audit | Who did what to which object and the outcome, in `audit_events`. The answer log keeps intent, sources and outcome, never the question text | §20, §30 |
| Emergency stop | `HARI_PAUSED=true`: no AI answers, no proactive messages | §1 |

## Run it

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt     # Linux/macOS: .venv/bin/python
cp .env.example .env                                        # then fill TELEGRAM_BOT_TOKEN (and OPENROUTER_API_KEY)
.venv/Scripts/python -m hari.main
```

The database creates itself (SQLite file `hari.db` locally; set `DATABASE_URL=postgresql+asyncpg://…` for production).
Knowledge files load at every start; a changed file replaces its old version, a removed one is retired.

**First HR / owner account** (nobody exists yet to invite them). The person writes `/start` to the bot, which shows
their Telegram ID. Then:

```bash
.venv/Scripts/python -m scripts.add_staff --tg 123456789 --name "Фамилия Исм Отасининг исми" --role hr --department HR --position "HR масъули"
```

Everyone else: HR presses **➕ Янги ходим**, hands the link to the person privately, and they open it themselves.

## Tests

```bash
.venv/Scripts/python -m pytest -q          # 54 tests, no network
.venv/Scripts/python -m pyflakes hari tests scripts
```

They cover A01, A02, A03, A04, A06, A07, A09, A12 and A13, plus an end-to-end run of the real dispatcher with a fake
Telegram connection (HR adds someone → link → plan → proof → mentor accepts → question → HR request → answer).

## Knowledge documents

One markdown file per document, with a passport (see `hari/knowledge/loader.py`):
`doc_id, title, version, status (draft|approved|retired), classification (public|internal|role), roles, departments,
effective_from/to, owner, approved_by, review_at`. Salary, bank, medical and complaint material is refused — it never
goes into the general HR search.

`knowledge/samples/` holds **sample** texts written from the spec (every title starts «НАМУНА»). HARI uses them
until the real, approved documents go into `knowledge/docs/` and `KNOWLEDGE_DIR=knowledge/docs` is set.
Real staff data only after the checks in §28 pass (§31).

## Layout

```
hari/
  main.py            start-up: schema, knowledge, polling, scheduler
  config.py          settings (.env)
  db/                models (§22) and sessions
  access.py          who may see / do what (§4)
  employees.py       cards, invites, identity binding (§5)
  knowledge/         loader (passport, chunks) and permission-filtered search (§12)
  guard.py           fixed safety rules and output checks (§23, §30)
  answer.py          question → answer pipeline (§10, §20)
  llm.py             OpenRouter client with model fallback
  onboarding.py      90-day plan and human acceptance (§9)
  cases.py           HR requests register and routing (§32)
  scheduler.py       reminders, escalation, reliable sending (§10)
  bot/               Telegram handlers, texts, keyboards
scripts/add_staff.py first HR / owner account
knowledge/           samples/ (for now) and docs/ (real documents)
tests/
```

## Not done yet (next phases, spec §26)

- **Weeks 9–12:** link to МИЯ (task / proof / KPI read from МИЯ, never computed here — §15, §21; needs an API or
  read-only access from Ulugbek), training and skill evidence (§14), exit checklist and access revocation (§25),
  dashboard and daily 18:15 / weekly reports for Зайнаб and the owner (§17), 7-day access review (§9).
- **Weeks 13–16:** vacancies, candidates, calendar, interview packet (§6–7).
- **Before the pilot:** real approved SOPs; the 150 scenarios as an automated test set (§13, §28); PostgreSQL on a
  server in Uzbekistan and masking personal data before the AI (§19 — needs the lawyer's check of the AI provider);
  approvals with action hash for role changes (§2); webhooks instead of polling; Russian fixed texts;
  revoke and reissue the bot token (it was shared in a chat).
