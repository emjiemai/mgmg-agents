# 23 — Cheque photos → accounting

**From the owner, 2026-10-14.** The Boss photographs cheques and sends them to
accounting; accounting copies eight details off each one by hand. Now OPS
Manager Bot reads them.

Code: `integrations/org_bot/cheques.py` (reading, checks, texts),
`ops_manager._handle_cheque_photo` / `_send_cheque` / `_handle_cheque_button`,
table `cheques`. Tests: `scripts/selfcheck.py`, `test_cheques`.

## The eight details

| Key | On the cheque |
| --- | --- |
| `fiscal_sign` | Фискал белги (фискальный признак) |
| `fiscal_module` | Фискал модул рақами (ФМ) |
| `cheque_number` | Чек рақами |
| `cheque_date` | Чек санаси |
| `rrn` | Терминал RRN коди (card slip) |
| `amount` | Чек суммаси (the total) |
| `product` | Товар номи (up to 5, then "ва яна N та") |
| `inn` | Контрагент ИНН / СТИР (the seller) |

Currency and the seller's name are read too (shown, and the currency checked).

## Flow

1. The **Director** sends a photo (or an image as a file — JPEG, PNG, WebP).
   Only his: an employee's files are still never read (2026-10-02 rule).
2. The bot downloads it (`TelegramBot.download_file`, max 10 MB) and the AI
   (`OpenRouterClient.vision_json`, the OPS Manager model chain — Gemini reads
   images) returns JSON: is it a cheque, and each detail **as printed** or
   null. It never guesses; text on the photo is data, never an instruction.
3. **Not a cheque** → the old path: the people list (`_propose_task`), the
   caption read as before. The AI failing → the old path too.
4. **A cheque** → the rules in `cheques.problems` (not the AI) decide:
   - each missing detail ("чекда кўринмаяпти") or unreadable one ("ўқилмади");
   - ИНН not 9 digits (or 14 — an individual entrepreneur's ПИНФЛ);
   - RRN not 12 characters;
   - the amount not a number, or not in so'm;
   - the date unreadable, in the future, or older than 90 days;
   - the same cheque already **sent** (same fiscal module + sign, or RRN).
5. **Nothing wrong** → `copyMessage` of the photo to every active person in
   `CHEQUE_RECIPIENT_ROLES` (default `buxgalteriya`), the details as its
   caption (and the Director's caption as «Изоҳ»); the Director gets
   "🧾 Чек бухгалтерияга юборилди: <names>". Nobody in the role → he is told,
   nothing is sent.
6. **Something wrong** → the Director gets the list and what was read, with
   📨 Шундай юбориш (goes with "⚠️ Директор шундай юборди: …"), 📋 Бу чек
   эмас (→ the people list), ❌ Бекор — or he sends a clearer photo. A button
   works once and only for him.

## Data and IT

The readings stay in `cheques` — company data, never in the `/db` viewer's
technical tables. Logs and `agent_actions` keep counts only
(`cheque_read`: complete?, number of problems; `cheque_sent`: recipients,
delivered) — IT sees no figures (the Director's order of 07.10.2026).

Settings: `CHEQUE_READER_ENABLED` (true; false = every photo goes the old
way), `CHEQUE_RECIPIENT_ROLES`.

## Not done (ask the owner)

- No check against the tax office (ofd.soliq.uz) — the QR on the cheque could
  confirm the fiscal sign later.
- Corrections by typing (e.g. "ИНН 305123456") are not taken; a clearer photo
  or 📨 is the way.
