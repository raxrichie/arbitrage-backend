import React, { useEffect, useState } from "react";

const formatMoney = (value) =>
  new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(value || 0);

const domains = {
  betika: "betika.co.tz", leonbet: "leon.bet", premierbet: "premierbet.co.tz",
  sportybet: "sportybet.com", sportpesa: "sportpesa.co.tz", wasafibet: "wasafibet.co.tz",
  sokabet: "sokabet.co.tz", meridianbet: "meridianbet.co.tz", parimatch: "parimatch.co.tz",
  "1xbet": "1xbet.tz", helabet: "helabet.co.tz",
};

function BookmakerLogo({ site }) {
  const domain = domains[String(site).toLowerCase()];
  return domain ? <img className="h-4 w-4 shrink-0 rounded-sm object-contain" src={`https://www.google.com/s2/favicons?domain=${domain}&sz=64`} alt="" /> : null;
}

function marketText(market) {
  if (!market) return "Market details unavailable";
  return String(market)
    .replace(/^total ([\w ]+) · over\/under (.+?) · (.+)$/i, "Total $1 · Over/Under $2 · $3")
    .replace(/^over under (.+)$/i, "Over/Under $1");
}

function EventCountdown({ startTime }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const start = startTime ? new Date(startTime) : null;
  if (!start || Number.isNaN(start.getTime())) {
    return <div className="mt-2 text-[11px] text-muted-foreground">Event start time unavailable from this bookmaker feed.</div>;
  }
  const seconds = Math.floor((start.getTime() - now) / 1000);
  const absolute = Math.abs(seconds);
  const days = Math.floor(absolute / 86400);
  const hours = Math.floor((absolute % 86400) / 3600);
  const minutes = Math.floor((absolute % 3600) / 60);
  const remainingSeconds = absolute % 60;
  const countdown = `${days ? `${days}d ` : ""}${days || hours ? `${hours}h ` : ""}${minutes}m ${remainingSeconds}s`;
  return (
    <div className="mt-2 flex flex-wrap items-baseline gap-x-2 text-[11px]">
      <span className="font-medium text-foreground">{seconds >= 0 ? "Starts in" : "Started"} {countdown}</span>
      <span className="text-muted-foreground">· {start.toLocaleString()}</span>
      <span className="basis-full text-[10px] text-muted-foreground">Countdown is to scheduled start; odds keep updating live and can change at any time.</span>
    </div>
  );
}

export default function ArbTable({ arbs = [], bankroll = 0, roundTo = 100 }) {
  if (!arbs.length) {
    return <div className="rounded-xl border border-border bg-card px-5 py-10 text-center text-sm text-muted-foreground">No arbitrage opportunities are available yet.</div>;
  }

  return (
    <div className="space-y-3">
      {arbs.map((arb, index) => {
        const selections = arb.selections || [];
        const margin = Math.max(0, Number(arb.margin) || 0);
        const inverseOdds = selections.reduce((total, item) => total + (Number(item.odds) > 0 ? 1 / Number(item.odds) : 0), 0);
        const match = typeof arb.match === "string" ? arb.match : arb.match?.name;
        return (
          <article key={`${arb.id || match || "arb"}-${index}`} className="rounded-xl border border-border bg-card p-4 shadow-xs sm:p-5">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
              <div className="min-w-0">
                <h3 className="break-words text-sm font-semibold text-foreground">{match || "Unnamed match"}</h3>
                <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{[arb.sport, marketText(arb.market)].filter(Boolean).join(" · ")}</p>
                <EventCountdown startTime={arb.startTime} />
              </div>
              <div className="flex shrink-0 items-center gap-4 sm:text-right">
                <div>
                  <div className="text-[10px] uppercase tracking-wide text-muted-foreground">Margin</div>
                  <div className="font-semibold text-emerald-600 dark:text-emerald-400">{(margin * 100).toFixed(2)}%</div>
                </div>
                <div>
                  <div className="text-[10px] uppercase tracking-wide text-muted-foreground">Profit est.</div>
                  <div className="font-semibold text-foreground">+{formatMoney(bankroll * margin)}</div>
                </div>
              </div>
            </div>

            <div className={`mt-4 grid gap-2 ${selections.length === 3 ? "sm:grid-cols-3" : "sm:grid-cols-2"}`}>
              {selections.map((selection, selectionIndex) => {
                const rawStake = inverseOdds && Number(selection.odds) > 0 ? bankroll * (1 / Number(selection.odds)) / inverseOdds : 0;
                const unit = Math.max(1, Number(roundTo) || 1);
                const stake = Math.round(rawStake / unit) * unit;
                const quoteAge = selection.observedAt ? Math.max(0, Math.floor((Date.now() - new Date(selection.observedAt).getTime()) / 1000)) : null;
                return (
                <div key={`${selection.selection}-${selection.site}-${selectionIndex}`} className="flex min-w-0 items-center justify-between gap-3 rounded-lg bg-muted/40 px-3 py-2.5">
                  <div className="min-w-0">
                    <div className="break-words text-xs font-medium text-foreground">{selection.selection}</div>
                    <div className="mt-1 flex min-w-0 items-center gap-1.5 text-[11px] text-muted-foreground"><BookmakerLogo site={selection.site} /><span className="truncate">{selection.site}</span></div>
                    <div className="mt-1 text-[10px] text-muted-foreground">Stake approx. {formatMoney(stake)}</div>
                    {quoteAge !== null && <div className="mt-0.5 text-[10px] text-muted-foreground">Quote checked {quoteAge}s ago</div>}
                  </div>
                  <div className="shrink-0 text-right text-base font-bold tabular-nums text-foreground">{Number(selection.odds).toFixed(2)}</div>
                </div>
                );
              })}
            </div>
          </article>
        );
      })}
    </div>
  );
}
