import React from "react";
import { ArrowDown, ArrowUp, CircleDot, Radio } from "lucide-react";

const domains = {
  betika: "betika.co.tz", leonbet: "leon.bet", premierbet: "premierbet.co.tz", sportybet: "sportybet.com",
  sportpesa: "sportpesa.co.tz", wasafibet: "wasafibet.co.tz", sokabet: "sokabet.co.tz", meridianbet: "meridianbet.co.tz",
  parimatch: "parimatch.co.tz", "1xbet": "1xbet.tz", helabet: "helabet.co.tz",
};

function parseChange(change) {
  const raw = String(change.match || "Odds update");
  const [eventPart = raw, selectionPart = ""] = raw.split(/(?:—|â€”)/);
  const siteMatch = selectionPart.match(/\(([^)]+)\)\s*$/);
  return {
    event: eventPart.trim(),
    selection: selectionPart.replace(/\s*\([^)]+\)\s*$/, "").trim() || "Market",
    site: siteMatch?.[1] || "bookmaker",
  };
}

function BookmakerIcon({ site }) {
  const domain = domains[site.toLowerCase()];
  return domain ? <img className="h-5 w-5 rounded-full bg-white object-contain" src={`https://www.google.com/s2/favicons?domain=${domain}&sz=64`} alt="" /> : <CircleDot className="h-5 w-5 text-slate-400" />;
}

export default function OddsFeed({ oddsChanges = [] }) {
  return (
    <section className="overflow-hidden rounded-2xl border border-slate-700/70 bg-slate-950 text-slate-100 shadow-sm dark:bg-slate-950">
      <div className="flex items-center gap-2 border-b border-slate-800 px-4 py-3.5">
        <span className="flex h-4 w-4 items-center justify-center rounded-full bg-emerald-400/15"><Radio className="h-3 w-3 text-emerald-400" /></span>
        <h3 className="text-sm font-semibold">Live Odds Feed</h3>
        <span className="ml-auto text-[10px] text-slate-500">{oddsChanges.length} moves</span>
      </div>
      {oddsChanges.length ? <ul className="max-h-[330px] divide-y divide-slate-800 overflow-y-auto">{oddsChanges.slice(0, 20).map((change, index) => {
        const { event, selection, site } = parseChange(change);
        const oldValue = Number(change.old);
        const newValue = Number(change.new);
        const moved = Number.isFinite(oldValue) && Number.isFinite(newValue);
        const rising = moved && newValue > oldValue;
        return <li key={`${change.match || "odds"}-${index}`} className="flex items-center gap-3 px-4 py-3 transition-colors hover:bg-slate-900">
          <span className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ${moved ? rising ? "bg-emerald-400/10 text-emerald-400" : "bg-rose-400/10 text-rose-400" : "bg-sky-400/10 text-sky-400"}`}>{moved ? rising ? <ArrowUp className="h-4 w-4" /> : <ArrowDown className="h-4 w-4" /> : <Radio className="h-3.5 w-3.5" />}</span>
          <BookmakerIcon site={site} />
          <div className="min-w-0 flex-1"><div className="flex items-center gap-1.5"><span className="truncate text-xs font-semibold text-slate-100">{site}</span><span className="rounded bg-slate-800 px-1.5 py-0.5 text-[9px] font-bold text-slate-400">{selection}</span></div><div className="mt-0.5 truncate text-[10px] text-slate-500">{event}</div></div>
          <div className="shrink-0 text-right text-[11px]"><span className="text-slate-500">{moved ? oldValue.toFixed(2) : "new"}</span><span className="mx-1 text-slate-600">→</span><span className={rising ? "font-semibold text-emerald-400" : moved ? "font-semibold text-rose-400" : "font-semibold text-sky-300"}>{Number.isFinite(newValue) ? newValue.toFixed(2) : "—"}</span></div><span className="w-6 shrink-0 text-right text-[9px] text-slate-600">now</span>
        </li>;
      })}</ul> : <p className="px-4 py-10 text-center text-xs text-slate-500">Waiting for verified collector updates.</p>}
    </section>
  );
}
