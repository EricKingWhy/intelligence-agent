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
  Key,
  ProcessTerminal,
  Spacer,
  Text,
  TuiMainScreen,
  getTerminalColorMode,
  matchesKey,
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
import {
  clipboardImageBindings,
  readClipboardImage,
  type ClipboardImage,
  type ClipboardImageOptions,
} from "./lib/clipboard-image.ts";
import {
  clipboardImageName,
  isImagePath,
  pastedImagePath,
  readImageFile,
  uploadDeclaredName,
} from "./lib/image-paste.ts";
import { renderDraftImage } from "./lib/image-view.ts";
import {
  compactDraftImages,
  imageMarker,
  type PendingImage,
} from "./lib/pending-images.ts";
import { resolveVisionSupport, type ModelOptionView } from "./lib/vision.ts";
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
import { renderPlanList } from "./views/plan.ts";

const RECONNECT_DELAY_MS = 1000;

/** 终端括号粘贴标记（pi-tui `StdinBuffer` 也按这两个标记聚合，见 stdin-buffer.js:23）。 */
const BRACKETED_PASTE_START = "\x1b[200~";
const BRACKETED_PASTE_END = "\x1b[201~";

export interface AppOptions {
  baseUrl: string;
  sessionId: string;
  /** 本机服务的 Bearer（host.ts 的凭据通道）；缺省 = 本地信任模式，行为不变。 */
  token?: string;
  /**
   * 注入 REST fetch（#827 MM-06 的测试 seam）。缺省 = `authorizedFetch(token)`
   * （带 Bearer 的同一条 fetch）。**只为测试注入**，生产路径不受影响。
   */
  fetchImpl?: typeof fetch;
  /** 剪贴板取图（默认 `lib/clipboard-image.readClipboardImage`）；测试注入假剪贴板。 */
  readClipboardImage?: (options?: ClipboardImageOptions) => Promise<ClipboardImage | null>;
  /** 启动时预载的图片路径（`ia-tui --session s @a.png`，PRD 用户故事 19 / AC4）。 */
  initialImages?: string[];
  /** 平台/环境（剪贴板阶梯与键位判定）；缺省 = `process.platform` / `process.env`。 */
  platform?: NodeJS.Platform;
  env?: NodeJS.ProcessEnv;
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
  /** 进度清单横幅（footer 常驻；`task/plan_updated` 投影，零项不挂）。 */
  private planText: Text | null = null;
  /** 待发图片（AC3）：下标 0 即正文里的 `[Image #1]`。提交/换会话时清空。 */
  pendingImages: PendingImage[] = [];
  /**
   * 待发图片区（footer）：说明行 + 每张图的缩略图（AC7）或文本占位（AC6）。
   * 用**一个长期挂载的容器**承载（不是每次增删组件）：`renderAll()` 会 clear 整个
   * footer，把容器重挂一次即可保持"图片区在其它横幅之前"的稳定次序。
   */
  private readonly imagesContainer = new Container();
  private readonly readClipboard: (options?: ClipboardImageOptions) => Promise<ClipboardImage | null>;
  private readonly platform: NodeJS.Platform;
  private readonly env: NodeJS.ProcessEnv;
  /** 完成组折叠开关（`ctrl+t` 切换；对应 Claude Code `showExpandedTodos`）。 */
  private planExpanded = false;
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
    this.fetchImpl = options.fetchImpl ?? authorizedFetch(options.token);
    this.platform = options.platform ?? process.platform;
    this.env = options.env ?? process.env;
    this.readClipboard = options.readClipboardImage ?? readClipboardImage;
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
    this.footerContainer.addChild(this.imagesContainer);
    this.tui.addChild(this.footerContainer);
    this.tui.addChild(this.editor);
    this.tui.addInputListener((data) => this.interceptKeys(data));
    // AC4：`@path` 命令行参数在启动时进待发图片数组（标记随首轮消息发出）。
    for (const filePath of options.initialImages ?? []) this.attachImageArg(filePath);
  }

  private interceptKeys(data: string): { consume: boolean } | undefined {
    if (data === "\x03") {
      // Ctrl+C：只停 TUI。不调 /cancel（在途 Tool 跑到稳定边界由服务端收口）。
      void this.quit();
      return { consume: true };
    }
    if (matchesKey(data, Key.ctrl("t"))) {
      // Ctrl+T：切换进度清单完成组的折叠/展开（Claude Code app:toggleTodos 同款）。
      // 用 pi-tui matchesKey 而非裸比较 `\x14`：Kitty/CSI-u 扩展编码（如 tmux 上报
      // `\x1b[116;5u`）下裸字节不成立，matchesKey 同时覆盖 legacy 与 CSI-u。
      this.planExpanded = !this.planExpanded;
      this.renderPlan();
      this.tui.requestRender();
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
    // AC1：粘贴剪贴板图片。键位按平台（Windows 用 Alt+V；WSL 双绑 Ctrl+V/Alt+V）。
    // 用 pi-tui matchesKey 而非裸比较 `\x1bv`：Kitty/CSI-u 扩展编码下裸字节不成立
    // （与 Ctrl+T 同一条纪律）。
    for (const binding of clipboardImageBindings({ platform: this.platform, env: this.env })) {
      if (matchesKey(data, binding)) {
        void this.pasteClipboardImage();
        return { consume: true };
      }
    }
    // AC2：终端粘贴一整段文本时的**单块**括号粘贴（pi-tui 的 StdinBuffer 保证
    // `\x1b[200~...\x1b[201~` 要么整块到达、要么等到收齐才回调）。若粘贴内容就是一条
    // 图片文件路径 -- 识别为附图并**吞掉**这段文本（不再当正文）；否则不拦截，
    // 交给 Editor 自己处理（多行文本、普通路径都照旧）。
    if (data.startsWith(BRACKETED_PASTE_START) && data.endsWith(BRACKETED_PASTE_END)) {
      const pasted = data.slice(BRACKETED_PASTE_START.length, -BRACKETED_PASTE_END.length);
      const filePath = pastedImagePath(pasted, this.platform);
      if (filePath !== null) {
        this.attachImageFile(filePath);
        return { consume: true };
      }
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
    if (!trimmed) {
      this.editor.setText("");
      return;
    }
    if (trimmed.startsWith("/")) {
      this.editor.setText("");
      await this.runCommand(trimmed);
      return;
    }
    const imageCount = this.pendingImages.length;
    // AC8：有图先做视觉能力预检 -- **在清空编辑器之前**判定，拒绝时草稿与图片原样保留
    // （用户可删标记只发文字，或换模型后重发）。目录查不到（网络失败）=> 不预检，
    // 让服务端 422 做权威判定（本地绝不猜"支持"）。
    if (imageCount > 0) {
      let models: ModelOptionView[] | null = null;
      try {
        models = await this.api.listModels();
      } catch {
        models = null;
      }
      if (models !== null && !resolveVisionSupport(models, this.state.modelName)) {
        this.appendNote(
          "当前模型不支持图片输入（supports_vision=false）：草稿与图片已保留。" +
            "换用支持视觉的模型后重发，或删掉正文里的 [Image #N] 标记只发文字。",
        );
        return;
      }
    }
    // AC3：提交时稠密重编号 -- 仍被正文引用的图保留（按原下标升序），标记被删掉的图丢弃。
    // `null` = 无需改写（无图，或全部引用且已是 1..K），正文逐字不变。
    const compacted = compactDraftImages(trimmed, imageCount);
    const content = compacted?.text ?? trimmed;
    const keptImages =
      compacted === null
        ? [...this.pendingImages]
        : compacted.keep
            .map((index) => this.pendingImages[index])
            .filter((image): image is PendingImage => image !== undefined);
    // AC5：先按服务端契约上传每张图（字节流式 + Content-Type/<name> 与字节判定一致），
    // 拿到 attachment_id 再投消息。上传失败**保留草稿**（已改的正文也留给用户重发）。
    const attachments: string[] = [];
    try {
      for (const image of keptImages) {
        const receipt = await this.api.uploadAttachment(this.options.sessionId, image.bytes, {
          name: uploadDeclaredName(image.name, image.mimeType),
          mediaType: image.mimeType,
        });
        attachments.push(receipt.attachment_id);
      }
    } catch (error) {
      this.appendNote(`upload failed: ${String(error)}`);
      return;
    }
    this.editor.setText("");
    this.setPendingImages([]);
    try {
      await this.api.sendMessage(this.options.sessionId, content, attachments);
    } catch (error) {
      this.appendNote(`send failed: ${String(error)}`);
    }
  }

  /**
   * 剪贴板取图（AC1）：拿到字节就追加一张待发图（正文里插 `[Image #N]`）；
   * 没有图 / 读失败给一行明确提示，**不静默**（PRD 用户故事 24）。
   */
  private async pasteClipboardImage(): Promise<void> {
    let image: ClipboardImage | null;
    try {
      image = await this.readClipboard({ platform: this.platform, env: this.env });
    } catch (error) {
      this.appendNote(`clipboard image failed: ${String(error)}`);
      return;
    }
    if (image === null) {
      this.appendNote("剪贴板里没有可用的图片（支持 PNG/JPEG/WEBP/GIF）");
      return;
    }
    this.addPendingImage({
      bytes: image.bytes,
      mimeType: image.mimeType,
      // 剪贴板字节没有本地文件名；声明名由 media type 决定（扩展名必须与字节判定一致）。
      name: clipboardImageName(image.mimeType),
      path: null,
    });
  }

  /** 图片文件路径 -> 附图（AC2 的落点；读不到/不是图片给明确错误）。 */
  private attachImageFile(filePath: string): void {
    const result = readImageFile(filePath);
    if (!result.ok) {
      this.appendNote(
        result.reason === "missing"
          ? `找不到或读不了这个文件：${filePath}`
          : `不是本仓支持的图片格式（PNG/JPEG/WEBP/GIF）：${filePath}`,
      );
      return;
    }
    this.addPendingImage(result.image);
  }

  /** `@path` 命令行参数（AC4）：非图片扩展名直接报错（不静默忽略）。 */
  private attachImageArg(filePath: string): void {
    if (!isImagePath(filePath)) {
      this.appendNote(`参数 @${filePath} 不是图片文件（PNG/JPEG/WEBP/GIF）`);
      return;
    }
    this.attachImageFile(filePath);
  }

  /** 追加一张待发图：数组 push + 光标处插 `[Image #N]`（N = 插入后的张数，AC3）。 */
  private addPendingImage(image: PendingImage): void {
    this.pendingImages.push(image);
    this.editor.insertTextAtCursor(imageMarker(this.pendingImages.length));
    this.renderPendingImages();
    this.tui.requestRender();
  }

  private setPendingImages(images: PendingImage[]): void {
    this.pendingImages = images;
    this.renderPendingImages();
    this.tui.requestRender();
  }

  /** 待发图片区（AC6/AC7）：协议可用 => pi-tui 缩略图；`WT_SESSION` 等 => 文本占位。 */
  private renderPendingImages(): void {
    this.imagesContainer.clear();
    if (this.pendingImages.length === 0) return;
    this.imagesContainer.addChild(
      new Text(
        this.theme.muted(
          `待发图片 ${String(this.pendingImages.length)} 张（Alt+V 再贴一张；删掉正文里的 [Image #N] 标记即撤销）`,
        ),
      ),
    );
    for (const image of this.pendingImages) {
      const rendered = renderDraftImage(image, {
        fallbackColor: (value) => this.theme.dim(value),
      });
      this.imagesContainer.addChild(
        rendered.kind === "image" ? rendered.component : new Text(rendered.lines.join("\n")),
      );
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
    this.planText = null;
    // footer 被 clear() 了：待发图片区重挂一次（次序恒定：图片区在最前）。
    this.footerContainer.addChild(this.imagesContainer);
    // 跨会话/重建重置折叠开关：避免上一会话的展开态泄漏到新会话（#382 审查 P3）。
    this.planExpanded = false;
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
    this.renderPlan();
    this.renderPendingImages();
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

  /** 增量投影：只重建内容变化的轮次；新增轮次追加（流式不闪烁由差分渲染兜底）。
   *
   *  无早退：`renderPlan` / `renderPauseBanner` / `renderApproval` 挂在 footer，
   *  与 turns 是否为空无关；若在无 turn 时早退，`task/plan_updated` 先于任何
   *  turn 到达（或 footer 类事件单独到达）就会漏画（#382 审查 P4）。
   *  turns 与 renderedTurns 皆空时下面的循环是 no-op，重建空对话容器无害。 */
  private renderIncremental(): void {
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
    this.renderPlan();
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

  /** 进度清单横幅：完成组折叠/展开由 `ctrl+t` 控制；零项不挂（不留空壳）。 */
  private renderPlan(): void {
    const lines = renderPlanList(this.state.plan, this.theme, { expanded: this.planExpanded });
    if (lines.length === 0) {
      if (this.planText !== null) {
        this.footerContainer.removeChild(this.planText);
        this.planText = null;
      }
      return;
    }
    const text = lines.join("\n");
    if (this.planText === null) {
      this.planText = new Text(text);
      this.footerContainer.addChild(this.planText);
    } else {
      this.planText.setText(text);
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
