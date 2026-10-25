'use client';

/**
 * Формирование черновика документа по модулю.
 *
 * Поля формы строятся из `fields_schema` шаблона — тот же состав полей,
 * по которому помощник задаёт вопросы в диалоге. Обязательные поля
 * отмечены: без них бэкенд вернёт отказ с их перечнем, и это правильно —
 * документ с пустым реквизитом по инструкции недопустим.
 *
 * Сборка DOCX идёт в фоновой задаче, поэтому после отправки состояние
 * черновика опрашивается до готовности файла.
 */

import { Download, FileSignature, Loader2, TriangleAlert } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Textarea } from '@/components/ui/textarea';
import { ApiError, documents } from '@/lib/api';
import type { DocumentTemplate, GeneratedDocument, TemplateField } from '@/lib/types';
import { cn, formatFileSize, plural } from '@/lib/utils';

/** Интервал опроса состояния черновика. */
const POLL_INTERVAL_MS = 1500;
/** Предел ожидания: дольше — считаем, что задача зависла. */
const POLL_ATTEMPTS = 40;

interface DocGeneratorProps {
  /** Диалог, из которого запущено формирование: попадёт в реестр черновиков. */
  sessionId?: string | null;
  /** Код шаблона, выбранного в диалоге. */
  initialTemplateCode?: string;
}

export function DocGenerator({ sessionId, initialTemplateCode = '' }: DocGeneratorProps) {
  const [templates, setTemplates] = useState<DocumentTemplate[]>([]);
  const [code, setCode] = useState(initialTemplateCode);
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [missing, setMissing] = useState<string[]>([]);
  const [document, setDocument] = useState<GeneratedDocument | null>(null);

  useEffect(() => {
    let cancelled = false;
    documents
      .templates()
      .then((items) => {
        if (cancelled) return;
        setTemplates(items);
        setCode((current) => current || items[0]?.code || '');
      })
      .catch((caught: unknown) => {
        if (!cancelled) {
          setError(caught instanceof ApiError ? caught.message : 'Не удалось загрузить бланки');
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (initialTemplateCode) setCode(initialTemplateCode);
  }, [initialTemplateCode]);

  const template = useMemo(
    () => templates.find((item) => item.code === code) || null,
    [templates, code],
  );

  // При смене бланка форма очищается: поля у модулей разные.
  useEffect(() => {
    setValues({});
    setMissing([]);
    setDocument(null);
  }, [code]);

  const setValue = useCallback((name: string, value: string) => {
    setValues((current) => ({ ...current, [name]: value }));
    setMissing((current) => current.filter((item) => item !== name));
  }, []);

  /** Опрашивает состояние черновика до готовности файла. */
  const waitForDocument = useCallback(async (documentId: string): Promise<void> => {
    for (let attempt = 0; attempt < POLL_ATTEMPTS; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
      const record = await documents.generatedOne(documentId);
      setDocument(record);

      if (record.status === 'ready') return;
      if (record.status === 'failed') {
        setError('Не удалось сформировать документ. Проверьте заполненные поля.');
        return;
      }
    }
    setError('Формирование занимает больше обычного. Черновик появится в разделе «Мои черновики».');
  }, []);

  const submit = async (): Promise<void> => {
    if (!template) return;

    setBusy(true);
    setError('');
    setMissing([]);
    setDocument(null);

    try {
      const accepted = await documents.generate({
        template_code: template.code,
        values,
        session_id: sessionId || null,
      });
      if (accepted.resource_id) {
        await waitForDocument(accepted.resource_id);
      }
    } catch (caught) {
      if (caught instanceof ApiError) {
        setError(caught.message);
        setMissing(caught.fields);
      } else {
        setError('Сервис недоступен. Повторите попытку.');
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <Label htmlFor="template-select">Бланк документа</Label>
        <Select value={code} onValueChange={setCode}>
          <SelectTrigger id="template-select">
            <SelectValue placeholder="Выберите бланк" />
          </SelectTrigger>
          <SelectContent>
            {templates.map((item) => (
              <SelectItem key={item.code} value={item.code}>
                {item.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {template ? (
        <>
          {template.description ? (
            <p className="text-xs text-muted-foreground">{template.description}</p>
          ) : null}

          {template.related_clauses?.length ? (
            <p className="font-mono text-[11px] text-muted-foreground">
              требования инструкции: п. {template.related_clauses.join(', ')}
            </p>
          ) : null}

          <div className="space-y-3">
            {(template.fields_schema as TemplateField[]).map((field) => (
              <FieldControl
                key={field.name}
                field={field}
                value={values[field.name] || ''}
                invalid={missing.includes(field.name)}
                onChange={(value) => setValue(field.name, value)}
              />
            ))}
          </div>

          {template.required_approvals?.length ? (
            <div className="rounded-md border border-border bg-muted/30 p-2">
              <p className="text-xs font-medium">Маршрут визирования</p>
              <ol className="mt-1 space-y-0.5 text-xs text-muted-foreground">
                {template.required_approvals.map((step, index) => (
                  <li key={`${step.department}-${index}`}>
                    {index + 1}. {step.department}
                    {step.term_days ? ` — до ${step.term_days} ${plural(step.term_days, ['рабочего дня', 'рабочих дней', 'рабочих дней'])}` : ''}
                  </li>
                ))}
              </ol>
            </div>
          ) : null}

          <Button onClick={submit} disabled={busy} className="w-full gap-2">
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileSignature className="h-4 w-4" />}
            {busy ? 'Формируется…' : 'Сформировать черновик'}
          </Button>
        </>
      ) : null}

      {error ? (
        <p className="flex items-start gap-1.5 text-xs text-destructive">
          <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          {error}
        </p>
      ) : null}

      {document?.status === 'ready' ? (
        <div className="space-y-2 rounded-md border border-success/40 bg-success-muted/40 p-3">
          <p className="text-sm font-medium">Черновик готов</p>
          <p className="text-xs text-muted-foreground">
            {document.filename} · {formatFileSize(document.file_size)}
          </p>
          {document.missing_fields?.length ? (
            <p className="text-xs text-warning">
              Не заполнено: {document.missing_fields.join(', ')}
            </p>
          ) : null}
          <Button
            size="sm"
            variant="secondary"
            className="w-full gap-2"
            onClick={() => documents.download(document.id, document.filename)}
          >
            <Download className="h-4 w-4" />
            Скачать DOCX
          </Button>
        </div>
      ) : null}
    </div>
  );
}

interface FieldControlProps {
  field: TemplateField;
  value: string;
  invalid: boolean;
  onChange: (value: string) => void;
}

/** Поле формы по описанию из схемы шаблона. */
function FieldControl({ field, value, invalid, onChange }: FieldControlProps) {
  const id = `field-${field.name}`;
  const label = field.label || field.name;

  return (
    <div className="space-y-1.5">
      <Label htmlFor={id} className="flex items-center gap-1.5">
        {label}
        {field.required ? <span className="text-destructive">*</span> : null}
        {invalid ? (
          <Badge variant="destructive" className="text-[10px]">
            обязательно
          </Badge>
        ) : null}
      </Label>

      {field.options?.length ? (
        <Select value={value} onValueChange={onChange}>
          <SelectTrigger id={id} className={cn(invalid && 'border-destructive')}>
            <SelectValue placeholder="Выберите значение" />
          </SelectTrigger>
          <SelectContent>
            {/* Пустой вариант Radix Select не принимает: пустая строка
                означает «выбор снят». Такой вариант в схеме шаблона
                отбрасывается, иначе отказ затронет весь экран. */}
            {field.options.filter(Boolean).map((option) => (
              <SelectItem key={option} value={option}>
                {option}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      ) : field.type === 'text' ? (
        <Textarea
          id={id}
          value={value}
          rows={3}
          onChange={(event) => onChange(event.target.value)}
          className={cn(invalid && 'border-destructive')}
        />
      ) : (
        <Input
          id={id}
          type={field.type === 'date' ? 'date' : field.type === 'number' ? 'number' : 'text'}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          className={cn(invalid && 'border-destructive')}
        />
      )}

      {field.question ? <p className="text-[11px] text-muted-foreground">{field.question}</p> : null}
    </div>
  );
}
