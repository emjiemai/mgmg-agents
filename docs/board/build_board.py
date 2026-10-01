"""Build the status-and-plan board as one SVG (drag into Figma -> editable)."""
from html import escape
from pathlib import Path

OUT = Path(__file__).with_name("emjiem-ai-hisobot.svg")
W = 1680
FONT = "Inter, Arial, sans-serif"

DONE, PROG, TODO = "done", "prog", "todo"
STATUS = {
    DONE: ("Бажарилди", "#E7F6EC", "#1E7B3A"),
    PROG: ("Жараёнда", "#FFF3D6", "#9A6400"),
    TODO: ("Бошланмаган", "#EEEFF2", "#5B6070"),
}

AGENTS = [
    ("H0", "Марказ: API, база, иккала Telegram бот, SAP маълумот қабул қилгич", DONE, "Доимий ишлайди"),
    ("A1", "Кунлик ҳисоботлар: 16:00 сўраш, 17:00 эслатма, бир аниқлаштирувчи савол", DONE, "Ҳар куни; шанба/якшанба — дам олиш куни ишловчилар"),
    ("A2", "Эрталабки брифинг: 5 рақам + ҳисобот юбормаганлар", DONE, "08:00, Директорга"),
    ("A3", "Топшириқлар назорати: муддат, эслатма, кечикканлар, жума кунги натижа", DONE, "Ҳар топшириқда, 08:00, жума 17:00"),
    ("A4", "Давомат (Verifix): ким кечикди, ким келмади", PROG, "Код тайёр. Verifix'дан client_id ва client_secret кутилмоқда"),
    ("B1", "Ёзма рухсатлар (EMJ-SOP-ADM-01) + сумма бўйича тасдиқлаш", DONE, "Сумма лимитларини Директор белгилаши керак"),
    ("B2", "30 кунлик пул календари: кирим ва чиқим", DONE, "Душанба 08:00 + истаган пайт сўраш"),
    ("B3", "Банк ↔ SAP ↔ 1C ↔ Didox солиштириш", TODO, "Банк, 1C ва Didox'га кириш керак"),
    ("B4", "Маълумот сифати текшируви", DONE, "Душанба 08:00, IT'га"),
    ("C1", "Лид йиғувчи: Telegram, WhatsApp, Instagram", PROG, "Garmin AI бот ишлаяпти; WhatsApp ва Instagram кириши керак"),
    ("C2", "Сотув прогнози ва режалар", TODO, "SAP'дан тўлиқ сотув тарихи керак"),
    ("C3", "Ётиб қолган товарларни сотиш", TODO, "SAP'дан тўлиқ қолдиқ ва кирим саналари керак"),
    ("C4", "Сервис ва шартнома эслатмалари", TODO, "Мижозлардаги ускуналар рўйхати керак"),
    ("C5", "Мижозларни қайтариш", TODO, "SAP'дан тўлиқ харид тарихи керак"),
    ("D1", "Захира ва буюртма сигнали", TODO, "SAP'дан тўлиқ қолдиқ ва сотув керак"),
    ("D2", "Ҳужжатлар ва сертификатлар", TODO, "Техник тўсиқ йўқ — режада кейинроқ"),
    ("E1", "Ходимлар KPI: Директорнинг 15 мезони, 6 қисм", DONE, "/maqsad, /kpi, /baho; ҳар ойнинг 1-куни баҳолаш"),
    ("E2", "AI операцион менежер", TODO, "Режа бўйича энг охирида; OPS Manager Bot қисман бажаради"),
    ("F1", "Маркетинг ва контент режа", TODO, "Instagram кириши керак"),
    ("F2", "Лид агенти: тендер ва лид қидириш + B2B сотувчиларга тақсимлаш", DONE, "08:00 ҳар куни, 15:00 ҳолат сўраш"),
    ("G1", "Раҳбарнинг шахсий ёрдамчиси (почта)", TODO, "Outlook кириши керак"),
]

EXTRA = [
    "Дебиторлик огоҳлантириши — ҳар куни 08:00",
    "Директор саволларига жавоб: ҳисобот, KPI, қарз, пул, SAP",
    "Топшириқ бўлимга ёки бир кишига; ходим хабари тасдиқ билан",
    "Ходимлар исми ва роли; янги «Молия» бўлими",
    "Шанба/якшанба ишловчилар — админ ботда белгиланади",
    "Жамоа кайфияти: 10:00, 14:00, 17:35 қисқа хабарлар",
    "Байрам куни /dam, ҳаммага эълон /elon",
    "Мижоз шикоятлари: QR код (Londry, Garmin), 3 тилда",
    "Маълумотлар базасини кўриш (/db), фақат ўқиш",
    "Garmin AI бот: Instagram → каталог → Telegram бот → менежер",
]

PLAN = [
    ("1", "Verifix: client_id ва client_secret олиш", "Давомат (A4) ишга тушади. Verifix қўллаб-қувватлаш хизматига ёзилди"),
    ("2", "SAP шлюз чегарасини ошириш (ҳозир 100 қатор)", "Тўлиқ захира ва сотув; C2, C3, D1 очилади"),
    ("3", "Директордан: тўлов лимитлари ва октябр мақсадлари", "B1 лимитлари; /maqsad орқали ҳар ходимга рақамли мақсад"),
    ("4", "Garmin AI ботни мустаҳкамлаш", "Лидларни базага сақлаш, Command Center ва KPI билан улаш, пуллик тариф"),
    ("5", "Ишлаётган агентлар самарасини бир ой ўлчаш", "Режа талаби: ҳар агентнинг И кўрсаткичи"),
    ("6", "Керакли киришлар", "Instagram/WhatsApp (C1, F1), Outlook (G1), банк/1C/Didox (B3), ускуналар рўйхати (C4)"),
]

parts: list[str] = []


def text(x, y, s, size=16, weight=400, fill="#1B1D24", anchor="start"):
    parts.append(
        f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" font-weight="{weight}" '
        f'fill="{fill}" text-anchor="{anchor}">{escape(s)}</text>'
    )


def rect(x, y, w, h, fill, r=0, stroke=None):
    s = f' stroke="{stroke}" stroke-width="1"' if stroke else ""
    parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}"{s}/>')


def wrap(s, limit):
    words, lines, cur = s.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > limit and cur:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    return lines + [cur]


M = 64
y = 64
# ---- header
rect(0, 0, W, 10, "#D71920")
text(M, y + 34, "ЭМЖИЕМ AI агентлар тизими — ҳисобот ва режа", 40, 700)
text(M, y + 72, "01.10.2026 · Тайёрлади: Улуғбек Исоқов, IT · Режа: «ЭМЖИЕМ AI Агентлар Тизими» (21 агент)", 18, 400, "#5B6070")
y += 112

# ---- counters
counts = {k: sum(1 for a in AGENTS if a[2] == k) for k in STATUS}
cards = [("21", "Режадаги агентлар", "#1B1D24", "#FFFFFF")] + [
    (str(counts[k]), STATUS[k][0], STATUS[k][2], STATUS[k][1]) for k in (DONE, PROG, TODO)
] + [(str(len(EXTRA)), "Режадан ташқари қўшимча", "#1F4E9E", "#E6EEFB")]
cw = (W - 2 * M - 4 * 20) / 5
for i, (num, label, fg, bg) in enumerate(cards):
    x = M + i * (cw + 20)
    rect(x, y, cw, 116, bg, 16, "#E2E4EA")
    text(x + 24, y + 62, num, 48, 700, fg)
    text(x + 24, y + 96, label, 17, 500, "#3A3E4A")
y += 116 + 48

# ---- agents table
text(M, y, "Агентлар жадвали", 26, 700)
y += 22
cols = [(M, 72, "Код"), (M + 72, 610, "Агент"), (M + 682, 170, "Ҳолат"), (M + 852, W - 2 * M - 852, "Изоҳ")]
rect(M, y, W - 2 * M, 44, "#1B1D24", 10)
for x, _w, h in cols:
    text(x + 16, y + 28, h, 15, 600, "#FFFFFF")
y += 44
for i, (code, name, st, note) in enumerate(AGENTS):
    name_l, note_l = wrap(name, 62), wrap(note, 74)
    h = 22 * max(len(name_l), len(note_l)) + 26
    rect(M, y, W - 2 * M, h, "#FFFFFF" if i % 2 else "#F7F8FA")
    text(cols[0][0] + 16, y + 30, code, 16, 700)
    for j, line in enumerate(name_l):
        text(cols[1][0] + 16, y + 30 + 22 * j, line, 16, 500)
    label, bg, fg = STATUS[st]
    rect(cols[2][0] + 12, y + 12, 140, 28, bg, 14)
    text(cols[2][0] + 82, y + 31, label, 14, 600, fg, "middle")
    for j, line in enumerate(note_l):
        text(cols[3][0] + 16, y + 30 + 22 * j, line, 15, 400, "#4A4F5C")
    y += h
rect(M, y, W - 2 * M, 1, "#E2E4EA")
y += 56

# ---- two columns: extras + plan
colw = (W - 2 * M - 40) / 2
top = y
text(M, y, "Режадан ташқари бажарилганлар", 26, 700)
yy = y + 24
rect(M, yy, colw, 40 + 44 * len(EXTRA), "#FFFFFF", 16, "#E2E4EA")
yy += 44
for item in EXTRA:
    parts.append(f'<circle cx="{M + 30}" cy="{yy - 6}" r="5" fill="#1E7B3A"/>')
    text(M + 48, yy, item, 16, 400)
    yy += 44
left_bottom = yy

x2 = M + colw + 40
text(x2, top, "Кейинги режа", 26, 700)
yy = top + 24
for num, head, sub in PLAN:
    sub_l = wrap(sub, 66)
    h = 58 + 22 * len(sub_l)
    rect(x2, yy, colw, h, "#FFFFFF", 16, "#E2E4EA")
    parts.append(f'<circle cx="{x2 + 34}" cy="{yy + 34}" r="18" fill="#D71920"/>')
    text(x2 + 34, yy + 40, num, 16, 700, "#FFFFFF", "middle")
    text(x2 + 66, yy + 32, head, 17, 600)
    for j, line in enumerate(sub_l):
        text(x2 + 66, yy + 56 + 22 * j, line, 15, 400, "#4A4F5C")
    yy += h + 12
y = max(left_bottom, yy) + 40

# ---- Garmin AI bot box
text(M, y, "Garmin AI бот (alohida loyiha: emjiemai/Garmin-AI-bot)".replace("alohida loyiha", "алоҳида лойиҳа"), 26, 700)
y += 24
steps = ["Instagram Reels", "Веб-каталог (квиз, солиштириш)", "AI Telegram бот (RU/UZ)", "Менежер: 🔥 иссиқ / 🟡 илиқ лид"]
bw = (W - 2 * M - 3 * 48) / 4
for i, s in enumerate(steps):
    bx = M + i * (bw + 48)
    rect(bx, y, bw, 76, "#E6EEFB" if i < 3 else "#E7F6EC", 14, "#C9D6EE")
    for j, line in enumerate(wrap(s, 26)):
        text(bx + bw / 2, y + 34 + 22 * j, line, 16, 600, "#1B1D24", "middle")
    if i < 3:
        ax = bx + bw + 8
        parts.append(f'<path d="M{ax} {y + 38} H{ax + 30} M{ax + 22} {y + 30} L{ax + 30} {y + 38} L{ax + 22} {y + 46}" '
                     'stroke="#5B6070" stroke-width="2.5" fill="none" stroke-linecap="round"/>')
y += 100
for line in (
    "Ҳолат: ишлаяпти. Мижоз Reel'дан каталогга ўтади, ботга кўрган соати билан келади; харидга тайёр бўлса менежерга телефон ва тафсилот билан хабар.",
    "Кейинги қадам: лидлар ҳозир вақтинча файлда (Render'нинг бепул тарифида қайта ишга тушганда ўчади) — базага ўтказиш ва Command Center билан улаш керак.",
):
    for l in wrap(line, 150):
        text(M, y, l, 16, 400, "#3A3E4A")
        y += 26
y += 40
text(M, y, "Ранглар: яшил — бажарилди · сариқ — жараёнда · кулранг — бошланмаган", 15, 400, "#8A8F9C")
H = y + 48

svg = (
    f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">'
    f'<rect width="{W}" height="{H}" fill="#F3F4F7"/>' + "".join(parts) + "</svg>"
)
OUT.write_text(svg, encoding="utf-8")
OUT.with_suffix(".html").write_text(
    f'<!doctype html><meta charset="utf-8"><body style="margin:0;background:#ddd">{svg}</body>', encoding="utf-8"
)
print(OUT, W, H, counts)
