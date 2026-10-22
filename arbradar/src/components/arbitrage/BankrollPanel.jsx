import React from "react";
import { Wallet } from "lucide-react";

const formatMoney = (value) => new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(value);

export default function BankrollPanel({ bankroll, onBankrollChange, arbs = [], avgMargin }) {
  const topPathProfit = arbs[0] ? bankroll * Math.max(0, Number(arbs[0].margin) || 0) : 0;
  return (
    <section className="rounded-xl border border-border bg-card p-4 shadow-xs">
      <div className="mb-4 flex items-center gap-2"><div className="flex h-8 w-8 items-center justify-center rounded-lg bg-sky-500/10 text-sky-600 dark:text-sky-400"><Wallet className="h-4 w-4" /></div><div><h3 className="text-sm font-semibold text-foreground">Bankroll</h3><p className="text-[10px] text-muted-foreground">Used for return estimates</p></div></div>
      <label className="block text-[11px] font-medium text-muted-foreground" htmlFor="bankroll">Available balance</label>
      <input id="bankroll" type="number" min="0" step="100" value={bankroll} onChange={(event) => onBankrollChange(Math.max(0, Number(event.target.value) || 0))} className="mt-1.5 w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-semibold text-foreground outline-none ring-offset-background focus:ring-2 focus:ring-emerald-500/30" />
      <div className="mt-4 grid grid-cols-2 gap-2.5"><div className="rounded-lg bg-muted/55 p-2.5"><div className="text-[10px] text-muted-foreground">Average margin</div><div className="mt-1 text-sm font-bold text-foreground">{avgMargin.toFixed(2)}%</div></div><div className="rounded-lg bg-emerald-500/10 p-2.5"><div className="text-[10px] text-emerald-700/70 dark:text-emerald-300/70">Top path profit</div><div className="mt-1 text-sm font-bold text-emerald-700 dark:text-emerald-300">+{formatMoney(topPathProfit)}</div></div></div>
    </section>
  );
}
