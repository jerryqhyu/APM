// Light, dark, or whatever the system is set to. The choice is remembered in this browser.

import { useEffect, useState } from "react";

export type Theme = "system" | "light" | "dark";
const KEY = "apm-theme";

function stored(): Theme {
  try {
    const value = localStorage.getItem(KEY);
    return value === "light" || value === "dark" ? value : "system";
  } catch {
    return "system";
  }
}

function apply(theme: Theme): void {
  if (theme === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
}

apply(stored()); // before the first paint, so the page never flashes the wrong theme

export function useTheme(): [Theme, (theme: Theme) => void] {
  const [theme, setTheme] = useState<Theme>(stored);
  useEffect(() => {
    apply(theme);
    try {
      if (theme === "system") localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, theme);
    } catch {
      // private browsing: the choice lasts for this page only
    }
  }, [theme]);
  return [theme, setTheme];
}
