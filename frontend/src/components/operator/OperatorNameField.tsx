import clsx from "clsx";
import { useOperatorName } from "@/hooks/useOperatorPrefs";
import { tr } from "@/lib/i18n";
import { OPERATOR_NAME_MAX, cleanOperatorName } from "@/lib/operatorPrompts";

/**
 * Who is answering. Every answer and every optical-state edit is recorded under this
 * name, so it is required; it is remembered on this browser and shared by every card and
 * panel on the page (change it in one place and the others follow).
 */
export function OperatorNameField() {
  const [name, setName] = useOperatorName();
  const missing = !cleanOperatorName(name);
  return (
    <label className="flex flex-wrap items-center gap-2">
      <span className="shrink-0 text-xs font-medium text-mast-accent">
        {tr("你的姓名", "Your name")} <span className="text-mast-danger">*</span>
      </span>
      <input
        value={name}
        onChange={(e) => setName(e.target.value)}
        maxLength={OPERATOR_NAME_MAX}
        autoComplete="name"
        aria-required="true"
        placeholder={tr("必填：你的答复和修改会记在这个名字下", "Required: your answers and edits are recorded under this name")}
        className={clsx(
          "min-w-[14rem] flex-1 rounded border bg-mast-bg px-3 py-1.5 text-sm text-mast-text placeholder:text-mast-muted focus:outline-none",
          missing ? "border-mast-warn-border focus:border-mast-warn" : "border-mast-border focus:border-mast-accent",
        )}
      />
    </label>
  );
}
