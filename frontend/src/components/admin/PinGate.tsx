import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { useUiStore } from "@/store";
import { Card } from "@/components/ui";
import { tr } from "@/lib/i18n";

/** Admin PIN unlock gate. Posts the raw PIN to the core (which compares its
 *  SHA-256 hex against config/admin_pin.txt); on success it flips the shared
 *  zustand flag so the rest of the page reveals. When no PIN exists yet, the
 *  gate offers to create one (POST /api/admin/set-pin accepts a first PIN
 *  without a current one). */

const REASON_TEXT: Record<string, () => string> = {
  no_pin_set: () =>
    tr(
      "尚未设置管理 PIN（config/admin_pin.txt 缺失或为空）。请先在下方设置一个。",
      "No admin PIN has been set (config/admin_pin.txt is missing or empty). Set one below first.",
    ),
  empty: () => tr("请输入 PIN。", "Enter the PIN."),
  wrong: () => tr("PIN 不正确。", "Incorrect PIN."),
  degraded: () =>
    tr(
      "PIN 文件不可读或服务不可用，管理门控暂不可用。",
      "The PIN file is unreadable or the service is unavailable, so the admin gate cannot be checked.",
    ),
};

function useSetPin(onDone: () => void) {
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const mut = useMutation({
    mutationFn: async (pin: string) => {
      const { data, error } = await api.POST("/api/admin/set-pin", {
        body: { new_pin: pin },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (data) => {
      if (data?.ok) {
        setError(null);
        queryClient.invalidateQueries({ queryKey: ["admin", "pin-status"] });
        onDone();
      } else {
        setError(data?.message || tr("设置失败", "Could not set the PIN"));
      }
    },
    onError: (e) =>
      setError(
        tr("设置失败：", "Could not set the PIN: ") + String((e as Error)?.message ?? e),
      ),
  });
  return { mut, error };
}

function SetPinForm({ onDone }: { onDone: () => void }) {
  const [pin, setPin] = useState("");
  const [again, setAgain] = useState("");
  const { mut, error } = useSetPin(onDone);
  const mismatch = again.length > 0 && pin !== again;
  const tooShort = pin.length > 0 && pin.length < 4;
  const input =
    "w-full rounded-md border border-mast-border bg-mast-bg px-3 py-2 text-sm text-mast-text outline-none focus:border-mast-accent";
  return (
    <form
      className="mt-4 space-y-2 border-t border-mast-border pt-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (pin.length >= 4 && pin === again) mut.mutate(pin);
      }}
    >
      <div className="text-sm font-medium text-mast-text">
        {tr("在此设置管理 PIN", "Set the admin PIN here")}
      </div>
      <input
        type="password"
        value={pin}
        onChange={(e) => setPin(e.target.value)}
        placeholder={tr("新 PIN（至少 4 位）", "New PIN (at least 4 characters)")}
        autoComplete="new-password"
        className={input}
      />
      <input
        type="password"
        value={again}
        onChange={(e) => setAgain(e.target.value)}
        placeholder={tr("再输一次", "Repeat the PIN")}
        autoComplete="new-password"
        className={input}
      />
      {tooShort && (
        <p className="text-xs text-mast-danger">{tr("PIN 至少 4 位。", "The PIN needs at least 4 characters.")}</p>
      )}
      {mismatch && (
        <p className="text-xs text-mast-danger">{tr("两次输入不一致。", "The two entries do not match.")}</p>
      )}
      {error && <p className="text-xs text-mast-danger">{error}</p>}
      <button
        type="submit"
        disabled={mut.isPending || pin.length < 4 || pin !== again}
        className="w-full rounded-md bg-mast-accent/20 px-3 py-2 text-sm font-medium text-mast-accent hover:bg-mast-accent/30 disabled:opacity-50"
      >
        {mut.isPending ? tr("保存中…", "Saving…") : tr("设置 PIN", "Set PIN")}
      </button>
    </form>
  );
}

export function PinGate() {
  const setPinUnlocked = useUiStore((s) => s.setPinUnlocked);
  const [pin, setPin] = useState("");
  const [reason, setReason] = useState<string | null>(null);

  const status = useQuery({
    queryKey: ["admin", "pin-status"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/admin/pin-status");
      if (error) throw error;
      return data;
    },
  });
  const noPin = status.data ? !status.data.pin_is_set : false;

  const unlock = useMutation({
    mutationFn: async (raw: string) => {
      const { data, error } = await api.POST("/api/admin/unlock-pin", {
        body: { pin: raw },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (data) => {
      if (data.ok) {
        setReason(null);
        setPinUnlocked(true);
      } else {
        setReason(data.reason ?? "wrong");
      }
    },
    onError: () => setReason("degraded"),
  });

  return (
    <div className="mx-auto max-w-lg py-12">
      <Card>
        <h2 className="mb-2 text-base font-semibold text-mast-text">
          {tr("高级管理 · 需要解锁", "Advanced · unlock required")}
        </h2>
        <p className="mb-2 text-sm text-mast-muted">
          {tr(
            "此页面包含安全限值、覆盖配置与硬件控制。请输入管理 PIN 解锁。",
            "This tab holds safety limits, configuration overrides and hardware controls. Enter the admin PIN to unlock it.",
          )}
        </p>

        <div className="mb-4 space-y-2 rounded-md border border-mast-border p-3 text-xs text-mast-muted">
          <div className="font-medium text-mast-text">
            {tr("关于管理 PIN", "About the admin PIN")}
          </div>
          <ul className="list-disc space-y-1 pl-4">
            <li>
              {tr(
                "PIN 保护：本高级管理页，以及高级能力、硬件模块、粗动驱动电压声明、修针方案覆写和站点画像这几项设置的修改。设置页里的其他项不需要 PIN。",
                "The PIN protects this Advanced tab, and changes to advanced capabilities, hardware modules, the coarse drive declaration, tip-conditioning overrides and the station profile. Other settings do not need it.",
              )}
            </li>
            <li>
              {tr(
                "没有默认 PIN：首次使用必须自己设置，未设置时上述受保护的设置一律拒绝修改。",
                "There is no default PIN. You must set one on first use; until then the protected settings above cannot be changed.",
              )}
            </li>
            <li>
              {tr(
                "设置位置：桌面启动器里的「设置高级管理密码」按钮；PIN 尚未设置时，也可以在下方直接设置。重启主服务后启动器设置的 PIN 生效。",
                "Where to set it: the “Set admin password” button in the desktop launcher; while no PIN exists you can also set it in the form below. A PIN set in the launcher takes effect after the main service restarts.",
              )}
            </li>
            <li>
              {tr(
                "忘记了：先停止 MAST，再删除安装目录下的 config/admin_pin.txt，然后重新设置。",
                "Forgot it: stop MAST, delete config/admin_pin.txt in the install folder, then set a new PIN.",
              )}
            </li>
            <li>
              {tr(
                "PIN 只防误操作（点错、旁人、没关的浏览器标签页），不是身份认证；磁盘上只保存 SHA-256，不保存原文。",
                "The PIN guards against accidents (a mis-click, a bystander, a browser tab left open). It is not authentication; only a SHA-256 hash is stored, never the PIN itself.",
              )}
            </li>
          </ul>
        </div>

        {noPin && (
          <p className="mb-3 rounded-md border border-mast-warn px-3 py-2 text-sm text-mast-warn">
            {tr(
              "这台机器还没有设置管理 PIN，请先设置一个。",
              "No admin PIN is set on this machine yet. Set one first.",
            )}
          </p>
        )}

        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            unlock.mutate(pin);
          }}
        >
          <label className="block text-sm text-mast-muted">{tr("管理密码 PIN", "Admin PIN")}</label>
          <input
            type="password"
            value={pin}
            onChange={(e) => setPin(e.target.value)}
            placeholder={tr("已设置的管理 PIN", "Your admin PIN")}
            autoComplete="off"
            className="w-full rounded-md border border-mast-border bg-mast-bg px-3 py-2 text-sm text-mast-text outline-none focus:border-mast-accent"
          />
          <button
            type="submit"
            disabled={unlock.isPending}
            className="w-full rounded-md bg-mast-accent/20 px-3 py-2 text-sm font-medium text-mast-accent hover:bg-mast-accent/30 disabled:opacity-50"
          >
            {unlock.isPending ? tr("校验中…", "Checking…") : tr("解锁", "Unlock")}
          </button>
        </form>
        {reason && (
          <p className="mt-3 text-sm text-mast-danger">
            {REASON_TEXT[reason]?.() ?? tr(`解锁失败：${reason}`, `Unlock failed: ${reason}`)}
          </p>
        )}

        {noPin && <SetPinForm onDone={() => setReason(null)} />}
      </Card>
    </div>
  );
}
