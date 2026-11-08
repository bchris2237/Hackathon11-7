import { useId } from "react";
import type { Service } from "../../../contracts/models";
import {
  SERVICES,
  errorsFor,
  type BusinessPatch,
  type FieldError,
  type FormBusiness,
} from "../state/scenario";

interface Props {
  index: number;
  business: FormBusiness;
  errors: FieldError[];
  onChange: (index: number, patch: BusinessPatch) => void;
}

const inputClass =
  "mt-1 block w-full rounded border border-slate-400 bg-white px-2 py-1.5 text-slate-900 aria-[invalid=true]:border-red-700";

interface FieldProps {
  label: string;
  hint?: string;
  error?: string;
  children: (ids: { id: string; describedBy: string | undefined; invalid: boolean }) => React.ReactNode;
}

function Field({ label, hint, error, children }: FieldProps) {
  const id = useId();
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const describedBy = [hint ? hintId : null, error ? errorId : null].filter(Boolean).join(" ") || undefined;
  return (
    <div>
      <label htmlFor={id} className="block text-sm font-medium text-slate-800">
        {label}
      </label>
      {children({ id, describedBy, invalid: Boolean(error) })}
      {hint && (
        <p id={hintId} className="mt-0.5 text-xs text-slate-600">
          {hint}
        </p>
      )}
      {error && (
        <p id={errorId} className="mt-0.5 text-xs font-medium text-red-800">
          {error}
        </p>
      )}
    </div>
  );
}

export default function BusinessForm({ index, business, errors, onChange }: Props) {
  const p = `businesses[${index}]`;
  const n = index + 1;
  const set = (patch: BusinessPatch) => onChange(index, patch);

  return (
    <fieldset className="min-w-0 rounded border border-slate-300 p-4">
      <legend className="px-1 text-base font-semibold">Business {n}</legend>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={`Business ${n} ID`} hint="Lowercase slug, e.g. devco" error={errorsFor(errors, `${p}.id`)}>
          {({ id, describedBy, invalid }) => (
            <input
              id={id}
              className={inputClass}
              value={business.id}
              aria-invalid={invalid}
              aria-describedby={describedBy}
              onChange={(e) => set({ id: e.target.value })}
            />
          )}
        </Field>
        <Field label={`Business ${n} name`} error={errorsFor(errors, `${p}.name`)}>
          {({ id, describedBy, invalid }) => (
            <input
              id={id}
              className={inputClass}
              value={business.name}
              aria-invalid={invalid}
              aria-describedby={describedBy}
              onChange={(e) => set({ name: e.target.value })}
            />
          )}
        </Field>
      </div>

      <div className="mt-3">
        <Field
          label={`Business ${n} description`}
          hint="Optional free text. Not part of the optimization input."
          error={errorsFor(errors, `${p}.description`)}
        >
          {({ id, describedBy, invalid }) => (
            <textarea
              id={id}
              rows={2}
              className={inputClass}
              value={business.description}
              aria-invalid={invalid}
              aria-describedby={describedBy}
              onChange={(e) => set({ description: e.target.value })}
            />
          )}
        </Field>
      </div>

      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <Field
          label={`Business ${n} capacity (hours)`}
          hint="Hours (2.5) or minutes (90m). Whole minutes only."
          error={errorsFor(errors, `${p}.capacity`)}
        >
          {({ id, describedBy, invalid }) => (
            <input
              id={id}
              inputMode="decimal"
              className={inputClass}
              value={business.capacity}
              aria-invalid={invalid}
              aria-describedby={describedBy}
              onChange={(e) => set({ capacity: e.target.value })}
            />
          )}
        </Field>
        <Field
          label={`Business ${n} coordination (hours)`}
          hint="Charged once if the business takes part in an exchange."
          error={errorsFor(errors, `${p}.coordination`)}
        >
          {({ id, describedBy, invalid }) => (
            <input
              id={id}
              inputMode="decimal"
              className={inputClass}
              value={business.coordination}
              aria-invalid={invalid}
              aria-describedby={describedBy}
              onChange={(e) => set({ coordination: e.target.value })}
            />
          )}
        </Field>
      </div>

      <div className="mt-3 grid gap-3 sm:grid-cols-3">
        {SERVICES.map((service: Service) => (
          <Field
            key={service}
            label={`Business ${n} ${service} estimate (hours)`}
            hint="Leave blank if unsupported."
            error={errorsFor(errors, `${p}.estimates.${service}`)}
          >
            {({ id, describedBy, invalid }) => (
              <input
                id={id}
                inputMode="decimal"
                className={inputClass}
                value={business.estimates[service]}
                aria-invalid={invalid}
                aria-describedby={describedBy}
                onChange={(e) => set({ estimates: { [service]: e.target.value } })}
              />
            )}
          </Field>
        ))}
      </div>

      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <Field label={`Business ${n} needs (service)`} error={errorsFor(errors, `${p}.requirement`)}>
          {({ id, describedBy, invalid }) => (
            <select
              id={id}
              className={inputClass}
              value={business.requirement.service}
              aria-invalid={invalid}
              aria-describedby={describedBy}
              onChange={(e) => set({ requirement: { service: e.target.value as Service } })}
            >
              {SERVICES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          )}
        </Field>
        <Field label={`Business ${n} deliverable ID`} error={errorsFor(errors, `${p}.deliverableId`)}>
          {({ id, describedBy, invalid }) => (
            <input
              id={id}
              className={inputClass}
              value={business.requirement.deliverableId}
              aria-invalid={invalid}
              aria-describedby={describedBy}
              onChange={(e) => set({ requirement: { deliverableId: e.target.value } })}
            />
          )}
        </Field>
      </div>
    </fieldset>
  );
}
