// Dropdowns whose value must be declared by a person (settings page: retract direction
// retract_motor_dir, piezo extension sign z_extend_sign).
//
// Previously an undeclared key still showed the default in the dropdown
// (`strAt(key) || "z+"`): the operator could not tell it was undeclared, and picking the
// already-displayed default was not a change, so nothing was saved. "Undeclared" is now a
// visible state:
//   - undeclared (key absent from the saved settings): the current value is a DISABLED
//     placeholder "(not declared, please choose)", with the real options below; any choice
//     is a real change and gets saved;
//   - declared: only the real options are listed, with NO "clear declaration" entry, since
//     a safety-relevant declaration should not be one click from deletion;
//   - an unrecognised stored value (hand-edited config, older format): shown as-is,
//     disabled, and the person is asked to choose again.

import { tr } from "./i18n.ts";

export interface ChoiceOption {
  value: string;
  label: string;
}

/** Value of the placeholder entry: never selectable, never saved. */
export const UNDECLARED_VALUE = "__undeclared__";

export interface DeclaredChoiceView {
  /** Current value of the select. */
  value: string;
  /** Dropdown options (the placeholder is disabled). */
  options: Array<ChoiceOption & { disabled?: boolean }>;
  /** The saved settings contain this key with a recognised value. */
  declared: boolean;
}

export function declaredChoiceView(persisted: unknown, choices: readonly ChoiceOption[], placeholder = tr("（未声明，请选择）", "(not declared, please choose)")): DeclaredChoiceView {
  const real = choices.map((c) => ({ value: c.value, label: c.label }));
  if (typeof persisted === "string" && persisted !== "") {
    if (choices.some((c) => c.value === persisted)) return { value: persisted, options: real, declared: true };
    return {
      value: UNDECLARED_VALUE,
      options: [{ value: UNDECLARED_VALUE, label: tr(`（存着认不出的值「${persisted}」，请重新选择）`, `(unrecognised stored value "${persisted}", please choose again)`), disabled: true }, ...real],
      declared: false,
    };
  }
  return { value: UNDECLARED_VALUE, options: [{ value: UNDECLARED_VALUE, label: placeholder, disabled: true }, ...real], declared: false };
}

/** Dropdown picked v: returns the value to save for a real option, null (no save) for the placeholder or an unrecognised value. */
export function choiceToSave(v: string, choices: readonly ChoiceOption[]): string | null {
  return choices.some((c) => c.value === v) ? v : null;
}
