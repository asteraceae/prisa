// PRISA web app: today's fare, the month as Eixample blocks, the year at a glance.
// Data comes from `python -m prisa.export` as window.PRISA_DATA (data/calendar.js,
// falling back to the illustrative data/sample.js).

const MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"];
const WEEKDAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"];
const WD_ABBR = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"];
const CONGESTION = ["muy baja", "baja", "media", "alta", "muy alta"];
const CROWD = ["Muy tranquilo", "Tranquilo", "Normal", "Concurrido", "Muy concurrido"];
const THEMES = ["auto", "light", "dark"];
const THEME_LABEL = { auto: "automático", light: "claro", dark: "oscuro" };

const $ = (id) => document.getElementById(id);
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
const eur = (x) => x.toLocaleString("es-ES", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " €";
const short = (x) => x.toLocaleString("es-ES", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const pct = (x) => Math.round(x * 100);
const longDate = (d) => `${WEEKDAYS[d.wd]} ${d.day} de ${MONTHS[d.month]}`;

let DATA, DAYS, state;

function loadData() {
  return new Promise((resolve, reject) => {
    const add = (src, onerror) => {
      const s = document.createElement("script");
      s.src = src;
      s.onload = () => (window.PRISA_DATA ? resolve(window.PRISA_DATA) : onerror());
      s.onerror = onerror;
      document.head.appendChild(s);
    };
    add("data/calendar.js", () => add("data/sample.js", () => reject(new Error("no data"))));
  });
}

function prepare(data) {
  return data.days.map((x, i) => {
    const [y, m, d] = x.d.split("-").map(Number);
    const date = new Date(y, m - 1, d);
    return { ...x, i, date, month: m - 1, day: d, wd: (date.getDay() + 6) % 7 };
  });
}

const price = (product, d) => (product.daily ? Math.round(product.price * (1 - d.disc) * 100) / 100 : product.price);
const single = () => DATA.products.find((p) => p.id === "single");
const monthDays = (m) => DAYS.filter((d) => d.month === m);

function pickToday() {
  const iso = (t) => `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, "0")}-${String(t.getDate()).padStart(2, "0")}`;
  const param = new URLSearchParams(location.search).get("hoy");
  const now = new Date();
  const real = DAYS.find((d) => d.d === iso(now));
  if (param) return { day: DAYS.find((d) => d.d === param) || DAYS[0], real: false };
  if (real) return { day: real, real: true };
  // outside the calendar year: preview the same date in that year
  const same = DAYS.find((d) => d.month === now.getMonth() && d.day === now.getDate()) || DAYS[0];
  return { day: same, real: false };
}

/* ---------- today ---------- */
function renderToday() {
  const d = state.today;
  const s = single();
  const pr = price(s, d);
  const week = DAYS.slice(d.i + 1, d.i + 8);
  const best = week.filter((x) => x.disc > 0).sort((a, b) => b.disc - a.disc || a.p - b.p)[0];
  const when = state.realToday ? `Hoy, <b>${longDate(d)}</b>` : `Vista previa para el <b>${longDate(d)} de ${DATA.meta.year}</b>`;
  $("today").innerHTML = `
    <div class="when">${when}</div>
    <div class="price-xl"><span class="sr-only">Billete sencillo: </span>${short(pr)}<span aria-hidden="true">€</span>${d.disc > 0 ? `<span class="cut">−${pct(d.disc)}%</span>` : ""}</div>
    <div class="facts">
      <p>Billete sencillo. ${d.disc > 0
        ? `La tarifa normal es ${eur(s.price)}: se espera congestión ${CONGESTION[d.lvl]}, así que viajar sale un ${pct(d.disc)} % más barato.`
        : `Se espera congestión ${CONGESTION[d.lvl]}: se paga la tarifa normal, ${eur(s.price)}.`}</p>
      <p class="best">${best
        ? `Si puedes elegir otro día esta semana: <button data-i="${best.i}">${longDate(best)}, ${eur(price(s, best))}</button>.`
        : "Esta semana no hay días más baratos."}</p>
    </div>`;
}

/* ---------- month ---------- */
function face(d) {
  const s = single();
  return `<span class="face l${d.lvl}"><span class="n">${d.day}${d.hol ? "<small>festivo</small>" : ""}${d.ev.length ? '<span class="ev" aria-hidden="true"></span>' : ""}</span>`
    + `<span class="pc">${d.disc > 0 ? "−" + pct(d.disc) + "%" : ""}</span><span class="pr">${short(price(s, d))} €</span></span>`;
}

function cellLabel(d) {
  const s = single();
  return `${longDate(d)}: ${eur(price(s, d))}${d.disc > 0 ? `, ${pct(d.disc)} % más barato` : ", tarifa normal"}, congestión ${CONGESTION[d.lvl]}${d.hol ? ", festivo" : ""}${d.ev.length ? ", " + d.ev.join(", ") : ""}`;
}

function renderMonth(build = false) {
  const list = monthDays(state.month);
  if (DAYS[state.sel].month !== state.month) state.sel = list[0].i;
  $("mname").textContent = `${cap(MONTHS[state.month])} ${DATA.meta.year}`;
  document.querySelector("[data-step='-1']").disabled = state.month === 0;
  document.querySelector("[data-step='1']").disabled = state.month === 11;
  document.querySelector(".dows").innerHTML = WD_ABBR.map((w) => `<span>${w}</span>`).join("");
  const lead = list[0].wd;
  const total = Math.ceil((lead + list.length) / 7) * 7;
  let html = "";
  for (let c = 0; c < total; c++) {
    const d = list[c - lead];
    const k = Math.floor(c / 7) + (c % 7);  // diagonal sweep for the first build
    if (!d) { html += `<span class="cell plot" style="--k:${k}" aria-hidden="true"><span class="face"></span></span>`; continue; }
    const sel = d.i === state.sel;
    html += `<button class="cell ${d.i === state.today.i && state.realToday ? "is-today" : ""}" style="--k:${k}" data-i="${d.i}"
      aria-pressed="${sel}" tabindex="${sel ? 0 : -1}" aria-label="${cellLabel(d)}">${face(d)}</button>`;
  }
  $("grid").innerHTML = html;
  $("grid").classList.toggle("build", build);
  $("scale").innerHTML = [0, 1, 2, 3, 4].map((l) => {
    const disc = DAYS.filter((d) => d.lvl === l).map((d) => d.disc);
    const lo = Math.round(Math.min(...disc, 1) * 100), hi = Math.round(Math.max(...disc, 0) * 100);
    const txt = hi === 0 ? "precio normal" : lo === hi ? `−${hi} %` : lo === 0 ? `hasta −${hi} %` : `−${lo} a −${hi} %`;
    return `<div><i class="l${l}"></i><span><b>${cap(CONGESTION[l])}</b> ${txt}</span></div>`;
  }).join("");
  renderDetail();
}

function why(d) {
  const out = [`Comparado con el resto de ${MONTHS[d.month]}, se espera congestión ${CONGESTION[d.lvl]}.`];
  out.push(d.wd >= 5 ? "Fin de semana." : "Día laborable.");
  if (d.hol) out.push(`Festivo: ${d.hol}.`);
  if (d.school) out.push("Vacaciones escolares.");
  for (const e of d.ev.slice(0, 3)) out.push(`Evento: ${e}.`);
  return out;
}

function renderDetail() {
  const d = DAYS[state.sel];
  const usual = DATA.products.find((p) => p.id === "t_usual");
  $("detail").innerHTML = `
    <h3>${cap(longDate(d))}</h3>
    <div class="chip"><i class="l${d.lvl}"></i>Congestión ${CONGESTION[d.lvl]}${d.disc > 0 ? `: ${pct(d.disc)} % más barato` : ": tarifa normal"}</div>
    <h4>Precio por título</h4>
    <ul class="titles">${DATA.products.map((p) => {
      const v = price(p, d);
      const ref = p.daily && d.disc > 0 ? `<s>${eur(p.price)}</s>` : "";
      const note = p.daily ? "" : "<small>mismo precio</small>";
      return `<li><span class="nm">${p.name}</span><span class="v">${ref}${eur(v)}${note}</span><span class="who">${p.what}</span></li>`;
    }).join("")}</ul>
    ${usual ? `<div class="promise">¿Vives aquí? Tu T-usual cuesta ${eur(usual.price)} al mes, sea el día que sea.</div>` : ""}
    <h4>Cómo estará cada sitio</h4>
    <ul class="places">${DATA.places.map((pl) => {
      const l = pl.lvl[d.i];
      return `<li><span>${pl.name}</span><span>${CROWD[l]}</span><span class="m" aria-hidden="true">${[0, 1, 2, 3, 4].map((k) => `<span class="${k <= l ? "on" : ""}"></span>`).join("")}</span></li>`;
    }).join("")}</ul>
    <h4>Por qué este precio</h4>
    <ul class="why">${why(d).map((w) => `<li>${w}</li>`).join("")}</ul>`;
}

/* ---------- year ---------- */
function renderYear() {
  const s = single();
  const disc = DAYS.filter((d) => d.disc > 0);
  const mean = DAYS.reduce((a, d) => a + d.disc, 0) / DAYS.length;
  const meanSingle = DAYS.reduce((a, d) => a + price(s, d), 0) / DAYS.length;
  const months = MONTHS.map((name, m) => {
    const list = monthDays(m);
    const n = list.filter((d) => d.disc > 0).length;
    let cells = "";
    for (let k = 0; k < list[0].wd; k++) cells += `<span class="cell plot"><span class="face"></span></span>`;
    cells += list.map((d) => `<span class="cell"><span class="face l${d.lvl}"></span></span>`).join("");
    return `<button class="month" data-open="${m}" aria-label="Abrir ${name}: ${n} días más baratos">
      <h4>${cap(name)}<small>${n} días más baratos</small></h4><span class="grid" aria-hidden="true">${cells}</span></button>`;
  }).join("");
  $("v-year").innerHTML = `
    <div class="cal-head"><h2>${DATA.meta.year}</h2></div>
    <div class="year-stats">
      <div><b>${disc.length}</b>días más baratos</div>
      <div><b>${pct(mean)} %</b>de descuento medio</div>
      <div><b>${eur(meanSingle)}</b>billete sencillo de media</div>
    </div>
    <div class="months">${months}</div>`;
}

/* ---------- shell ---------- */
function setView(view) {
  state.view = view;
  $("v-month").hidden = view !== "month";
  $("v-year").hidden = view !== "year";
  document.querySelectorAll("[data-view]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.view === view));
  view === "month" ? renderMonth() : renderYear();
  history.replaceState(null, "", view === "year" ? "#año" : `#${DAYS[state.sel].d}`);
}

function select(i, focus = false) {
  i = Math.max(0, Math.min(DAYS.length - 1, i));
  const changedMonth = DAYS[i].month !== state.month;
  state.sel = i;
  state.month = DAYS[i].month;
  if (changedMonth) renderMonth();
  else {
    document.querySelectorAll("#grid .cell[data-i]").forEach((c) => {
      const on = +c.dataset.i === i;
      c.setAttribute("aria-pressed", on);
      c.tabIndex = on ? 0 : -1;
    });
    renderDetail();
  }
  history.replaceState(null, "", `#${DAYS[i].d}`);
  if (focus) document.querySelector(`#grid .cell[data-i="${i}"]`)?.focus();
}

function applyTheme(t) {
  if (t === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
  try { localStorage.setItem("prisa-theme", t); } catch (e) {}
  $("theme").innerHTML = `<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6.5" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M8 1.5a6.5 6.5 0 0 1 0 13z" fill="currentColor"/></svg><span>Tema: ${THEME_LABEL[t]}</span>`;
  $("theme").setAttribute("aria-label", `Tema ${THEME_LABEL[t]}. Cambiar tema`);
  state.theme = t;
}

function init(data) {
  DATA = data;
  DAYS = prepare(data);
  const t = pickToday();
  state = { view: "month", today: t.day, realToday: t.real, sel: t.day.i, month: t.day.month, theme: "auto" };
  let saved = "auto";
  try { saved = localStorage.getItem("prisa-theme") || "auto"; } catch (e) {}
  applyTheme(THEMES.includes(saved) ? saved : "auto");
  $("notice").hidden = !data.meta.illustrative;
  $("foot").textContent = `Calendario ${data.meta.year} publicado por PRISA el ${data.meta.generated}, a partir de la previsión de congestión con datos de movilidad. `
    + "Proyecto académico (UPF, Mobile Data Challenge 2026): no es una tarifa oficial.";

  const hash = decodeURIComponent(location.hash.slice(1));
  const fromHash = DAYS.find((d) => d.d === hash);
  if (fromHash) { state.sel = fromHash.i; state.month = fromHash.month; }
  renderToday();
  if (hash === "año") setView("year");
  else { $("v-month").hidden = false; renderMonth(true); }

  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-view],[data-i],[data-step],[data-open],#theme");
    if (!b) return;
    if (b.id === "theme") return applyTheme(THEMES[(THEMES.indexOf(state.theme) + 1) % THEMES.length]);
    if (b.dataset.view) return setView(b.dataset.view);
    if (b.dataset.step) { state.month = Math.max(0, Math.min(11, state.month + +b.dataset.step)); return renderMonth(); }
    if (b.dataset.open) { state.month = +b.dataset.open; state.sel = monthDays(state.month)[0].i; return setView("month"); }
    if (b.dataset.i) {
      if (state.view !== "month") { state.sel = +b.dataset.i; state.month = DAYS[state.sel].month; return setView("month"); }
      select(+b.dataset.i);
    }
  });
  $("grid").addEventListener("keydown", (e) => {
    const moves = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 };
    if (e.key in moves) { e.preventDefault(); select(state.sel + moves[e.key], true); }
    if (e.key === "Home") { e.preventDefault(); select(monthDays(state.month)[0].i, true); }
    if (e.key === "End") { e.preventDefault(); select(monthDays(state.month).at(-1).i, true); }
  });
}

loadData().then(init).catch(() => {
  $("today").innerHTML = '<p>No hay calendario que mostrar. Genera los datos con <code>uv run python -m prisa.export --sample</code>.</p>';
});
