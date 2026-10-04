import {
  createContext,
  useContext,
  useId,
  useState,
  type ComponentProps,
  type ReactNode,
} from "react";
import { Icon } from "../Icon";

type FieldState = { id: string; describedBy?: string; invalid: boolean };
const FieldContext = createContext<FieldState | null>(null);

function joinIds(...ids: (string | undefined)[]) {
  return ids.filter(Boolean).join(" ") || undefined;
}

/** Wires the control inside a <Field> to its label, hint and error. */
function useFieldControl(props: {
  id?: string;
  "aria-describedby"?: string;
  "aria-invalid"?: ComponentProps<"input">["aria-invalid"];
}) {
  const field = useContext(FieldContext);
  return {
    id: props.id ?? field?.id,
    "aria-describedby": joinIds(field?.describedBy, props["aria-describedby"]),
    "aria-invalid": props["aria-invalid"] ?? (field?.invalid || undefined),
  };
}

/**
 * Label, control, optional hint and error. Put exactly one Input, Textarea,
 * Select or PasswordInput inside; it receives the id and descriptions.
 * Write a hint only where people would otherwise make a mistake.
 */
export function Field({
  label,
  hint,
  error,
  optional = false,
  id,
  className = "",
  children,
}: {
  label: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  optional?: boolean;
  id?: string;
  className?: string;
  children: ReactNode;
}) {
  const generated = useId();
  const controlId = id ?? generated;
  // An error replaces the hint, so the two never stack or disagree; errors
  // must therefore state the rule themselves.
  const hintId = hint && !error ? `${controlId}-hint` : undefined;
  const errorId = error ? `${controlId}-error` : undefined;
  return (
    <div className={`ui-field ${className}`.trim()}>
      <div className="ui-field__label-row">
        <label className="ui-field__label" htmlFor={controlId}>
          {label}
        </label>
        {optional && <span className="ui-field__optional">Optional</span>}
      </div>
      <FieldContext.Provider
        value={{
          id: controlId,
          describedBy: joinIds(errorId, hintId),
          invalid: !!error,
        }}
      >
        {children}
      </FieldContext.Provider>
      {error && (
        <p id={errorId} className="ui-field__error">
          <Icon name="danger" />
          <span>{error}</span>
        </p>
      )}
      {hint && !error && (
        <p id={hintId} className="ui-hint">
          {hint}
        </p>
      )}
    </div>
  );
}

export function Input({ className = "", ...props }: ComponentProps<"input">) {
  return (
    <input
      {...props}
      {...useFieldControl(props)}
      className={`ui-input ${className}`.trim()}
    />
  );
}

export function Textarea({
  className = "",
  ...props
}: ComponentProps<"textarea">) {
  return (
    <textarea
      {...props}
      {...useFieldControl(props)}
      className={`ui-input ui-textarea ${className}`.trim()}
    />
  );
}

export function Select({
  className = "",
  children,
  ...props
}: ComponentProps<"select">) {
  return (
    <span className={`ui-select ${className}`.trim()}>
      <select
        {...props}
        {...useFieldControl(props)}
        className="ui-input ui-select__control"
      >
        {children}
      </select>
      <Icon name="chevron-down" className="ui-select__icon" />
    </span>
  );
}

/**
 * Password input with a show/hide toggle. Pass revealed/onRevealedChange to
 * hide the value again from outside, for example after submitting.
 */
export function PasswordInput({
  revealed,
  onRevealedChange,
  className = "",
  ...props
}: Omit<ComponentProps<"input">, "type"> & {
  revealed?: boolean;
  onRevealedChange?: (revealed: boolean) => void;
}) {
  const [own, setOwn] = useState(false);
  const visible = revealed ?? own;
  const control = useFieldControl(props);
  return (
    <span className={`ui-input-group ${className}`.trim()}>
      <input
        {...props}
        {...control}
        type={visible ? "text" : "password"}
        className="ui-input ui-input-group__control"
      />
      <button
        type="button"
        className="ui-input-group__button"
        aria-label={visible ? "Hide password" : "Show password"}
        aria-pressed={visible}
        aria-controls={control.id}
        disabled={props.disabled}
        onClick={() => {
          setOwn(!visible);
          onRevealedChange?.(!visible);
        }}
      >
        <Icon name={visible ? "eye-off" : "eye"} />
      </button>
    </span>
  );
}

type ChoiceProps = Omit<ComponentProps<"input">, "type" | "children"> & {
  label: ReactNode;
  description?: ReactNode;
};

function Choice({
  type,
  label,
  description,
  className = "",
  ...props
}: ChoiceProps & { type: "checkbox" | "radio" }) {
  const generated = useId();
  const id = props.id ?? generated;
  const descriptionId = description ? `${id}-description` : undefined;
  return (
    <label className={`ui-choice ${className}`.trim()} htmlFor={id}>
      <input
        {...props}
        id={id}
        type={type}
        className={`ui-choice__input ui-choice__input--${type}`}
        aria-describedby={joinIds(descriptionId, props["aria-describedby"])}
      />
      <span className="ui-choice__text">
        <span className="ui-choice__label">{label}</span>
        {description && (
          // Hidden from the label's name; still read as the description.
          <span
            id={descriptionId}
            className="ui-choice__description"
            aria-hidden="true"
          >
            {description}
          </span>
        )}
      </span>
    </label>
  );
}

export function Checkbox(props: ChoiceProps) {
  return <Choice {...props} type="checkbox" />;
}

export function Radio(props: ChoiceProps) {
  return <Choice {...props} type="radio" />;
}

/** On/off setting that applies immediately. Use Checkbox inside forms that submit. */
export function Switch({
  label,
  description,
  className = "",
  ...props
}: ChoiceProps) {
  const generated = useId();
  const id = props.id ?? generated;
  const descriptionId = description ? `${id}-description` : undefined;
  return (
    <label className={`ui-choice ui-switch ${className}`.trim()} htmlFor={id}>
      <span className="ui-choice__text">
        <span className="ui-choice__label">{label}</span>
        {description && (
          // Hidden from the label's name; still read as the description.
          <span
            id={descriptionId}
            className="ui-choice__description"
            aria-hidden="true"
          >
            {description}
          </span>
        )}
      </span>
      <input
        {...props}
        id={id}
        type="checkbox"
        role="switch"
        className="ui-switch__input"
        aria-describedby={descriptionId}
      />
    </label>
  );
}

/** Groups related controls under a legend, e.g. a set of radios. */
export function Fieldset({
  legend,
  hint,
  error,
  variant = "list",
  className = "",
  children,
}: {
  legend: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  /** "cards" lays radios out as bordered, equal-width options. */
  variant?: "list" | "cards";
  className?: string;
  children: ReactNode;
}) {
  const id = useId();
  return (
    <fieldset
      className={`ui-fieldset ui-fieldset--${variant} ${className}`.trim()}
      aria-describedby={joinIds(
        error ? `${id}-error` : undefined,
        hint ? `${id}-hint` : undefined,
      )}
    >
      <legend className="ui-field__label">{legend}</legend>
      <div className="ui-fieldset__options">{children}</div>
      {error && (
        <p id={`${id}-error`} className="ui-field__error">
          <Icon name="danger" />
          <span>{error}</span>
        </p>
      )}
      {hint && (
        <p id={`${id}-hint`} className="ui-hint">
          {hint}
        </p>
      )}
    </fieldset>
  );
}

/** The one style for help text outside a Field. Use sparingly. */
export function Hint({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return <p className={`ui-hint ${className}`.trim()}>{children}</p>;
}
