/** ToolCard — per-tool renderer with specialized layouts.
 *
 * L0-L2 Progressive Disclosure（PRD §6，ADR-0014 D2）：
 *   - L0 → summary row only（status/icon/name/args/duration）
 *   - L1 → + Input/Output inline detail
 *   - L2 → + full content surface（bash/diff/read/slice 专属块）+ raw event JSON
 * 生效级 = App 层 manual override ?? 全局 density 默认；点击行循环 L0→L1→L2。
 * hover "Inspect" chip → 进 Inspector（PRD §9.1 联动）。
 *
 * Four content shapes (L2):
 *   - bash → terminal block (mono, dark inset, exit code badge)
 *   - edit/apply_patch/write → diff view (green/red, before/after)
 *   - inspect_artifact → artifact slice view (numbered lines, per-line truncation chips)
 *   - read → continuation-aware block；everything else → generic JSON card
 *
 * Status color: running=warning, success=green, failed=red.
 */

import { memo, useEffect, useRef, useState, type FocusEvent, type MouseEvent } from 'react';
import { createPortal } from 'react-dom';
import { AlertTriangle, ArrowRight, Scissors } from 'lucide-react';
import type { ToolCall } from '../types';
import type { TraceDensity } from '../lib/density';
import { defaultLevelFor, toolEventKey, type DisclosureLevel } from '../lib/disclosure';
import { formatDuration, formatShortDuration, stringifyForDisplay, truncateForDisplay } from '../lib/format';
import { toolKind } from '../lib/eventKind';
import {
  GREP_TRUNCATED_SUFFIX,
  hasGrepTruncatedSuffix,
  parseReadShape,
  splitMcpToolName,
  stripGrepTruncatedSuffix,
  type ReadShape,
} from '../lib/toolShapes';
import { useTickingNow } from '../hooks/useTickingNow';
import { caret } from './Caret';
import { kindGlyph, statusGlyph } from './Glyph';
import { CopyButton } from './CopyButton';
import { Collapse } from './Collapse';
import { ToolOutputStream } from './ToolOutputStream';
import { ArtifactViewer } from './ArtifactViewer';
import { DiffBlock } from './DiffBlock';
import { JsonTree } from './JsonTree';

interface Props {
  tool: ToolCall;
  density: TraceDensity;
  /** 生效展开级（override ?? density 默认）。缺省 = density 推导（未接 disclosure 的旧用法）。 */
  level?: DisclosureLevel;
  /** 点击行 → 循环 L 级（F1/#270：TurnView 用 useCallback 建立的**引用稳定**回调，
   *  否则本组件的 memo 恒 miss）。带 (key, density) 参数：key 由本组件从自己的
   *  tool_call_id 现算，上层就不必按节点现场建闭包。缺省回退旧行为。 */
  onCycleLevel?: (key: string, density: TraceDensity) => void;
  /** hover Inspect chip 点击 → 进 Inspector。缺省时点击行为回退（旧：onFocus / 本地展开）。 */
  onFocus?: (tool: ToolCall) => void;
  /** #186：归档 diff 的「就地展开」要按会话读 artifact 内容。缺省 = 不提供展开入口。 */
  sessionId?: string;
}

// memo：投影层 copy-on-write 保证未触及的 tool 引用稳定——同 turn 内其它工具卡
// 在本工具更新时跳过重渲染（配合 App 层 useCallback 稳定的回调）。
export const ToolCard = memo(function ToolCard({ tool, density, level, onCycleLevel, onFocus, sessionId }: Props) {
  const isBash = tool.name === 'bash';
  const isDiffTool = ['edit', 'apply_patch', 'write'].includes(tool.name);
  const slice = tool.name === 'inspect_artifact' ? tryParseSlice(tool.result) : null;
  // read 新形状（df4f7d8 §1.3）：解析续读/单行截断标记；形状不符回退 GenericBlock。
  const readShape = tool.name === 'read' ? parseReadShape(tool.result) : null;
  const duration = formatDuration(tool.started_at, tool.completed_at);
  // 生效 L 级：显式 level 优先，否则 density 推导（旧契约：detailed/raw 展开明细）。
  const effectiveLevel: DisclosureLevel = level ?? defaultLevelFor(density);
  const hasInspect = Boolean(onFocus);
  // PRD §5.2/§10.2：语义图标按工具 kind 分发（终端/查阅/写入/MCP/扳手），
  // 替换原 bash/其它 二分；状态仍由 act-status 列表达（中断 ≠ 错误）。
  // F-10：kind 同时落到 data-kind，图标色是唯一按类型分色的通道。
  const kind = toolKind(tool.name);
  // 失败摘要（PRD §5.2 Error：第二行 error_code 摘要，不满屏红、不强制展开）。
  // 真相在投影层：`tool/result` 的信封 `{ok,message,error_code}` 在那里解析一次
  // （`ToolCall.error`）。视图不再回头拆 `result`——`data` 为空时 result 只剩
  // message 字符串，error_code 早就不在里面了（#1429 F1）。
  const errorShape = tool.status === 'failed' ? tool.error ?? null : null;

  const handleRowClick = () => {
    if (onCycleLevel) {
      onCycleLevel(toolEventKey(tool.tool_call_id), density);
      return;
    }
    // 旧回退（未接 disclosure 的用法）：点击 = 进 Inspector。
    onFocus?.(tool);
  };

  /* #1429 打磨点 5：参数中间省略。头段可收缩（CSS ellipsis），尾段固定——
     行宽不足时省略号落在**中段**，尾部的路径/文件仍可辨（V2 `.tc-arg .ma/.mb`）。
     显示串不预截断（`summarizeArgs` 默认不再砍到 60 字）：否则阈值 64 的中间省略
     永远触发不了，长命令只剩前 60 字，tooltip 也只是那 60 字（#1429 F5）。 */
  const argsSummary = summarizeArgs(tool);
  const argsParts = splitArgsForDisplay(argsSummary);
  const argsTruncatable = argsParts.tail !== '';
  const [argTip, setArgTip] = useState<{ x: number; y: number } | null>(null);
  const argsRef = useRef<HTMLSpanElement>(null);
  /* tooltip 的 id 直接由 tool_call_id 派生（全局唯一），不取 `useId()`：每行一次
     hook + 上下文读写在 500 行规模上是可测的固定税（#1429 F2）。 */
  const tipId = `arg-tip-${tool.tool_call_id}`;
  const closeTip = () => setArgTip(null);
  /* 键盘路径（#1429 P3）：参数行本身**不可聚焦**——带 `tabindex` 的 span 嵌在
     行 button 里属于非法 interactive 嵌套，浏览器不会把它放进 Tab 序。改为行
     button 聚焦时给出同一份 tooltip（按钮是合法焦点目标，Esc 关闭不变）。 */
  const openArgTip = () => {
    const el = argsRef.current;
    if (!el || !argsTruncatable) return;
    const r = el.getBoundingClientRect();
    setArgTip({ x: r.left, y: r.bottom + 4 });
  };
  useEffect(() => {
    if (argTip === null) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closeTip();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [argTip]);

  /* 快捷动作（复制 / 定位）只在行被 hover 或聚焦后挂载——理由见下方 JSX 注释。
     失焦时若焦点落进本卡内部（Tab 到复制按钮），保持挂载。 */
  const [actionsShown, setActionsShown] = useState(false);
  /* 焦点仍在卡内时不卸载快捷动作：Tab 到复制按钮的瞬间指针已离开卡片，无条件
     `setActionsShown(false)` 会把正要激活的按钮抽走（#1429 复审 P3）。与 onRowBlur
     同一条判据，两条退出路径共用。 */
  const onRowLeave = (e: MouseEvent<HTMLDivElement>) => {
    const active = document.activeElement;
    if (active instanceof Node && e.currentTarget.contains(active)) return;
    setActionsShown(false);
  };
  const onRowBlur = (e: FocusEvent<HTMLDivElement>) => {
    const next = e.relatedTarget;
    if (next instanceof Node && e.currentTarget.contains(next)) return;
    setActionsShown(false);
  };

  const detailOpen = effectiveLevel >= 1;
  const bodyOpen = effectiveLevel >= 2;
  /* 收起动画要保留（`Collapse` 靠 transitionend 卸载子树），所以「开过一次」之后
     两个 Collapse 一直挂着；但 L0 常态下**一个都不挂**——闭合的 Collapse 仍是一个
     组件 + 4 个 hook，200 行常驻就是每次渲染白付 ~6ms（#1429 F2 perf 探针）。
     ref 在渲染期写入是幂等的，不会触发额外渲染。 */
  const openedOnce = useRef(false);
  if (detailOpen || bodyOpen) openedOnce.current = true;
  /* V2 `.tc` 的两种状态底色：失败淡红、运行中主色呼吸条。 */
  const stateClass =
    tool.status === 'failed' ? ' is-fail' : tool.status === 'running' ? ' is-run' : '';
  /* 复制载荷只在本卡真的露出快捷动作时才算：`JSON.stringify(result, null, 2)` 是
     整份结果的序列化，200 张卡每次渲染都算一遍纯属白付（#1429 F2）。 */
  const copyText = !actionsShown || tool.result === undefined
    ? ''
    : typeof tool.result === 'string'
      ? tool.result
      : JSON.stringify(tool.result, null, 2);

  return (
    <div
      /* data-stream-key：Main↔Inspector 联动定位锚（PRD §9.2 反向跳转目标）+ pulse */
      className={`act-node-wrap act-card${stateClass}`}
      data-stream-key={`tool:${tool.tool_call_id}`}
      onMouseEnter={() => setActionsShown(true)}
      onMouseLeave={onRowLeave}
      onFocus={() => setActionsShown(true)}
      onBlur={onRowBlur}
    >
      <button
        className={`act-node act-node-${density}`}
        onClick={handleRowClick}
        aria-level={effectiveLevel}
        aria-expanded={detailOpen}
        /* 键盘路径：行 button 是真焦点目标；聚焦即给出全量参数 tooltip，
           失焦关闭（Esc 在 window 级监听里，两条路径共用）。
           `aria-describedby` 也挂在 button 上——tooltip 的触发者与读屏的关联点
           必须是**同一个**可聚焦元素（#1429 复审 P3：挂在不可聚焦的 `.act-args`
           span 上，键盘用户永远读不到它描述的内容）。 */
        aria-describedby={argTip !== null ? tipId : undefined}
        onFocus={openArgTip}
        onBlur={closeTip}
      >
        <span className={`act-status act-status-${tool.status}`}>
          {tool.status === 'running'
            ? <span className="status-spinner" />
            : statusGlyph(tool.status)}
        </span>
        {density !== 'compact' && <span className="act-icon" data-kind={kind}>{kindGlyph(kind, 14)}</span>}
      {/* MCP 工具名（da394a9 Phase 8）：mcp__{server}__{tool} 拆 server 徽章 + 工具名 */}
      {(() => {
        const mcp = splitMcpToolName(tool.name);
        return mcp ? (
          <span className="act-name">
            <span className="act-server" title={`MCP server: ${mcp.server}`}>{mcp.server}</span>
            {mcp.tool}
          </span>
        ) : (
          <span className="act-name">{tool.name}</span>
        );
      })()}
      {density !== 'compact' && (
        <span
          ref={argsRef}
          className="act-args"
          data-full={argsTruncatable ? argsSummary : undefined}
          onMouseEnter={openArgTip}
          onMouseLeave={closeTip}
        >
          {/* 不拆分时（参数 ≤64 字）直接放文本：`.act-args` 自己就是省略容器，
              少一个 span（200 行就是 200 个节点，见 perf 探针）。拆分时才需要
              head/tail 两个盒子做**中段**省略。 */}
          {argsParts.tail === '' ? argsParts.head : (
            <>
              <span className="act-args-head">{argsParts.head}</span>
              <span className="act-args-tail">{argsParts.tail}</span>
            </>
          )}
        </span>
      )}
      {density === 'compact' && (
        <span className="act-args act-args-compact">{summarizeArgs(tool, 32)}</span>
      )}
      <span className="act-duration">
        {duration ?? (tool.status === 'running' ? <LiveToolDuration startedAt={tool.started_at} /> : null)}
      </span>
      {caret('act-chev')}
      </button>
      {/* V2 打磨点 3：悬停浮现的快捷动作（复制 / 定位）。定位走 Inspector 联动
          （`onFocus`），不做工具级重试——失败卡按 P0-1 裁决只呈现错误。
          只在**行被 hover / 聚焦后**挂载：复制按钮自带 3 个 hook + 图标，200 行常驻
          的代价是单次渲染的 1/3（见 perf 探针）；而这组动作本来就只在悬停时可见，
          常驻 DOM 只是白白拖慢首帧。键盘可达：行内聚焦即挂载，Tab 下一步就能到。 */}
      {actionsShown && (hasInspect || copyText !== '') && (
        <span className="act-quick-acts">
          {copyText !== '' && (
            <span className="act-quick-act">
              <CopyButton text={copyText} label="复制输出" />
            </span>
          )}
          {hasInspect && (
            <button
              type="button"
              className="act-quick-act act-quick-locate"
              title="定位到运行详情"
              aria-label="定位到运行详情"
              onClick={(e) => {
                e.stopPropagation();
                onFocus?.(tool);
              }}
            >
              <ArrowRight size={11} />
            </button>
          )}
        </span>
      )}
      {argTip !== null && typeof document !== 'undefined' && createPortal(
        <span
          id={tipId}
          className="arg-tip is-on"
          role="tooltip"
          style={{ left: argTip.x, top: argTip.y }}
        >
          {argsSummary}
        </span>,
        document.body,
      )}

      {/* T3（#96）：流式输出尾窗——running 渲染 chunks 活流（S14 活流不需展开）；
          终态以 result 校准（契约 C2「tool/result 终态全量兜底」——delta 缺口/
          重连丢失时 partial chunks 不得永久遮蔽全量），result 无可渲染输出时
          chunks 兜底。chunks 数据本身永不清除（events/raw 可查）。 */}
      {tool.output && tool.output.length > 0 && (tool.status === 'running' || !tool.result) && (
        <ToolOutputStream chunks={tool.output} streaming={tool.status === 'running'} />
      )}

      {/* 失败卡关键错误行（#1429 打磨点 8）：默认露出、红字 + 淡红底、不画框。
          只呈现错误，不给工具级重试按钮（P0-1 裁决）；完整 Raw 仍在 L2。 */}
      {errorShape && (
        <div className="act-error-line" role="status">
          <AlertTriangle size={11} aria-hidden="true" />
          <span>
            <b className="act-error-code">{errorShape.errorCode}</b>
            {errorShape.message && ` — ${truncate(errorShape.message, 160)}`}
          </span>
        </div>
      )}

      {/* L1 inline detail：Input/Output 摘要（C3 微标签分区，与 Inspector io-tabs 同语言）。
          展开/收起走 grid-rows 0fr→1fr（打磨点 2）；收起后子树卸载，balanced 档
          DOM 里仍然没有内联明细。 */}
      {openedOnce.current && (
        <Collapse open={detailOpen} className="act-detail-inline">
          <div className="act-field">
            <div className="act-field-label">输入</div>
            <pre className="act-detail-args">{JSON.stringify(tool.args, null, 2)}</pre>
          </div>
          {tool.result !== undefined && (
            <div className="act-field">
              <div className="act-field-label">输出</div>
              <pre className="act-detail-result">
                {truncate(
                  typeof tool.result === 'string' ? tool.result : JSON.stringify(tool.result, null, 2),
                  400,
                )}
              </pre>
            </div>
          )}
        </Collapse>
      )}

      {/* L2 advanced inline：完整内容面（四形态专属块）+ 原始事件 JSON。 */}
      {openedOnce.current && (
        <Collapse open={bodyOpen} className="tool-card-body">
          {isBash && <BashBlock tool={tool} />}
          {isDiffTool && tool.diff && <DiffBlock diff={tool.diff} sessionId={sessionId} />}
          {slice && <ArtifactSliceBlock slice={slice} />}
          {readShape && <ReadBlock shape={readShape} />}
          {!isBash && !isDiffTool && !slice && !readShape && <GenericBlock tool={tool} />}
          {/* 被截断处的「就地展开」（#186 AC2）：判据用**投影**挂上的 `tool.artifact`
              （来自 `artifact/externalized`），不在视图里解析 marker——标记长什么样是
              后端的事，前端只认投影这一份真相（AC7 / 不变量 #22）。与 Artifacts 清单
              共用 `ArtifactViewer`，不新开导航面。
              这里同时覆盖命令输出与通用结果两条外置路径（不需要各写一遍）。

              归档 diff 例外：那一份已经由上面的 `DiffBlock` 给了展开入口（`tool.diff`
              归档时，内容就是同一个 artifact），再渲染一次会变成两个同名同效的按钮。 */}
          {tool.artifact && sessionId && !(isDiffTool && tool.diff?.archived) && (
            <ArtifactViewer
              sessionId={sessionId}
              artifactId={tool.artifact.artifact_id}
              label="查看完整内容"
            />
          )}
        </Collapse>
      )}
      {bodyOpen && (tool.raw_call || tool.raw_result) && (
        <div className="act-raw">
          {tool.raw_call && (
            <div className="act-raw-section">
              <div className="act-raw-label">tool/call 原始事件</div>
              <pre>{JSON.stringify(tool.raw_call, null, 2)}</pre>
            </div>
          )}
          {tool.raw_result && (
            <div className="act-raw-section">
              <div className="act-raw-label">tool/result 原始事件</div>
              <pre>{JSON.stringify(tool.raw_result, null, 2)}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
});

/** 运行中工具的已运行时长（只给"已跑多久"，不给假百分比）。 */
function LiveToolDuration({ startedAt }: { startedAt?: string }) {
  const now = useTickingNow(1000);
  if (!startedAt) return null;
  const text = formatShortDuration(now - new Date(startedAt).getTime());
  return text === '' ? null : <>{text}</>;
}

/** 中间省略的切分：短串不切；长串保留尾部 `ARG_TAIL_CHARS` 字符作为固定尾段。 */
const ARG_TAIL_CHARS = 32;
function splitArgsForDisplay(summary: string): { head: string; tail: string } {
  if (summary.length <= ARG_TAIL_CHARS * 2) return { head: summary, tail: '' };
  return {
    head: summary.slice(0, summary.length - ARG_TAIL_CHARS),
    tail: summary.slice(summary.length - ARG_TAIL_CHARS),
  };
}

/** 行内参数摘要。默认**不预截断**（#1429 F5）：中间省略靠 `splitArgsForDisplay`
 *  + CSS ellipsis，先砍到 60 字会让 64 字阈值永远触发不了，长命令连 tooltip 都只剩
 *  前 60 字。`max` 只给 compact 档用（那一档本来就只放一行短摘要）。 */
function summarizeArgs(tool: ToolCall, max?: number): string {
  const a = tool.args;
  const raw = tool.name === 'bash'
    ? String(a.command ?? '')
    : 'path' in a
      ? String(a.path)
      : Object.entries(a).slice(0, 2).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(' ');
  return max === undefined ? raw : truncate(raw, max);
}

function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n) + '…' : s;
}

function BashBlock({ tool }: { tool: ToolCall }) {
  const cmd = String(tool.args.command ?? '');
  const out = typeof tool.result === 'string' ? tool.result : JSON.stringify(tool.result ?? '', null, 2);
  const exitCode = (tool.result as { exit_code?: number } | string | undefined);
  const code = typeof exitCode === 'object' && exitCode ? exitCode.exit_code : undefined;
  // bash 被取消（data.cancelled，df4f7d8 §1.3）——展示"已取消"而非普通失败
  // （投影层已把 status 映射为 stopped；这里补明确文案，注明后端保证的语义）。
  const cancelled = (tool.result as { cancelled?: boolean } | null | undefined)?.cancelled === true;

  return (
    <div className="bash-block">
      <div className="bash-cmd-line">
        <span className="bash-prompt">$</span>
        <code>{cmd}</code>
      </div>
      {/* T3（#96）：仅 running+chunks 时屏蔽 result 输出行（活流双写）；
          终态由 result 校准渲染（stream 区已退位，无双写）。 */}
      {!(tool.status === 'running' && tool.output?.length) && out && (
        <pre className="bash-output">{truncateForDisplay(out)}</pre>
      )}
      {cancelled && (
        <span className="bash-cancelled" title="命令被超时/断连取消，进程树已终止">
          已取消
        </span>
      )}
      {code !== undefined && (
        <span className={`exit-badge ${code === 0 ? 'exit-ok' : 'exit-err'}`}>exit {code}</span>
      )}
    </div>
  );
}

function GenericBlock({ tool }: { tool: ToolCall }) {
  const resultIsObject = typeof tool.result === 'object' && tool.result !== null;
  return (
    <div className="generic-block">
      <div className="generic-section">
        <div className="generic-label">参数</div>
        <div className="detail-json">
          <JsonTree value={tool.args} />
        </div>
      </div>
      {tool.result !== undefined && (
        <div className="generic-section">
          <div className="generic-label">结果</div>
          {resultIsObject ? (
            <div className="detail-json">
              <JsonTree value={tool.result} />
            </div>
          ) : (
            <TruncationAwarePre
              text={truncateForDisplay(stringifyForDisplay(tool.result))}
              className="generic-result"
            />
          )}
        </div>
      )}
    </div>
  );
}

/** 逐行渲染并把 grep 截断尾巴（`... [truncated]`，单行 >500 字符，df4f7d8 §1.3）
 *  弱化为静音标记——内容是真实的，但视觉上明确"此处有省略"，不与正文混淆。 */
function TruncationAwarePre({ text, className }: { text: string; className?: string }) {
  if (!text.includes(GREP_TRUNCATED_SUFFIX)) return <pre className={className}>{text}</pre>;
  const lines = text.split('\n');
  return (
    <pre className={className}>
      {lines.map((line, i) => {
        const cut = hasGrepTruncatedSuffix(line);
        return (
          <span key={i}>
            {cut ? (
              <>
                {stripGrepTruncatedSuffix(line)}
                <span className="trunc-suffix" title="该行因超长被截断">{GREP_TRUNCATED_SUFFIX}</span>
              </>
            ) : (
              line
            )}
            {i < lines.length - 1 ? '\n' : ''}
          </span>
        );
      })}
    </pre>
  );
}

// ── read 专用渲染（df4f7d8 §1.3：续读标记 / 单行截断 / 空文件）──

function ReadBlock({ shape }: { shape: ReadShape }) {
  // 空文件是正常成功（content:"" + total_lines:0），不渲染成失败或空泡。
  const empty = shape.content === '' && (shape.totalLines === null || shape.totalLines === 0);
  return (
    <div className="read-block">
      <div className="read-meta">
        {empty && <span className="read-empty">空文件（0 行）</span>}
        {!empty && shape.totalLines !== null && (
          <span className="read-total">共 {shape.totalLines.toLocaleString()} 行</span>
        )}
        {shape.continuation && (
          <span className="read-range">
            已显示 {shape.continuation.shownFrom}–{shape.continuation.shownTo} 行
          </span>
        )}
        {shape.lineTruncated && (
          <span
            className="read-line-trunc"
            title="该行超过 51200 字节被截断，不可续读——需用 bash 分段读取"
          >
            第 {shape.lineTruncated.line} 行超长截断（{shape.lineTruncated.bytes.toLocaleString()} 字节）· 不可续读
          </span>
        )}
      </div>
      {!empty && shape.content !== '' && <pre className="read-content">{truncateForDisplay(shape.content)}</pre>}
      {shape.continuation && (
        <div className="read-continue">
          <span className="read-continue-hint">续读参数（下一次 read 调用的 offset）</span>
          <code className="read-continue-code">offset={shape.continuation.nextOffset}</code>
          <CopyButton text={`offset=${shape.continuation.nextOffset}`} label="复制续读 offset" />
        </div>
      )}
    </div>
  );
}

// ── inspect_artifact 专用渲染 ──

/** 后端 ArtifactSlice.lines 的单行 item 形状（spec 06 §4 / storage/artifact.py）。
 *  超长行截断后额外携带 truncated=true + full_length（原始字符数）。 */
interface SliceLine {
  line_number: number;
  text: string;
  truncated?: boolean;
  full_length?: number;
}

interface SliceData {
  artifact_id: string;
  lines: SliceLine[];
  total_lines: number;
  returned_lines: number;
  truncated: boolean;
}

/** 从 ToolResult 中解析 ArtifactSlice（data 字段经 tryParseContent 已是对象）。
 *  形状不符（失败结果 / 旧数据）返回 null，回退 GenericBlock。 */
function tryParseSlice(result: unknown): SliceData | null {
  if (typeof result !== 'object' || result === null) return null;
  const r = result as Record<string, unknown>;
  if (!Array.isArray(r.lines)) return null;
  const lines = r.lines.filter(
    (l): l is SliceLine =>
      typeof l === 'object' && l !== null &&
      typeof (l as Record<string, unknown>).line_number === 'number' &&
      typeof (l as Record<string, unknown>).text === 'string',
  );
  if (lines.length === 0) return null;
  return {
    artifact_id: String(r.artifact_id ?? ''),
    lines,
    total_lines: Number(r.total_lines ?? 0),
    returned_lines: Number(r.returned_lines ?? lines.length),
    truncated: r.truncated === true,
  };
}

function ArtifactSliceBlock({ slice }: { slice: SliceData }) {
  const truncatedCount = slice.lines.filter((l) => l.truncated).length;
  return (
    <div className="slice-block">
      <div className="slice-meta">
        <span className="slice-meta-item">共 {slice.total_lines} 行</span>
        {slice.truncated && <span className="slice-meta-item">返回 {slice.lines.length} 行</span>}
        {truncatedCount > 0 && (
          <span className="slice-trunc-chip">
            <Scissors size={14} />
            {truncatedCount} 行超长截断 · 可放宽 max_chars_per_line 重读
          </span>
        )}
      </div>
      <div className="slice-lines">
        {slice.lines.map((l) => (
          <div key={l.line_number} className={`slice-line ${l.truncated ? 'slice-line-trunc' : ''}`}>
            <span className="slice-line-no">{l.line_number}</span>
            <code className="slice-line-text">{l.text}</code>
            {l.truncated && (
              <span className="slice-line-chip" title={`原始 ${l.full_length ?? '?'} 字符，当前显示前 2000`}>
                …{l.full_length ?? '?'} 字符
              </span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
