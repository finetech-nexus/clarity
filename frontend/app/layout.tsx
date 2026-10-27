import "./globals.css";
import type { ReactNode } from "react";

export const metadata = {
  title: "ClarityCheck",
  description: "KYC, KYB, AML et conformité",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="fr">
      <body>{children}</body>
    </html>
  );
}
