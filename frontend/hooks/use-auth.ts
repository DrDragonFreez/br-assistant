'use client';

/**
 * Состояние аутентификации, общее для всех экранов.
 *
 * Хранилище на zustand, а не React-контекст: профиль нужен и в чате,
 * и в админке, и в заголовке, а оборачивать всё приложение провайдером
 * ради одного объекта незачем.
 *
 * Токены здесь не лежат — ими занимается lib/auth.ts (access в памяти,
 * refresh в localStorage). Этот слой отвечает только за профиль и права.
 */

import { create } from 'zustand';

import { ApiError, auth } from '@/lib/api';
import { cacheUser, clearTokens, getCachedUser, getRefreshToken } from '@/lib/auth';
import type { CurrentUser } from '@/lib/types';

interface AuthState {
  user: CurrentUser | null;
  /** Профиль ещё не запрашивался — интерфейс показывает скелет. */
  loading: boolean;
  error: string;
  /** Вход по логину и паролю. */
  login: (email: string, password: string) => Promise<void>;
  /** Выход с отзывом токена на сервере. */
  logout: () => Promise<void>;
  /**
   * Восстанавливает сессию при открытии страницы: access-токен живёт
   * в памяти вкладки и после перезагрузки теряется, но refresh остаётся.
   */
  restore: () => Promise<void>;
  /** Проверка права; оболочка над profile.permissions. */
  can: (permission: string) => boolean;
}

export const useAuth = create<AuthState>((set, get) => ({
  user: getCachedUser(),
  loading: true,
  error: '',

  async login(email: string, password: string) {
    set({ loading: true, error: '' });
    try {
      await auth.login(email, password);
      const user = await auth.me();
      cacheUser(user);
      set({ user, loading: false, error: '' });
    } catch (error) {
      set({
        user: null,
        loading: false,
        error: error instanceof Error ? error.message : 'Не удалось войти',
      });
      throw error;
    }
  },

  async logout() {
    try {
      await auth.logout();
    } finally {
      clearTokens();
      set({ user: null, loading: false, error: '' });
    }
  },

  async restore() {
    if (!getRefreshToken()) {
      set({ user: null, loading: false });
      return;
    }
    try {
      const user = await auth.me();
      cacheUser(user);
      set({ user, loading: false, error: '' });
    } catch (caught) {
      // Сессию закрываем только если сервер отверг токен. Недоступность
      // сервиса, превышенный лимит запросов и обрыв сети — временные
      // помехи: показываем сохранённый профиль, а права всё равно
      // проверяет бэкенд на каждом запросе.
      const rejected = caught instanceof ApiError && caught.isAuthError;
      if (!rejected) {
        set({ user: getCachedUser(), loading: false });
        return;
      }
      clearTokens();
      set({ user: null, loading: false });
    }
  },

  can(permission: string) {
    return Boolean(get().user?.permissions.includes(permission));
  },
}));
