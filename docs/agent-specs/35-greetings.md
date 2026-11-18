# 35 — Holiday and birthday greetings (2026-11-18)

**Asked by:** the Boss, task T-2026-11-0052 (02.11): «AI операцион менежер ҳаммани байрамлар ва туғилган кун билан
табриклашни бошласин, бунинг учун маълумотлар олдиндан киргазилсин».

**Code:** `integrations/org_bot/greetings.py`, `agents/greetings/agent.py` (08:00 job, after the morning message).
**Table:** `greetings_sent` (day, employee, kind, key — once each). **Setting:** `GREETINGS_ENABLED` (true).

## Data — entered in advance, in the duties register's Google Sheet (`DUTIES_SHEET_ID`)
- **«Байрамлар»**: Сана | Номи | Кимга | Табрик. «01.09» = every year; «20.03.2026» = that year only (Рамазон / Қурбон
  ҳайит — added each year). Кимга: ҳамма / эркаклар / аёллар — by the admin's ака / опа on `/xodimlar` (never guessed;
  nobody set → no men's / women's greeting). Табрик: own text, or empty = the house text. The bot creates the tab with
  Uzbekistan's fixed holidays (01.01, 14.01 эркаклар, 08.03 аёллар, 21.03, 09.05, 01.09, 01.10, 08.12); edit freely.
- **«Туғилган кунлар»**: Исм | Сана («15.04» or «15.04.1990» — the year is ignored), matched to one active employee by
  name in any word order; a name that fits nobody or several is skipped (counted). 29.02 → 28.02 in other years.

## What goes out
- Each employee whose day it is: one polite line, friendly voice (name with ака / опа, no emoji): «Алишер ака, Мустақиллик
  куни муборак бўлсин, сизга ва оилангизга соғлик, тинчлик ва бахт тилаймиз, ЭМЖИЕМ жамоаси»; birthday: «… туғилган
  кунингиз муборак бўлсин, сизга мустаҳкам соғлик, оилавий бахт ва ишларингизда омад тилаймиз, ЭМЖИЕМ жамоаси».
- The Director: «🎂 Бугун … нинг туғилган куни — бот уларни табриклади». The Director himself isn't greeted by the bot.
- Goes on days off too. Logs: counts only (sent, failed, birthdays, unmatched).

Test: `test_greetings_1118`.
