// ════════════════════════════════════════════════════════════════════════════
// 数据图库 —— 构建结束后前端该做什么（自动增量更新）。
//
// 手动构建（用户点了按钮）：像以前一样，立刻重拉索引、重读标记、弹 toast。
// 自动构建（后台定时 / 打开页面触发）**不能打扰正在看图的人**：
//   * 什么都没变：什么都不做（不弹 toast、不重拉索引、不动标记）；
//   * 有变化：用户还没动过页面（没开大图、列表在第一屏附近、没勾选、没在改标记）⇒ 直接重拉索引，
//     不要他去点；否则只提示一下，索引留到「点提示」或「换视图」时再拉 —— 重拉索引会让列表拿到新的
//     数组，卡片网格退回前 150 张、滚动位置和打开着的大图都丢，勾选也不再对得上；
//   * 标记永远不因自动构建而重读（构建根本不写标记）。
// 纯函数，没有 React / 网络依赖，node --test 直接测。
// ════════════════════════════════════════════════════════════════════════════

export interface BuildEnd {
  running?: boolean;
  phase?: string;
  trigger?: string;
  started?: string | null;
  finished?: string | null;
  n_new?: number;
  n_changed?: number;
  n_failed?: number;
  n_removed?: number;
  n_render?: number;
  n_analysis?: number;
  /** 环境标签变了的条目数（sidecar 在文件之后才写成也算；数据文件本身可能一个没变）。 */
  n_env?: number;
}

export type IndexAction = "now" | "later" | "none";
export type ToastKind = "none" | "updated" | "light" | "failed" | "error" | "cancelled";

export interface AfterBuild {
  index: IndexAction;
  /** 重读标记。自动构建不写标记，所以只有手动构建才重读。 */
  marks: boolean;
  toast: ToastKind;
}

/** 一轮构建的身份；还在跑时是 null。用「身份变了」而不是「running 的下降沿」判断结束：
 *  页面在后台时轮询暂停，整轮自动构建可能在没人看到 running=true 的情况下跑完。 */
export function buildKey(st: BuildEnd): string | null {
  if (st.running) return null;
  return `${st.started ?? ""}|${st.finished ?? ""}|${st.phase ?? ""}`;
}

/** 索引内容是否可能变了：新增、变动、消失、重出缩略图、重算判据、环境标签变了。 */
export function indexChanged(st: BuildEnd): boolean {
  return (
    (st.n_new ?? 0) > 0 ||
    (st.n_changed ?? 0) > 0 ||
    (st.n_removed ?? 0) > 0 ||
    (st.n_render ?? 0) > 0 ||
    (st.n_analysis ?? 0) > 0 ||
    (st.n_env ?? 0) > 0
  );
}

/** 「用户还没动过页面」：四条同时成立才算。判断本身要看 DOM 和仓库，在组件里做，这里只收结果。 */
export interface ViewTouch {
  lightboxOpen: boolean;
  /** 滚动容器的 scrollTop 与可视高度（px）。 */
  scrollTop: number;
  viewportHeight: number;
  selected: number;
  /** 有没存盘的标记改动，或焦点在输入框 / 文本域里（正在写备注、标签）。 */
  editing: boolean;
}

export function isPristine(t: ViewTouch): boolean {
  return !t.lightboxOpen && !t.editing && t.selected === 0 && t.scrollTop <= Math.max(0, t.viewportHeight);
}

export function planAfterBuild(st: BuildEnd, pristine = false): AfterBuild {
  const manual = st.trigger !== "auto";
  if (manual) {
    return {
      index: "now",
      marks: true,
      toast: st.phase === "error" ? "error" : st.phase === "cancelled" ? "cancelled" : "updated",
    };
  }
  // 自动构建
  if (st.phase === "error") return { index: "none", marks: false, toast: "error" };
  if (st.phase === "cancelled") return { index: "none", marks: false, toast: "none" }; // 多半是手动点击顶掉了它，后面还有一轮
  if (indexChanged(st)) return { index: pristine ? "now" : "later", marks: false, toast: "light" };
  if ((st.n_failed ?? 0) > 0) return { index: "none", marks: false, toast: "failed" };
  return { index: "none", marks: false, toast: "none" };
}
