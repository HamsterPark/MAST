// 合帧时钟 —— 「20 Hz 的光谱刷新不能卡 UI」的那一半。
//
// 数据到达的节奏和屏幕刷新的节奏是两回事：一个流可以一秒推 20 帧，五个流一起推就是
// 100 次「有新数据」。如果每一次都去重画 uPlot 或者 setState，React 和画布会被
// 打成碎片。这里的规矩是：数据来了只**登记**「某个视图脏了」，真正的重画统一放在下一个
// requestAnimationFrame 里，每个视图每帧最多画一次。
//
// 后台标签页里浏览器根本不调 requestAnimationFrame —— 这正好就是我们要的「后台不画」：
// 登记照样发生（只是覆盖同一个键），回到前台的第一帧把最新的状态画一次。
//
// 没有 rAF 的环境（node 测试、某些 webview）退回 setTimeout(16)。

export type RafLike = (cb: (t: number) => void) => number;
export type CafLike = (handle: number) => void;

export interface FrameClock {
  /** 下一帧调用 fn。同一个 key 在一帧里登记多次只调最后那个 fn 一次。 */
  request(key: object, fn: () => void): void;
  /** 取消某个 key 尚未执行的登记（组件卸载时）。 */
  cancel(key: object): void;
  /** 立刻执行所有登记（测试用）。 */
  flush(): void;
  /** 还在等下一帧的登记数。 */
  pending(): number;
}

export function createFrameClock(env: { raf?: RafLike | null; caf?: CafLike | null } = {}): FrameClock {
  const g = globalThis as unknown as { requestAnimationFrame?: RafLike; cancelAnimationFrame?: CafLike };
  const raf: RafLike =
    env.raf ??
    (typeof g.requestAnimationFrame === "function"
      ? g.requestAnimationFrame.bind(globalThis)
      : (cb) => setTimeout(() => cb(Date.now()), 16) as unknown as number);
  const caf: CafLike =
    env.caf ??
    (typeof g.cancelAnimationFrame === "function"
      ? g.cancelAnimationFrame.bind(globalThis)
      : (h) => clearTimeout(h as unknown as ReturnType<typeof setTimeout>));

  let queue = new Map<object, () => void>();
  let handle: number | null = null;

  const run = () => {
    handle = null;
    const now = queue;
    queue = new Map();
    for (const fn of now.values()) {
      try {
        fn();
      } catch (err) {
        // 一个视图的绘制错误不许让别的视图停画
        // eslint-disable-next-line no-console
        console.warn("[live] draw failed", err);
      }
    }
  };

  return {
    request(key, fn) {
      queue.set(key, fn);
      if (handle === null) handle = raf(run);
    },
    cancel(key) {
      queue.delete(key);
      if (queue.size === 0 && handle !== null) {
        caf(handle);
        handle = null;
      }
    },
    flush() {
      if (handle !== null) {
        caf(handle);
        handle = null;
      }
      run();
    },
    pending() {
      return queue.size;
    },
  };
}

/** 全应用共用的一只时钟：所有实时视图在同一帧里一起画。 */
export const frameClock: FrameClock = createFrameClock();
