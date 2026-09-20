/** Eastern Time, `YYYY-MM-DD HH:MM:SS`, matching app/common/clock.py and bff/clock.py.
 *
 *  The whole project stores and displays one timezone so two timestamps from different
 *  sources can be compared by eye. Dependency-free for the same reason the Python
 *  helpers are: a date library is a large thing to add for one format. */

export function fmtET(ts: string | number | null | undefined): string {
  if (ts == null || ts === "") return "";
  let ms: number;
  if (typeof ts === "string") {
    // The events table's sort key is "<timestamp>#<seq>"; take the timestamp.
    const head = ts.split("#")[0].trim();
    const m = head.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})/);
    if (m) return `${m[1]} ${m[2]}`;   // already ET from the backend
    const n = Number.parseInt(head, 10);
    if (Number.isNaN(n)) return "";
    ms = n < 1e12 ? n * 1000 : n;
  } else {
    ms = ts < 1e12 ? ts * 1000 : ts;
  }
  if (!ms) return "";
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/New_York",
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).formatToParts(new Date(ms));
  const g = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
  const hh = g("hour") === "24" ? "00" : g("hour");
  return `${g("year")}-${g("month")}-${g("day")} ${hh}:${g("minute")}:${g("second")}`;
}

/** Elapsed time between two ET stamps, for a run's duration. */
export function duration(from?: string, to?: string): string {
  const a = Date.parse((from ?? "").replace(" ", "T") + "-05:00");
  const b = Date.parse((to ?? "").replace(" ", "T") + "-05:00");
  if (Number.isNaN(a) || Number.isNaN(b) || b < a) return "—";
  const s = Math.round((b - a) / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}
