"use client";

/**
 * Theme = light | dark | system, remembered in localStorage.
 *
 * next-themes does the one thing that is hard to do by hand: it injects an
 * inline <script> that sets `.dark` on <html> before the first paint, so a
 * dark-mode reader never sees a white flash (see Next's "preventing flash
 * before hydration" guide). `<html suppressHydrationWarning>` in layout.tsx is
 * the matching half — the class the script added is not in React's output.
 */

import { ThemeProvider as NextThemesProvider } from "next-themes";

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  return (
    <NextThemesProvider attribute="class" defaultTheme="system" enableSystem disableTransitionOnChange>
      {children}
    </NextThemesProvider>
  );
}
