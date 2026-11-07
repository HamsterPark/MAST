import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { Button, Field, Modal, SelectField, TextField, useToast } from "../controls";
import { tr } from "../../lib/i18n";

// Experiment controls reused by the Experiments tab:
//   - new experiment    POST /api/experiments {name, goal}
//   - submit feedback   POST /api/feedback
// Both invalidate the relevant queries so the views refresh.

export function StartExperimentButton() {
  const qc = useQueryClient();
  const { toast, node } = useToast();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [goal, setGoal] = useState("");

  const m = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/experiments", {
        body: { name, goal, thread_id: null },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (d) => {
      if (d.ok) {
        toast(tr("实验已创建", "Experiment created"));
        setOpen(false);
        setName("");
        setGoal("");
        qc.invalidateQueries({ queryKey: ["experiments"] });
      } else {
        toast(d.degraded ? tr("记录后端未连接（降级）", "Records backend not connected (degraded)") : tr("创建失败", "Creation failed"), "err");
      }
    },
    onError: () => toast(tr("创建失败", "Creation failed"), "err"),
  });

  return (
    <>
      <Button variant="primary" onClick={() => setOpen(true)}>
        {tr("新建实验", "New experiment")}
      </Button>
      <Modal open={open} onClose={() => setOpen(false)} title={tr("新建实验", "New experiment")}>
        <div className="space-y-4">
          <Field label={tr("名称", "Name")}>
            <TextField value={name} onChange={setName} placeholder={tr("实验名称", "Experiment name")} />
          </Field>
          <Field label={tr("目标", "Goal")}>
            <TextField value={goal} onChange={setGoal} placeholder={tr("实验目标（可选）", "Experiment goal (optional)")} />
          </Field>
          <div className="flex justify-end gap-2">
            <Button onClick={() => setOpen(false)}>{tr("取消", "Cancel")}</Button>
            <Button
              variant="primary"
              onClick={() => m.mutate()}
              disabled={!name || m.isPending}
            >
              {m.isPending ? tr("创建中…", "Creating…") : tr("创建", "Create")}
            </Button>
          </div>
        </div>
      </Modal>
      {node}
    </>
  );
}

// There is no "end experiment" action: an experiment can always be resumed, even
// years later, and writing a summary or report does not require archiving it. The
// primary action on the records page is "switch to this experiment" (RecordsPage).
//
// The backend's POST /api/experiments/{id}/end still exists (marked deprecated;
// it only clears the scope pointer when the experiment is the current one), but
// the UI no longer offers it.

const RATING_OPTIONS = [
  { value: "up", label: tr("👍 好", "👍 Good") },
  { value: "down", label: tr("👎 差", "👎 Poor") },
  { value: "neutral", label: tr("😐 中性", "😐 Neutral") },
];

export function FeedbackButton({ experimentId }: { experimentId?: string | null }) {
  const qc = useQueryClient();
  const { toast, node } = useToast();
  const [open, setOpen] = useState(false);
  const [rating, setRating] = useState("up");
  const [comment, setComment] = useState("");

  const m = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/feedback", {
        body: {
          rating,
          comment,
          experiment_id: experimentId ?? null,
          sample_id: null,
          conversation_id: null,
          agent: "",
        },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (d) => {
      if (d.ok) {
        toast(tr("反馈已记录", "Feedback recorded"));
        setOpen(false);
        setComment("");
        if (experimentId) qc.invalidateQueries({ queryKey: ["experiment", experimentId] });
      } else {
        toast(tr("记录失败", "Failed to record"), "err");
      }
    },
    onError: () => toast(tr("记录失败", "Failed to record"), "err"),
  });

  return (
    <>
      <Button onClick={() => setOpen(true)}>{tr("提交反馈", "Submit feedback")}</Button>
      <Modal open={open} onClose={() => setOpen(false)} title={tr("用户反馈", "Feedback")}>
        <div className="space-y-4">
          <Field label={tr("评分", "Rating")}>
            <SelectField value={rating} onChange={setRating} options={RATING_OPTIONS} />
          </Field>
          <Field label={tr("评论", "Comment")}>
            <TextField value={comment} onChange={setComment} placeholder={tr("可选评论", "Optional comment")} />
          </Field>
          <div className="flex justify-end gap-2">
            <Button onClick={() => setOpen(false)}>{tr("取消", "Cancel")}</Button>
            <Button variant="primary" onClick={() => m.mutate()} disabled={m.isPending}>
              {m.isPending ? tr("提交中…", "Submitting…") : tr("提交", "Submit")}
            </Button>
          </div>
        </div>
      </Modal>
      {node}
    </>
  );
}
