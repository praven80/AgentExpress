import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  build: {
    // Both IaC paths upload this directory. Keeping it inside web/ means the
    // Terraform `aws_s3_object` set and CDK's `Source.asset` point at one place.
    outDir: "dist",
    emptyOutDir: true,
    // Hashed asset names so a CloudFront cache cannot serve a stale bundle against
    // a fresh index.html. index.html itself is uploaded with no-cache by the IaC.
    assetsInlineLimit: 0,
    sourcemap: false,
  },
  server: {
    port: 5173,
    // `npm run dev` against a deployed API: the BFF is on a different origin, so the
    // dev server proxies /api to it rather than fighting CORS.
    proxy: process.env.VITE_API_BASE
      ? { "/api": { target: process.env.VITE_API_BASE, changeOrigin: true } }
      : undefined,
  },
  test: {
    environment: "jsdom",
    globals: true,
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
} as never);
