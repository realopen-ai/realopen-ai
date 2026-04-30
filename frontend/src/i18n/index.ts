import en from "./locales/en.json";
import frRaw from "./locales/fr.json";

// Base schema (source of truth)
type BaseTranslations = typeof en;

// Enforce structure
const fr: BaseTranslations = frRaw;

export const translations = { en, fr };

export type Language = keyof typeof translations;
export type TranslationKey = keyof typeof en;
