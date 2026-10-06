/**
 * TUI 装配：TuiMainScreen（保留终端 scrollback，长任务回看友好）+ Editor +
 * 对话投影。所有状态来自服务端事件流（GET /stream 重连续传 + seq 幂等），
 * 本地不持有第二份会话 JSONL。
 *
 * 在场协议（ADR-0046 / Spec 11 第 6.2 节）：
 * - Ctrl+C / /quit 只停 TUI，不杀 Python Core 在途 Tool；退出时 best-effort
 *   POST /client-exit（服务端接缝未开时如实提示，不伪造暂停成功）；
 * - client_absent 暂停不自动续跑；/resume 显式提交 resume_basis=client_return。
 */
import {
  Container,
  Editor,
  ProcessTerminal,
  Spacer,
  Text,
  TuiMainScreen,
  getTerminalColorMode,
  type TUI,
} from "@earendil-works/pi-tui";

import { ApiClient } from "./api.ts";
import { applyEvent, createState, type ConversationState } from "./adapter.ts";
import { formatTokens } from "./format.ts";
import { SeqCursor, openStream } from "./sse.ts";
import { createTheme, type IaTheme } from "./theme.ts";
import {
  conversationComponents,
  emptyStateComponent,
  statusLine,
  turnComponents,
} from "./views/chat.ts";
import { approvalLines } from "./views/approval.ts";
import { renderPauseLines, renderResumeHint } from "./views/pause.ts";

const RECONNECT_DELAY_MS = 1000;

export interface AppOptions {
  baseUrl: string;
  sessionId: string;
}

export class TuiApp {
  readonly state: ConversationState = createState();
  readonly cursor = new SeqCursor();
  private readonly api: ApiClient;
  private readonly theme: IaTheme;
  private readonly tui: TUI;
  private readonly editor: Editor;
  private readonly chatContainer = new Container();
  private readonly footerContainer = new Container();
  private readonly statusText = new Text("");
  private pauseBannerText: Text | null = null;
  private approvalText: Text | null = null;
  private running = true;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private renderedTurnCount = 0;

  constructor(private readonly options: AppOptions) {
    this.theme = createTheme(getTerminalColorMode());
    this.api = new ApiClient(options.baseUrl);
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
    // 批准内联问答：y/N 键盘决策；消费按键防止漏进编辑器。
    const approval = this.state.pendingApprovals[0];
    if (approval !== undefined && (data === "y" || data === "n")) {
      void this.decideApproval(approval.approvalId, data === "y");
      return { consume: true };
    }
    return undefined;
  }

  /** 启动：拉历史重建 + 订阅直播流 + 渲染循环。 */
  async start(): Promise<void> {
    await this.rebuildFromHistory();
    this.tui.start();
    this.tui.setFocus(this.editor);
    void this.subscribeLoop();
  }

  /** 全量重建（进会话 / truncated）：GET /events，幂等游标回填 max seq。 */
  async rebuildFromHistory(): Promise<void> {
    const events = await this.api.getEvents(this.options.sessionId);
    for (const event of events) applyEvent(this.state, event);
    const maxSeq = events.reduce(
      (acc, e) => (e.seq !== null && e.seq > acc ? e.seq : acc), -1,
    );
    this.cursor.markRebuilt(maxSeq);
    this.renderAll();
  }

  /** 订阅循环：断开按游标重连（seq 幂等去重）；truncated 走全量重建。 */
  private async subscribeLoop(): Promise<void> {
    while (this.running) {
      const outcome = await new Promise<"ended" | "error" | "truncated">((resolve) => {
        void openStream(
          this.options.baseUrl,
          this.options.sessionId,
          this.cursor,
          {
            onFrame: (frame) => {
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
        );
      });
      if (!this.running) break;
      if (outcome === "truncated") continue; // 重建后立即重连
      await new Promise((resolve) => {
        this.reconnectTimer = setTimeout(resolve, RECONNECT_DELAY_MS);
      });
    }
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
    const [name] = command.split(/\s+/);
    const sessionId = this.options.sessionId;
    try {
      switch (name) {
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
          this.appendNote("commands: /cancel /compact /budget /progress /resume /quit");
          break;
        default:
          this.appendNote(`unknown command: ${command}（/help 查看可用命令）`);
      }
    } catch (error) {
      this.appendNote(`command failed: ${String(error)}`);
    }
  }

  /** 显式续跑：client_absent 只接受 client_return；用户命令触发，不自动。 */
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
    await this.api.resume(this.options.sessionId, {
      resume_basis: "client_return",
      expected_version: pause.budgetVersion ?? undefined,
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

  /** 全量重画（历史重建 / truncated 重建）。 */
  private renderAll(): void {
    this.chatContainer.clear();
    this.footerContainer.clear();
    this.pauseBannerText = null;
    this.approvalText = null;
    this.renderedOrphanCount = 0;
    if (this.state.turns.length === 0) {
      this.chatContainer.addChild(emptyStateComponent(this.theme));
    }
    for (const component of conversationComponents(this.state, this.theme)) {
      this.chatContainer.addChild(component);
    }
    this.renderedTurnCount = this.state.turns.length;
    this.renderOrphanArtifacts();
    this.renderPauseBanner();
    this.renderApproval();
    this.refreshStatus();
  }

  /** 增量投影：只追加新轮次 + 刷新页脚（流式不闪烁由差分渲染兜底）。 */
  private renderIncremental(): void {
    while (this.renderedTurnCount < this.state.turns.length) {
      const turn = this.state.turns[this.renderedTurnCount];
      if (turn === undefined) break;
      if (this.renderedTurnCount > 0 || this.state.turns.length > 1) {
        // 轮次之间空一行（aesthetics 间距纪律）
        this.chatContainer.addChild(new Spacer());
      }
      for (const component of turnComponents(turn, this.theme)) {
        this.chatContainer.addChild(component);
      }
      this.renderedTurnCount += 1;
    }
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

  private renderedOrphanCount = 0;

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
