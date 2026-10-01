# Attendance from Verifix (A4)

**Code:** `integrations/verifix/client.py` (the API), `integrations/verifix/attendance.py`
(the rules), `integrations/common/translit.py` (Latin names → Cyrillic)
**Shows up in:** the 08:00 brief (yesterday), OPS Manager Bot's `davomat`
answers (today so far + last 30 days), Admin Bot `/verifix` (connection check)
**Mode:** read-only — nothing is ever written to Verifix
**Switch:** on as soon as `VERIFIX_CLIENT_ID` and `VERIFIX_CLIENT_SECRET` are
set; `VERIFIX_ENABLED=false` hides it again

## What the Director sees

In the morning brief, under the daily reports:

    🔴 Давомат (27.09.2026): 2 киши кечикди, 1 киши келмади — 14 кишидан
       • Ширин Умматова — келмади
       • Алишер Каримов — 35 дақиқа кечикди (09:35)
       • Бобур Алиев — 12 дақиқа кечикди (09:12)

🟢 when everyone came on time, 🟡 when someone was late, 🔴 when someone
didn't come. No block on a day nobody had to work, and none at all until
Verifix is connected. If Verifix can't be read: "⚠️ Verifix'дан маълумот
олиб бўлмади".

And any time, in OPS Manager Bot: "bugun kim kechikdi?", "ishga kim
kelmadi?", "Alisher soat nechada keldi?", "bu oy kim ko'p kechikdi?".

## The rules

From Verifix's own timesheet (`timesheet$export`), per employee per day:

- Only **working days** of the employee's Verifix schedule count; a day off
  or a holiday is never "absent", even if they came in.
- **Late** — first arrival more than `VERIFIX_LATE_GRACE_MINUTES` (default
  **5**) after the schedule's start. The minutes are the difference between
  the two times. (Verifix documents its time facts as seconds, but its own
  example only adds up as minutes — so their size isn't used.)
- **Didn't come** — a working day with no arrival and nothing excusing it.
  Today, while the day is still on, it reads "ҳали келмади"; before the
  shift starts nobody is counted.
- **Excused** — Verifix recorded sick leave, vacation, unpaid leave, a
  business trip or a day off; an hourly leave excuses coming late. Which is
  which is read from the company's own time kinds (`time_kind$list`) by
  name, then by Verifix's standard letter codes (ОП, Б, ОТ, К, О, ПО...).
- **Left early** — noted in the bot's answers (not in the brief).
- Names are shown in Cyrillic; names typed in Latin in Verifix are converted
  ("G'ofurov Sherzod" → "Ғофуров Шерзод").

## Connecting it (once)

In Verifix (as an administrator):

1. **Role:** Настройки → Администрирование → Пользователи → Роли → create a
   role (e.g. "MGMG AI") → Прикрепить → Прикрепление доступов → Доступные →
   **Внешние системы** → attach the attendance report (the API docs call it
   **Отчет по посещениям**, `timesheet$export`) and **Виды времени** (time
   kinds) → allow reading → Сохранить. If the list names them differently,
   pick the ones about the timesheet and time kinds; `/verifix` below tells
   you if one is missing.
2. **Client:** switch to the Администрирование organisation → Настройки →
   Внешние системы → **Клиенты OAuth2 для сервера для компании** → create:
   type **client credentials**, your organisation, the role from step 1.
   Save and copy the **client_id** and **client_secret** (shown once).

In Render → Environment Groups → **mgmg-shared**:

    VERIFIX_CLIENT_ID=<client_id>
    VERIFIX_CLIENT_SECRET=<client_secret>

Then in Admin Bot send **/verifix**: "✅ Verifix уланди. Табелда: N ходим…"
means it works; otherwise it says what's wrong (wrong id/secret, or the role
is missing a form). The next 08:00 brief carries attendance.

Optional: `VERIFIX_LATE_GRACE_MINUTES` (default 5), `VERIFIX_FILIAL_ID`
(only if Verifix support says it's needed — client credentials already name
the organisation). The address, `https://app.verifix.com`, is fixed in the
code on purpose: old `VERIFIX_BASE_URL` / `VERIFIX_API_TOKEN` / `VERIFIX_MODE`
/ `VERIFIX_CSV_DIR` values from the integration removed on 2026-09-15 are
ignored and can be deleted from the env group.

## Or: a Verifix login and password (used since 2026-10-01)

The company couldn't create an OAuth client, so the docs' other way in is
supported ("Basic auth", marked deprecated but working). In Render →
`mgmg-shared`:

    VERIFIX_LOGIN=<user>@<company>     e.g. admins@emjiem
    VERIFIX_PASSWORD=<password>
    VERIFIX_FILIAL_ID=<the organisation's ID>

The organisation's ID is required in this mode. `/verifix` then shows the
organisation's name (`core/filial$info`), so a wrong ID is seen at once.
Best practice: a separate Verifix user for the bot with read access only,
not a person's own (or the main admin) account. If both ways are filled in,
the client id/secret win.

## API facts this relies on

Source: Verifix Public API v1.0,
https://documenter.getpostman.com/view/23097602/2s83ziN3VR (sent by Verifix
support, 2026-09-28).

- Token: `POST /security/oauth/token` with `grant_type=client_credentials`,
  `client_id`, `client_secret`, `scope=read` → `access_token`,
  `expires_in` (10800 s in their example). Renewed a minute early, and once
  more if Verifix answers 401.
- Every call: `Authorization: Bearer …`, `project_code: vhr`.
- Lists page with the `cursor` request header and `meta.next_cursor`
  (-1 = done); timesheet pages hold at most 100 employees.
- Numbers come as strings and some JSON is labelled text/plain — read as
  JSON regardless.
- Verifix's webhooks only announce hirings and dismissals, so attendance is
  read on demand, not pushed.

## Not done (yet)

- Linking a Verifix person to their Telegram account — the brief uses
  Verifix's names; a report-vs-attendance comparison needs that link.
- A late-arrival alert during the day (e.g. at 09:30) — easy to add on the
  same data if the Director wants it.
