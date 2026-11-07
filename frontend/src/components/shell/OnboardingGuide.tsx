import { useEffect, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import clsx from "clsx";
import { tr } from "@/lib/i18n";
import { LanguageSwitch } from "@/components/shell/LanguageSwitch";
import {
  ISSUES_URL,
  RECOMMENDED_MODEL_LABEL,
  REPO_URL,
} from "@/lib/project";

// First-run guide. MAST is meant to be driven from Claude Code through the MAST
// plugin (MCP server); the built-in agent is a legacy feature. Installing and
// connecting Claude Code is walked through in the desktop launcher, so this guide
// focuses on what MAST is and how to use it once Claude Code is connected.
//
// It is a dialog that can always be closed (button, backdrop, Escape) and never
// blocks the rest of the UI: the E-STOP in the top bar must stay reachable. It
// opens by itself once per browser, and again from the "Guide" button.

const DONE_KEY = "mast.onboarding.v1.done";
const OPEN_EVENT = "mast:open-onboarding";

/** Open the guide from anywhere (top bar button, settings page). */
export function openOnboardingGuide(): void {
  window.dispatchEvent(new Event(OPEN_EVENT));
}

function readDone(): boolean {
  try {
    return window.localStorage.getItem(DONE_KEY) === "1";
  } catch {
    return false;
  }
}

function markDone(): void {
  try {
    window.localStorage.setItem(DONE_KEY, "1");
  } catch {
    /* without storage the guide opens again next time; harmless */
  }
}

function Cmd({ children }: { children: string }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    void navigator.clipboard?.writeText(children).then(
      () => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      },
      () => {},
    );
  };
  return (
    <div className="my-1.5 flex items-stretch overflow-hidden rounded-mast-ctl border border-mast-border bg-mast-bg">
      <code className="min-w-0 flex-1 overflow-x-auto whitespace-pre px-3 py-2 font-mono text-[12px] text-mast-text">
        {children}
      </code>
      <button
        type="button"
        onClick={copy}
        className="shrink-0 border-l border-mast-border px-2.5 text-[11px] text-mast-muted hover:text-mast-accent"
      >
        {copied ? tr("已复制", "Copied") : tr("复制", "Copy")}
      </button>
    </div>
  );
}

function ExtLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a href={href} target="_blank" rel="noreferrer" className="text-mast-accent underline">
      {children}
    </a>
  );
}

interface Step {
  title: string;
  body: ReactNode;
}

function steps(): Step[] {
  return [
    {
      title: tr("欢迎使用 MAST", "Welcome to MAST"),
      body: (
        <>
          <p>
            {tr(
              "MAST 是扫描隧道显微镜（STM）的控制中枢：它连接仪器、管理实验与样品记录、提供经过安全检查的技能（扫描、谱学、退针等）。",
              "MAST is a control hub for scanning tunneling microscopes (STM). It connects to the instrument, keeps experiment and sample records, and exposes safety-checked skills (scanning, spectroscopy, withdrawing the tip, and more).",
            )}
          </p>
          <div className="mt-3 rounded-mast-ctl border border-mast-accent bg-mast-accent-soft p-3">
            <p className="font-semibold text-mast-text">
              {tr("推荐用法：用 Claude Code 控制 MAST", "Recommended: control MAST from Claude Code")}
            </p>
            <p className="mt-1">
              {tr(
                `我们推荐在 Claude Code 中使用 ${RECOMMENDED_MODEL_LABEL}，通过 MAST 插件（MCP 服务器）来操作仪器。Claude Code 负责思考与规划，MAST 负责执行、安全检查和记录。`,
                `We recommend Claude Code with ${RECOMMENDED_MODEL_LABEL}, connected through the MAST plugin (an MCP server). Claude Code does the reasoning and planning; MAST executes, enforces safety checks and keeps the records.`,
              )}
            </p>
            <p className="mt-1">
              {tr(
                "怎么装 Claude Code、怎么把它连上 MAST，MAST 启动器（你启动 MAST 的那个桌面程序）里有分步引导；插件会在 Claude Code 第一次运行时自动装好，一般不用手动操作。",
                "Installing Claude Code and connecting it to MAST is walked through in the MAST launcher (the desktop app you started MAST from); the plugin registers itself the first time Claude Code runs, so there is usually nothing to set up by hand.",
              )}
            </p>
            <p className="mt-1">
              {tr(
                "MAST 内置的 agent（仪器 Chat、Agents 等）属于旧版功能，默认隐藏，可在「设置 → 常规设置 → 旧版功能」中重新打开。",
                "The built-in agents (Instrument Chat, Agents, etc.) are legacy features. They are hidden by default and can be turned back on under Settings → General → Legacy features.",
              )}
            </p>
          </div>
          <p className="mt-3">
            {tr("界面语言可以随时切换：", "You can switch the interface language at any time:")}
          </p>
          <LanguageSwitch className="mt-1.5" />
        </>
      ),
    },
    {
      title: tr("用 Claude Code 开始一次会话", "Start a session with Claude Code"),
      body: (
        <>
          <p>
            {tr(
              "你能看到这个页面，说明 MAST 主服务已经在运行、Claude Code 可以在本机连上它。直接用自然语言告诉 Claude 你想做什么，例如：",
              "If you can see this page, the MAST main service is running and Claude Code can reach it on this machine. Tell Claude what you want in plain language, for example:",
            )}
          </p>
          <Cmd>
            {tr(
              "读一下 MAST 的 briefing，看看仪器现在的状态，然后为样品 Au(111) 建一个实验。",
              "Read the MAST briefing, check the instrument state, then set up an experiment for an Au(111) sample.",
            )}
          </Cmd>
          <ul className="mt-2 list-disc space-y-1 pl-5">
            <li>
              {tr(
                "Claude 会先调用 mast_briefing、mast_observe；每次仪器动作之后都会先看结果再做下一步。",
                "Claude starts with mast_briefing and mast_observe, and looks at the result of every instrument action before the next one.",
              )}
            </li>
            <li>
              {tr(
                "扫描和谱学需要先设定实验与样品（mast_scope），否则会被拒绝。",
                "Scans and spectroscopy need an experiment and sample (mast_scope) first; otherwise they are refused.",
              )}
            </li>
            <li>
              {tr(
                "顶栏的操作模式（安全 / 半自动 / 自动）由你决定，Claude 不会更改。急停（E-STOP）始终可用。",
                "The operating mode in the top bar (Safe / Semi / Auto) is your choice and Claude does not change it. E-STOP is always available.",
              )}
            </li>
            <li>
              {tr(
                "第一次在真仪器上使用时，请在旁边监督。",
                "Stay with the instrument the first time you use it on real hardware.",
              )}
            </li>
          </ul>
          <p className="mt-2 text-mast-muted">
            {tr(
              "完整的中英文指南随安装包提供，位于安装目录的 docs\\external\\ 下。",
              "The full guide (English and Chinese) ships with the installer under docs\\external\\ in the install folder.",
            )}
          </p>
        </>
      ),
    },
    {
      title: tr("从另一台电脑使用（Tailscale）", "Use it from another computer (Tailscale)"),
      body: (
        <>
          <p>
            {tr(
              "想在别的电脑上用 Claude Code 或 Codex 操作这台仪器？在仪器电脑的 MAST 启动器里点「设置 AI 远程访问」按钮——它用 Windows 自带的 SSH 加一把密钥，把访问限制在你自己的 Tailscale 网络内，并给出要在另一台电脑上用的那行 ssh 命令。两台电脑都装 Tailscale、登录同一账号即可（",
              'Want Claude Code or Codex on another computer to work with this instrument? On the instrument PC, click the "Set up AI remote access" button in the MAST launcher — it uses Windows\' built-in SSH plus a key, limits access to your own Tailscale network, and gives you the ssh line to use on the other computer. Just install Tailscale on both computers and sign in to the same account (',
            )}
            <ExtLink href="https://tailscale.com/download">tailscale.com/download</ExtLink>
            {tr("）。", ").")}
          </p>
          <p className="mt-2">
            {tr(
              "想在另一台电脑上打开这个网页界面：在启动器勾选「局域网 / 远程访问」、设用户名和密码、点「重启主服务」，然后打开 https://<仪器电脑的 Tailscale IP>:7862。用数字 IP，不要用 .ts.net 名字（浏览器会拒绝自签证书）。",
              'To open this web interface on the other computer: tick "LAN / remote access" in the launcher, set a username and password, click "Restart main service", then open https://<the instrument PC\'s Tailscale IP>:7862. Use the numeric IP, not the .ts.net name (browsers reject the self-signed certificate).',
            )}
          </p>
        </>
      ),
    },
    {
      title: tr("支持的仪器与反馈", "Supported instruments and feedback"),
      body: (
        <>
          <p>
            {tr(
              "MAST 目前支持 Nanonis 控制器（通过 Nanonis TCP 编程接口）。架构上可以添加其他控制器与外设的支持。",
              "MAST currently supports Nanonis controllers through the Nanonis TCP programming interface. Support for other controllers and peripherals can be added.",
            )}
          </p>
          <p className="mt-2">
            {tr(
              "需要支持其他仪器、遇到问题或有建议，欢迎在 GitHub 上提 issue：",
              "If you need support for another instrument, hit a problem or have a suggestion, please open an issue on GitHub:",
            )}
          </p>
          <p className="mt-1">
            <ExtLink href={ISSUES_URL}>{ISSUES_URL}</ExtLink>
          </p>
          <p className="mt-1 text-mast-muted">
            {tr("项目主页：", "Project page: ")}
            <ExtLink href={REPO_URL}>{REPO_URL}</ExtLink>
          </p>
          <p className="mt-3 text-mast-muted">
            {tr(
              "标注为「不完善」的功能仍在开发中；标注为「旧版」的功能默认隐藏。这个引导可以随时从顶栏的「引导」按钮重新打开。",
              'Features marked "incomplete" are still being developed; features marked "legacy" are hidden by default. You can reopen this guide from the "Guide" button in the top bar.',
            )}
          </p>
        </>
      ),
    },
  ];
}

export function OnboardingGuide() {
  const [open, setOpen] = useState<boolean>(() => !readDone());
  const [i, setI] = useState(0);

  useEffect(() => {
    const onOpen = () => {
      setI(0);
      setOpen(true);
    };
    window.addEventListener(OPEN_EVENT, onOpen);
    return () => window.removeEventListener(OPEN_EVENT, onOpen);
  }, []);

  const close = () => {
    markDone();
    setOpen(false);
  };

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  if (!open) return null;
  const all = steps();
  const step = all[i]!;
  const last = i === all.length - 1;

  return createPortal(
    <div
      className="fixed inset-0 z-[9000] flex items-center justify-center bg-black/50 p-4"
      onClick={close}
    >
      <div
        role="dialog"
        aria-modal="false"
        aria-label={tr("MAST 使用引导", "MAST getting started")}
        data-testid="onboarding-guide"
        onClick={(e) => e.stopPropagation()}
        className="flex max-h-[90vh] w-full max-w-[640px] flex-col rounded-mast-card border border-mast-border bg-mast-panel shadow-xl"
      >
        <div className="flex items-center justify-between border-b border-mast-border px-5 py-3">
          <div>
            <div className="text-[11px] uppercase tracking-wider text-mast-faint">
              {tr("使用引导", "Getting started")} · {i + 1} / {all.length}
            </div>
            <h2 className="text-[17px] font-semibold text-mast-text">{step.title}</h2>
          </div>
          <button
            type="button"
            onClick={close}
            aria-label={tr("关闭", "Close")}
            className="rounded px-2 py-1 text-mast-muted hover:text-mast-text"
          >
            ✕
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4 text-[13.5px] leading-relaxed text-mast-text">
          {step.body}
        </div>
        <div className="flex items-center justify-between border-t border-mast-border px-5 py-3">
          <div className="flex gap-1">
            {all.map((_, k) => (
              <button
                key={k}
                type="button"
                onClick={() => setI(k)}
                aria-label={`${k + 1}`}
                className={clsx(
                  "h-2 w-2 rounded-full",
                  k === i ? "bg-mast-accent" : "bg-mast-border hover:bg-mast-muted",
                )}
              />
            ))}
          </div>
          <div className="flex gap-2">
            {i > 0 && (
              <button
                type="button"
                onClick={() => setI(i - 1)}
                className="rounded-mast-ctl border border-mast-border px-3 py-1.5 text-[12.5px] text-mast-muted hover:text-mast-text"
              >
                {tr("上一步", "Back")}
              </button>
            )}
            <button
              type="button"
              onClick={() => (last ? close() : setI(i + 1))}
              className="rounded-mast-ctl bg-mast-accent px-3 py-1.5 text-[12.5px] font-semibold text-white"
            >
              {last ? tr("开始使用", "Get started") : tr("下一步", "Next")}
            </button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}
