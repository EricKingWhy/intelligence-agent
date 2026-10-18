/** #1304 Agent 看板：顶栏入口打开的全屏浮层——四列严格投影 + 注意力覆盖层 + 搜索 + 拖拽。
 *
 *  ## 浮层形状为什么**不**用 Radix Dialog（与 MemoryPanel 分道）
 *
 *  本仓两条浮层惯例并存：`MemoryPanel`/`RecoveryListPanel`/`CommandPalette` 走 Radix
 *  Dialog（有焦点陷阱），`ContextUsagePanel`/`TaskReviewPanel` 走裸 overlay + 手写 Esc。
 *  看板选后者，三条理由：
 *  1. 看板是**全屏**面，没有"浮层外还有要操作的东西"，焦点陷阱买不到东西；
 *  2. AC-15 要求板开着时 `Ctrl/Cmd+K` **仍然**唤起命令面板并聚焦其输入框——Radix 的
 *     modal Dialog 会把焦点锁在自己内部（`trapFocus`）并把外部内容 `aria-hidden`，
 *     两层面板会互相打架；
 *  3. 既有先例 `ContextUsagePanel.tsx:112-123` / `TaskReviewPanel.tsx:149-159` 就是
 *     这个形状（`role="dialog"` + `aria-label` + 手写 Esc），照抄即零新机制。
 *
 *  ## 数据源（本票的注入缝）
 *
 *  打开时拉一次 `GET /api/sessions`（`includeArchived: true`）+ 手动刷新按钮，**不引入
 *  轮询定时器**（票面「不做边界」：避免与 `wsStream.ts:380/404/428` 的既有轮询语义打架）。
 *  数据源经 `loadSessions` prop 注入（缺省 = `lib/api.ts::listSessions`）——T3（#1305）
 *  把 WS 快照接到这个缝上，本组件不用改。
 *
 *  ## 拖拽是本票**唯一**有写语义的交互（票面 §4.7）
 *
 *  只有 backlog -> 执行中接受拖入；其余落点一律拒绝。落点动作两步且**顺序不可换**：
 *  先 `/task/definition` 再 `/messages`（反过来的话，run 会在 `defined=false` 的窗口里
 *  让 `product_state` 恒 `""`，卡片闪回待办列）。任一步失败 -> 卡片**不动**（板是投影，
 *  下一次快照就是真相；不做乐观移动）。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { RefreshCw, X } from 'lucide-react';
import {
  listSessions,
  postSessionCommand,
  readErrorDetail,
  sendMessage,
  SessionError,
} from '../lib/api';
import type { SessionSummary } from '../types';
import {
  COLUMN_LABELS,
  COLUMN_TESTIDS,
  KANBAN_COLUMNS,
  type KanbanColumn,
  deliverableCount,
  filterByTitle,
  groupSessions,
  readShowAccepted,
  unknownStateCount,
  writeShowAccepted,
} from '../lib/kanban';
import { KanbanCard } from './KanbanCard';
import '../styles/kanban.css';

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 数据源注入缝（T3 用 WS 快照替换；缺省 = 打开时拉一次 REST）。 */
  loadSessions?: () => Promise<SessionSummary[]>;
}

/** 缺省数据源：与 `useSession.refreshSessions` 同款——**总是**要全量（含已归档），
 *  可见性由视图开关决定而不是由请求决定（#171 的纪律，不变量 #22）。 */
const defaultLoadSessions = (): Promise<SessionSummary[]> =>
  listSessions({ includeArchived: true });

/** 拖拽被拒绝时的说明（AC-9① 的判据文本）。 */
const REJECT_TEXT = '只有 backlog 卡可以拖到「执行中」';

/** 顶部提示行的自动消失时长（showNotice 与其 cleanup 共用，禁止第二处各写一份）。 */
const NOTICE_MS = 4000;

/** 落点两步里第一步的 409 是**正常**语义（该会话已定义过任务），不是错误——
 *  跳过定义、直接开跑（AC-10）。 */
function isAlreadyDefined(error: unknown): boolean {
  return error instanceof SessionError && error.status === 409;
}

export function KanbanPanel({ open, onOpenChange, loadSessions = defaultLoadSessions }: Props) {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState('');
  /** 开关是**视图状态**：首帧就要按它渲染（同 `lib/railArchive.ts` 的纪律：
   *  "先渲染默认态、再在 effect 里改"会闪一下）。所以用**惰性初始化**直接读
   *  localStorage，而不是在 effect 里补——顺带避免了 effect 里的 setState。 */
  const [showAccepted, setShowAccepted] = useState(readShowAccepted);
  /** 拖拽中的会话 id（同一次拖拽内跨列共享；`null` = 没有在拖）。 */
  const draggingRef = useRef<string | null>(null);
  /** 正在落点的会话 id 集（P3-1：快速连续拖拽同一张卡时，同会话的第二组
   *  写请求被静默拦截，不发第二次）。 */
  const dropInFlight = useRef<Set<string>>(new Set());
  const [notice, setNotice] = useState<string | null>(null);
  /** 相对时间的基准：**一次加载一个值**（不给每张卡各自 `Date.now()`，也不起定时器）。 */
  const [now, setNow] = useState(() => Date.now());
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const rows = await loadSessions();
      setSessions(rows);
      setLoadError(null);
      setNow(Date.now());
    } catch (error) {
      // 失败**不清**列表：把已经拿到的行说成"没有会话"是假陈述（F9 的既有纪律）。
      setLoadError((error as Error).message || '加载会话列表失败');
    } finally {
      setLoading(false);
    }
  }, [loadSessions]);

  // 打开时拉一次（关闭时不动——关着的时候拉是白做的）。
  useEffect(() => {
    if (open) void reload();
  }, [open, reload]);

  const showNotice = useCallback((text: string) => {
    setNotice(text);
    if (noticeTimer.current !== null) clearTimeout(noticeTimer.current);
    noticeTimer.current = setTimeout(() => setNotice(null), NOTICE_MS);
  }, []);

  useEffect(
    () => () => {
      if (noticeTimer.current !== null) clearTimeout(noticeTimer.current);
    },
    [],
  );

  // Esc 关闭（裸 overlay 惯例，同 ContextUsagePanel / TaskReviewPanel）。
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        // 更近的一层（Radix 命令面板在 document 捕获相位 preventDefault）已认领的
        // Esc 不再关板——否则 Ctrl+K 开面板后按 Esc 会**同时**关掉面板与看板
        // （AC-15 要的是板还在、搜索框仍可聚焦）。判据同 `App.tsx:418`。
        if (event.defaultPrevented) return;
        event.preventDefault();
        onOpenChange(false);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onOpenChange]);

  const visible = useMemo(() => filterByTitle(sessions, query), [sessions, query]);
  const grouped = useMemo(() => groupSessions(visible), [visible]);
  const hiddenDeliverable = useMemo(() => deliverableCount(visible), [visible]);
  /** 后端新增的未知状态卡数（P4-4：与 deliverable 同一条"不许静默丢卡"纪律）。 */
  const hiddenUnknown = useMemo(() => unknownStateCount(visible), [visible]);

  const toggleAccepted = () => {
    const next = !showAccepted;
    setShowAccepted(next);
    writeShowAccepted(next);
  };

  /** 落点写路径的实际执行体（原样从 `runDropOnExecuting` 抽出——P3-1 的 in-flight
   *  守卫由外层做，这里只管"落点两步"本身）。 */
  const doRunDropOnExecuting = useCallback(
    async (session: SessionSummary) => {
      const taskText = session.first_user_message;
      if (taskText === null) {
        // 不可拖的卡本来就拖不动；这里是纵深防御（真到了也绝不发一条空任务）。
        showNotice('空会话不能开跑：先发一条消息');
        return;
      }
      try {
        try {
          await postSessionCommand(session.session_id, '/task/definition', {
            task_text: taskText,
          });
        } catch (error) {
          // 409 = 该会话已定义过任务——**跳过这一步，不是错误**（AC-10）。
          if (!isAlreadyDefined(error)) throw error;
        }
        // `sendMessage` 返回**原始 Response**（非 2xx 不抛，见 `lib/api.ts:772` 的签名）
        // ——必须自己查 `ok`。漏掉这一步，后端 500 会被当成成功受理，然后 refetch
        // 拿回一张没动的卡，用户看到的是"拖了、没反应、也没说话"（AC-10 的反向面）。
        const res = await sendMessage(session.session_id, {
          content: taskText,
          mode: 'queue',
        });
        if (!res.ok) {
          throw new SessionError(res.status, await readErrorDetail(res));
        }
      } catch (error) {
        // 任一步失败 -> 如实说出后端 detail，卡片**不动**（不做乐观移动）。
        showNotice((error as Error).message || '拉起会话失败');
        return;
      }
      // 成功 -> 主动 refetch 一次（唯一真相刷新点）。卡片位置由新快照决定。
      await reload();
    },
    [reload, showNotice],
  );

  /** 落点写路径：只有 backlog -> 执行中走到这里（票面 §4.7 的两步）。 */
  const runDropOnExecuting = useCallback(
    async (session: SessionSummary) => {
      const sessionId = session.session_id;
      // P3-1：快速连续拖拽同一张卡到执行中会发出两组
      // `POST /task/definition` + `POST /messages`（任务被 queue 两次）。
      // 同一会话的第一组还在飞时，第二次落点直接**静默返回**（不 toast，避免噪音）。
      //
      // 标记只覆盖"写请求在飞"这段，不绑状态（`finally` 是唯一删除点；绑了会把下面两个瞬时
      // 故障放大成永久不可拖）。仍能重拖的窗口：① `loadSessions()` 失败（`reload` 吞错不重抛）；
      // ② `reload()` 200 但该行投影走 `_empty` 兜底（派生异常 ⇒ `product_state=""`）仍是 backlog。
      if (dropInFlight.current.has(sessionId)) return;
      dropInFlight.current.add(sessionId);
      try {
        await doRunDropOnExecuting(session);
      } finally {
        dropInFlight.current.delete(sessionId);
      }
    },
    [doRunDropOnExecuting],
  );

  if (!open) return null;

  const renderColumn = (column: KanbanColumn) => {
    if (column === 'accepted' && !showAccepted) return null;
    const cards = grouped[column];
    return (
      <section
        key={column || 'backlog'}
        className="kanban-col"
        data-testid={`kanban-col-${COLUMN_TESTIDS[column]}`}
        data-column={column}
        aria-label={COLUMN_LABELS[column]}
        onDragOver={(event) => {
          // 所有列都 preventDefault：否则浏览器对"不接受"的列显示禁止光标且**不派发
          // drop**，拒绝提示就无从给出（AC-9① 要的就是那条提示）。真正接不接受在
          // drop 里判——照效果图 `kanban.html:254-271` 的结构。
          event.preventDefault();
        }}
        onDrop={(event) => {
          event.preventDefault();
          const sessionId = draggingRef.current;
          draggingRef.current = null;
          if (sessionId === null) return;
          const session = sessions.find((row) => row.session_id === sessionId);
          if (session === undefined) return;
          // 接受条件是**源列与目标列都要对**（票面 §4.7：「只有 backlog → 执行中」）：
          // 只看目标列会漏掉"把执行中的卡拖回执行中"这一路——那会为一条**非 backlog**
          // 会话发出定义+开跑两步写请求，正是本票要禁的。
          if (column !== 'executing' || session.product_state !== '') {
            showNotice(REJECT_TEXT);
            return;
          }
          void runDropOnExecuting(session);
        }}
      >
        <header className="kanban-col-head">
          <span className="kanban-col-label">{COLUMN_LABELS[column]}</span>
          <span className="kanban-col-count">{cards.length}</span>
        </header>
        <div className="kanban-col-body">
          {cards.map((session) => (
            <KanbanCard
              key={session.session_id}
              session={session}
              now={now}
              /* 可拖条件：**非空会话**即可拖（与列无关）。
                 两条 AC 各自要求一个方向，只有这个取值同时满足：
                 - AC-9② 空 backlog 卡必须 `draggable="false"`（不可拖）；
                 - AC-9① 执行中列的卡拖到待验证列要**给出拒绝提示**——而浏览器对
                   `draggable="false"` 的元素根本不派发 dragstart/drop，拖不起来也就
                   没有提示。所以"接不接受"必须在**落点**判（见 onDrop），不能靠
                   "不给拖"来表达——那会静默，而本票要的是说出来。 */
              draggable={session.first_user_message !== null}
              onDragStart={() => {
                draggingRef.current = session.session_id;
              }}
            />
          ))}
        </div>
      </section>
    );
  };

  return (
    // 全屏面板（票面 §4.1「全屏浮层」）：**没有**背板遮罩，也没有"点外面关闭"——
    // 面板铺满视口，背板会被它整片盖住（那层就成了永不接收点击的死代码）。
    // 关闭路径是 Esc 与关闭按钮两条（AC-17 判据即此），照 `TaskReviewPanel` 的
    // 裸 overlay 惯例但不带遮罩层。
    <div className="kanban-panel" role="dialog" aria-label="Agent 看板">
        <header className="kanban-head">
          <h2 className="kanban-title">Agent 看板</h2>
          <input
            className="kanban-search"
            type="search"
            aria-label="搜索卡片"
            placeholder="搜索卡片标题"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <button
            type="button"
            className={`kanban-toggle${showAccepted ? ' on' : ''}`}
            /* 开关语义（不是两个动作按钮），照 `SessionList.tsx:286-290` 的「显示已归档」先例。 */
            aria-pressed={showAccepted}
            onClick={toggleAccepted}
          >
            显示已接受
          </button>
          <button
            type="button"
            className="icon-btn"
            aria-label="刷新看板"
            title="重新拉取会话列表"
            onClick={() => void reload()}
          >
            <RefreshCw size={14} />
          </button>
          <button
            type="button"
            className="icon-btn"
            aria-label="关闭看板"
            onClick={() => onOpenChange(false)}
          >
            <X size={14} />
          </button>
        </header>

        {/* deliverable 不渲染但**不静默丢卡**（AC-3 / 不变量 #21）：计数 > 0 时说出来。
            正常产品态下 N 恒为 0（C5 证据见 Epic 总稿 §10.2）。 */}
        {hiddenDeliverable > 0 && (
          <div className="kanban-notice kanban-notice-warn" role="status">
            有 {hiddenDeliverable} 张卡片处于 deliverable 状态，该列 v1 未渲染
          </div>
        )}
        {hiddenUnknown > 0 && (
          <div className="kanban-notice kanban-notice-warn" role="status">
            有 {hiddenUnknown} 张卡片处于后端新增的未知状态，未渲染
          </div>
        )}

        {loadError !== null && (
          <div className="kanban-notice kanban-notice-error" role="alert">
            加载会话列表失败：{loadError}
          </div>
        )}
        {notice !== null && (
          <div className="kanban-notice" role="status">
            {notice}
          </div>
        )}
        {loading && sessions.length === 0 && (
          <div className="kanban-notice" role="status">
            加载中…
          </div>
        )}

      <div className="kanban-board">{KANBAN_COLUMNS.map(renderColumn)}</div>
    </div>
  );
}
