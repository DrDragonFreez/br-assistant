import { type ClassValue, clsx } from 'clsx';
import { twMerge } from 'tailwind-merge';

/**
 * Объединяет классы Tailwind, разрешая конфликты.
 * Обязателен для shadcn/ui.
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

/** Форматирует размер файла в человекочитаемый вид. */
export function formatFileSize(bytes: number): string {
  if (bytes === 0) return '0 Б';
  const units = ['Б', 'КБ', 'МБ', 'ГБ'];
  const index = Math.floor(Math.log(bytes) / Math.log(1024));
  const value = bytes / Math.pow(1024, index);
  return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

/** Форматирует длительность в миллисекундах. */
export function formatLatency(ms: number): string {
  if (ms < 1000) return `${ms} мс`;
  return `${(ms / 1000).toFixed(1)} с`;
}

/**
 * Склоняет существительное по числу.
 * @example plural(5, ['пункт', 'пункта', 'пунктов']) → 'пунктов'
 */
export function plural(count: number, forms: [string, string, string]): string {
  const absolute = Math.abs(count) % 100;
  const remainder = absolute % 10;
  if (absolute > 10 && absolute < 20) return forms[2];
  if (remainder > 1 && remainder < 5) return forms[1];
  if (remainder === 1) return forms[0];
  return forms[2];
}

/** Форматирует дату в коротком виде для списков. */
export function formatDate(iso: string | null): string {
  if (!iso) return '—';
  const date = new Date(iso);
  const now = new Date();
  const isToday = date.toDateString() === now.toDateString();

  if (isToday) {
    return date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  }
  return date.toLocaleDateString('ru-RU', {
    day: '2-digit',
    month: 'short',
    year: date.getFullYear() === now.getFullYear() ? undefined : 'numeric',
  });
}

/** Форматирует дату и время полностью. */
export function formatDateTime(iso: string | null): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

/** Переводит код роли в название на русском. */
export const ROLE_LABELS: Record<string, string> = {
  employee: 'Сотрудник',
  methodologist: 'Методолог ЦДО',
  auditor: 'Аудитор',
  admin: 'Администратор',
};

/** Переводит гриф доступа в название на русском. */
export const ACCESS_LEVEL_LABELS: Record<string, string> = {
  public: 'Общедоступно',
  internal: 'Для внутреннего пользования',
  confidential: 'Ограниченный доступ',
};

/** Переводит тип документа базы знаний. */
export const DOC_TYPE_LABELS: Record<string, string> = {
  instruction: 'Инструкция',
  regulation: 'Регламент',
  matrix: 'Матрица согласования',
  template_guide: 'Рекомендации по шаблонам',
  faq: 'Часто задаваемые вопросы',
  other: 'Иное',
};

/** Переводит статус индексации редакции. */
export const VERSION_STATUS_LABELS: Record<string, string> = {
  pending: 'Ожидает индексации',
  indexing: 'Индексируется',
  indexed: 'Проиндексирована',
  failed: 'Ошибка индексации',
  archived: 'В архиве',
};

/** Переводит вердикт проверки документа. */
export const VERDICT_LABELS: Record<string, string> = {
  pending: 'Проверяется',
  compliant: 'Соответствует требованиям',
  minor_issues: 'Незначительные замечания',
  major_issues: 'Требует доработки',
  failed: 'Проверка не выполнена',
};

/** Переводит уровень критичности замечания. */
export const SEVERITY_LABELS: Record<string, string> = {
  critical: 'Критично',
  major: 'Существенно',
  minor: 'Замечание',
};

/** Ограничивает строку по длине, добавляя многоточие. */
export function truncate(value: string, max: number): string {
  if (value.length <= max) return value;
  return `${value.slice(0, max - 1)}…`;
}

/** Определяет цветовой класс по проценту релевантности. */
export function relevanceColor(percent: number): string {
  if (percent >= 75) return 'text-success';
  if (percent >= 50) return 'text-warning';
  return 'text-muted-foreground';
}
