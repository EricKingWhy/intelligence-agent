/**
 * TUI 装配：TuiMainScreen（保留终端 scrollback，长任务回看友好）+ Editor +
 * 对话投影。所有状态来自服务端事件流（GET /stream 重连续传 + seq 幂等），
 * 本地不持有第二份会话 JSONL。
 *
 * 在场协议（ADR-0046 / Spec 11 第 6.2 节）：
 * - Ctrl+C / /quit 只停 TUI，不杀 Python Core 在途 Tool；退出时 best-effort
 *   POST /client-exit（服务端接缝未开时如实提示，不伪造暂停成功）；
 * - client_absent 暂停不自动续跑；/resume 显式提交 resume_basis=client_return
 *   （wire 形状：run_id + resume_basis + budget.expected_version，app.py ResumeRequest）。
 */
import {
  Container,
  Editor,
  ProcessTerminal,
  Spacer,
  Text,
  TuiMainScreen,
  getTerminalColorMode,
  type Component,
  type TUI,
} from "@earendil-works/pi-tui";

import { ApiClient } from "./api.ts";
import {
  applyEvent,
  createState,
  type ConversationState,
  type Turn,
} from "./adapter.ts";
import { formatTokens } from "./format.ts";
import { authorizedFetch } from "./host.ts";
import { SeqCursor, openStream } from "./sse.ts";
import { createTheme, type IaTheme } from "./theme.ts";
import {
  conversationComponents,
  emptyStateComponent,
  statusLine,
  turnComponents,
} from "./views/chat.ts";
import { approvalDecision, approvalLines } from "./views/approval.ts";
import { sessionSelectList } from "./views/sessionselect.ts";
import { renderPauseLines, renderResumeHint } from "./views/pause.ts";

const RECONNECT_DELAY_MS = 1000;

export interface AppOptions {
  baseUrl: string;
  sessionId: string;
  /** 本机服务的 Bearer（host.ts 的凭据通道）；缺省 = 本地信任模式，行为不变。 */
  token?: string;
}

/** 已渲染轮次的签名：文本尾部 + 工具卡摘要（变了才重建该轮组件）。 */
function turnSignature(turn: Turn): string {
  return (
    `${turn.role}|${turn.text.length}|${turn.text.slice(-80)}|` +
    turn.tools
      .map(
        (t) =>
          `${t.toolCallId}:${t.status}:${t.output.length}:${t.message.length}:${t.artifact?.artifact_id ?? ""}`,
      )
      .join(";")
  );
}

interface RenderedTurn {
  sig: string;
  comps: Component[];
}

export class TuiApp {
  state: ConversationState = createState();
  cursor = new SeqCursor();
  private readonly api: ApiClient;
  private readonly fetchImpl: typeof fetch;
  private readonly theme: IaTheme;
  private readonly tui: TUI;
  private readonly editor: Editor;
  private readonly chatContainer = new Container();
  private readonly footerContainer = new Container();
  private readonly statusText = new Text("");
  private pauseBannerText: Text | null = null;
  private approvalText: Text | null = null;
  private renderedTurns: RenderedTurn[] = [];
  private renderedOrphanCount = 0;
  private running = true;
  /** 会话代际：切会话（/sessions /new）时 +1，旧订阅循环自行退出。 */
  private generation = 0;
  private abort = new AbortController();
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(
    private readonly options: AppOptions,
    private readonly isTTY: boolean = Boolean(process.stdin.isTTY),
  ) {
    this.theme = createTheme(getTerminalColorMode());
    // W-21 D5 (#817)：服务端非 fail-open，REST / SSE / client-exit 全走带
    // Bearer 的同一条 fetch（无 token 时 authorizedFetch 原样返回全局 fetch）。
    this.fetchImpl = authorizedFetch(options.token);
    this.api = new ApiClient(options.baseUrl, this.fetchImpl);
    const terminal = new ProcessTerminal();
    this.tui = new TuiMainScreen(terminal);
    this.editor = new Editor(this.tui, {
      borderColor: (text) => this.theme.accent(text),
      selectList: {
        selectedPrefix: (t) => this.theme.accent(t),
        selectedText: (t) => this.theme.accent(t),
        description: (t) => this.theme.muted(t),
        scrollInfo: (t) => this.theme.dim(t),
        noMatch: (t) => this.theme.muted(t),
      },
    });
    this.editor.onSubmit = (text) => void this.handleSubmit(text);
    this.tui.addChild(this.statusText);
    this.tui.addChild(this.chatContainer);
    this.tui.addChild(this.footerContainer);
    this.tui.addChild(this.editor);
    this.tui.addInputListener((data) => this.interceptKeys(data));
  }

  private interceptKeys(data: string): { consume: boolean } | undefined {
    if (data === "\x03") {
      // Ctrl+C：只停 TUI。不调 /cancel（在途 Tool 跑到稳定边界由服务端收口）。
      void this.quit();
      return { consume: true };
    }
    // 批准内联问答：TTY y/N 决策；非 TTY 在 renderApproval 处默认拒绝。
    const approval = this.state.pendingApprovals[0];
    if (approval !== undefined && (data === "y" || data === "n")) {
      const decision = approvalDecision(this.isTTY, data);
      if (decision !== "wait") {
        void this.decideApproval(approval.approvalId, decision === "approved");
      }
      return { consume: true };
    }
    return undefined;
  }

  /** 启动：拉历史重建 + 订阅直播流 + 渲染循环。 */
  async start(): Promise<void> {
    await this.rebuildFromHistory();
    this.tui.start();
    this.tui.setFocus(this.editor);
    this.subscribeLoop();
  }

  /** 全量重建（进会话 / truncated）：重置状态后从 GET /events 重投影，
   *  幂等游标回填 max seq。重放不叠加（不变量 #22：重建后状态仍可对账）。 */
  async rebuildFromHistory(): Promise<void> {
    const events = await this.api.getEvents(this.options.sessionId);
    this.state = createState();
    for (const event of events) applyEvent(this.state, event);
    const maxSeq = events.reduce(
      (acc, e) => (e.seq !== null && e.seq > acc ? e.seq : acc), -1,
    );
    this.cursor.markRebuilt(maxSeq);
    this.renderAll();
  }

  /** 订阅循环：断开按游标重连（seq 幂等去重）；truncated 走全量重建；
   *  切会话（generation 变化）后旧循环退出，由 switchSession 起新循环。 */
  private subscribeLoop(): void {
    const gen = this.generation;
    void (async () => {
      while (this.running && gen === this.generation) {
        const outcome = await new Promise<"ended" | "error" | "truncated">((resolve) => {
          void openStream(
            this.options.baseUrl,
            this.options.sessionId,
            this.cursor,
            {
              onFrame: (frame) => {
                if (gen !== this.generation) return;
                applyEvent(this.state, frame);
                this.renderIncremental();
              },
              onTruncated: () => {
                void this.rebuildFromHistory();
              },
              onClosed: (reason, error) => {
                if (reason === "error" && error !== undefined) {
                  this.appendNote(`stream reconnect: ${String(error)}`);
                }
                resolve(reason);
              },
            },
            { signal: this.abort.signal, fetchImpl: this.fetchImpl },
          );
        });
        if (!this.running || gen !== this.generation) return;
        if (outcome === "truncated") continue; // 重建后立即重连
        await new Promise((resolve) => {
          this.reconnectTimer = setTimeout(resolve, RECONNECT_DELAY_MS);
        });
      }
    })();
  }

  private async handleSubmit(text: string): Promise<void> {
    const trimmed = text.trim();
    this.editor.addToHistory(text);
    this.editor.setText("");
    if (!trimmed) return;
    if (trimmed.startsWith("/")) {
      await this.runCommand(trimmed);
      return;
    }
    try {
      await this.api.sendMessage(this.options.sessionId, trimmed);
    } catch (error) {
      this.appendNote(`send failed: ${String(error)}`);
    }
  }

  /** 斜杠命令：走服务端接口，不在本地造第二套状态。 */
  private async runCommand(command: string): Promise<void> {
    const [name, ...args] = command.split(/\s+/);
    const sessionId = this.options.sessionId;
    try {
      switch (name) {
        case "/sessions":
          await this.showSessionPicker();
          break;
        case "/new": {
          const task = args.join(" ").trim();
          const created = await this.api.createSession();
          const newId = created.session_id;
          if (typeof newId !== "string" || !newId) {
            this.appendNote("create failed: 服务端未返回 session_id");
            break;
          }
          await this.switchSession(newId);
          if (task) await this.api.sendMessage(newId, task);
          break;
        }
        case "/cancel":
          await this.api.cancel(sessionId);
          this.appendNote("cancel requested");
          break;
        case "/compact":
          await this.api.compact(sessionId);
          this.appendNote("compact requested");
          break;
        case "/budget": {
          const budget = await this.api.budget(sessionId);
          this.appendNote(`budget: ${JSON.stringify(budget)}`);
          break;
        }
        case "/progress": {
          const progress = await this.api.progress(sessionId);
          this.appendNote(`progress: ${JSON.stringify(progress)}`);
          break;
        }
        case "/resume":
          await this.resumePaused();
          break;
        case "/quit":
          await this.quit();
          break;
        case "/help":
          this.appendNote(
            "commands: /sessions /new <task> /cancel /compact /budget /progress /resume /quit",
          );
          break;
        default:
          this.appendNote(`unknown command: ${command}（/help 查看可用命令）`);
      }
    } catch (error) {
      this.appendNote(`command failed: ${String(error)}`);
    }
  }

  /** Task/Session 选择器：SelectList 覆盖层；数据来自 GET /api/sessions。 */
  private async showSessionPicker(): Promise<void> {
    const sessions = await this.api.listSessions();
    const list = sessionSelectList(sessions, this.theme);
    const handle = this.tui.showOverlay(list, { width: "80%", maxHeight: "60%" });
    list.onCancel = () => handle.hide();
    list.onSelect = (item) => {
      handle.hide();
      void this.switchSession(item.value).catch((error: unknown) => {
        this.appendNote(`switch failed: ${String(error)}`);
      });
    };
  }

  /** 切会话：打断旧订阅（generation + abort），重置状态与游标，重建 + 重订。 */
  private async switchSession(sessionId: string): Promise<void> {
    if (sessionId === this.options.sessionId) return;
    this.generation += 1;
    this.abort.abort();
    this.abort = new AbortController();
    if (this.reconnectTimer !== null) clearTimeout(this.reconnectTimer);
    (this.options as { sessionId: string }).sessionId = sessionId;
    this.cursor = new SeqCursor();
    await this.rebuildFromHistory();
    this.subscribeLoop();
  }

  /** 显式续跑：client_absent 只接受 client_return；用户命令触发，不自动。
   *  wire 形状 = ResumeRequest（app.py）：run_id + resume_basis + budget.expected_version。 */
  private async resumePaused(): Promise<void> {
    const pause = this.state.pauseInfo;
    if (pause === null) {
      this.appendNote("没有待恢复的暂停");
      return;
    }
    if (pause.reason !== "client_absent") {
      this.appendNote(
        `reason=${pause.reason} 不由 TUI 续跑；用 CLI resume（expected_version ${String(pause.budgetVersion ?? "")}）`,
      );
      return;
    }
    if (!pause.runId) {
      this.appendNote("暂停事件缺 run_id（来源 seq " + String(pause.seq ?? "?") + "），无法恢复");
      return;
    }
    await this.api.resume(this.options.sessionId, {
      run_id: pause.runId,
      resume_basis: "client_return",
      budget: { expected_version: pause.budgetVersion ?? undefined },
    });
    this.appendNote("resume submitted (client_return)");
  }

  private async decideApproval(approvalId: string, approved: boolean): Promise<void> {
    try {
      await this.api.approve(this.options.sessionId, approvalId, approved);
      this.appendNote(`approval ${approvalId}: ${approved ? "approved" : "denied"}`);
    } catch (error) {
      this.appendNote(`approve failed: ${String(error)}`);
    }
  }

  private appendNote(text: string): void {
    this.chatContainer.addChild(new Text(this.theme.muted(text)));
    this.tui.requestRender();
  }

  /** 全量重画（进会话 / truncated 重建 / 切会话）。 */
  private renderAll(): void {
    this.footerContainer.clear();
    this.pauseBannerText = null;
    this.approvalText = null;
    this.renderedOrphanCount = 0;
    if (this.state.turns.length === 0) {
      // 空状态：短文案 + 命令提示，不堆装饰框
      this.renderedTurns = [{ sig: "", comps: [emptyStateComponent(this.theme)] }];
    } else {
      this.renderedTurns = this.state.turns.map((turn) => ({
        sig: turnSignature(turn),
        comps: turnComponents(turn, this.theme),
      }));
    }
    this.rebuildChat();
    this.renderedOrphanCount = 0;
    this.renderOrphanArtifacts();
    this.renderPauseBanner();
    this.renderApproval();
    this.refreshStatus();
  }

  /** 用 renderedTurns 重建对话容器（轮次间空行；aesthetics 间距纪律）。 */
  private rebuildChat(): void {
    this.chatContainer.clear();
    this.renderedTurns.forEach((block, i) => {
      if (i > 0) this.chatContainer.addChild(new Spacer());
      for (const component of block.comps) this.chatContainer.addChild(component);
    });
  }

  /** 增量投影：只重建内容变化的轮次；新增轮次追加（流式不闪烁由差分渲染兜底）。 */
  private renderIncremental(): void {
    if (this.state.turns.length === 0 && this.renderedTurns.length === 0) return;
    // 已渲染轮次：签名变了才重建该轮（流式 delta 落在最后一轮）
    for (let i = 0; i < this.state.turns.length; i++) {
      const turn = this.state.turns[i];
      if (turn === undefined) continue;
      const sig = turnSignature(turn);
      const rendered = this.renderedTurns[i];
      if (rendered === undefined) {
        this.renderedTurns[i] = { sig, comps: turnComponents(turn, this.theme) };
      } else if (rendered.sig !== sig) {
        // 空状态占位块（sig 为空）也要让位
        this.renderedTurns[i] = { sig, comps: turnComponents(turn, this.theme) };
      }
    }
    if (this.renderedTurns.length > this.state.turns.length) {
      this.renderedTurns.length = this.state.turns.length;
    }
    this.rebuildChat();
    this.renderOrphanArtifacts();
    this.renderPauseBanner();
    this.renderApproval();
    this.refreshStatus();
  }

  /** artifact/created 找不到宿主工具卡时：一行入口占位（真实 id，不编归属）。 */
  private renderOrphanArtifacts(): void {
    while (this.renderedOrphanCount < this.state.orphanArtifacts.length) {
      const artifact = this.state.orphanArtifacts[this.renderedOrphanCount];
      if (artifact === undefined) break;
      const size = artifact.size === null ? "" : ` · ${String(artifact.size)}B`;
      const mime = artifact.mime_type === null ? "" : ` · ${artifact.mime_type}`;
      this.chatContainer.addChild(
        new Text(
          this.theme.muted(
            `artifact ${artifact.artifact_id}${size}${mime}（未关联工具卡）`,
          ),
        ),
      );
      this.renderedOrphanCount += 1;
    }
  }

  private renderPauseBanner(): void {
    if (this.pauseBannerText !== null) {
      this.footerContainer.removeChild(this.pauseBannerText);
      this.pauseBannerText = null;
    }
    const pause = this.state.pauseInfo;
    if (pause === null) return;
    const lines = [
      ...renderPauseLines(pause.data),
      renderResumeHint(this.options.sessionId, pause.data),
      `  (来源 seq ${String(pause.seq ?? "unknown")})`,
    ];
    this.pauseBannerText = new Text(lines.join("\n"));
    this.footerContainer.addChild(this.pauseBannerText);
  }

  private renderApproval(): void {
    if (this.approvalText !== null) {
      this.footerContainer.removeChild(this.approvalText);
      this.approvalText = null;
    }
    const approval = this.state.pendingApprovals[0];
    if (approval === undefined) return;
    // 非 TTY（管道/录制环境）默认拒绝（审批拒绝不可绕过）（Cline 语义）
    const decision = approvalDecision(this.isTTY, null);
    if (decision === "denied") {
      void this.decideApproval(approval.approvalId, false);
      return;
    }
    this.approvalText = new Text(approvalLines(approval, this.theme).join("\n"));
    this.footerContainer.addChild(this.approvalText);
  }

  private refreshStatus(): void {
    const usage = this.state.usageTotal;
    const tokens = usage
      ? ` · tokens in ${formatTokens(usage.prompt_tokens)}, out ${formatTokens(usage.completion_tokens)}`
      : "";
    this.statusText.setText(statusLine(this.state, this.theme) + tokens);
    this.tui.requestRender();
  }

  /** 退出：best-effort 明确退出信号（ADR-0046），停 TUI；不 cancel run。 */
  async quit(): Promise<void> {
    this.running = false;
    if (this.reconnectTimer !== null) clearTimeout(this.reconnectTimer);
    const outcome = await this.api.signalClientExit(this.options.sessionId, "ia-tui");
    if (!outcome.delivered) {
      process.stdout.write(
        `ia-tui: client-exit 信号未送达（HTTP ${outcome.status}）。` +
          "服务端在场接缝未开放时按断线宽限基线处理；在途 run 不会被本信号暂停。\n",
      );
    }
    this.tui.stop();
    process.exit(0);
  }
}
