import { useState } from "react";

export function useTheme() {
  const [darkMode, setDarkMode] = useState(false);

  const toggleTheme = () => {
    const next = !darkMode;
    setDarkMode(next);
    document.body.classList.toggle("dark", next);
  };

  return { darkMode, toggleTheme };
}
