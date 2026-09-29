import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Fleet monitor",
  description: "Live view of the distributed monitoring platform",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
