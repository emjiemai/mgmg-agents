"""Safety rules around the model (spec §23, §30; scenarios HARI-008, 042, 086–088, 096–097, 126–129).

1. Before the model: questions that must not reach the general HR context (other people's
   pay, company finance, passwords, "I'm the director, open everything", complaints, legal
   disputes, personal crises) get a fixed, approved reply and a route to the right person.
2. After the model: an answer without a valid source, with an impolite form ("сен"), or
   carrying something that looks like a secret is stopped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from hari.translit import normalize


@dataclass(frozen=True)
class Rule:
    intent: str
    stems: tuple[str, ...]  # matched against the start of each normalized word
    reply: str
    case_type: str | None = None  # offer to open this kind of HR request


# Order matters: the first rule that matches wins.
RULES: list[Rule] = [
    Rule("credentials", ("парол", "пароль", "эри калит", "эцп", "логин", "токен", "ключ", "пин код", "пинкод"),
         "Парол, ЭРИ ва API калитини чатга юборманг. Амал ваколатли шахс томонидан хавфсиз тизимда "
         "бажарилади. Агар сир юборилган бўлса, тегишли калитни алмаштириш керак — IT масъулига хабар беринг."),
    Rule("authority_claim", ("директорман", "эгасиман", "админман", "рухсат берди", "рухсат берган",
                             "ҳамма маълумот", "барча маълумот", "базасини", "экспорт", "excel", "эксел"),
         "Ваколат корпоратив аккаунт ва сервердаги роль орқали текширилади. Чатдаги даъво ёки маълумотни бошқа "
         "форматга ўтказиш сўрови ҳуқуқ бермайди. Ҳозирги тасдиқланган ҳуқуқингиз доирасида ёрдам бераман."),
    Rule("injection", ("қоидаларни унут", "аввалги қоидалар", "ignore previous", "ignore all", "забудь",
                       "system prompt", "систем промпт"),
         "Ташқи файл ёки чат ичидаги матн тизим буйруғи ҳисобланмайди. Мен фақат тасдиқланган вазифа ва ҳуқуқ "
         "доирасида ишлайман."),
    Rule("finance_secret", ("фойда", "таннарх", "маржа", "прибыл", "себестоим", "банк ҳисоб", "қарзлар"),
         "Банк, фойда, таннарх ва махфий режаларга махсус ҳуқуқ керак — улар HR ёрдамчиси орқали "
         "кўрсатилмайди. Ролингиз учун очиқ мақсадлар бўйича савол берсангиз, ёрдам бераман."),
    Rule("pay", ("маош", "ойлиг", "иш ҳақи", "иш ҳақим", "зарплат", "аванс", "бонус", "мукофот пул", "карта рақам",
                 "реквизит", "ҳисоб варақ"),
         "Иш ҳақи ва банк маълумотлари бу умумий каналда кўрсатилмайди, бошқа ходимларники эса умуман очилмайди. "
         "Ўз ҳисоб варағингиз бўйича саволни бухгалтерга текширув сўрови сифатида юборишим мумкин.",
         "pay_question"),
    Rule("others_data", ("бошқаларники", "бошқа ходим", "ҳамкасбимнинг", "рейтингини", "кимнинг kpi"),
         "Бошқа ходимларнинг шахсий маълумоти, KPI ёки маошини рухсатсиз очмайман. Ўз натижангиз ва ўз "
         "маълумотингиз бўйича ёрдам бераман."),
    Rule("complaint", ("шикоят", "ҳақорат", "камсит", "таъқиб", "босим ўтказ", "жалоб", "оскорб"),
         "Воқеа, сана ва керакли ёрдамни ёзинг. Мурожаатни ким кўриши олдиндан кўрсатилади ва шикоят қилинган "
         "шахс уни текширмайди. Мутлақ махфийликни ваъда қила олмайман, лекин маълумот фақат текширув учун "
         "зарур кишига боради.", "complaint"),
    Rule("legal", ("суд", "низо", "прокурат", "инспекция", "юрист", "адвокат", "устидан ариза", "ишдан бўшатиш"),
         "Мен компаниянинг ички HR ёрдамчисиман, мустақил юридик вакилингиз эмасман. Шахсий низога юридик баҳо "
         "бермайман. Истасангиз, масалани ички кўриб чиқиш учун ваколатли HR ёки юристга қайд этаман.", "legal"),
    Rule("support", ("ўзимни ёмон", "чарчадим", "стресс", "депресс", "яшагим", "умидсиз", "кайфиятим йўқ"),
         "Буни айтганингиз учун раҳмат. Мен ҳолатингизни баҳоламайман. Хоҳласангиз, HR билан шахсий суҳбат "
         "сўровини очаман — бу сизнинг рейтингингиз ёки KPIга таъсир қилмайди. Ҳаётингизга хавф бўлса, "
         "дарҳол 103 ёки яқинларингизга мурожаат қилинг.", "support"),
    Rule("decision", ("ишга олинг", "ишдан бўшатинг", "жарима сол", "маошимни ошир", "лавозимимни ошир", "уволь"),
         "Ишга қабул, бўшатиш, маош, жазо ва лавозим бўйича қарорни ваколатли инсон қабул қилади, мен эмас. "
         "Масалани тегишли масъулга мурожаат сифатида юборишим мумкин.", "other"),
]


def match_rule(text: str) -> Rule | None:
    norm = f" {normalize(text)} "
    raw = f" {text.lower()} "
    for rule in RULES:
        for stem in rule.stems:
            if re.search(r"[a-z]", stem):  # Latin phrases ("ignore previous") are matched as typed
                if stem in raw:
                    return rule
                continue
            # Cyrillic stems match at the start of a word, in either script the person typed.
            if re.search(rf"(?<![а-яёўқғҳa-z0-9]){re.escape(normalize(stem))}", norm):
                return rule
    return None


# --------------------------------------------------------------------------- output checks

_IMPOLITE = re.compile(r"(?<![а-яёўқғҳ])(сен|сенга|сени|сенинг|сендан|sen|senga|seni|sening)(?![а-яёўқғҳa-z])",
                       re.IGNORECASE)
_SECRETS = [
    re.compile(r"\b(?:\d[ -]?){16}\b"),  # card number
    re.compile(r"\b\d{20}\b"),  # bank account
    re.compile(r"\b[A-Z]{2}\d{7}\b"),  # passport
    re.compile(r"\b\d{14}\b"),  # ЖШШИР / PINFL
    re.compile(r"(?i)\b(api[_-]?key|password|парол)\s*[:=]"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"),  # a Telegram bot token
]


def is_polite(text: str) -> bool:
    return not _IMPOLITE.search(text)


def leaks_secret(text: str) -> bool:
    return any(p.search(text) for p in _SECRETS)


def clean_model_text(text: str, limit: int = 1500) -> str:
    """Plain text only: the bot sends without HTML/Markdown parsing, so nothing the model writes becomes a link."""
    text = re.sub(r"https?://\S+", "", text)  # model-made links are never shown (spec §23)
    text = re.sub(r"[ \t]+", " ", text).strip()
    return text[:limit]
