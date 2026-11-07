"""Everything the bot says. Written in Uzbek Cyrillic; shown in Latin to people who chose Latin."""

from __future__ import annotations

from hari.db.models import Employee
from hari.translit import cyrillic_to_latin


def say(emp: Employee | None, text: str) -> str:
    return cyrillic_to_latin(text) if emp is not None and emp.language == "uz_latn" else text


def both(label: str) -> set[str]:
    """A menu label in both scripts, so a button matches whichever the person sees."""
    return {label, cyrillic_to_latin(label)}


# Menu
BTN_PLAN = "📋 Режам"
BTN_CASE = "📝 Мурожаат"
BTN_MY_CASES = "📂 Мурожаатларим"
BTN_PROFILE = "👤 Профилим"
BTN_HELP = "❔ Ёрдам"
BTN_QUEUE = "📥 Иш навбати"
BTN_NEW_EMP = "➕ Янги ходим"
BTN_STAFF = "👥 Ходимлар"

UNKNOWN_USER = (
    "Ассалому алайкум. Мен HARI — EMJIEM ходимлари учун HR ёрдамчиси.\n\n"
    "Сизнинг Telegram аккаунтингиз ҳали ходим профилига боғланмаган. HR масъулидан шахсий ҳавола "
    "олинг ва уни шу Telegram аккаунтдан очинг. Исм ёки телефонни ёзишнинг ўзи етарли эмас.\n\n"
    "Сизнинг Telegram ID: {tg_id}"
)
INVITE_INVALID = (
    "Бу ҳавола ишламайди: муддати ўтган, аллақачон ишлатилган ёки нотўғри. HR масъулидан янги ҳавола сўранг."
)
INVITE_TAKEN = (
    "Бу Telegram аккаунт бошқа ходим профилига боғланган. Ҳар бир ходимнинг ўз аккаунти бўлиши керак. "
    "HR масъулига мурожаат қилинг."
)


def welcome(emp: Employee, manager: str | None, mentor: str | None) -> str:
    lines = [
        f"Хуш келибсиз, {emp.full_name}! Мен HARI — EMJIEMнинг ички HR AI ёрдамчисиман.",
        "",
        "Вазифа, ўқиш, SOP, жадвал ва HR мурожаатларингиз бўйича ёрдам бераман. "
        "Кадр ва тўлов қарорларини ваколатли инсон қабул қилади.",
        "",
        f"Лавозимингиз: {emp.position} ({emp.department})",
    ]
    if manager:
        lines.append(f"Раҳбарингиз: {manager}")
    if mentor:
        lines.append(f"Мураббийингиз: {mentor}")
    lines += ["", "Саволингизни оддий матн билан ёзинг ёки пастдаги менюдан танланг."]
    return "\n".join(lines)


HELP = (
    "HARI нима қила олади:\n"
    "• Саволингизга тасдиқланган ҳужжатдан манбаси билан жавоб беради — шунчаки ёзинг.\n"
    "• 📋 Режам — мослашув режангиз ва бугунги қадамлар.\n"
    "• 📝 Мурожаат — таътил, табель хатоси, маълумотнома, шикоят ва бошқалар.\n"
    "• 📂 Мурожаатларим — мурожаатларингиз ҳолати.\n"
    "• 👤 Профилим — маълумотингиз ва хатони тузатиш сўрови.\n\n"
    "Мен AIман: манба топилмаса, тахмин қилмайман ва саволни инсонга юборишни таклиф қиламан. "
    "Ишга қабул, маош, жазо ва бўшатишни ваколатли инсон ҳал қилади."
)

STEP_ICONS = {"accepted": "✅", "submitted": "⏳", "returned": "↩️", "pending": "▫️"}
STEP_ICON_FUTURE = "🔒"
KIND_LABELS = {"read": "танишиш", "task": "вазифа", "meeting": "учрашув"}
ACCEPTOR_LABELS = {"self": "ўзингиз", "mentor": "мураббий", "manager": "раҳбар", "hr": "HR"}

ASK_PROOF = (
    "«{title}» — натижани юборинг: қисқа матн, фото ёки файл. "
    "Буни {acceptor} кўриб, қабул қилади. Бекор қилиш: /bekor"
)
PROOF_SENT = "Раҳмат. Натижа {acceptor}га юборилди. Қабул қилингач, хабар бераман."
PROOF_NO_ACCEPTOR = ("Раҳмат, натижа сақланди. Ҳозир уни қабул қиладиган фаол масъул топилмади — "
                     "HR масъулига хабар берилди.")
STEP_ACCEPTED_TO_EMP = "✅ «{title}» қадами қабул қилинди. Раҳмат!"
STEP_RETURNED_TO_EMP = "↩️ «{title}» қадами қайтарилди.\nСабаб: {reason}\n\nТузатиб, қайта юборинг (📋 Режам)."
PLAN_DONE = "🎉 Мослашув режангиздаги барча қадамлар қабул қилинди."

CASE_PICK = "Мурожаат турини танланг:"
CASE_ASK_BODY = ("«{label}» — масалани қисқа ёзинг: нима, қачон ва қандай ёрдам керак. "
                 "Соғлиқ ёки оилангизга оид ортиқча тафсилот ёзиш шарт эмас. Бекор қилиш: /bekor")
CASE_COMPLAINT_NOTE = ("Мурожаатни ким кўриши олдиндан кўрсатилади; шикоят қилинган шахс уни текширмайди. "
                       "Мутлақ махфийликни ваъда қила олмайман, лекин маълумот фақат текширув учун зарур кишига боради.")
CASE_OPENED = ("📨 Мурожаат қабул қилинди: {number}\nТур: {label}\nКўриб чиқувчи: {who}\n"
               "Биринчи жавоб муддати: {due}\n\nЖавоб келганда шу ерга хабар бераман.")
CASE_UNASSIGNED = "ҳозирча тайинланмаган — HR навбатида"
CANCELLED = "Бекор қилинди."
NOT_ALLOWED = "Бу амал учун ҳуқуқингиз йўқ."
