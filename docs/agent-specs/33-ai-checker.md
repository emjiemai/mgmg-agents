# 33 — AI checker (2026-11-16)

**Why (the owner, 16.11):** the Boss doesn't tap ✅ / ❌ — late reports, explanations, task proofs and deadline asks
waited for days (the 15.11 brief: two tasks «текширувда», nothing decided). «Юборганман» as an explanation should be
rejected (no report came); a late report with real work in it («Лондри1 тозалаб, Лондри2 тозалаб…») approved.
Decided: the AI decides **immediately**; the Boss can undo.

**Code:** `integrations/org_bot/ai_checker.py`; table `ai_decisions`; settings `AI_CHECKER_ENABLED` (true),
`AI_CHECKER_KINDS` (`explanation,late_report,task_proof,reschedule`).

## Flow
1. The card goes to the deciders as before (`_deliver_explanations`, `late_report.deliver`, `_deliver_review`, the
   deadline ask). Each delivered card is kept (chat, message id, caption or text).
2. `ai_checker.spawn(kind, review_…)` runs in the background (the employee's reply never waits): one OpenRouter
   JSON call (`SYSTEM`) with facts only — the text sent, the day(s), for a «no report» explanation what the bot holds
   for that day (`store.report_states`: submitted / late waiting / declined / excused / none), for a task its summary,
   expected result, typed proof and «has a file», for a deadline the old / asked date, reason and moves so far.
3. Verdict `approve` / `reject` / `unsure` + one polite Uzbek Cyrillic reason (`tone.is_polite`, else unsure).
4. Sure → the AI **presses the decider's own button** (same handler, same messages to the employee): a callback with
   `ai: true`, no query id, from the Director (the task's assigner). Explanation approve → ⬜ Сабабли; explanation
   reject → nothing pressed (the card keeps ⬜). Late report → ✅ / ❌ (card says «— AI текширувчи»). Task → ✅ or a
   ↩️ reason code (`RETURN_REASONS` e/q/r/o), the employee also gets «🤖 Изоҳ: …». Deadline → ✅ Розиман / ❌.
   Only a real decision counts (`APPLIED`); «someone was first» is shown as unsure.
5. A note under the card: «🤖 AI текширувчи ✅/❌ … — reason» + **↩️ AI қарорини бекор қилиш** (`aiu:<id>`).
   Unsure → «аниқ хулоса қила олмади … Ўзингиз ҳал қилинг», nothing decided.

## Undo (`aiu:`)
Only the card's owner or the Director. The record's columns before / after the press are in `ai_decisions`
(`TASK_COLS`, `REPORT_COLS`); undo restores `before` only while the row is still exactly `after` (`restore_sql`),
puts the card's buttons back, edits the note, tells the employee «… қарор бекор қилинди — раҳбар қайта кўриб
чиқади», task event `ai_undone` (PULSE «AI қарори бекор қилинди»). Not undone: dependants already told they may
start (accepted task). An excused explanation is undone with the card's own ↩ button.

## Rules kept
Files employees send are never read: a proof that is only a file → unsure (the Boss checks). Logs: counts and the
verdict only (`ai_check_<kind>`: decision, applied) — never the text. The note is a reply (never held by the message
budget). `TASK_PROOF_HINT_ENABLED` (advisory line) is superseded by this for decisions.

Test: `test_ai_checker_1116`.
