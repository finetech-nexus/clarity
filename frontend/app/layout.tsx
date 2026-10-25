import "./globals.css";
import type { ReactNode } from "react";

export const metadata = {
  title: "Clarity",
  description: "Plateforme bancaire KYC et AML",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="fr">
      <body>{children}</body>
    </html>
  );
}
