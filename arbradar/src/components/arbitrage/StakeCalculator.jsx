import React from "react";
import { Coins } from "lucide-react";

const OUTCOME_LABELS = { "1": "Home Win (1)", X: "Draw (X)", "2": "Away Win (2)" };

function fmt(n) {
  return n.toLocaleString("en-US", { maximumFractionDigits: 0 });
}

export default function StakeCalculator({ arb, bankroll, roundTo }) {
  const calc = React.useMemo(
    () => calculateStakesLocal(arb, bankroll, roundTo),
    [arb, bankroll, roundTo]
  );

  return (
    <div>
      <div className="flex items-center gap-2 mb-3">
        <Coins className="w-4 h-4 text-amber-500 dark:text-amber-400" />
        <h4 className="text-foreground font-semibold text-sm">Stake Distribution</h4>
        <span className="text-[10px] text-muted-foreground ml-auto">
          Rounded to Multiples of {roundTo}
        </span>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-2.5">
        {["1", "X", "2"].map((o) => {
          const s = calc.stakes[o];
          return (
            <div key={o} className="bg-card border border-border rounded-xl p-3 shadow-xs">
              <div className="flex items-center justify-between mb-1.5">
                <span className="text-[11px] font-medium text-muted-foreground">{OUTCOME_LABELS[o]}</span>
                <span className="text-[10px] text-muted-foreground/70">@ {s.odd.toFixed(2)}</span>
              </div>
              <div className="text-[10px] text-muted-foreground/60 mb-2 truncate">{s.bookmaker.name}</div>
              <div className="flex items-baseline gap-1.5">
                <span className="text-foreground font-bold text-lg">{fmt(s.rounded)}</span>
                <span className="text-[10px] text-muted-foreground/40 line-through">{fmt(s.raw)}</span>
              </div>
            </div>
          );
        })}
      </div>

      <div className="grid grid-cols-3 gap-2.5 mt-3">
        <div className="bg-card border border-border rounded-xl p-3 shadow-xs">
          <div className="text-[10px] text-muted-foreground mb-1">Total Placed</div>
          <div className="text-foreground font-bold text-sm">{fmt(calc.totalStake)}</div>
        </div>
        <div className="bg-card border border-border rounded-xl p-3 shadow-xs">
          <div className="text-[10px] text-muted-foreground mb-1">Guaranteed Return</div>
          <div className="text-sky-600 dark:text-sky-400 font-bold text-sm">{fmt(calc.minReturn)}</div>
        </div>
        <div className="bg-emerald-500/5 border border-emerald-500/20 rounded-xl p-3">
          <div className="text-[10px] text-emerald-600/70 dark:text-emerald-500/70 mb-1">Net Profit</div>
          <div className="text-emerald-600 dark:text-emerald-400 font-bold text-sm">
            +{fmt(calc.profit)} <span className="text-[10px] opacity-80">({calc.profitPct.toFixed(2)}%)</span>
          </div>
        </div>
      </div>
    </div>
  );
}

function calculateStakesLocal(arb, bankroll, roundTo) {
  const totalInv = arb.invSum;
  const stakes = {};
  ["1", "X", "2"].forEach((o) => {
    const proportion = (1 / arb.best[o].odd) / totalInv;
    const raw = bankroll * proportion;
    const rounded = Math.max(roundTo, Math.round(raw / roundTo) * roundTo);
    stakes[o] = { raw, rounded, bookmaker: arb.best[o].bookmaker, odd: arb.best[o].odd };
  });
  const totalStake = stakes["1"].rounded + stakes.X.rounded + stakes["2"].rounded;
  const returns = {
    "1": stakes["1"].rounded * stakes["1"].odd,
    X: stakes.X.rounded * stakes.X.odd,
    "2": stakes["2"].rounded * stakes["2"].odd,
  };
  const minReturn = Math.min(returns["1"], returns.X, returns["2"]);
  const profit = minReturn - totalStake;
  return { stakes, totalStake, minReturn, profit, profitPct: (profit / totalStake) * 100 };
}