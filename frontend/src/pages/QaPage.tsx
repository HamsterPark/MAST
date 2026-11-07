import { Section } from "@/components/ui";
import { QaPanel } from "@/components/qa/QaPanel";
import { tr } from "@/lib/i18n";

// Query assistant — single-turn, read-only knowledge Q&A (parity rebuild of the old
// Gradio query-assistant tab). All UI/state lives in QaPanel; see its header comment for
// why the backend is flagged missing rather than wired to the stateful chat graph.
export default function QaPage() {
  return (
    <Section title={tr("查询助手", "Query assistant")}>
      <QaPanel />
    </Section>
  );
}
