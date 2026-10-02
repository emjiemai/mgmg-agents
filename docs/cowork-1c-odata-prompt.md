# Prompt for Claude Cowork — read-only OData access to our 1C on Clobus

Copy everything below the line into Cowork.

---

## Goal

In our company's 1C database, hosted on **Clobus.uz** (built on 1C:Fresh), set up the
**standard OData interface** with a **separate service user**, so that our Telegram bot
(the "MGMG Command Center") can **read** data from 1C. The bot must never be able to
change anything. At the end, give me the four results listed in "What I need back".

I am already logged in to 1C in the browser with an administrator account (if not,
**stop and ask me to log in** — never type a password yourself).

## Background (so you don't have to guess)

- 1C:Fresh documents this setting here: https://1cfresh.com/articles/data_odata
- The menu is one of these, depending on the configuration:
  - **Администрирование → Синхронизация данных → Настройки стандартного интерфейса OData**
  - **Администрирование и НСИ → Синхронизация данных → Настройки стандартного интерфейса OData**
  - **Настройки → Синхронизация данных → Настройки стандартного интерфейса OData**
- Only a user with **«Полные права»** can open it.
- Tab **«Авторизация»**: create the service user. 1C gives it the role
  **«УдаленныйДоступOData»** automatically; it doesn't appear in the normal user list.
- Tab **«Состав»**: tick which objects are readable over OData.
- The address the bot will use = the database's browser address + `odata/standard.odata/`,
  e.g. `https://<clobus host>/a/<app>/<number>/odata/standard.odata/`.

## Steps

1. **Find out which 1C this is** — open «Справка → О программе» (or the "?" / "i" menu) and
   note the configuration name and version (e.g. «Бухгалтерия для Узбекистана», «Бухгалтерия
   предприятия 3.0», «Управление компанией 3.0», «Розница 3.0»). Note the database's name too.
2. **Note the database address** from the address bar, cut right after the number part
   (`…/a/<app>/<number>/`). Do NOT copy anything after `#` or any token in the URL.
3. **Open the OData settings** using the menu paths above. Use the 1C search (magnifier /
   «Поиск по меню») with the words «OData» or «стандартного интерфейса» if the menu differs.
4. On **«Авторизация»**: create the service user.
   - Login: `mgmg_bot_odata`
   - Password: **ask me to type it myself** (or let 1C generate one and tell me to copy it
     straight into my password manager). Never write the password in your reply, notes,
     screenshots you describe, or any file.
5. On **«Состав»**: tick **only** these (names differ slightly per configuration — pick the
   closest match, and list exactly what you ticked):
   - **Cash and bank:** registers for money balances/movements — e.g.
     `РегистрНакопления.ДенежныеСредства` / «Денежные средства», «Движения денежных средств»;
     in «Бухгалтерия» it is the accounting register `РегистрБухгалтерии.Хозрасчетный`
     (balances of accounts 50 and 51) and the chart of accounts `ПланСчетов.Хозрасчетный`.
   - **Bank and cash accounts:** catalogs «Банковские счета», «Кассы» (and «Валюты»).
   - **Counterparties and debts:** catalog «Контрагенты», «Договоры контрагентов», and
     settlement registers («Расчеты с покупателями», «Расчеты с поставщиками» or similar).
   - **Payments:** documents «Поступление на расчетный счет», «Списание с расчетного счета»,
     «Приходный кассовый ордер», «Расходный кассовый ордер» (or their equivalents).
   - **Organisations:** catalog «Организации».
   If 1C says a ticked object needs another object (dependency), tick that too and tell me.
   Do **not** tick: salaries/«Зарплата», personal data of employees («Физические лица»,
   «Сотрудники»), users, settings, or anything about access rights.
6. **Before pressing «Записать»/«Сохранить»/«ОК», stop** and show me: the configuration name,
   the login you will create, and the full list of ticked objects. Wait for my "yes".
7. After I confirm, save. Then check that the OData interface reports as enabled
   (the form usually shows the published state / no errors).

## Cases to handle — stop and tell me, don't improvise

- **The menu item is missing / greyed out:** don't change roles or configuration. Tell me
  exactly which menus you saw. The fix is Clobus support (+998 78 120-00-88, @Clobus_uz,
  info@clobus.uz): «стандартный интерфейс OData ва служебный пользователь учун рухсат керак».
- **"Недостаточно прав" / no admin rights:** stop; I'll log in with the right account.
- **A service user already exists:** don't delete or change it. Tell me its login and what
  is ticked, and ask whether to reuse it or create `mgmg_bot_odata` beside it.
- **Several organisations or several databases** on the same Clobus account: list them and
  ask which one (the bot needs the main working database, not a test copy).
- **Any dialog asking to update the configuration, restructure the database, change the
  tariff, or pay:** cancel it and tell me.
- **The site asks for a login, SMS code or CAPTCHA:** stop and let me do it.
- **You can't find an object from step 5:** don't tick something similar-sounding just to
  have it; tell me what you looked for and the closest names you saw.

## What not to do, ever

- Don't create, edit or post any document, payment or catalog item.
- Don't change existing users, roles, passwords or settings outside the OData form.
- Don't type or reveal any password; don't store it anywhere.
- Don't open or test the OData address with the password yourself.

## What I need back (no secrets)

1. Configuration name and version (step 1) and the database name.
2. The bot's address: `<database address>/odata/standard.odata/`.
3. The service user's login (not the password).
4. The exact list of objects ticked on «Состав», plus anything you couldn't find.

I'll put the address, login and password into our server myself.
