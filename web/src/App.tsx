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

import { useEffect, useRef, useState } from 'react';
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
  const [focus, setFocus] = useState<InspectorFocus>({ kind: 'run' });
  const focusTool = (tool: ToolCall) => setFocus({ kind: 'tool', tool });
  const focusEvent = (event: AgentEvent) => setFocus({ kind: 'event', event });
  const focusRun = () => setFocus({ kind: 'run' });
  // 空状态示例任务 → 注入 Composer（对象引用变化触发注入，可重复点击）
  const [presetTask, setPresetTask] = useState<PresetTask | null>(null);
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

  const handleNew = () => {
    // selectSession 内部处理流取消（切走即放弃当前流，幂等）
    selectSession(null);
    focusRun();
  };

  const handleSubmit = (task: string) => {
    focusRun();
    void submitTask({ task, max_steps: 10, auto_approve: true });
  };

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
          onSelect={(id) => {
            selectSession(id);
            focusRun();
          }}
          onNew={handleNew}
        />

        <section className="app-workspace">
          {error && <div className="app-error">{error}</div>}
          <Conversation
            conversation={conversation}
            loadingHistory={loadingHistory}
            density={density}
            onPresetTask={(text) => setPresetTask({ text, id: Date.now() })}
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
