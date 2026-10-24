'use client';

/**
 * Загрузка проекта документа на проверку.
 *
 * Файл не хранится: бэкенд разбирает его, проверяет и удаляет — в проекте
 * документа могут быть персональные данные и реквизиты, держать их в
 * сервисе незачем. Проверка идёт фоновой задачей, поэтому отчёт
 * опрашивается до появления вердикта.
 */

import { FileUp, Loader2, TriangleAlert, X } from 'lucide-react';
import { useCallback, useRef, useState, type DragEvent } from 'react';

import { DocChecker } from '@/components/documents/DocChecker';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { ApiError, documents } from '@/lib/api';
import type { ValidationReport } from '@/lib/types';
import { cn, formatFileSize } from '@/lib/utils';

/** Форматы, которые принимает загрузчик бэкенда. */
const ACCEPTED = ['.docx', '.pdf', '.txt', '.md'] as const;
/** Предел размера совпадает с MAX_UPLOAD_SIZE_MB на бэкенде. */
const MAX_SIZE_BYTES = 50 * 1024 * 1024;

const POLL_INTERVAL_MS = 1500;
const POLL_ATTEMPTS = 40;

/** Виды документов для подсказки проверяющему. */
const DOC_KINDS: { value: string; label: string }[] = [
  { value: '', label: 'Определить автоматически' },
  { value: 'order', label: 'Приказ' },
  { value: 'direction', label: 'Распоряжение' },
  { value: 'memo', label: 'Служебная записка' },
  { value: 'letter', label: 'Письмо' },
  { value: 'protocol', label: 'Протокол' },
];

export function DocUploader() {
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [docKind, setDocKind] = useState('');
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [report, setReport] = useState<ValidationReport | null>(null);

  const pickFile = useCallback((candidate: File | null): void => {
    setError('');
    setReport(null);

    if (!candidate) {
      setFile(null);
      return;
    }

    const extension = candidate.name.slice(candidate.name.lastIndexOf('.')).toLowerCase();
    if (!ACCEPTED.includes(extension as (typeof ACCEPTED)[number])) {
      setError(`Формат ${extension} не поддерживается. Допустимы: ${ACCEPTED.join(', ')}`);
      setFile(null);
      return;
    }
    if (candidate.size > MAX_SIZE_BYTES) {
      setError(`Файл больше ${formatFileSize(MAX_SIZE_BYTES)}`);
      setFile(null);
      return;
    }

    setFile(candidate);
  }, []);

  const handleDrop = (event: DragEvent<HTMLDivElement>): void => {
    event.preventDefault();
    setDragging(false);
    pickFile(event.dataTransfer.files?.[0] || null);
  };

  /** Опрашивает отчёт до появления вердикта. */
  const waitForReport = useCallback(async (reportId: string): Promise<void> => {
    for (let attempt = 0; attempt < POLL_ATTEMPTS; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
      const current = await documents.validationReport(reportId);
      setReport(current);
      if (current.verdict !== 'pending') return;
    }
    setError('Проверка занимает больше обычного. Отчёт появится в разделе проверок.');
  }, []);

  const submit = async (): Promise<void> => {
    if (!file) return;

    setBusy(true);
    setError('');
    try {
      const accepted = await documents.validate(file, docKind);
      if (accepted.resource_id) {
        await waitForReport(accepted.resource_id);
      }
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Не удалось отправить файл');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-3">
      <div
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
        className={cn(
          'rounded-lg border border-dashed p-4 text-center transition-colors',
          dragging ? 'border-primary bg-primary/5' : 'border-border bg-muted/20',
        )}
      >
        <input
          ref={inputRef}
          type="file"
          accept={ACCEPTED.join(',')}
          className="hidden"
          onChange={(event) => pickFile(event.target.files?.[0] || null)}
        />

        {file ? (
          <div className="flex items-center gap-2 text-left">
            <FileUp className="h-4 w-4 shrink-0 text-primary" />
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium">{file.name}</p>
              <p className="text-xs text-muted-foreground">{formatFileSize(file.size)}</p>
            </div>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="h-7 w-7"
              aria-label="Убрать файл"
              onClick={() => pickFile(null)}
            >
              <X className="h-4 w-4" />
            </Button>
          </div>
        ) : (
          <>
            <FileUp className="mx-auto h-6 w-6 text-muted-foreground" />
            <p className="mt-2 text-sm">Перетащите проект документа сюда</p>
            <p className="text-xs text-muted-foreground">{ACCEPTED.join(', ')} — до 50 МБ</p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="mt-2"
              onClick={() => inputRef.current?.click()}
            >
              Выбрать файл
            </Button>
          </>
        )}
      </div>

      <div className="space-y-1.5">
        <Label htmlFor="doc-kind">Вид документа</Label>
        <Select value={docKind} onValueChange={setDocKind}>
          <SelectTrigger id="doc-kind">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {DOC_KINDS.map((kind) => (
              <SelectItem key={kind.value || 'auto'} value={kind.value}>
                {kind.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <Button onClick={submit} disabled={!file || busy} className="w-full gap-2">
        {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileUp className="h-4 w-4" />}
        {busy ? 'Проверяется…' : 'Проверить на соответствие'}
      </Button>

      <p className="text-[11px] text-muted-foreground">
        Файл не сохраняется: он разбирается, проверяется и удаляется.
      </p>

      {error ? (
        <p className="flex items-start gap-1.5 text-xs text-destructive">
          <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          {error}
        </p>
      ) : null}

      {report ? <DocChecker report={report} /> : null}
    </div>
  );
}
