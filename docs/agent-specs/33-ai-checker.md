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

## Admin Bot `/aicheck` (16.11)
- `/aicheck sinov` — a live test: seven fixed made-up samples (`SAMPLES`: «Юборганман» with no report → reject,
  illness → approve, «Лондри1 тозалаб…» → approve, «ок» → reject, task proofs, a 4th deadline move) through the real
  AI; IT gets «кутилган / AI» per sample. Nothing is changed, nobody else is messaged.
- `/aicheck` — what already waited before the checker (`store.waiting_for_boss`: explanations of 14 days answered
  but not excused, undecided late reports, tasks in review; never one already in `ai_decisions`): one heading to the
  Director, then each card sent to him again with its buttons and judged like a new one. IT gets counts only.
  Both run in the background (a dozen AI calls outlast Telegram's webhook wait).

Test: `test_ai_checker_1116`.

## Proof files (2026-11-18, the owner)
A task proof sent as a file used to reach the checker as the word «файл» (`task_register.evidence_of`'s label) — the
AI could only say unsure. Now (`proof_files.py`, `AI_CHECKER_READ_FILES`): the file is downloaded once and read —
Word paragraphs + tables, Excel rows per sheet (≤ 300 each), PDF text (≤ 20 pages; a scan → page 1 as a picture),
photos / images via `vision_json`; ≤ 12 000 characters, ≤ 15 MB; video, voice and archives stay unsure. The label
«файл» / «фото» is not passed as proof text. Nothing read is stored or logged; `ai_decisions.read_file` marks a
decision made with the file. File ids are kept at submit (`tasks.evidence_file_id/kind/mime/name`); a proof from
before is found in `employee_files` or recovered by a silent `forwardMessage` within the employee's own chat (the
copy deleted at once). Start-up runs the waiting file proofs once (`proof_files_backfill`, job_runs lease, counts to
IT); `/aicheck` re-checks a file proof whose decision was unsure without the file.
