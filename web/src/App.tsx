/** App — single-frame application shell (Phase 2, Brief §5/§6).
 *
 * ONE solid frame: App Bar on top, three regions below separated by 1px
 * dividers — Session Rail | Agent Workspace | Run Inspector. No floating
 * cards, no per-panel shadows (Brief §6.1 "One Shell, Not Three Cards").
 *
 * Responsive (frozen decision: collapse ≠ unmount):
 *   - ≥1200px: all three regions
 *   - <1200px: Inspector collapses (toggle in App Bar), stays mounted
 *   - <820px:  Rail collapses to 56px icon rail (overlay drawer deferred)
 *
 * Panel geometry is transient — NOT persisted (invariant #22: no second truth).
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useSession } from './hooks/useSession';
import { TopBar } from './components/TopBar';
import { SessionList } from './components/SessionList';
import { Conversation } from './components/Conversation';
import { Composer } from './components/Composer';
import { StepDetail, type InspectorFocus } from './components/StepDetail';
import { applyDensity, initDensity, type TraceDensity } from './lib/density';
import type { ToolCall, PresetTask, AgentEvent } from './types';
import './styles/app.css';

export default function App() {
  const {
    sessions,
    selectedId,
    conversation,
    loadingHistory,
    streaming,
    error,
    titlesById,
    selectSession,
    submitTask,
    cancelStream,
  } = useSession();

  // 密度四档（冻结决策）：状态在 App（TopBar 切换、Conversation 消费），persist 由 lib/density 负责。
  const [density, setDensity] = useState<TraceDensity>(initDensity);
  const changeDensity = (next: TraceDensity) => {
    setDensity(next);
    applyDensity(next);
  };

  // Inspector 焦点（Brief "上下文 Inspector"）：Run 级 ↔ 事件级，一键返回，不用弹窗。
  // 全部 useCallback：下游 SessionList/Composer/Conversation/StepDetail 的 memo
  // 依赖引用稳定的回调，普通函数每次渲染新引用会让 memo 全部失效。
  const [focus, setFocus] = useState<InspectorFocus>({ kind: 'run' });
  const focusRun = useCallback(() => setFocus({ kind: 'run' }), []);
  const focusTool = useCallback((tool: ToolCall) => setFocus({ kind: 'tool', tool }), []);
  const focusEvent = useCallback((event: AgentEvent) => setFocus({ kind: 'event', event }), []);
  // 空状态示例任务 → 注入 Composer（对象引用变化触发注入，可重复点击）
  const [presetTask, setPresetTask] = useState<PresetTask | null>(null);
  const onPresetTask = useCallback((text: string) => setPresetTask({ text, id: Date.now() }), []);
  // Inspector 折叠是视图状态：收起不卸载（DSH 语义，冻结决策）。
  // 窄屏（<1200px）默认收起；用户手动切换后以手动值优先（仅本会话内，不持久化）。
  const [inspectorOpen, setInspectorOpen] = useState(
    () => typeof window === 'undefined' || window.innerWidth >= 1200,
  );
  const userToggledRef = useRef(false);
  useEffect(() => {
    const mq = window.matchMedia('(max-width: 1200px)');
    const onChange = (e: MediaQueryListEvent) => {
      if (!userToggledRef.current) setInspectorOpen(!e.matches);
    };
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, []);

  const toggleInspector = () => {
    userToggledRef.current = true;
    setInspectorOpen((v) => !v);
  };

  const handleNew = useCallback(() => {
    // selectSession 内部处理流取消（切走即放弃当前流，幂等）
    selectSession(null);
    focusRun();
  }, [selectSession, focusRun]);

  const handleSelect = useCallback((id: string) => {
    selectSession(id);
    focusRun();
  }, [selectSession, focusRun]);

  const handleSubmit = useCallback((task: string) => {
    focusRun();
    void submitTask({ task, max_steps: 10, auto_approve: true });
  }, [submitTask, focusRun]);

  return (
    <div className="app-frame">
      <TopBar
        conversation={conversation}
        streaming={streaming}
        inspectorOpen={inspectorOpen}
        onToggleInspector={toggleInspector}
        density={density}
        onDensityChange={changeDensity}
      />

      <main className={`app-regions ${inspectorOpen ? '' : 'inspector-closed'}`}>
        <SessionList
          sessions={sessions}
          selectedId={selectedId}
          liveSessionId={streaming ? selectedId : null}
          titlesById={titlesById}
          onSelect={handleSelect}
          onNew={handleNew}
        />

        <section className="app-workspace">
          {error && <div className="app-error">{error}</div>}
          <Conversation
            conversation={conversation}
            loadingHistory={loadingHistory}
            density={density}
            onPresetTask={onPresetTask}
            onFocusTool={focusTool}
          />
          <Composer
            streaming={streaming}
            onSubmit={handleSubmit}
            onCancel={cancelStream}
            presetTask={presetTask}
          />
        </section>

        <StepDetail
          conversation={conversation}
          streaming={streaming}
          focus={focus}
          onFocusRun={focusRun}
          onFocusTool={focusTool}
          onFocusEvent={focusEvent}
        />
      </main>
    </div>
  );
}
