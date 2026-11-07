// "Unlock manual control" (contract §13.1): one switch per device card. Confirmed once on unlock;
// re-locks after 10 minutes without activity. While unlocked, CONFIRM-level jogs are no longer
// confirmed one by one (DANGEROUS ones still are, and stop always runs directly).
//
// The state is "time of the last action": the check is computed from now (isUnlocked) and does not
// depend on a timer firing on time, since background tabs throttle timers to >= 1 s or more and a
// timer-based lock would come late.

export const UNLOCK_IDLE_MS = 10 * 60_000;

export class UnlockStore {
  private last = new Map<string, number>();
  private listeners = new Set<() => void>();
  private version = 0;
  private now: () => number;
  readonly idleMs: number;

  constructor(now: () => number = () => Date.now(), idleMs = UNLOCK_IDLE_MS) {
    this.now = now;
    this.idleMs = idleMs;
  }

  private emit(): void {
    this.version += 1;
    for (const cb of this.listeners) {
      try {
        cb();
      } catch {
        /* a failing listener must not block the others */
      }
    }
  }

  subscribe(cb: () => void): () => void {
    this.listeners.add(cb);
    return () => {
      this.listeners.delete(cb);
    };
  }

  getVersion(): number {
    return this.version;
  }

  unlock(id: string): void {
    this.last.set(id, this.now());
    this.emit();
  }

  lock(id: string): void {
    if (this.last.delete(id)) this.emit();
  }

  /** One action: while unlocked, extends by 10 minutes; while locked, does nothing (a click never unlocks). */
  touch(id: string): void {
    if (this.isUnlocked(id)) this.last.set(id, this.now());
  }

  isUnlocked(id: string): boolean {
    const t = this.last.get(id);
    if (t === undefined) return false;
    if (this.now() - t >= this.idleMs) {
      this.last.delete(id);
      return false;
    }
    return true;
  }

  /** Time left until the automatic lock (ms); null while locked. */
  remainingMs(id: string): number | null {
    if (!this.isUnlocked(id)) return null;
    return this.idleMs - (this.now() - this.last.get(id)!);
  }
}

/** The instance the page uses (one per browser tab; reloading the page locks everything). */
export const unlockStore = new UnlockStore();
