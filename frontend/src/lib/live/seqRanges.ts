// 一组「还缺的 seq」—— 合并、有序的闭区间。
//
// 增量流（xy / map2d / status）的帧一条都不能丢：契约 §7 说服务端对它们只合批、
// 不丢帧，而真丢了的时候（重连、慢消费者）会发 `dropped{from,to}`，客户端用 REST
// 补齐。补齐要回答三个问题：缺哪几段、某一帧是不是在补的范围里、补完了没有。
// 这三件事都在这里，单独成文件是因为它是整套补齐逻辑里唯一需要「区间算术」的地方，
// 而区间算术的错（差一、相邻不合并、删中间一个点不拆段）不会报错，只会让某一帧
// 被当成重复静静地丢掉。

export type SeqRange = readonly [from: number, to: number];

export class SeqRanges {
  private ranges: Array<[number, number]> = [];

  /** 加入 [from, to]（闭区间）。非法或空区间忽略。 */
  add(from: number, to: number): void {
    if (!Number.isFinite(from) || !Number.isFinite(to)) return;
    let a = Math.floor(from);
    let b = Math.floor(to);
    if (b < a) return;
    const out: Array<[number, number]> = [];
    let placed = false;
    for (const [x, y] of this.ranges) {
      if (y < a - 1) {
        out.push([x, y]);
      } else if (x > b + 1) {
        if (!placed) {
          out.push([a, b]);
          placed = true;
        }
        out.push([x, y]);
      } else {
        // 重叠或相邻：并进当前这一段
        a = Math.min(a, x);
        b = Math.max(b, y);
      }
    }
    if (!placed) out.push([a, b]);
    this.ranges = out;
  }

  /** 去掉单个 seq（补到了一帧）。落在区间中间时把区间拆成两段。 */
  remove(seq: number): void {
    this.removeRange(seq, seq);
  }

  /** 去掉 [from, to]（这一段补到了，或者确认补不回来了）。 */
  removeRange(from: number, to: number): void {
    if (!Number.isFinite(from) || !Number.isFinite(to) || to < from) return;
    const out: Array<[number, number]> = [];
    for (const [x, y] of this.ranges) {
      if (y < from || x > to) {
        out.push([x, y]);
        continue;
      }
      if (x < from) out.push([x, from - 1]);
      if (y > to) out.push([to + 1, y]);
    }
    this.ranges = out;
  }

  has(seq: number): boolean {
    for (const [x, y] of this.ranges) {
      if (seq < x) return false;
      if (seq <= y) return true;
    }
    return false;
  }

  /** 缺的帧总数。 */
  total(): number {
    let n = 0;
    for (const [x, y] of this.ranges) n += y - x + 1;
    return n;
  }

  /** 最早缺的那一帧；不缺时为 null。 */
  first(): number | null {
    return this.ranges.length ? this.ranges[0]![0] : null;
  }

  list(): SeqRange[] {
    return this.ranges.map(([x, y]) => [x, y] as const);
  }

  isEmpty(): boolean {
    return this.ranges.length === 0;
  }

  clear(): void {
    this.ranges = [];
  }
}
