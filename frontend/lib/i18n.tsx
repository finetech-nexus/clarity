"use client";

import { createContext, ReactNode, useContext, useEffect, useMemo, useState } from "react";

export type Locale = "fr" | "en" | "ar";

const STORAGE_KEY = "clarity_locale";

const MESSAGES: Record<Locale, Record<string, string>> = {
  fr: {
    "nav.kyc": "KYC",
    "nav.kyb": "KYB",
    "nav.aml": "AML",
    "nav.settings": "Paramétrage",
    "nav.overview": "Vue d'ensemble",
    "nav.customers": "Clients",
    "nav.sessions": "Sessions",
    "nav.verification": "Vérification",
    "nav.businesses": "Clients",
    "nav.screening": "Criblage",
    "nav.risk": "Risque",
    "nav.transactions": "Transactions",
    "nav.alerts": "Alertes",
    "nav.investigations": "Investigations",
    "nav.reviews": "Revues",
    "nav.reports": "Rapports",
    "nav.rules": "Scénarios",
    "nav.workflows": "Parcours",
    "nav.audit": "Audit",
    "nav.integrations": "Intégrations",
    "nav.administration": "Administration",
    "nav.quit": "Quitter",
    "lang.label": "Langue",
    "verify.title": "Vérification",
    "verify.lede": "Dossier de vérification : informations saisies, pièces et données lues sur les documents.",
    "verify.queue": "Liste des dossiers",
    "verify.detail": "Détail du dossier",
    "verify.prev": "Précédent",
    "verify.next": "Suivant",
    "verify.score": "Score de risque",
    "verify.score.breakdown": "Détail du calcul",
    "verify.score.empty": "Score non calculé",
    "verify.score.click": "Cliquer pour voir le détail du calcul",
    "verify.cases": "dossier(s)",
    "verify.page": "page",
    "admin.purgeReseed": "Purger et recharger les exemples",
    "admin.purgeReseed.help": "Supprime clients et vérifications, puis recharge des scénarios de score variés.",
  },
  en: {
    "nav.kyc": "KYC",
    "nav.kyb": "KYB",
    "nav.aml": "AML",
    "nav.settings": "Settings",
    "nav.overview": "Overview",
    "nav.customers": "Customers",
    "nav.sessions": "Sessions",
    "nav.verification": "Verification",
    "nav.businesses": "Businesses",
    "nav.screening": "Screening",
    "nav.risk": "Risk",
    "nav.transactions": "Transactions",
    "nav.alerts": "Alerts",
    "nav.investigations": "Investigations",
    "nav.reviews": "Reviews",
    "nav.reports": "Reports",
    "nav.rules": "Scenarios",
    "nav.workflows": "Workflows",
    "nav.audit": "Audit",
    "nav.integrations": "Integrations",
    "nav.administration": "Administration",
    "nav.quit": "Sign out",
    "lang.label": "Language",
    "verify.title": "Verification",
    "verify.lede": "Verification case: submitted details, documents and data read from ID papers.",
    "verify.queue": "Case list",
    "verify.detail": "Case detail",
    "verify.prev": "Previous",
    "verify.next": "Next",
    "verify.score": "Risk score",
    "verify.score.breakdown": "Score breakdown",
    "verify.score.empty": "Score not calculated",
    "verify.score.click": "Click to see the score breakdown",
    "verify.cases": "case(s)",
    "verify.page": "page",
    "admin.purgeReseed": "Purge and reload examples",
    "admin.purgeReseed.help": "Removes customers and verifications, then reloads varied risk-score scenarios.",
  },
  ar: {
    "nav.kyc": "KYC",
    "nav.kyb": "KYB",
    "nav.aml": "AML",
    "nav.settings": "الإعدادات",
    "nav.overview": "نظرة عامة",
    "nav.customers": "العملاء",
    "nav.sessions": "الجلسات",
    "nav.verification": "التحقق",
    "nav.businesses": "الشركات",
    "nav.screening": "الفرز",
    "nav.risk": "المخاطر",
    "nav.transactions": "المعاملات",
    "nav.alerts": "التنبيهات",
    "nav.investigations": "التحقيقات",
    "nav.reviews": "المراجعات",
    "nav.reports": "التقارير",
    "nav.rules": "السيناريوهات",
    "nav.workflows": "المسارات",
    "nav.audit": "التدقيق",
    "nav.integrations": "التكاملات",
    "nav.administration": "الإدارة",
    "nav.quit": "خروج",
    "lang.label": "اللغة",
    "verify.title": "التحقق",
    "verify.lede": "ملف التحقق: البيانات المدخلة والمستندات والبيانات المقروءة.",
    "verify.queue": "قائمة الملفات",
    "verify.detail": "تفاصيل الملف",
    "verify.prev": "السابق",
    "verify.next": "التالي",
    "verify.score": "درجة المخاطر",
    "verify.score.breakdown": "تفصيل الحساب",
    "verify.score.empty": "لم تُحسب الدرجة",
    "verify.score.click": "انقر لعرض تفصيل الحساب",
    "verify.cases": "ملف/ملفات",
    "verify.page": "صفحة",
    "admin.purgeReseed": "مسح وإعادة تحميل الأمثلة",
    "admin.purgeReseed.help": "يحذف العملاء وعمليات التحقق ثم يعيد سيناريوهات درجات مخاطر متنوعة.",
  },
};

type I18nValue = {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  t: (key: string) => string;
};

const I18nContext = createContext<I18nValue | null>(null);

function readLocale(): Locale {
  if (typeof window === "undefined") return "fr";
  const stored = window.localStorage.getItem(STORAGE_KEY);
  if (stored === "fr" || stored === "en" || stored === "ar") return stored;
  return "fr";
}

export function LocaleProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>("fr");

  useEffect(() => {
    setLocaleState(readLocale());
  }, []);

  useEffect(() => {
    document.documentElement.lang = locale;
    document.documentElement.dir = locale === "ar" ? "rtl" : "ltr";
    window.localStorage.setItem(STORAGE_KEY, locale);
  }, [locale]);

  const value = useMemo<I18nValue>(() => ({
    locale,
    setLocale: setLocaleState,
    t: (key: string) => MESSAGES[locale][key] || MESSAGES.fr[key] || key,
  }), [locale]);

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n() {
  const ctx = useContext(I18nContext);
  if (!ctx) throw new Error("useI18n requires LocaleProvider");
  return ctx;
}

export const LOCALES: Array<{ id: Locale; label: string }> = [
  { id: "fr", label: "FR" },
  { id: "en", label: "EN" },
  { id: "ar", label: "AR" },
];
