// Illustrative data for the PRISA mockups: NOT model output.
// May 2027 copies the team's PDF mockup; the other months follow a plausible
// shape (quiet winter, busy summer, busier weekends, holidays and events).
const PRISA = (() => {
  const YEAR = 2027;
  const TIERS = [
    { key: "muy-baja", name: "Muy baja", min: 0.0, discount: 0.3 },
    { key: "baja", name: "Baja", min: 0.2, discount: 0.2 },
    { key: "media", name: "Media", min: 0.4, discount: 0.1 },
    { key: "alta", name: "Alta", min: 0.6, discount: 0 },
    { key: "muy-alta", name: "Muy alta", min: 0.8, discount: 0 },
  ];
  const PRODUCTS = [
    { id: "sencillo", name: "Billete sencillo", who: "Turista ocasional", ref: 2.9 },
    { id: "tcasual", name: "T-casual (por viaje)", who: "Turista de varios días", ref: 1.3 },
    { id: "tdia", name: "T-Dia", who: "Turista", ref: 11.0 },
    { id: "residente", name: "Residente (por viaje)", who: "Tope: media del mes", ref: 1.3, capped: true },
  ];
  const HOLIDAYS = {
    "2027-01-01": "Año Nuevo", "2027-01-06": "Reyes", "2027-03-26": "Viernes Santo",
    "2027-03-29": "Lunes de Pascua", "2027-05-01": "Fiesta del Trabajo", "2027-05-17": "Pascua Granada",
    "2027-06-24": "Sant Joan", "2027-08-15": "La Asunción", "2027-09-11": "Diada",
    "2027-09-24": "La Mercè", "2027-10-12": "Fiesta Nacional", "2027-11-01": "Todos los Santos",
    "2027-12-06": "Constitución", "2027-12-08": "Inmaculada", "2027-12-25": "Navidad",
    "2027-12-26": "Sant Esteve",
  };
  const EVENT_SPANS = [
    ["2027-03-01", "2027-03-04", "Mobile World Congress"],
    ["2027-03-07", "2027-03-07", "Marató de Barcelona"],
    ["2027-06-02", "2027-06-06", "Primavera Sound"],
    ["2027-06-17", "2027-06-19", "Sónar"],
    ["2027-09-23", "2027-09-26", "Fiestas de La Mercè"],
    ["2027-08-16", "2027-08-22", "Fiestas de Gràcia"],
  ];
  const ZONES = [
    { id: "city", name: "Toda la ciudad", shift: 0, summer: 0 },
    { id: "sagrada", name: "Sagrada Família", shift: 0.06, summer: 0.03 },
    { id: "parkguell", name: "Park Güell / La Salut", shift: 0.0, summer: 0.12 },
    { id: "gotic", name: "Gòtic", shift: 0.04, summer: 0.04 },
    { id: "barceloneta", name: "Barceloneta", shift: -0.05, summer: 0.2 },
  ];
  const MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
    "septiembre", "octubre", "noviembre", "diciembre"];
  const WEEKDAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"];
  const WD_SHORT = ["L", "M", "X", "J", "V", "S", "D"];
  const WD_ABBR = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"];

  const MONTH_BASE = [0.14, 0.17, 0.27, 0.42, 0.5, 0.67, 0.71, 0.76, 0.5, 0.3, 0.18, 0.24];
  const WEEKDAY_EFFECT = [-0.01, -0.03, -0.01, 0.01, 0.07, 0.1, 0.05];
  // May 2027 from the PDF: M media, A alta, X muy alta
  const MAY = "AAMAMMMMXMMMMAMAAMAMMMAAMMMMAAM";
  const MAY_P = { M: 0.5, A: 0.69, X: 0.86 };

  const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const noise = (i) => { const x = Math.sin(i * 12.9898 + 4.1) * 43758.5453; return x - Math.floor(x); };
  const clamp = (x, a = 0.02, b = 0.97) => Math.min(b, Math.max(a, x));
  const tierOf = (p) => { let t = TIERS[0]; for (const tt of TIERS) if (p >= tt.min) t = tt; return t; };
  const weekdayIdx = (d) => (d.getDay() + 6) % 7; // Monday = 0

  const events = {};
  for (const [a, b, name] of EVENT_SPANS) {
    for (let d = new Date(a + "T12:00"); iso(d) <= b; d.setDate(d.getDate() + 1)) events[iso(d)] = name;
  }

  function cityPressure(d, i) {
    const m = d.getMonth();
    if (m === 4) {
      const c = MAY[d.getDate() - 1];
      if (d.getDate() === 11) return 0.55;
      return MAY_P[c] + (noise(i) - 0.5) * 0.12;
    }
    // blend towards the neighbouring month so the seasons change gradually
    const frac = (d.getDate() - 1) / 30;
    const other = frac < 0.5 ? Math.max(0, m - 1) : Math.min(11, m + 1);
    const w = Math.abs(frac - 0.5) * 0.6;
    const base = MONTH_BASE[m] * (1 - w) + MONTH_BASE[other] * w;
    const key = iso(d);
    return clamp(base + WEEKDAY_EFFECT[weekdayIdx(d)] + (HOLIDAYS[key] ? 0.12 : 0) + (events[key] ? 0.1 : 0)
      + (noise(i) - 0.5) * 0.2);
  }

  const cache = {};
  function days(zoneId = "city") {
    if (cache[zoneId]) return cache[zoneId];
    const zone = ZONES.find((z) => z.id === zoneId) || ZONES[0];
    const out = [];
    for (let d = new Date(YEAR, 0, 1, 12), i = 0; d.getFullYear() === YEAR; d.setDate(d.getDate() + 1), i++) {
      const summer = Math.max(0, 1 - Math.abs(d.getMonth() - 6.5) / 3);
      const pc = cityPressure(d, i);
      const p = zone.id === "city" ? pc : clamp(pc + zone.shift + zone.summer * summer + (noise(i + 999) - 0.5) * 0.06);
      const t = tierOf(p);
      const key = iso(d);
      out.push({
        i, date: new Date(d), iso: key, month: d.getMonth(), day: d.getDate(), wd: weekdayIdx(d),
        p, tier: t, discount: t.discount, holiday: HOLIDAYS[key] || null, event: events[key] || null,
      });
    }
    cache[zoneId] = out;
    return out;
  }

  const price = (ref, discount) => Math.round(ref * (1 - discount) * 100) / 100;
  const monthDays = (zoneId, m) => days(zoneId).filter((d) => d.month === m);

  function summary(list) {
    const disc = list.filter((d) => d.discount > 0);
    const mean = (f) => list.reduce((s, d) => s + f(d), 0) / list.length;
    const byTier = TIERS.map((t) => ({ tier: t, n: list.filter((d) => d.tier.key === t.key).length }));
    return {
      n: list.length,
      discounted: disc.length,
      meanDiscount: mean((d) => d.discount),
      meanSingle: mean((d) => price(2.9, d.discount)),
      meanCasual: mean((d) => price(1.3, d.discount)),
      meanDia: mean((d) => price(11, d.discount)),
      atReference: list.filter((d) => d.discount === 0).length,
      byTier,
    };
  }

  // resident per-trip price is capped at the month's mean T-casual price
  const residentCap = (zoneId, m) => summary(monthDays(zoneId, m)).meanCasual;

  function productPrices(day, zoneId = "city") {
    const cap = residentCap(zoneId, day.month);
    return PRODUCTS.map((p) => {
      let v = price(p.ref, day.discount);
      if (p.capped) v = Math.min(v, Math.round(cap * 100) / 100);
      return { ...p, value: v, cap: p.capped ? cap : null };
    });
  }

  function hotspots(day) {
    return ZONES.filter((z) => z.id !== "city").map((z) => ({ id: z.id, name: z.name, p: days(z.id)[day.i].p }));
  }

  function factors(day) {
    const f = [];
    const season = day.p >= 0.6 && [5, 6, 7].includes(day.month) ? "Temporada alta"
      : [0, 1, 10, 11].includes(day.month) ? "Temporada baja" : "Temporada media";
    f.push(season);
    f.push(day.wd >= 5 ? "Fin de semana" : day.wd === 4 ? "Viernes" : "Día laborable");
    f.push(day.holiday ? `Festivo: ${day.holiday}` : "No festivo");
    if (day.event) f.push(`Evento: ${day.event}`);
    return f;
  }

  const eur = (x) => x.toLocaleString("es-ES", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " €";
  const pct = (x, digits = 0) => (x * 100).toLocaleString("es-ES", { minimumFractionDigits: digits, maximumFractionDigits: digits }) + " %";
  const longDate = (d) => `${WEEKDAYS[d.wd]} ${d.day} de ${MONTHS[d.month]}`;
  const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

  // the demo pretends today is 11 May 2027, the day selected in the PDF
  const TODAY_ISO = "2027-05-11";
  const todayIndex = () => days().findIndex((d) => d.iso === TODAY_ISO);

  return {
    YEAR, TIERS, PRODUCTS, ZONES, MONTHS, WEEKDAYS, WD_SHORT, WD_ABBR,
    days, monthDays, summary, price, productPrices, residentCap, hotspots, factors,
    eur, pct, longDate, cap, todayIndex, tierOf,
  };
})();
