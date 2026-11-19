# 37 — The Boss's morning package (2026-11-19)

**Why (the owner, 19.11):** «he is not seeing the brief, so we need to use psychology, we need to prove that this
system is already working». The brief picture stays short; concrete proof follows it: who sold what, what everyone
wrote, what everyone did.

**Where:** `integrations/org_bot/morning_digest.py`, called by `agents/ceo-daily-brief/agent.py` right after the
picture is sent and stored (a failure never fails the brief). Once a day (`agent_actions` `morning_digest_sent`);
`MORNING_DIGEST_ENABLED` (true). Every active Director gets, each as its own message:

1. **Report calendar** — the /hisobotlar picture for the month of yesterday (`report_calendar.picture(day=yesterday)`;
   on the 1st the whole past month). The text form if the picture can't be drawn.
2. **Кечаги сотувлар** — SAP's pushed invoice lines (`v_sap_gateway_latest` tool `sales_lines`) dated yesterday,
   cancelled left out: per salesperson (SlpName → the bot's name, `sap_entry.display_name`; none → «Сотувчи
   кўрсатилмаган · киритган: <SAP user>»), each document's customer and so'm total (DocTotalSy — the same as the
   brief's «Кечаги сотув»), each product × pieces and its price (GTotalSC, the line in so'm with VAT; «2 × 9 805 000 =
   19 610 000 сўм»); credit notes as «↩️ қайтариш» with minus. Then the shops' Billz cheque lines of yesterday by shop
   and seller (`positions_kept`), returns marked — with the note that shop cheques are entered into SAP too. At most
   12 products per invoice and 120 lines in all («+яна N», the full list offered in words). Stale SAP (last push before
   today) is said.
3. **Кечаги ҳисоботлар** — every report of the last asked day (the brief's reports day), in full and escaped: the
   person, role, time; «22:00 да чатдан тузилди», «📎 N та файл», a late one with its state (кечиккан, қабул
   қилинди / рад этилди / қарор кутилмоқда); who didn't send; excused counted. Long lines are wrapped so the
   message splitter keeps tags whole.
4. **Кеча ким нима қилди** — for the reports day: the bot's own work (messages sent from `message_budget`, reports
   taken, tasks given, task steps, AI checker decisions with «сизга қолдирди», complaints in / closed), then each
   person: report time / chat-built / late / none, task steps with codes (`PERSON_EVENTS`; more than four counted),
   daily duty cards (✅ ⛔ 👎 жавобсиз), complaints closed (`resolved_by` matched to the person), lead answers,
   explanations, questions to the work AI (counts). People with nothing at all are named at the end; an excused day
   isn't «nothing»; the Director is never listed.

Each message ends with a line of what to ask in words (his menu is PULSE only, spec 36).

**The Garmin manager:** his team's calendar right after his Garmin brief (`agents/garmin-brief/agent.py`
`team_calendar`).

**Privacy:** the Director only; nothing to IT (the order of 07.10.2026). The log row keeps the part names and counts
(`parts`, `failed`, `sap_docs`, `shop_lines`, `reports`, `people`, `directors`) — never text, names or sums.

**Not here on purpose:** goals and the open-task list (out of the picture by the owner); overdue tasks still come in
the task tracker's morning list.

Test: `test_garmin_screens_and_digest_1119`.
