import { useState, useEffect } from "react";

export function useArbEngine() {
  const [now, setNow] = useState(Date.now());
  // Production serves the dashboard and API from one origin. Vite proxies the
  // same paths to the local API during development.
  const apiBase = import.meta.env.VITE_API_URL || "";
  const [data, setData] = useState({
    arbs: [],
    oddsChanges: [],
    marginHistory: [],
    sourceCount: 0,
    isLive: false,
  });

  useEffect(() => {
    const stream = new EventSource(`${apiBase}/engine/live`);

    stream.addEventListener("engine", (event) => {
      try {
        setData({ ...JSON.parse(event.data), isLive: true });
      } catch (error) {
        console.error("Error parsing live engine data:", error);
      }
    });

    stream.onerror = () => {
      setData((current) => ({ ...current, isLive: false }));
    };

    return () => stream.close();
  }, [apiBase]);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  return { ...data, now };
}
