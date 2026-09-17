import type { Metadata } from "next";
import { IBM_Plex_Mono, IBM_Plex_Sans } from "next/font/google";
import "./globals.css";
import { Nav } from "@/components/nav";
import { SignIn } from "@/components/sign-in";
import { ThemeProvider } from "@/components/theme-provider";
import { cn } from "@/lib/utils";


const plexSans = IBM_Plex_Sans({
  variable: "--font-plex-sans",
  subsets: ["latin"],
  weight: ["400", "500", "600"],
});

const plexMono = IBM_Plex_Mono({
  variable: "--font-plex-mono",
  subsets: ["latin"],
  weight: ["400", "500", "600"],
});

export const metadata: Metadata = {
  title: "AuditReady",
  description:
    "Grounded, cited compliance answers over tickets, emails and change records. No citation, no verdict.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    // suppressHydrationWarning: next-themes sets the theme class on <html> from an
    // inline script before React hydrates, so the DOM legitimately differs.
    <html
      lang="en"
      className={cn("h-full", plexSans.variable, plexMono.variable, "font-sans")}
      suppressHydrationWarning
    >
      <body className="min-h-full">
        <ThemeProvider>
          <div className="flex min-h-screen items-start">
            <Nav />
            <main className="min-w-0 flex-1 px-6 py-7 md:px-8 lg:px-10">{children}</main>
          </div>
          {/* Mounted always, visible only when the API asks for a credential. */}
          <SignIn />
        </ThemeProvider>
      </body>
    </html>
  );
}
