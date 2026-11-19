# 36 — The bot's screens by asking in words (2026-11-19): the Director, the Garmin manager

**Why (the owner, 19.11):** the Director's ☰ menu had 16 commands — "keep the important ones in the menu, the others
should be capabilities: when the Boss asks anything, it does it".

**Menu now: /pulse only** (later on 19.11, the owner: «only /pulse should be kept»; seven for a few hours before) —
`telegram/menus.py` `DIRECTOR`, and the Garmin manager's `GARMIN_MANAGER` the same way. The report calendar comes
every morning after the brief (spec 37). Every typed command still works; /bekor is shown inside each flow that
can be cancelled.

**Asked in words:** the router (`prompt.CLASSIFY_SYSTEM_PROMPT`, rule «THE BOT'S OWN SCREENS AND BUTTON ACTIONS») sends
a request for one of the bot's own screens — or for an action he confirms with a button — to the analyst with agent
`bot_ekranlari` (`roles.AGENTS`); it overrides the «can't change a record» rule. The analyst's tool `open_screen`
(Director only, `TOOL_SYSTEMS = None`) queues the screen; after its answer `screens.open_all` runs the **same function
the command runs** (`integrations/org_bot/screens.py`, `SCREENS`):

| key | what | command |
| --- | ---- | ------- |
| pulse | PULSE link | /pulse |
| kpi | KPI table | /kpi |
| report_calendar | report calendar picture | /hisobotlar |
| goals | goals and results | /maqsadlar |
| set_goal | goal for a named person (`kg:` card) or the picker | /maqsad |
| rating_cards | rating cards — only with `KPI_MODE=full`, else a note | /baho |
| excused | the named person's 14-day grid (tap a day) or the people list | /sababli |
| leads | lead statistics with ⏸ / ▶️ | /lidlar |
| reconcile | SAP ↔ 1C ↔ Billz Excel | /farqlar |
| duties | duties register with ▶️ / ⏸ | /vazifalar |
| plans | plans vs fact | /reja |
| sap_entry | who didn't enter SAP | /kiritish |
| weekly_review | the week's duties analysis now | /tahlil |
| garmin_stock | Garmin stock Excel | /qoldiq |
| old_stock | old stock PDF | /eski |

**The Garmin manager** (19.11): his messages go straight to his analyst (scope `sap_garmin`, `billz`, `garmin`); the
same `open_screen` tool is built from **his** list (`screens.GARMIN_SCREENS`, `screens.for_role`; `TOOL_SYSTEMS`
`{"garmin"}` — accounting never gets it) and his answers open the queued screens (`_answer_data_question_now`).
Each runs what his command runs — his team's:

| key | what | command |
| --- | ---- | ------- |
| pulse | PULSE link (his team) | /pulse |
| kpi | Garmin сотув KPI table | /kpi |
| report_calendar | his team's calendar | /hisobotlar |
| goals | his team's goals with result buttons (= his /natija) | /maqsadlar |
| set_goal | a goal for his team member | /maqsad |
| excused | his team member's grid / the list | /sababli |
| duties | his team's day (daily cards) | /vazifalar |
| sap_entry | his team's SAP entry | /kiritish |
| garmin_stock | Garmin stock Excel | /qoldiq |
| old_stock | old stock of the Garmin warehouses | /eski |
| competitors | the competitor check | /raqobat |

**Rules:** actions only open the card — the Director confirms with his own tap; the AI never changes anything. At most
three screens tried per answer, each once; a screen that fails is told with its command, the others still open.
Only the Director and the Garmin manager, each their own list (checked again in `open_screen`). Logs: `screen_opened` with the key and outcome only. When the
analyst is down, the fallback (`_fetch_screens_data`) tells him each screen's command.

Tests: `test_screens_1119`, `test_garmin_screens_and_digest_1119`.
