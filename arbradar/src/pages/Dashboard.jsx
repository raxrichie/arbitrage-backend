// src/pages/Dashboard.jsx
import React, { useState, useEffect } from "react";
import { Radar, TrendingUp, Activity, Layers, Sun, Moon, Info, Download } from "lucide-react";
import { useArbEngine } from "@/hooks/useArbEngine";
import { useTheme } from "@/hooks/useTheme";
import ArbTable from "@/components/arbitrage/ArbTable";
import BankrollPanel from "@/components/arbitrage/BankrollPanel";
import OddsFeed from "@/components/arbitrage/OddsFeed";
import MarginChart from "@/components/arbitrage/MarginChart";

function fmt(n) {
  return n.toLocaleString("en-US", { maximumFractionDigits: 0 });
}

function InstallAppControl() {
  const [installPrompt, setInstallPrompt] = useState(null);
  const [showHelp, setShowHelp] = useState(false);
  const [isInstalled, setIsInstalled] = useState(() => window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true);

  useEffect(() => {
    const capturePrompt = (event) => {
      event.preventDefault();
      setInstallPrompt(event);
    };
    const markInstalled = () => setIsInstalled(true);
    window.addEventListener("beforeinstallprompt", capturePrompt);
    window.addEventListener("appinstalled", markInstalled);
    return () => {
      window.removeEventListener("beforeinstallprompt", capturePrompt);
      window.removeEventListener("appinstalled", markInstalled);
    };
  }, []);

  const install = async () => {
    if (!installPrompt) {
      setShowHelp(true);
      return;
    }
    await installPrompt.prompt();
    setInstallPrompt(null);
  };

  if (isInstalled) return null;

  return <>
    <button onClick={install} className="flex h-8 items-center gap-1.5 rounded-xl border border-border bg-muted px-2.5 text-[11px] font-medium text-foreground hover:bg-accent" title="Install ArbRadar on this device">
      <Download className="h-3.5 w-3.5" /><span className="hidden sm:inline">Install app</span>
    </button>
    {showHelp && <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setShowHelp(false); }}>
      <section role="dialog" aria-modal="true" aria-labelledby="install-help-title" className="w-full max-w-sm rounded-xl border border-border bg-card p-5 text-sm shadow-xl">
        <h2 id="install-help-title" className="font-semibold text-foreground">Install ArbRadar</h2>
        <p className="mt-2 text-muted-foreground">Use your browser menu to install this app. On iPhone or iPad, open the Share menu and choose “Add to Home Screen.”</p>
        <button onClick={() => setShowHelp(false)} className="mt-4 rounded-lg bg-emerald-600 px-3 py-2 text-xs font-semibold text-white">Got it</button>
      </section>
    </div>}
  </>;
}

export default function Dashboard() {
  const { arbs, oddsChanges, marginHistory, sourceCount, isLive, diagnostics = {}, complianceChecks = [], nearMisses = [], sourceFamilies = [], collectorHealth = {}, opportunityHistory = [], oldestOddsAt, lastOddsAt, oldestCurrentArbOddsAt, now = Date.now() } = useArbEngine();
  const { theme, toggleTheme } = useTheme();
  
  const [bankroll, setBankroll] = useState(() => {
    const saved = localStorage.getItem("arb_bankroll");
    return saved ? Number(saved) : 500000;
  });
  const [roundTo, setRoundTo] = useState(100);
  const [minimumMargin, setMinimumMargin] = useState(0);

  useEffect(() => {
    localStorage.setItem("arb_bankroll", String(bankroll));
  }, [bankroll]);

  const visibleArbs = (arbs || []).filter((arb) => Math.abs(Number(arb.margin) || 0) * 100 >= minimumMargin);
  const avgMargin =
    visibleArbs.length > 0
      ? (visibleArbs.reduce((s, a) => s + Math.abs(a.margin), 0) / visibleArbs.length) * 100
      : 0;
  const bestPathProfit = visibleArbs[0] ? bankroll * Math.max(0, Number(visibleArbs[0].margin) || 0) : 0;
  const observedAt = oldestCurrentArbOddsAt || lastOddsAt || oldestOddsAt;
  const oddsAgeSeconds = observedAt ? Math.max(0, Math.floor((now - new Date(observedAt).getTime()) / 1000)) : null;
  const feedState = !isLive ? "OFFLINE" : !sourceCount || oddsAgeSeconds === null ? "NO LIVE ODDS" : oddsAgeSeconds <= 90 ? "LIVE ODDS" : "STALE ODDS";
  const feedIsFresh = feedState === "LIVE ODDS";


  return (
    <div className="min-h-screen bg-background text-foreground transition-colors duration-200">
      {/* Header */}
      <header className="border-b border-border sticky top-0 z-20 bg-background/80 backdrop-blur-md shadow-xs">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 py-3.5 flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-emerald-500 to-sky-600 flex items-center justify-center shadow-sm">
              <Radar className="w-5 h-5 text-white" />
            </div>
            <div>
              <h1 className="font-bold text-base sm:text-lg leading-none tracking-tight">ArbRadar</h1>
              <p className="text-[10px] text-muted-foreground mt-0.5">
                {sourceCount} active bookmaker {sourceCount === 1 ? "feed" : "feeds"}
              </p>
            </div>
          </div>
          
          <div className="flex items-center gap-3">
            <InstallAppControl />
            {/* Theme Toggle Button */}
            <button
              onClick={toggleTheme}
              className="w-8 h-8 rounded-xl bg-muted border border-border flex items-center justify-center hover:bg-accent hover:text-accent-foreground active:scale-95 transition-all shadow-xs"
              title="Toggle theme view"
            >
              {theme === "dark" ? (
                <Sun className="w-4 h-4 text-amber-400" />
              ) : (
                <Moon className="w-4 h-4 text-slate-700" />
              )}
            </button>

            <div className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border ${feedIsFresh ? "bg-emerald-500/10 border-emerald-500/20" : "bg-amber-500/10 border-amber-500/20"}`}>
              <div className="relative">
                <div className={`w-1.5 h-1.5 rounded-full ${feedIsFresh ? "bg-emerald-500" : "bg-amber-500"}`} />
                {feedIsFresh && <div className="absolute inset-0 w-1.5 h-1.5 rounded-full bg-emerald-500 animate-ping" />}
              </div>
              <span className={`text-[10px] font-bold tracking-wider ${feedIsFresh ? "text-emerald-600 dark:text-emerald-400" : "text-amber-700 dark:text-amber-300"}`}>{feedState}</span>
            </div>
          </div>
        </div>
      </header>

      {/* Stats Bar */}
      <div className="max-w-7xl mx-auto px-4 sm:px-6 pt-5">
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <StatCard icon={Activity} label="Active Paths" value={visibleArbs.length} color="emerald" />
          <StatCard icon={TrendingUp} label="Best Margin" value={visibleArbs[0] ? `${(Math.max(0, Number(visibleArbs[0].margin) || 0) * 100).toFixed(2)}%` : "—"} color="sky" />
          <StatCard icon={Layers} label="Avg Value" value={`${avgMargin.toFixed(2)}%`} color="amber" />
          <StatCard icon={TrendingUp} label="Best Path Profit" value={`+${fmt(bestPathProfit)}`} color="emerald" />
        </div>
      </div>

      {/* Main Grid */}
      <main className="max-w-7xl mx-auto px-4 sm:px-6 py-5 grid grid-cols-1 lg:grid-cols-3 gap-5">
        {/* Left: Arb Table */}
        <div className="lg:col-span-2 space-y-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <h2 className="text-foreground/90 font-semibold text-sm">Identified Arbitrage Margins</h2>
            <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
              <label className="flex items-center gap-2 text-[11px] text-muted-foreground">Show from
              <select value={minimumMargin} onChange={(e) => setMinimumMargin(Number(e.target.value))} className="bg-card border border-border text-foreground text-[11px] rounded-lg px-2 py-1 focus:outline-none focus:ring-1 focus:ring-emerald-500/20 shadow-xs">
                <option value={0}>All positive</option><option value={0.5}>0.5%</option><option value={1}>1%</option><option value={2}>2%</option><option value={5}>5%</option>
              </select>
              </label>
              <label className="flex items-center gap-2 text-[11px] text-muted-foreground">Adjust increments
              <select
                value={roundTo}
                onChange={(e) => setRoundTo(Number(e.target.value))}
                className="bg-card border border-border text-foreground text-[11px] rounded-lg px-2 py-1 focus:outline-none focus:ring-1 focus:ring-emerald-500/20 shadow-xs"
              >
                <option value={50}>50</option>
                <option value={100}>100</option>
                <option value={500}>500</option>
                <option value={1000}>1,000</option>
              </select>
              </label>
            </div>
          </div>
          
          <ArbTable 
            arbs={visibleArbs} 
            bankroll={bankroll} 
            roundTo={roundTo} 
          />
          <section className="rounded-xl border border-border bg-card p-4 text-xs text-muted-foreground">
            <div className="flex items-center gap-2 font-semibold text-foreground"><Info className="h-4 w-4 text-sky-500" />Engine diagnostics</div>
            <p className="mt-2">{diagnostics.reason || "Waiting for the first engine scan."}</p>
            <p className="mt-1 text-[11px]">{diagnostics.oddsRows ?? 0} verified odds · {diagnostics.sources?.length ?? 0} sources · {diagnostics.matchingMarkets ?? 0} matching markets</p>
          </section>
          <details className="rounded-xl border border-border bg-card p-4 text-xs text-muted-foreground">
            <summary className="cursor-pointer font-semibold text-foreground">Fixture and odds safeguards · {complianceChecks.filter((check) => check.status === "missing").length} gaps</summary>
            <p className="mt-1 text-[11px]">Review of the virtual-event filtering and data-quality criteria you listed. “Partial” means a limited check exists, not full verification.</p>
            <div className="mt-3 space-y-2">
              {complianceChecks.map((check) => <div className="flex items-start justify-between gap-3 border-t border-border/60 pt-2" key={check.name}>
                <div className="min-w-0"><div className="font-medium text-foreground">{check.name}</div><div className="mt-0.5">{check.detail}</div></div>
                <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold ${check.status === "implemented" ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400" : check.status === "partial" ? "bg-amber-500/10 text-amber-600 dark:text-amber-400" : "bg-rose-500/10 text-rose-600 dark:text-rose-400"}`}>{check.status}</span>
              </div>)}
              {!complianceChecks.length && <p>Safeguard status will appear after the next engine scan.</p>}
            </div>
          </details>
          <section className="rounded-xl border border-border bg-card p-4 text-xs text-muted-foreground">
            <h3 className="font-semibold text-foreground">Bookmaker feed families</h3>
            <p className="mt-1 text-[11px]">Feeds grouped by the collection technology they share.</p>
            <div className="mt-3 grid gap-2 sm:grid-cols-2">
              {sourceFamilies.map((family) => <div className="rounded-lg border border-border/70 bg-muted/20 p-2.5" key={family.name}>
                <div className="font-medium text-foreground">{family.name}</div>
                <div className="mt-1 break-words text-[11px]">{family.bookmakers.join(", ")}</div>
              </div>)}
              {!sourceFamilies.length && <p>Feed family details appear when the engine has current bookmaker data.</p>}
            </div>
          </section>
          <details className="rounded-xl border border-border bg-card p-4 text-xs text-muted-foreground">
            <summary className="cursor-pointer font-semibold text-foreground">Collector health</summary>
            <p className="mt-1 text-[11px]">Updated {collectorHealth.updatedAt ? new Date(collectorHealth.updatedAt).toLocaleString() : "after the next collection cycle"}. Feeds are retried on the normal refresh schedule.</p>
            <div className="mt-3 space-y-2">
              {Object.entries(collectorHealth.bookmakers || {}).map(([site, status]) => <div className="flex items-start justify-between gap-3 border-t border-border/60 pt-2" key={site}>
                <div className="min-w-0"><div className="font-medium text-foreground">{site} <span className="font-normal text-muted-foreground">· {status.family}</span></div>
                  <div className="mt-0.5 text-[10px]">{status.eventCount ?? 0} events · {status.marketCount ?? 0} markets{status.lastAttemptAt ? ` · checked ${new Date(status.lastAttemptAt).toLocaleTimeString()}` : ""}</div>
                  {status.error && <div className="mt-1 break-words text-rose-600 dark:text-rose-400">{status.error}</div>}
                </div>
                <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold ${status.status === "ok" ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400" : status.status === "error" ? "bg-rose-500/10 text-rose-600 dark:text-rose-400" : "bg-amber-500/10 text-amber-600 dark:text-amber-400"}`}>{status.status || "unknown"}</span>
              </div>)}
              {!Object.keys(collectorHealth.bookmakers || {}).length && <p>Health details will appear after the next collector cycle.</p>}
            </div>
          </details>
          {nearMisses.length > 0 && <section className="rounded-xl border border-amber-500/20 bg-card p-4 text-xs text-muted-foreground">
            <div className="font-semibold text-foreground">Closest margin opportunities</div>
            <p className="mt-1 text-[11px]">Top cross-book paths that are still below break-even.</p>
            <div className="mt-3 space-y-2">{nearMisses.slice(0, 10).map((arb, index) => <div className="flex items-center justify-between gap-3" key={`${arb.id}-${index}`}><span className="truncate">{arb.match} · {arb.market}</span><span className="shrink-0 font-semibold text-amber-600 dark:text-amber-400">{Number(arb.profit).toFixed(2)}%</span></div>)}</div>
          </section>}
        </div>

        {/* Right: Bankroll + Odds Feed */}
        <div className="space-y-5">
          <BankrollPanel
            bankroll={bankroll}
            onBankrollChange={setBankroll}
            arbs={visibleArbs}
            avgMargin={avgMargin}
          />
          <OddsFeed oddsChanges={oddsChanges} />
        </div>
      </main>

      {/* Margin Chart */}
      <div className="max-w-7xl mx-auto px-4 sm:px-6 pb-8">
        <MarginChart marginHistory={marginHistory} />
        <section className="mt-5 rounded-xl border border-border bg-card p-4 shadow-xs">
          <h2 className="text-sm font-semibold text-foreground">Recently observed arbitrage</h2>
          <p className="mt-1 text-[10px] text-muted-foreground">Saved opportunity snapshots, including odds and margins that later changed or disappeared.</p>
          <div className="mt-3 space-y-2">
            {[...opportunityHistory].slice(-30).reverse().map((entry, index) => {
              const lastSeenMs = entry.lastSeenAt ? new Date(entry.lastSeenAt).getTime() : 0;
              const stillRecent = lastSeenMs && now - lastSeenMs <= 90_000;
              return <article key={`${entry.key}-${index}`} className="rounded-lg border border-border/70 bg-muted/20 p-3">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div className="min-w-0"><div className="break-words text-xs font-medium text-foreground">{entry.match}</div><div className="mt-0.5 text-[11px] text-muted-foreground">{[entry.sport, entry.market].filter(Boolean).join(" · ")}</div></div>
                <div className="shrink-0 text-right"><div className="flex items-center justify-end gap-2"><span className={`rounded px-1.5 py-0.5 text-[9px] font-bold ${stillRecent ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400" : "bg-muted text-muted-foreground"}`}>{stillRecent ? "RECENT" : "EXPIRED"}</span><span className="text-xs font-semibold text-emerald-600 dark:text-emerald-400">{Number(entry.margin).toFixed(2)}%</span></div><div className="mt-1 text-[10px] text-muted-foreground">Last seen {entry.lastSeenAt ? new Date(entry.lastSeenAt).toLocaleString() : "—"}</div></div>
              </div>
              <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[10px] text-muted-foreground">{(entry.selections || []).map((leg, legIndex) => <span key={`${leg.site}-${leg.selection}-${legIndex}`}>{leg.site}: {leg.selection} @ {Number(leg.odds).toFixed(2)}</span>)}</div>
            </article>;
            })}
            {!opportunityHistory.length && <p className="py-5 text-center text-xs text-muted-foreground">No opportunities have been recorded yet. History starts with the next verified scan.</p>}
          </div>
        </section>
      </div>

      <footer className="max-w-7xl mx-auto px-4 sm:px-6 pb-8 border-t border-border/40 pt-4">
        <p className="text-[10px] text-muted-foreground/60 text-center">
          Odds and arbitrage paths are shown only when collectors provide complete, identified bookmaker markets. No bets are placed by this dashboard.
        </p>
      </footer>
    </div>
  );
}

function StatCard({ icon: Icon, label, value, color }) {
  const colors = {
    emerald: "text-emerald-600 bg-emerald-500/10 dark:text-emerald-400",
    sky: "text-sky-600 bg-sky-500/10 dark:text-sky-400",
    amber: "text-amber-600 bg-amber-500/10 dark:text-amber-400",
  };
  return (
    <div className="bg-card border border-border rounded-xl p-3.5 flex items-center gap-3 shadow-xs">
      <div className={`w-9 h-9 rounded-lg flex items-center justify-center shrink-0 ${colors[color]}`}>
        <Icon className="w-4 h-4" />
      </div>
      <div className="min-w-0">
        <div className="text-[10px] text-muted-foreground truncate">{label}</div>
        <div className="text-foreground font-bold text-sm sm:text-base truncate">{value}</div>
      </div>
    </div>
  );
}
