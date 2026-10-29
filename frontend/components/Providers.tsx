"use client";

import { ReactNode } from "react";
import { LocaleProvider } from "../lib/i18n";

export default function Providers({ children }: { children: ReactNode }) {
  return <LocaleProvider>{children}</LocaleProvider>;
}
