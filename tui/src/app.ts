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
  MAX_IMAGE_BYTES,
} from "./lib/image-paste.ts";
import { renderDraftImage } from "./lib/image-view.ts";
import {
  compactDraftImages,
  imageMarker,
  parseImageMarkers,
  stripImageMarkers,
  type PendingImage,
} from "./lib/pending-images.ts";
import { resolveVisionSupport, type ModelOptionView } from "./lib/vision.ts";
import { SeqCursor, openStream } from "./sse.ts";
import { createTheme, GLYPHS, type IaTheme } from "./theme.ts";
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
/**
 * 连续重连失败上限（#843，W-21 D13）：Host 被 kill 后桌面以**新端口**重开服务，已附着的
 * TUI 不重解析端点文件，旧 base 永久 `fetch failed`；修复前是无声的永久重连风暴。
 * 本票裁决只做「熔断 + 可见提示」，不做 base 迁移（scope 锁死）。
 *
 * 取值 = 5（连续失败，退避窗口约 5 秒），与 `vscode-languageclient`
 * `DefaultErrorHandler` 同值、语义相近：来源 microsoft/vscode-languageserver-node
 * client/src/common/client.ts:1181 `new DefaultErrorHandler(this, maxRestartCount ?? 4)`
 * （即连续 5 次退出后 `CloseAction.DoNotRestart` + 用户可见 message，client.ts:465-470）。
 * 更短会和单次网络抖动难以区分，更长只是把「转瞬即逝 vs 已死」的判定拖长；
 * 只有干净收束（`ended`：Host 应答了流）才重置，所以短暂抖动不会累积成假熔断。
 */
const MAX_RECONNECT_FAILURES = 5;

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
   * 待发图片的**内容版本**：数组每次增删都 +1。footer 投影按「版本 + 正文标记集」缓存，
   * 没有变化就不重投影（`renderDraftImage` 要 base64 编码整张图，逐键重画代价太大）。
   */
  private pendingImagesVersion = 0;
  /** 上一次 footer 图片区投影的键（版本 + 标记集）；`null` = 还没投影过。 */
  private imageViewKey: string | null = null;
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
  /** 连续重连失败次数（#843）；干净收束（`ended`）归零，达 `MAX_RECONNECT_FAILURES` 熔断。 */
  private reconnectFailures = 0;
  /** 是否已停止重连（熔断后为 true；提示横幅在场期间不再订阅）。 */
  private streamStopped = false;
  /** 熔断提示横幅（footer 常驻，与 pause/approval 横幅同位）；null = 未熔断。 */
  private streamHintText: Text | null = null;

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
    // #3（独立审查 P3）：footer 的图片区是「正文当前标记集」的投影，所以正文一变就要
    // 重投影。pi-tui Editor 的 onChange 覆盖全部文本改动（输入 / 删除 / 粘贴 / setText），
    // 是这里唯一可靠的钩子。
    this.editor.onChange = () => this.renderPendingImages();
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
    // AC1：粘贴剪贴板图片。键位见 `clipboardImageBindings`（恒为 Alt+V）。用 pi-tui
    // matchesKey 而非裸比较 `\x1bv`：Kitty/CSI-u 扩展编码下裸字节不成立（与 Ctrl+T 同一条纪律）。
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
   *  切会话（generation 变化）后旧循环退出，由 switchSession 起新循环。
   *  连续失败达 `MAX_RECONNECT_FAILURES` 即熔断退出（#843）：不再重试并给出可见提示。 */
  private subscribeLoop(): void {
    const gen = this.generation;
    void (async () => {
      while (this.running && gen === this.generation) {
        let rebuild: Promise<void> = Promise.resolve();
        let outcome = await new Promise<"ended" | "error" | "truncated">((resolve) => {
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
                rebuild = this.rebuildFromHistory();
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
        if (outcome === "truncated") {
          // ADR-0016 2.3 节：先等全量重建完成，再以重建游标重连（对齐 web doTruncatedRebuild）；
          // 重建失败按一次 "error" 计（与 web scheduleReconnect 同一退避/额度）。
          try {
            await rebuild;
          } catch (error) {
            this.appendNote(`stream rebuild: ${String(error)}`);
            outcome = "error";
          }
        }
        if (!this.running || gen !== this.generation) return;
        if (outcome === "truncated") continue; // 重建已完成，按新游标立即重连
        // 计数规则（#843）：只有 outcome 为 "error" 才计一次失败；
        // "ended" 是 Host 干净收束（这条流曾活过），清零连续失败计数；
        // abort 路径在上面 generation/running 守卫处已被丢弃，不进入计数。
        this.reconnectFailures = outcome === "ended" ? 0 : this.reconnectFailures + 1;
        if (this.reconnectFailures >= MAX_RECONNECT_FAILURES) {
          this.stopStreaming();
          return;
        }
        await new Promise((resolve) => {
          this.reconnectTimer = setTimeout(resolve, RECONNECT_DELAY_MS);
        });
      }
    })();
  }

  /** 熔断收口（#843）：停止重连，并在 chat 留一行 + footer 挂常驻横幅。
   *  只停客户端重连，不 cancel 服务端 run（与本包其它失败路径同纪律）。 */
  private stopStreaming(): void {
    this.streamStopped = true;
    if (this.reconnectTimer !== null) clearTimeout(this.reconnectTimer);
    // chat 里留一行收尾（替代原本每秒一行的重连风暴），footer 再挂常驻横幅：
    // 与 pause/approval 横幅同一个渲染位置，避免熔断提示被 renderAll 漏画（#843 审查 P2）。
    this.appendNote(this.streamStoppedMessage());
    this.renderStreamHint();
  }

  /** 熔断提示文案：说清发生了什么 + 用户能做什么（chat 行与 footer 横幅共用一份）。 */
  private streamStoppedMessage(): string {
    return (
      "Host 连接已断开（可能已重启），已停止重连；请重开 TUI：" +
      `ia-tui --session ${this.options.sessionId}`
    );
  }

  /** 熔断提示（footer 常驻）：说清发生了什么 + 用户能做什么。
   *
   *  为什么是「用户自己看提示然后重开」而不是自动重试：TUI 只订阅启动时读到的那个
   *  base（`host.ts:138` 的端点文件只在自身启动/派生路径上解析），Host 换了端口就再没有
   *  对得上的地址；继续重试永远不会成功，静默重试正是本票要消灭的行为。
   *  这也是成熟产品在同一位置的选择：tmux 客户端在 server 消失时 fail-closed 退出并
   *  在一行里说清原因（tmux client.c:211 `"server exited unexpectedly"`，由
   *  client.c:581 `CLIENT_EXIT_LOST_SERVER` / client.c:781 `CLIENT_EXIT_SERVER_EXITED` 置位，
   *  client.c:185-206 `client_exit_message()` 渲染），而不是无限重连。 */
  private renderStreamHint(): void {
    if (this.streamHintText !== null) {
      this.footerContainer.removeChild(this.streamHintText);
      this.streamHintText = null;
    }
    if (!this.streamStopped) return;
    this.streamHintText = new Text(this.theme.error(`${GLYPHS.circle} ${this.streamStoppedMessage()}`));
    this.footerContainer.addChild(this.streamHintText);
    this.tui.requestRender();
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
    // 本次提交开始时的数组张数（快照）：下标 >= 它的才是各 await 窗口内新贴的图。
    // 必须在**任何 await 之前**取：下面 AC8 的视觉预检 `listModels` 同样是异地的 await，
    // 它的窗口内新贴的图若晚于快照，收尾会被当成本次已提交的图静默清掉（复审 N1）。
    const countAtSubmit = this.pendingImages.length;
    // AC3：提交时稠密重编号 -- 仍被正文引用的图保留（按原下标升序），标记被删掉的图丢弃，
    // 引用不到的悬空标记（手打 `[Image #99]`）由 compact 一并删掉。`null` = 无需改写。
    const compacted = compactDraftImages(trimmed, countAtSubmit);
    const keep = compacted === null ? this.pendingImages.map((_, index) => index) : compacted.keep;
    const content = compacted?.text ?? trimmed;
    // AC8：**只有正文里仍被引用的图**才需要视觉能力预检（独立审查 P1：按数组长度判定会把
    // "删光标记只发文字"的用户永久拒掉，而默认 preset 常常不声明 supports_vision）。
    // 判定仍在清空编辑器之前，拒绝时草稿与图片原样保留。目录查不到（网络失败）=> 不预检，
    // 让服务端 422 做权威判定（本地绝不猜"支持"）。
    if (keep.length > 0) {
      let models: ModelOptionView[] | null = null;
      try {
        models = await this.api.listModels();
      } catch {
        models = null;
      }
      if (models !== null && !resolveVisionSupport(models, this.state.modelName)) {
        this.appendNote(
          "当前模型不支持图片输入（supports_vision=false）：只有正文里仍被 [Image #N] 引用的图" +
            "才触发本预检。删掉正文里剩余的 [Image #N] 标记只发文字，或换用支持视觉的模型后重发。",
        );
        return;
      }
    }
    const keptImages = keep
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
    // 独立审查 P3（#2）：上传是异地的 await，这段窗口里用户可能又贴了新图（下标 >= countAtSubmit）
    // 且标记就在正文里（用户自己删掉标记的仍算撤销）。收尾清空数组后把这些新图重新入列，
    // 绝不被静默吞掉；窗口之前就有、却没进 keep 的图是用户主动删掉的，不许复活（AC3 语义）。
    const survivingMarkers = new Set(parseImageMarkers(this.editor.getText()));
    const carried = this.pendingImages.filter(
      (_, index) => index >= countAtSubmit && survivingMarkers.has(index + 1),
    );
    this.editor.setText("");
    this.setPendingImages([]);
    if (carried.length > 0) {
      for (const image of carried) this.addPendingImage(image);
      this.appendNote(`提交期间新粘贴的 ${String(carried.length)} 张图片已保留（标记已重编号）`);
    }
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
          : result.reason === "too_large"
            ? `图片超过 ${String(MAX_IMAGE_BYTES / (1024 * 1024))} MiB 上限` +
              `（与服务端 attachment_max_image_bytes 同口径），请压缩后重试：${filePath}`
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
    this.pendingImagesVersion += 1;
    this.editor.insertTextAtCursor(imageMarker(this.pendingImages.length));
    this.renderPendingImages();
    this.tui.requestRender();
  }

  private setPendingImages(images: PendingImage[]): void {
    this.pendingImages = images;
    this.pendingImagesVersion += 1;
    this.renderPendingImages();
    this.tui.requestRender();
  }

  /**
   * 待发图片区（AC6/AC7）：**只投影正文当前仍引用的图**（`[Image #N]` <-> 下标 N-1）--
   * 用户在正文里删掉标记，footer 的计数与缩略图立刻跟着少一张（独立审查 P3：原先渲染整个
   * 数组，删完标记仍显示，与"删标记即撤销"的提示自相矛盾）。
   * 协议可用 => pi-tui 缩略图；`WT_SESSION` 等 => 文本占位。
   * 「版本 + 标记集」没变就不重投影（见 `imageViewKey`）。
   */
  private renderPendingImages(): void {
    const markers = new Set(parseImageMarkers(this.editor.getText()));
    const visible = this.pendingImages.filter((_, index) => markers.has(index + 1));
    const key = `${String(this.pendingImagesVersion)}|${[...markers].sort((a, b) => a - b).join(",")}`;
    if (key === this.imageViewKey) return;
    this.imageViewKey = key;
    this.imagesContainer.clear();
    if (visible.length === 0) return;
    this.imagesContainer.addChild(
      new Text(
        this.theme.muted(
          `待发图片 ${String(visible.length)} 张（Alt+V 再贴一张；删掉正文里的 [Image #N] 标记即撤销）`,
        ),
      ),
    );
    for (const image of visible) {
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
    // 熔断是**上一会话**的状态：新会话重新给满阈值（#843）。
    this.reconnectFailures = 0;
    this.streamStopped = false;
    this.options.sessionId = sessionId;
    this.cursor = new SeqCursor();
    // 独立审查 P3（#6）：待发图片是**旧会话**的草稿，切会话时清空数组并抹掉正文里的标记，
    // 兑现 `pendingImages` 注释的承诺（否则旧会话的图会被带进新会话）。
    this.setPendingImages([]);
    const draft = this.editor.getText();
    if (parseImageMarkers(draft).length > 0) this.editor.setText(stripImageMarkers(draft));
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
    this.renderStreamHint();
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
