/** 「在此项目中新建任务」确认面（WS-6 / #169 AC9–AC14；#204 裁定收窄职责）。
 *
 *  #204 后本弹窗的职责只剩：选目录（已由入口确定）+ 设默认权限 + 创建**空会话**
 *  ——任务内容在 chat 输入框里发，不在这里写（用户裁定：弹窗不该有「任务内容」
 *  输入框，创建文件 + 设好默认权限后在输入框发第一条消息就行）。创建走
 *  `launch=false`（POST /api/sessions?launch=false），**不启动 run、不返回 SSE**。
 *
 *  为什么仍要确认面：这个入口会以**项目目录**为 cwd 起会话，而 cwd 在 ADR-0027
 *  里就是会话的 workspace root——agent 会直接读写那个目录。误点一个菜单项不该等于
 *  把某个仓库交给 agent，所以路径必须在按下之前被肉眼看到（Q2=C 的硬要求）。
 *
 *  权限档的默认档**不写进请求体**（这是本票最容易踩的坑）：后端
 *  `service.create_and_launch` 的语义是「显式传了非 danger-full-access 的档 →
 *  切交互式审批」（`permission_mode_explicit`）。所以"默认 = workspace-write +
 *  自动执行"只能靠**不发这个键**来表达；一旦把默认档也塞进 payload，每次工具调用
 *  都会弹审批卡。只有用户主动改档才发 `permission_mode`。创建响应回传后端真实
 *  写入的会话档位（`onPermissionInitialized`），前端用它初始化 composer 权限 pill
 *  ——不要各自取默认值，那正是不一致的来源（#204 裁定 §3）。
 *
 *  失败留在本浮层（AC12）：项目目录被移走 → 后端 422「目录不存在：…」，这句话
 *  必须出现在用户目光所在的位置（浮层里）。表单状态放只在打开时挂载的内层组件
 *  （与 ProjectDialogs 同一条结构纪律：挂载即初始化，关闭即丢弃）。 */

import { useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { Shield, TriangleAlert, X } from 'lucide-react';
import type { CatalogEntry } from '../lib/api';
import type { Project } from '../types';
import { OptionPicker, toCatalogOptions } from './OptionPicker';
import { catalogIcon } from '../lib/catalogIcons';

/** 默认权限档——与后端 `web/app.py::CreateSessionRequest.permission_mode` 的默认值
 *  同名同义（workspace-write + auto-approve）。它只作为**本地选中态**，不发进请求体：
 *  见文件头注释。 */
const DEFAULT_PERMISSION_MODE = 'workspace-write';

interface Props {
  /** 待开任务的项目；null = 关闭。 */
  project: Project | null;
  /** 权限档清单（GET /api/permission-modes）。空数组 = 端点缺席 → 不渲染选择器，
   *  用默认档（与 Composer 的降级立场一致：不伪造列表）。 */
  permissionModes: CatalogEntry[];
  onOpenChange: (open: boolean) => void;
  /** 创建空会话（launch=false，不启动 run）。resolve `null` = 已创建（调用方关闭
   *  浮层、用响应回传的档位初始化权限 pill）；否则 resolve 一句**给用户看的原因**
   *  （浮层就地显示）。 */
  onCreateSession: (
    project: Project,
    permissionMode: string | null,
  ) => Promise<string | null>;
}

export function StartTaskInProjectDialog({
  project,
  permissionModes,
  onOpenChange,
  onCreateSession,
}: Props) {
  return (
    <Dialog.Root
      open={project !== null}
      onOpenChange={(open) => {
        if (!open) onOpenChange(false);
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="palette-overlay" />
        {project && (
          <StartTaskForm
            project={project}
            permissionModes={permissionModes}
            onOpenChange={onOpenChange}
            onCreateSession={onCreateSession}
          />
        )}
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function StartTaskForm({
  project,
  permissionModes,
  onOpenChange,
  onCreateSession,
}: Props & { project: Project }) {
  const [mode, setMode] = useState<string>(DEFAULT_PERMISSION_MODE);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  const submit = async () => {
    if (pending) return; // #204 裁定 §1：提交守卫只看 pending（没有任务内容可判空）
    setPending(true);
    setError(null);
    // 默认档 → null → api 层不发 permission_mode 键（见文件头注释）。
    const chosen = mode === DEFAULT_PERMISSION_MODE ? null : mode;
    const failure = await onCreateSession(project, chosen);
    if (failure) {
      setError(failure);
      setPending(false);
      return;
    }
    onOpenChange(false); // 成功即关闭：用户接下来要在 chat 输入框发第一条消息
    // #204 裁定 §3：pill 初始化由 App 用**创建响应回传的**会话级档位完成
    // （handleStartTaskInProject 里 setSelectedPermissionMode(created.permissionMode)）。
    // 本组件**不**再传本地选中态——弹窗本地值只是请求意图，后端真实写入的档位才是
    // pill 的事实源（本地值 + 后端值并存双写，本地后到会覆盖响应值，正是 §3 要消灭
    // 的"各自取默认值"不一致）。
  };

  return (
    <Dialog.Content className="project-dialog" aria-label="在此项目中新建任务">
      <div className="project-dialog-head">
        <Dialog.Title className="project-dialog-title">
          <Shield size={14} aria-hidden="true" /> 在此项目中新建任务
        </Dialog.Title>
        <Dialog.Close asChild>
          <button className="icon-btn project-dialog-close" aria-label="关闭">
            <X size={14} />
          </button>
        </Dialog.Close>
      </div>

      <Dialog.Description className="project-dialog-desc">
        在项目「{project.title}」里开一个新会话。它把这个目录作为工作目录——agent
        的文件操作都相对它解析，创建后自动归入本项目（同一目录，无需再「加入项目」）。
        创建后不会立刻运行任何任务——回到主界面在输入框发第一条消息即可。
      </Dialog.Description>

      {/* AC10：路径明示行（逐字文案，e2e 断言）。强视觉是因为它承担的是
          "你正在把哪个目录交给 agent"这个确认责任。 */}
      <div className="project-path-callout" role="note">
        <TriangleAlert size={13} aria-hidden="true" />
        <span>
          Agent 将直接读写该目录：
          <span className="project-path-callout-path mono">{project.path}</span>
        </span>
      </div>

      {permissionModes.length > 0 ? (
        <div className="project-field">
          <span className="project-field-label">权限模式</span>
          {/* #201：ControlPicker 已并入 OptionPicker（同一份实现 + 视觉）。这里保持
              原有语义不变：表单字段形态由 `.project-field` 的 CSS 决定，选中回落到
              `DEFAULT_PERMISSION_MODE`（对话框里没有"未选"这一档——它是首建会话的
              权限来源，必须有个确定值）。 */}
          <OptionPicker
            ariaLabel="权限模式"
            title="工具调用如何批准？"
            options={toCatalogOptions(permissionModes, catalogIcon)}
            value={mode}
            onChange={(id) => setMode(id ?? DEFAULT_PERMISSION_MODE)}
            icon={Shield}
            placeholder="默认（工作区写入，自动执行）"
            disabled={pending}
          />
          <span className="project-field-hint">
            默认档 = 工作区写入 + 自动执行，工具调用不会逐次询问；改档后每次工具调用都需要你审批。
          </span>
        </div>
      ) : (
        // 清单端点缺席：不伪造三档，说清"用的是默认档"（选择器降级隐藏）。
        <div className="project-notice" role="status">
          权限档清单未加载，将使用默认档：工作区写入 + 自动执行。
        </div>
      )}

      {error && (
        <div className="project-error" role="alert">
          {error}
        </div>
      )}

      <div className="project-dialog-actions">
        <Dialog.Close asChild>
          <button className="project-btn">取消</button>
        </Dialog.Close>
        <button
          className="project-btn project-btn-primary"
          onClick={() => void submit()}
          disabled={pending}
        >
          {pending ? '正在创建…' : '创建会话'}
        </button>
      </div>
    </Dialog.Content>
  );
}
