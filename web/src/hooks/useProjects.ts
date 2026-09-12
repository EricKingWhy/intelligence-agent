/** 项目列表 + 项目动作（WS-5 / #155）。
 *
 *  **不做第二套真相**（不变量 #22）：本 hook 只持有 `GET /api/projects` 的最近一次
 *  响应，"项目有哪些会话"永远从账本（`session_ids`）读，不在前端累积成员名单。
 *  每次写操作后**重新拉取**而不是本地改一份影子列表——后端有一次原子性 + 成员资格
 *  过滤（WS-2/WS-3），本地重算必然会在某个边界上和它不一致。
 *
 *  失败时也会刷新一次：写操作失败最常见的两类原因（409 会话 cwd 不一致 / 账本已被
 *  别处改动）都意味着**本地看到的状态已经过期**，刷新让 UI 立刻追上真相，而不是让
 *  用户对着一个永远失败的按钮重试。 */

import { useCallback, useMemo, useState } from 'react';
import {
  attachSessionToProject,
  createProject,
  deleteProject,
  detachSessionFromProject,
  listProjects,
  renameProject,
  reorderProjectSession,
} from '../lib/api';
import type { Project, ProjectDeleted } from '../types';

export interface ProjectActions {
  /** 注册一个**已存在**的目录（幂等）。失败抛 ProjectError（404 路径不存在等）。 */
  create: (path: string, title?: string | null) => Promise<Project>;
  rename: (projectId: string, title: string) => Promise<void>;
  /** **软删除**：只摘注册记录与账本，返回后端写好的 `detail`（AC5 要原样展示）。 */
  remove: (projectId: string) => Promise<ProjectDeleted>;
  attach: (projectId: string, sessionId: string) => Promise<void>;
  detach: (projectId: string, sessionId: string) => Promise<void>;
  /** `before=null` → 追加队尾（DOM insertBefore 语义）。 */
  reorder: (projectId: string, sessionId: string, before: string | null) => Promise<void>;
}

export interface ProjectsState {
  projects: Project[];
  /** `GET /api/projects` 失败的原因（**不**清空既有列表：清空会把每条会话都变成
   *  未分组、并把项目行整片抹掉——一次网络抖动不该重排用户的界面）。 */
  loadError: string | null;
  refresh: () => Promise<void>;
  actions: ProjectActions;
}

export function useProjects(): ProjectsState {
  const [projects, setProjects] = useState<Project[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setProjects(await listProjects());
      setLoadError(null);
    } catch (e) {
      setLoadError((e as Error).message || '加载项目失败');
    }
  }, []);

  /** 写操作 → 拉取最新真相；失败同样拉取（见文件头注释），再把错误抛给调用方
   *  （由对话框或项目区错误条负责显示——hook 自己不弹 UI）。 */
  const after = useCallback(
    async <T,>(run: () => Promise<T>): Promise<T> => {
      try {
        const result = await run();
        await refresh();
        return result;
      } catch (e) {
        await refresh().catch(() => undefined);
        throw e;
      }
    },
    [refresh],
  );

  const actions = useMemo<ProjectActions>(
    () => ({
      create: (path, title) => after(() => createProject(path, title)),
      rename: async (projectId, title) => {
        await after(() => renameProject(projectId, title));
      },
      remove: (projectId) => after(() => deleteProject(projectId)),
      attach: async (projectId, sessionId) => {
        await after(() => attachSessionToProject(projectId, sessionId));
      },
      detach: async (projectId, sessionId) => {
        await after(() => detachSessionFromProject(projectId, sessionId));
      },
      reorder: async (projectId, sessionId, before) => {
        await after(() => reorderProjectSession(projectId, sessionId, before));
      },
    }),
    [after],
  );

  return { projects, loadError, refresh, actions };
}
