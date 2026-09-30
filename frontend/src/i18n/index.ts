import en from "./locales/en.json";
import frRaw from "./locales/fr.json";
import arRaw from "./locales/ar.json";

// Base schema (source of truth)
type BaseTranslations = typeof en;

// Enforce structure
const fr: BaseTranslations = frRaw;
const ar: BaseTranslations = arRaw;

export const translations = { en, fr, ar };

export type Language = keyof typeof translations;
export type TranslationKey = keyof typeof en;
