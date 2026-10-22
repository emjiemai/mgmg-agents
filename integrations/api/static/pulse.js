/* PULSE — the page's behaviour (2026-10-22). No framework; the chart is TradingView Lightweight Charts 4.2.0. */
(function () {
  "use strict";

  const state = { days: 30, sector: "all", overview: null, tapeSeen: new Set(), person: null, personDay: null };
  const $ = (id) => document.getElementById(id);
  const NS = "http://www.w3.org/2000/svg";
  const TAPE_EVERY = 60 * 1000;
  const OVERVIEW_EVERY = 5 * 60 * 1000;

  // ---- small helpers ----------------------------------------------------------------------------------------
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }
  function svgEl(tag, attrs) {
    const node = document.createElementNS(NS, tag);
    Object.entries(attrs || {}).forEach(([k, v]) => node.setAttribute(k, v));
    return node;
  }
  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

  const fmtInt = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 });
  const fmt1 = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 });
  function som(v) {
    if (v === null || v === undefined) return ["—", ""];
    const a = Math.abs(v);
    if (a >= 1e9) return [fmt1.format(v / 1e9), "млрд"];
    if (a >= 1e6) return [fmt1.format(v / 1e6), "млн"];
    if (a >= 1e3) return [fmtInt.format(v / 1e3), "минг"];
    return [fmtInt.format(v), "сўм"];
  }
  function pct(v) { return v === null || v === undefined ? ["—", ""] : [fmt1.format(v), "%"]; }

  // ▲ / ▼ as drawn marks, coloured by whether the change is good (inverted for complaints)
  function delta(node, value, opts) {
    node.textContent = "";
    node.className = "delta";
    if (value === null || value === undefined) return;
    const good = opts && opts.invert ? value < 0 : value > 0;
    if (value !== 0) node.classList.add(good ? "up" : "down");
    if (value !== 0) {
      const s = svgEl("svg", { viewBox: "0 0 10 10", "aria-hidden": "true" });
      s.appendChild(svgEl("path", { d: value > 0 ? "M5 1.5 9.2 8.5H.8z" : "M5 8.5 .8 1.5h8.4z" }));
      node.appendChild(s);
    }
    node.appendChild(document.createTextNode(fmt1.format(Math.abs(value)) + (opts && opts.pct ? "%" : "")));
    node.setAttribute("aria-label", (value > 0 ? "+" : value < 0 ? "−" : "") + fmt1.format(Math.abs(value)));
  }

  // a sparkline: the line through the days that have a value (a day without one is stepped over), a faint
  // wash under it, the last point marked
  function spark(values, w, h) {
    const s = svgEl("svg", { class: "spark", viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: "none", "aria-hidden": "true" });
    const pts = values.map((v, i) => [i, v]).filter(([, v]) => v !== null && v !== undefined);
    if (pts.length < 2) return s;
    const nums = pts.map(([, v]) => v);
    const lo = Math.min(...nums), hi = Math.max(...nums), span = hi - lo || 1, step = w / Math.max(1, values.length - 1);
    const y = (v) => h - 3 - ((v - lo) / span) * (h - 6);
    let line = "", lastX = 0, lastY = 0;
    pts.forEach(([i, v], k) => {
      const X = (i * step).toFixed(1), Y = y(v).toFixed(1);
      line += `${k ? "L" : "M"}${X},${Y}`;
      lastX = X; lastY = Y;
    });
    const firstX = (pts[0][0] * step).toFixed(1);
    const area = `${line}L${lastX},${h}L${firstX},${h}Z`;
    s.appendChild(svgEl("path", { class: "a", d: area }));
    s.appendChild(svgEl("path", { class: "l", d: line, "vector-effect": "non-scaling-stroke" }));
    s.appendChild(svgEl("circle", { cx: lastX, cy: lastY, r: 2 }));
    return s;
  }

  // drawn icons for the tape and the day (one stroke family)
  const ICONS = {
    in: "M10 4h7a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-7M4 12h10M10 8l4 4-4 4",
    late: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2",
    out: "M14 4H7a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1h7M20 12H10M16 8l4 4-4 4",
    absent: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM9 9l6 6M15 9l-6 6",
    task: "M5 4h14v16H5zM9 12l2 2 4-4",
    report: "M7 3h7l4 4v14H7zM14 3v4h4M10 12h5M10 16h5",
    missed: "M7 3h7l4 4v14H7zM14 3v4h4M5 21 19 5",
    explain: "M4 5h16v11H9l-5 4zM8 9h8M8 12h5",
    complaint: "M12 3 22 20H2zM12 10v4M12 17v.01",
    resolved: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM8 12l3 3 5-6",
  };
  function icon(kind) {
    const s = svgEl("svg", { class: `ic k-${kind}`, viewBox: "0 0 24 24", "aria-hidden": "true" });
    s.appendChild(svgEl("path", { d: ICONS[kind] || ICONS.task }));
    return s;
  }

  // ---- data -------------------------------------------------------------------------------------------------
  async function api(path) {
    const res = await fetch(path, { credentials: "same-origin", headers: { Accept: "application/json" } });
    if (res.status === 401) { location.replace("/pulse"); throw new Error("expired"); }
    if (!res.ok) throw new Error(String(res.status));
    return res.json();
  }
  function notice(text, retry) {
    const box = $("notice");
    if (!text) { box.hidden = true; return; }
    $("notice-text").textContent = text;
    $("notice-retry").onclick = () => { box.hidden = true; retry(); };
    box.hidden = false;
  }

  // ---- tickers ----------------------------------------------------------------------------------------------
  function renderTickers(tickers) {
    const box = $("tickers");
    box.textContent = "";
    tickers.forEach((t) => {
      const item = el("article", "ticker");
      item.appendChild(el("span", "label", t.label));
      const value = el("span", "value");
      if (t.value === null || t.value === undefined) {
        item.classList.add("off");
        value.textContent = "—";
      } else {
        const [n, unit] = t.unit === "som" ? som(t.value) : t.unit === "pct" ? pct(t.value) : [fmtInt.format(t.value), ""];
        value.textContent = n;
        if (unit) value.appendChild(el("small", null, unit));
      }
      item.appendChild(value);
      const d = el("span");
      delta(d, t.delta, { invert: t.invert, pct: t.unit === "som" });
      d.classList.add("delta");
      item.appendChild(d);
      item.appendChild(spark(t.spark || [], 120, 26));
      box.appendChild(item);
    });
  }
  function skeleton() {
    const box = $("tickers");
    box.textContent = "";
    for (let i = 0; i < 6; i++) {
      const item = el("article", "ticker");
      item.appendChild(el("span", "skel"));
      item.appendChild(el("span", "skel big"));
      box.appendChild(item);
    }
  }

  // ---- the index chart --------------------------------------------------------------------------------------
  let chart = null, area = null, bars = null;
  function chartOptions() {
    return {
      layout: { background: { type: "solid", color: css("--ground") }, textColor: css("--ink-3"), fontFamily: "Noto Sans, system-ui, sans-serif", fontSize: 12 },
      grid: { vertLines: { visible: false }, horzLines: { color: css("--rule") } },
      rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.08, bottom: 0.34 } },
      timeScale: { borderVisible: false, fixLeftEdge: true, fixRightEdge: true, lockVisibleTimeRangeOnResize: true },
      crosshair: { mode: 1, vertLine: { color: css("--ink-3"), labelBackgroundColor: css("--ink") }, horzLine: { color: css("--ink-3"), labelBackgroundColor: css("--ink") } },
      handleScroll: false, handleScale: false,
      localization: { locale: "ru-RU", dateFormat: "dd.MM.yyyy" },
    };
  }
  function ensureChart() {
    if (chart || !window.LightweightCharts) return;
    chart = LightweightCharts.createChart($("chart"), { ...chartOptions(), autoSize: true });
    bars = chart.addHistogramSeries({ priceScaleId: "vol", priceFormat: { type: "custom", formatter: (v) => som(v).join(" ") }, lastValueVisible: false, priceLineVisible: false });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.8, bottom: 0 }, visible: false });
    area = chart.addAreaSeries({ lineWidth: 2, priceLineVisible: false, lastValueVisible: true, priceFormat: { type: "price", precision: 0, minMove: 1 } });
    chart.subscribeCrosshairMove((param) => legend(param.time ? param : null));
    paintChart();
  }
  // the legend follows the crosshair: that day's index and sales; at rest, the last day's
  function legend(param) {
    const o = state.overview;
    if (!o) return;
    const at = param && param.time ? o.index.series.findIndex((p) => p.d === param.time) : o.index.series.length - 1;
    if (at < 0) return;
    const p = o.index.series[at], v = o.index.volume[at];
    const [y, m, d] = p.d.split("-");
    $("hover-day").textContent = `${d}.${m}`;
    $("hover-index").textContent = p.v === null ? "—" : fmtInt.format(p.v);
    $("hover-sales").textContent = v && v.v !== null && v.v !== undefined ? som(v.v).join(" ") : "—";
  }
  function paintChart() {
    if (!chart) return;
    chart.applyOptions(chartOptions());
    area.applyOptions({ lineColor: css("--line"), topColor: css("--area"), bottomColor: css("--area"), crosshairMarkerBackgroundColor: css("--line") });
    if (state.overview) setChartData(state.overview);
  }
  function setChartData(o) {
    if (!chart) return;
    area.setData(o.index.series.map((p) => (p.v === null ? { time: p.d } : { time: p.d, value: p.v })));
    const last = o.index.volume.length - 1;
    bars.setData(o.index.volume.map((p, i) => (p.v === null || p.v === undefined ? { time: p.d } : { time: p.d, value: p.v, color: i === last ? css("--ink-3") : css("--bars") })));
    chart.timeScale().fitContent();
    legend(null);
  }
  function renderIndex(o) {
    $("index-value").textContent = o.index.value === null ? "—" : fmtInt.format(o.index.value);
    delta($("index-delta"), o.index.delta);
    ensureChart();
    setChartData(o);
  }

  // ---- the tape ---------------------------------------------------------------------------------------------
  function renderTape(items) {
    const list = $("tape");
    const first = state.tapeSeen.size === 0;
    list.textContent = "";
    if (!items.length) { list.appendChild(el("li", "empty", "Ҳали ҳаракат йўқ")); return; }
    items.forEach((it) => {
      const key = `${it.t}|${it.pid}|${it.label}|${it.detail}`;
      const li = el("li");
      if (!first && !state.tapeSeen.has(key)) li.classList.add("fresh");
      state.tapeSeen.add(key);
      li.appendChild(el("span", "t", it.t));
      li.appendChild(icon(it.kind));
      const what = el("span", "what");
      if (it.pid) {
        const who = el("button", "who", it.who);
        who.type = "button";
        who.addEventListener("click", () => openPerson(it.pid));
        what.appendChild(who);
        what.appendChild(document.createTextNode(" "));
      }
      what.appendChild(el("span", "act", it.label));
      if (it.detail) { what.appendChild(document.createTextNode(" ")); what.appendChild(el("span", "det", it.detail)); }
      li.appendChild(what);
      list.appendChild(li);
    });
  }

  // ---- the board --------------------------------------------------------------------------------------------
  function renderSectors(o) {
    const box = $("sectors");
    box.textContent = "";
    if (o.sectors.length < 2) return;
    const options = [{ key: "all", label: "Ҳамма", score: o.index.value }, ...o.sectors];
    options.forEach((s) => {
      const b = el("button", null, s.label);
      b.type = "button";
      b.setAttribute("aria-pressed", String(state.sector === s.key));
      if (s.score !== null && s.score !== undefined) b.appendChild(el("span", "s", fmtInt.format(s.score)));
      b.addEventListener("click", () => { state.sector = s.key; renderSectors(o); renderBoard(o); });
      box.appendChild(b);
    });
  }
  function renderBoard(o) {
    const body = $("board");
    body.textContent = "";
    const ids = state.sector === "all" ? null : new Set((o.sectors.find((s) => s.key === state.sector) || { people: [] }).people);
    const people = o.people.filter((p) => !ids || ids.has(p.id));
    if (!people.length) {
      const tr = el("tr"), td = el("td", "empty", "Ходим йўқ");
      td.colSpan = 5; tr.appendChild(td); body.appendChild(tr);
      return;
    }
    people.forEach((p, i) => {
      const tr = el("tr");
      tr.appendChild(el("td", "n", String(i + 1)));
      const who = el("td"), cell = el("div", "person-cell");
      const state_ = p.today === "on_time" || p.today === "present" ? "in" : p.today === "late" ? "late" : p.today === "absent" ? "absent" : "";
      const dot = el("span", `dot ${state_}`);
      const said = { in: "Келди", late: "Кечикди", absent: "Келмади" }[state_];
      if (said) { dot.title = said; cell.appendChild(el("span", "sr", said)); }
      dot.setAttribute("aria-hidden", "true");
      cell.appendChild(dot);
      const btn = el("button");
      btn.type = "button";
      btn.appendChild(el("span", "name", p.name));
      btn.appendChild(el("span", "role", p.role));
      btn.addEventListener("click", (e) => { e.stopPropagation(); openPerson(p.id); });
      cell.appendChild(btn);
      who.appendChild(cell);
      tr.appendChild(who);
      const sp = el("td", "spark-cell");
      sp.appendChild(spark(p.spark, 120, 26));
      tr.appendChild(sp);
      tr.appendChild(el("td", `num score${p.score === null ? " none" : ""}`, p.score === null ? "—" : fmtInt.format(p.score)));
      const dc = el("td", "num"), d = el("span");
      delta(d, p.delta);
      dc.appendChild(d);
      tr.appendChild(dc);
      tr.addEventListener("click", () => openPerson(p.id));
      body.appendChild(tr);
    });
  }

  // ---- one person -------------------------------------------------------------------------------------------
  let pChart = null, pArea = null, opener = null;
  async function openPerson(id, day) {
    const panel = $("person");
    if (panel.hidden) opener = document.activeElement;
    state.person = id;
    try {
      const p = await api(`/pulse/api/person/${encodeURIComponent(id)}?days=${state.days}` + (day ? `&day=${day}` : ""));
      renderPerson(p);
      if (panel.hidden) { panel.hidden = false; $("person-close").focus(); }
    } catch (e) {
      if (e.message !== "expired") notice("Юклаб бўлмади", () => openPerson(id, day));
    }
  }
  function closePerson() {
    $("person").hidden = true;
    state.person = null;
    if (opener && opener.focus) opener.focus();
  }
  function renderPerson(p) {
    $("p-name").textContent = p.name;
    $("p-role").textContent = p.role;
    // the board's own score for the period (one number per person everywhere); the line's average only without it
    const row = state.overview && state.overview.people.find((x) => x.id === p.id);
    const vals = p.line.map((x) => x.v).filter((v) => v !== null);
    const avg = row && row.score !== null ? row.score : vals.length ? Math.round(vals.reduce((a, b) => a + b, 0) / vals.length) : null;
    $("p-score").textContent = avg === null ? "—" : fmtInt.format(avg);
    delta($("p-delta"), row ? row.delta : null);
    if (!pChart && window.LightweightCharts) {
      pChart = LightweightCharts.createChart($("p-chart"), { ...chartOptions(), autoSize: true });
      pChart.applyOptions({ rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.1, bottom: 0.08 } } });
      pArea = pChart.addAreaSeries({ lineWidth: 2, priceLineVisible: false, priceFormat: { type: "price", precision: 0, minMove: 1 } });
      pChart.subscribeClick((param) => { if (param.time && state.person) openPerson(state.person, param.time); });
      pChart.subscribeCrosshairMove((param) => parts(param && param.time ? param.time : state.personDay));
    }
    if (pChart) {
      pChart.applyOptions(chartOptions());
      pArea.applyOptions({ lineColor: css("--line"), topColor: css("--area"), bottomColor: css("--area") });
      pArea.setData(p.line.map((x) => (x.v === null ? { time: x.d } : { time: x.d, value: x.v })));
      pArea.setMarkers([{ time: p.day, position: "aboveBar", color: css("--ink"), shape: "circle", size: 0.6 }]);
      pChart.timeScale().fitContent();
    }
    state.personDay = p.day;
    state.personLine = p.line;
    parts(p.day);
    const days = p.line.map((x) => x.d), at = days.indexOf(p.day);
    $("day-prev").disabled = at <= 0;
    $("day-next").disabled = at < 0 || at >= days.length - 1;
    $("day-prev").onclick = () => openPerson(p.id, days[at - 1]);
    $("day-next").onclick = () => openPerson(p.id, days[at + 1]);
    const [y, m, d] = p.day.split("-");
    $("p-day").textContent = `${d}.${m}.${y}`;
    const list = $("p-timeline");
    list.textContent = "";
    if (!p.timeline.length) { list.appendChild(el("li", "empty", "Ҳаракат йўқ")); return; }
    p.timeline.forEach((it) => {
      const li = el("li");
      li.appendChild(el("span", "t", it.t || "—"));
      li.appendChild(icon(it.kind));
      const what = el("span");
      what.appendChild(el("span", "label", it.label));
      if (it.detail) what.appendChild(el("span", "det", it.detail));
      li.appendChild(what);
      list.appendChild(li);
    });
  }

  // the day's three parts — attendance, report, tasks — each by its drawn icon, as a number (— = not counted)
  const PARTS = [["attendance", "in", "Давомат"], ["report", "report", "Ҳисобот"], ["tasks", "task", "Топшириқ"]];
  function parts(day) {
    const box = $("p-parts"), line = state.personLine || [];
    const at = line.find((x) => x.d === day);
    box.textContent = "";
    if (!at) return;
    const [y, m, d] = at.d.split("-");
    box.appendChild(el("span", "when", `${d}.${m}`));
    PARTS.forEach(([key, kind, said]) => {
      const v = at.parts ? at.parts[key] : null;
      const part = el("span", `part${v === null || v === undefined ? " off" : v === 0 ? " zero" : ""}`);
      part.title = said;
      part.appendChild(icon(kind));
      part.appendChild(el("span", "sr", said));
      part.appendChild(document.createTextNode(v === null || v === undefined ? "—" : fmtInt.format(v)));
      box.appendChild(part);
    });
  }

  // ---- loading ----------------------------------------------------------------------------------------------
  async function loadOverview() {
    $("screen").setAttribute("aria-busy", "true");
    try {
      const o = await api(`/pulse/api/overview?days=${state.days}`);
      state.overview = o;
      $("asof").textContent = o.asOf;
      renderTickers(o.tickers);
      renderIndex(o);
      if (state.sector !== "all" && !o.sectors.some((s) => s.key === state.sector)) state.sector = "all";
      renderSectors(o);
      renderBoard(o);
      notice(null);
    } catch (e) {
      if (e.message !== "expired") notice("Юклаб бўлмади", loadOverview);
    } finally {
      $("screen").setAttribute("aria-busy", "false");
    }
  }
  async function loadTape() {
    try { renderTape((await api("/pulse/api/tape")).items); } catch (e) { /* the next round tries again */ }
  }

  document.querySelectorAll(".range button").forEach((b) => {
    b.addEventListener("click", () => {
      state.days = Number(b.dataset.days);
      document.querySelectorAll(".range button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      skeleton();
      loadOverview();
      if (state.person) openPerson(state.person, state.personDay);
    });
  });
  $("person-close").addEventListener("click", closePerson);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("person").hidden) closePerson(); });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    paintChart();
    if (state.overview) { renderTickers(state.overview.tickers); renderBoard(state.overview); }
    if (state.person) openPerson(state.person, state.personDay);
  });

  skeleton();
  loadOverview();
  loadTape();
  setInterval(loadTape, TAPE_EVERY);
  setInterval(loadOverview, OVERVIEW_EVERY);
})();
