// UI language: English (default) or Simplified Chinese.
//
// Every user-visible string is written inline as a pair, `tr("中文", "English")`,
// next to where it is used. There is no shared dictionary file: a pair cannot go
// out of sync with its call site, and files can be translated independently.
//
// The language is read once per page load. Switching calls `setLang`, which stores
// the choice and reloads the page, so module-level constants built with `tr()` are
// always in the active language and no component needs to subscribe to a store.
//
// Outside a browser (the `node --test` unit tests) there is no stored choice and
// `tr()` returns the Chinese text, which is what the existing test fixtures pin.

export type Lang = "en" | "zh";

export const LANG_STORAGE_KEY = "mast.lang";
export const DEFAULT_LANG: Lang = "en";

function detectLang(): Lang {
  if (typeof window === "undefined") return "zh";
  try {
    const v = window.localStorage.getItem(LANG_STORAGE_KEY);
    if (v === "en" || v === "zh") return v;
  } catch {
    /* storage unavailable: fall through to the default */
  }
  return DEFAULT_LANG;
}

const current: Lang = detectLang();

// The backend picks the language of the text it returns (error messages, status
// lines, initialisation steps) from this cookie, which every same-origin request
// carries — fetch, EventSource and WebSocket alike.
export const LANG_COOKIE = "mast_lang";
if (typeof document !== "undefined") {
  try {
    document.cookie = `${LANG_COOKIE}=${current}; path=/; max-age=31536000; SameSite=Lax`;
  } catch {
    /* cookies disabled: the backend falls back to its own default */
  }
}

export function getLang(): Lang {
  return current;
}

/** Pick the string for the active language. */
export function tr(zh: string, en: string): string {
  return current === "zh" ? zh : en;
}

/** Store the language and reload so every string is rebuilt in it. */
export function setLang(lang: Lang): void {
  if (lang === current) return;
  try {
    window.localStorage.setItem(LANG_STORAGE_KEY, lang);
  } catch {
    /* without storage the choice cannot survive a reload */
    return;
  }
  window.location.reload();
}

/** BCP-47 tag for <html lang> and Intl formatters. */
export function langTag(): string {
  return current === "zh" ? "zh-CN" : "en";
}
