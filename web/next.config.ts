import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Standalone output: `next build` emits .next/standalone with a server.js and
  // only the node_modules it traced. That is what web/Dockerfile ships — the
  // app, not the toolchain.
  output: "standalone",
  // The dev-only route indicator defaults to bottom-left, exactly where the
  // sidebar footer (theme toggle, engine status) sits; it covered the collapsed
  // theme button. Bottom-right is empty on every page.
  devIndicators: { position: "bottom-right" },
};

export default nextConfig;
