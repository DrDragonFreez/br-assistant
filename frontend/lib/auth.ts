/**
 * Хранение токенов и состояние аутентификации.
 *
 * Access-токен живёт в памяти (не в localStorage): это снижает риск кражи
 * через XSS. Refresh-токен кладётся в localStorage — иначе пользователю
 * пришлось бы входить заново при каждом обновлении страницы. Такой
 * компромисс стандартен для SPA без серверных сессий.
 *
 * В корпоративном контуре правильнее хранить refresh в httpOnly-куке,
 * но это требует общего домена для фронта и бэка — в прод-варианте
 * за Nginx так и будет.
 */

import type { CurrentUser, TokenResponse } from './types';

const REFRESH_KEY = 'br_refresh_token';
const USER_KEY = 'br_user';

/** Access-токен в памяти процесса. */
let accessToken: string | null = null;
/** Момент истечения access-токена (unix ms). */
let expiresAt = 0;

export function setTokens(tokens: TokenResponse): void {
  accessToken = tokens.access_token;
  // Обновляем за 60 секунд до истечения, чтобы не ловить 401 в полёте.
  expiresAt = Date.now() + tokens.expires_in * 1000 - 60_000;

  if (typeof window !== 'undefined') {
    try {
      window.localStorage.setItem(REFRESH_KEY, tokens.refresh_token);
    } catch {
      // Приватный режим браузера: работаем без сохранения сессии.
    }
  }
}

export function getAccessToken(): string | null {
  return accessToken;
}

export function isAccessTokenExpired(): boolean {
  return !accessToken || Date.now() >= expiresAt;
}

export function getRefreshToken(): string | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage.getItem(REFRESH_KEY);
  } catch {
    return null;
  }
}

export function clearTokens(): void {
  accessToken = null;
  expiresAt = 0;
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.removeItem(REFRESH_KEY);
    window.localStorage.removeItem(USER_KEY);
  } catch {
    // Нечего очищать.
  }
}

/**
 * Кеширует профиль пользователя.
 * Нужно, чтобы интерфейс не мигал при первой загрузке, пока идёт /auth/me.
 */
export function cacheUser(user: CurrentUser): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(USER_KEY, JSON.stringify(user));
  } catch {
    // Кеш необязателен.
  }
}

export function getCachedUser(): CurrentUser | null {
  if (typeof window === 'undefined') return null;
  try {
    const raw = window.localStorage.getItem(USER_KEY);
    return raw ? (JSON.parse(raw) as CurrentUser) : null;
  } catch {
    return null;
  }
}

/** Проверяет наличие права у пользователя. */
export function hasPermission(user: CurrentUser | null, permission: string): boolean {
  return Boolean(user?.permissions.includes(permission));
}

/** Проверяет принадлежность к одной из ролей. */
export function hasRole(user: CurrentUser | null, ...roles: string[]): boolean {
  return Boolean(user && roles.includes(user.role));
}

/**
 * Признак того, что пользователь вошёл.
 * Наличие refresh-токена достаточно: access восстановится автоматически.
 */
export function isAuthenticated(): boolean {
  return Boolean(accessToken || getRefreshToken());
}
