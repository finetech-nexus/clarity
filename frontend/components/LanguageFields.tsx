"use client";

export const LANGUAGES = [
  { value: "fr", label: "Français" },
  { value: "en", label: "English" },
  { value: "ar", label: "العربية" },
] as const;

export type Lang = (typeof LANGUAGES)[number]["value"];
export type Copy = Record<Lang, string>;

export function emptyCopy(legacy = ""): Copy {
  return { fr: legacy, en: "", ar: "" };
}

export function copyFrom(value: Partial<Copy> | undefined, legacy = ""): Copy {
  return {
    fr: value?.fr || legacy,
    en: value?.en || "",
    ar: value?.ar || "",
  };
}

export function preferred(copy: Copy, legacy = ""): string {
  return copy.fr || copy.en || copy.ar || legacy;
}
