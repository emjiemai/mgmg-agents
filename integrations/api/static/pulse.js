/* PULSE — the page's behaviour (2026-10-22). No framework; the chart is TradingView Lightweight Charts 4.2.0. */
(function () {
  "use strict";

  // anim: the next render moves (on entry and on a new period); a refresh every few minutes doesn't
  const state = { days: 30, sector: "all", overview: null, tapeSeen: new Set(), person: null, personDay: null, anim: true };
  const $ = (id) => document.getElementById(id);
  const COLORS = ["--accent", "--accent-2", "--accent-3", "--accent", "--accent-2", "--accent-4"];

  // the theme: what the viewer chose (kept in this browser), else the device's
  function setTheme(name) {
    document.documentElement.dataset.theme = name;
    const meta = document.querySelector('meta[name="color-scheme"]');
    if (meta) meta.content = name;
    document.querySelectorAll("[data-theme-toggle]").forEach((b) => b.setAttribute("aria-pressed", String(name === "light")));
  }
  let kept = null;
  try { kept = localStorage.getItem("pulse-theme"); } catch (e) { /* private window */ }
  setTheme(kept || (window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"));
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

  // ---- motion -----------------------------------------------------------------------------------------------
  const calm = window.matchMedia("(prefers-reduced-motion: reduce)");
  const ease = (t) => 1 - Math.pow(1 - t, 3);
  const runs = new Map();
  // a series rises from its floor, left to right like a wave; without motion (or on a refresh) it is just set
  function put(series, points, floor, anim) {
    cancelAnimationFrame(runs.get(series));
    runs.delete(series);
    if (!anim || calm.matches || !points.some((p) => p.value !== undefined)) { series.setData(points); return; }
    const t0 = performance.now(), n = points.length, ms = 1300;
    const frame = (now) => {
      const t = Math.min(1, (now - t0) / ms);
      series.setData(points.map((p, i) => {
        if (p.value === undefined) return p;
        const local = Math.max(0, Math.min(1, (t - (i / n) * 0.45) / 0.55));
        return { ...p, value: floor + (p.value - floor) * ease(local) };
      }));
      if (t < 1) runs.set(series, requestAnimationFrame(frame)); else runs.delete(series);
    };
    runs.set(series, requestAnimationFrame(frame));
  }
  // the scale fixed to the final values, so it doesn't jump while the line rises
  function holdScale(series, points, lo) {
    const vals = points.filter((p) => p.value !== undefined).map((p) => p.value);
    if (!vals.length) { series.applyOptions({ autoscaleInfoProvider: undefined }); return null; }
    const min = lo !== undefined ? lo : Math.min(...vals), max = Math.max(...vals);
    const pad = max === min ? 1 : 0;
    series.applyOptions({ autoscaleInfoProvider: () => ({ priceRange: { minValue: min - pad, maxValue: max + pad } }) });
    return { min: min - pad, max: max + pad };
  }
  // a number counts up to its value
  function count(to, show, anim) {
    if (!anim || calm.matches || to === null || to === undefined) { show(to); return; }
    const t0 = performance.now(), ms = 1000;
    const frame = (now) => {
      const t = Math.min(1, (now - t0) / ms);
      show(t < 1 ? to * ease(t) : to);
      if (t < 1) requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
  }
  // a figure with its unit (сўм shortened, %, a count), counted up
  function figure(node, v, unit, anim) {
    node.textContent = "";
    if (v === null || v === undefined) { node.textContent = "—"; return; }
    const [, u] = unit === "som" ? som(v) : unit === "pct" ? pct(v) : ["", ""];
    const div = u === "млрд" ? 1e9 : u === "млн" ? 1e6 : u === "минг" ? 1e3 : 1;
    const text = document.createTextNode("");
    node.appendChild(text);
    if (u) node.appendChild(el("small", null, u));
    const f = unit === "som" && div < 1e6 ? fmtInt : unit === "count" ? fmtInt : fmt1;
    count(v, (x) => { text.nodeValue = f.format(unit === "count" ? Math.round(x) : x / div); }, anim);
  }

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
  function spark(values, w, h, i) {
    const s = svgEl("svg", { class: state.anim && !calm.matches ? "spark draw" : "spark", viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: "none", "aria-hidden": "true" });
    const pts = values.map((v, i) => [i, v]).filter(([, v]) => v !== null && v !== undefined);
    if (i !== undefined) s.style.setProperty("--i", i);
    if (pts.length < 2) return s;
    const nums = pts.map(([, v]) => v);
    const lo = Math.min(...nums), hi = Math.max(...nums), span = hi - lo || 1, step = w / Math.max(1, values.length - 1);
    const y = (v) => h - 3 - ((v - lo) / span) * (h - 6);
    // a smooth curve through the points (Catmull-Rom as cubic Béziers)
    const xy = pts.map(([i, v]) => [i * step, y(v)]);
    let line = `M${xy[0][0].toFixed(1)},${xy[0][1].toFixed(1)}`;
    for (let k = 0; k < xy.length - 1; k++) {
      const p0 = xy[k - 1] || xy[k], p1 = xy[k], p2 = xy[k + 1], p3 = xy[k + 2] || p2;
      const c1 = [p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6];
      const c2 = [p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6];
      line += `C${c1[0].toFixed(1)},${c1[1].toFixed(1)} ${c2[0].toFixed(1)},${c2[1].toFixed(1)} ${p2[0].toFixed(1)},${p2[1].toFixed(1)}`;
    }
    const lastX = xy[xy.length - 1][0].toFixed(1), lastY = xy[xy.length - 1][1].toFixed(1);
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
    tickers.forEach((t, i) => {
      const item = el("article", state.anim ? "ticker rise" : "ticker");
      item.style.setProperty("--c", `var(${COLORS[i % COLORS.length]})`);
      item.style.setProperty("--i", i);
      item.appendChild(el("span", "label", t.label));
      const value = el("span", "value");
      if (t.value === null || t.value === undefined) item.classList.add("off");
      figure(value, t.value, t.unit, state.anim);
      item.appendChild(value);
      const foot = el("div", "foot"), d = el("span");
      delta(d, t.delta, { invert: t.invert, pct: t.unit === "som" });
      foot.appendChild(d);
      foot.appendChild(spark(t.spark || [], 84, 30, i));
      item.appendChild(foot);
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

  // ---- the index chart and the sales chart (two charts, one crosshair) --------------------------------------
  let chart = null, area = null, salesChart = null, bars = null, syncing = false;
  function chartOptions() {
    return {
      // no logo on the charts (the owner, 2026-10-23); the library's required credit is the link at the page's foot
      layout: { background: { type: "solid", color: css("--card") }, textColor: css("--ink-3"), fontFamily: "Noto Sans, system-ui, sans-serif", fontSize: 12, attributionLogo: false },
      grid: { vertLines: { visible: false }, horzLines: { color: css("--rule") } },
      rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.1, bottom: 0.08 } },
      timeScale: { borderVisible: false, fixLeftEdge: true, fixRightEdge: true, lockVisibleTimeRangeOnResize: true },
      crosshair: { mode: 1, vertLine: { color: css("--ink-3"), labelBackgroundColor: css("--card-2") }, horzLine: { color: css("--ink-3"), labelBackgroundColor: css("--card-2") } },
      handleScroll: false, handleScale: false,
      localization: { locale: "ru-RU", dateFormat: "dd.MM.yyyy" },
    };
  }
  function ensureChart() {
    if (chart || !window.LightweightCharts) return;
    chart = LightweightCharts.createChart($("chart"), { ...chartOptions(), autoSize: true });
    // lastPriceAnimation: the last day's point breathes — the line is live
    area = chart.addAreaSeries({ lineWidth: 3, lineType: 2, priceLineVisible: false, lastValueVisible: true, crosshairMarkerRadius: 5, lastPriceAnimation: 1, priceFormat: { type: "price", precision: 0, minMove: 1 } });
    salesChart = LightweightCharts.createChart($("sales-chart"), { ...chartOptions(), autoSize: true });
    bars = salesChart.addHistogramSeries({ priceFormat: { type: "custom", minMove: 1, formatter: (v) => som(v)[0] + " " + som(v)[1] }, lastValueVisible: false, priceLineVisible: false });
    salesChart.priceScale("right").applyOptions({ scaleMargins: { top: 0.1, bottom: 0 } });
    // the same day under the finger on both charts
    const follow = (from, to, series, values) => (param) => {
      legend(param && param.time ? param.time : null);
      if (syncing) return;
      syncing = true;
      if (param && param.time) {
        const v = values().find((p) => p.d === param.time);
        if (v && v.v !== null && v.v !== undefined) to.setCrosshairPosition(v.v, param.time, series); else to.clearCrosshairPosition();
      } else to.clearCrosshairPosition();
      syncing = false;
    };
    chart.subscribeCrosshairMove(follow(chart, salesChart, bars, () => (state.overview ? state.overview.index.volume : [])));
    salesChart.subscribeCrosshairMove(follow(salesChart, chart, area, () => (state.overview ? state.overview.index.series : [])));
    paintChart();
  }
  // the legends follow the crosshair: that day's score and sales; at rest, the last day's
  function legend(time) {
    const o = state.overview;
    if (!o) return;
    const at = time ? o.index.series.findIndex((p) => p.d === time) : o.index.series.length - 1;
    if (at < 0) return;
    const p = o.index.series[at], v = o.index.volume[at];
    const [, m, d] = p.d.split("-");
    $("hover-day").textContent = `${d}.${m}`;
    $("hover-sales-day").textContent = `${d}.${m}`;
    $("hover-index").textContent = p.v === null ? "—" : fmtInt.format(p.v);
    $("hover-sales").textContent = v && v.v !== null && v.v !== undefined ? som(v.v).join(" ") : "—";
  }
  function paintChart() {
    if (!chart) return;
    chart.applyOptions(chartOptions());
    salesChart.applyOptions(chartOptions());
    area.applyOptions({ lineColor: css("--accent"), topColor: css("--area-top"), bottomColor: css("--area-bottom"), crosshairMarkerBackgroundColor: css("--card"), crosshairMarkerBorderColor: css("--accent") });
    if (state.overview) setChartData(state.overview, false);
  }
  function setChartData(o, anim) {
    if (!chart) return;
    const line = o.index.series.map((p) => (p.v === null ? { time: p.d } : { time: p.d, value: p.v }));
    const range = holdScale(area, line);
    put(area, line, range ? range.min - (range.max - range.min) * 0.12 : 0, anim);
    const last = o.index.volume.length - 1;
    const vol = o.index.volume.map((p, i) => (p.v === null || p.v === undefined ? { time: p.d } : { time: p.d, value: p.v, color: i === last ? css("--sales-last") : css("--sales") }));
    holdScale(bars, vol, 0);
    put(bars, vol, 0, anim);
    chart.timeScale().fitContent();
    salesChart.timeScale().fitContent();
    legend(null);
  }
  function renderIndex(o) {
    figure($("index-value"), o.index.value, "count", state.anim);
    delta($("index-delta"), o.index.delta);
    const sales = o.tickers.find((t) => t.key === "sales") || {};
    figure($("sales-value"), sales.value, "som", state.anim);
    delta($("sales-delta"), sales.delta, { pct: true });
    ensureChart();
    setChartData(o, state.anim);
  }

  // ---- the tape ---------------------------------------------------------------------------------------------
  function renderTape(items) {
    const list = $("tape");
    const first = state.tapeSeen.size === 0;
    list.textContent = "";
    if (!items.length) { list.appendChild(el("li", "empty", "Ҳали ҳаракат йўқ")); return; }
    items.forEach((it, i) => {
      const key = `${it.t}|${it.pid}|${it.label}|${it.detail}`;
      const li = el("li");
      if (!first && !state.tapeSeen.has(key)) li.classList.add("fresh");
      if (first && !calm.matches) { li.classList.add("rise"); li.style.setProperty("--i", i); }
      state.tapeSeen.add(key);
      const badge = el("span", "badge");
      badge.title = KEY[it.kind] || it.label;
      badge.appendChild(icon(it.kind));
      li.appendChild(badge);
      const what = el("span", "what");
      if (it.pid) {
        const who = el("button", "who", it.who);
        who.type = "button";
        who.addEventListener("click", () => openPerson(it.pid));
        what.appendChild(who);
      }
      const act = el("span", "act", it.label);
      if (it.detail) { act.appendChild(document.createTextNode(" · ")); act.appendChild(el("span", "det", it.detail)); }
      what.appendChild(act);
      li.appendChild(what);
      li.appendChild(el("span", "t", it.t));
      list.appendChild(li);
    });
  }
  // what each mark on the tape means (the ⓘ button opens it; open or closed is kept in this browser)
  const KEY = { in: "Келди", late: "Кечикди", out: "Кетди", task: "Топшириқ", report: "Ҳисобот",
                explain: "Сабабини ёзди", complaint: "Шикоят келди", resolved: "Шикоят ҳал бўлди" };
  function renderKey() {
    const box = $("tape-key");
    Object.entries(KEY).forEach(([kind, said]) => {
      const li = el("li"), badge = el("span", "badge");
      badge.appendChild(icon(kind));
      li.appendChild(badge);
      li.appendChild(el("span", null, said));
      box.appendChild(li);
    });
    const show = (open) => { box.hidden = !open; $("key-toggle").setAttribute("aria-expanded", String(open)); };
    let open = true;
    try { open = localStorage.getItem("pulse-key") !== "closed"; } catch (e) { /* first visit: open */ }
    show(open);
    $("key-toggle").addEventListener("click", () => {
      const now = box.hidden;
      show(now);
      try { localStorage.setItem("pulse-key", now ? "open" : "closed"); } catch (e) { /* not kept */ }
    });
  }

  // a person's initials on a colour of their own
  function avatar(name, id) {
    const words = String(name || "").trim().split(/\s+/);
    const a = el("span", "avatar", ((words[0] || "")[0] || "") + ((words[1] || "")[0] || ""));
    let h = 0;
    for (const ch of String(id)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    a.style.setProperty("--c", `var(${["--accent", "--accent-2", "--accent-3", "--accent-4"][h % 4]})`);
    return a;
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
      const tr = el("tr", state.anim ? "rise" : "");
      tr.style.setProperty("--i", i);
      tr.appendChild(el("td", "n", String(i + 1)));
      const who = el("td"), cell = el("div", "person-cell");
      const state_ = p.today === "on_time" || p.today === "present" ? "in" : p.today === "late" ? "late" : p.today === "absent" ? "absent" : "";
      const av = avatar(p.name, p.id);
      const dot = el("span", `dot ${state_}`);
      const said = { in: "Келди", late: "Кечикди", absent: "Келмади" }[state_];
      if (said) { av.title = said; cell.appendChild(el("span", "sr", said)); }
      av.appendChild(dot);
      cell.appendChild(av);
      const btn = el("button");
      btn.type = "button";
      btn.appendChild(el("span", "name", p.name));
      btn.appendChild(el("span", "role", p.role));
      btn.addEventListener("click", (e) => { e.stopPropagation(); openPerson(p.id); });
      cell.appendChild(btn);
      who.appendChild(cell);
      tr.appendChild(who);
      const sp = el("td", "spark-cell");
      sp.appendChild(spark(p.spark, 150, 30, i));
      tr.appendChild(sp);
      const sc = el("td", `num score${p.score === null ? " none" : ""}`);
      figure(sc, p.score, "count", state.anim);
      tr.appendChild(sc);
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
    const fresh = panel.hidden || state.person !== id;
    state.person = id;
    try {
      const p = await api(`/pulse/api/person/${encodeURIComponent(id)}?days=${state.days}` + (day ? `&day=${day}` : ""));
      renderPerson(p, fresh);
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
  function renderPerson(p, fresh) {
    $("p-name").textContent = p.name;
    const big = avatar(p.name, p.id);
    $("p-avatar").textContent = big.textContent;
    $("p-avatar").style.setProperty("--c", big.style.getPropertyValue("--c"));
    $("p-role").textContent = p.role;
    // the board's own score for the period (one number per person everywhere); the line's average only without it
    const row = state.overview && state.overview.people.find((x) => x.id === p.id);
    const vals = p.line.map((x) => x.v).filter((v) => v !== null);
    const avg = row && row.score !== null ? row.score : vals.length ? Math.round(vals.reduce((a, b) => a + b, 0) / vals.length) : null;
    figure($("p-score"), avg, "count", fresh);
    delta($("p-delta"), row ? row.delta : null);
    if (!pChart && window.LightweightCharts) {
      pChart = LightweightCharts.createChart($("p-chart"), { ...chartOptions(), autoSize: true });
      pChart.applyOptions({ rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.1, bottom: 0.08 } } });
      pArea = pChart.addAreaSeries({ lineWidth: 3, lineType: 2, priceLineVisible: false, priceFormat: { type: "price", precision: 0, minMove: 1 } });
      pChart.subscribeClick((param) => { if (param.time && state.person) openPerson(state.person, param.time); });
      pChart.subscribeCrosshairMove((param) => parts(param && param.time ? param.time : state.personDay));
    }
    if (pChart) {
      pChart.applyOptions(chartOptions());
      pArea.applyOptions({ lineColor: css("--accent"), topColor: css("--area-top"), bottomColor: css("--area-bottom") });
      const line = p.line.map((x) => (x.v === null ? { time: x.d } : { time: x.d, value: x.v }));
      const range = holdScale(pArea, line);
      put(pArea, line, range ? range.min - (range.max - range.min) * 0.12 : 0, fresh);
      pArea.setMarkers([{ time: p.day, position: "aboveBar", color: css("--accent"), shape: "circle", size: 0.7 }]);
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
      const badge = el("span", "badge");
      badge.appendChild(icon(it.kind));
      li.appendChild(badge);
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
      state.anim = false;
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
      state.anim = true;
      document.querySelectorAll(".range button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      skeleton();
      loadOverview();
      if (state.person) openPerson(state.person, state.personDay);
    });
  });
  $("person-close").addEventListener("click", closePerson);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("person").hidden) closePerson(); });
  function repaint() {
    paintChart();
    if (state.overview) { renderTickers(state.overview.tickers); renderBoard(state.overview); }
    if (state.person) openPerson(state.person, state.personDay);
  }
  document.querySelectorAll("[data-theme-toggle]").forEach((b) => b.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    setTheme(next);
    try { localStorage.setItem("pulse-theme", next); } catch (e) { /* the choice just isn't kept */ }
    repaint();
  }));
  window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", (e) => {
    let kept = null;
    try { kept = localStorage.getItem("pulse-theme"); } catch (err) { /* none */ }
    if (!kept) { setTheme(e.matches ? "light" : "dark"); repaint(); }
  });

  skeleton();
  renderKey();
  loadOverview();
  loadTape();
  document.querySelectorAll(".rail-link[href]").forEach((a) => a.addEventListener("click", () => {
    document.querySelectorAll(".rail-link").forEach((x) => x.classList.toggle("on", x === a));
  }));
  setInterval(loadTape, TAPE_EVERY);
  setInterval(loadOverview, OVERVIEW_EVERY);
})();
