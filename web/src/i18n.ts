import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import enUS from './locales/en-US.json';
import zhCN from './locales/zh-CN.json';
import { detectLocale } from './lib/locale';

// Locale resolution (cookie → browser language → English) lives in ./lib/locale,
// shared with the cookie helpers that components use. The cross-tab `storage`
// listener was removed along with localStorage-based locale: locale now rides a
// shared cookie (readable server-side, unlike localStorage); other tabs adopt a
// change on their next navigation.
//
// The document's language follows the app's, so the browser shapes CJK text
// as Chinese and `:lang()` rules can key on it. Bound before init, which
// reports the first language through the same event.
i18n.on('languageChanged', (lng) => {
  if (typeof document !== 'undefined') document.documentElement.lang = lng;
});

i18n.use(initReactI18next).init({
  resources: {
    'en-US': { translation: enUS },
    'zh-CN': { translation: zhCN },
  },
  lng: detectLocale(),
  fallbackLng: 'en-US',
  interpolation: { escapeValue: false },
});

export default i18n;
