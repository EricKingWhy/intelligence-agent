/** 会话语境归属的文案投影（#1028）。
 *
 *  背景：项目成员规则是**会话 cwd 与项目路径 canonical 精确相等**（后端
 *  `workspace/index.py`），不匹配的会话被静默剪掉、UI 零提示——用户以为「选中了
 *  项目，agent 就知道项目在哪」，实际上会话落在别处。本模块把「当前会话属于谁 +
 *  工作目录」显形为一个常驻 chip（成熟产品机制：VS Code 的工作区上下文永不隐形、
 *  Claude Code 启动横幅打印 cwd）。
 *
 *  **只做文案，不做判定**：项目名来自后端（`SessionSummary.workspace`），cwd 来自
 *  后端（`session/started.data.cwd` 的 Web 侧投影）。前端**不比对路径**——那是后端
 *  的成员资格真相（不变量 #22），自己比一次就是第二套真相。所以本函数是纯的：
 *  入参 null 表示"未知/未声明"，输出如实说"未分组 / 未指定目录"，不编字面值。 */

export interface Ownership {
  /** `project` = 归属某项目；`ungrouped` = 未归属任何已注册项目。chip 据此着色。 */
  kind: 'project' | 'ungrouped';
  /** 项目名（未归属 → null）。 */
  projectTitle: string | null;
  /** cwd 绝对路径（未知 → null）。 */
  cwd: string | null;
  /** chip 上显示的一句话。 */
  label: string;
  /** 悬停提示（携带完整上下文，长路径被 chip 截断时仍可读全）。 */
  title: string;
}

/** 把（项目名, cwd）翻成归属 chip 的文案。两个入参都来自后端，null = 未知。 */
export function describeOwnership(
  projectTitle: string | null,
  cwd: string | null,
): Ownership {
  if (projectTitle !== null) {
    return {
      kind: 'project',
      projectTitle,
      cwd,
      label: cwd !== null ? `项目「${projectTitle}」 · ${cwd}` : `项目「${projectTitle}」`,
      title:
        cwd !== null
          ? `当前会话归属项目「${projectTitle}」，工作目录：${cwd}`
          : `当前会话归属项目「${projectTitle}」，工作目录未知`,
    };
  }
  return {
    kind: 'ungrouped',
    projectTitle: null,
    cwd,
    label: cwd !== null ? `未分组（cwd: ${cwd}）` : '未分组（未指定目录）',
    title:
      cwd !== null
        ? `当前会话未归属任何已注册项目，工作目录：${cwd}。项目要求会话目录与项目路径一致。`
        : '当前会话未归属任何已注册项目，工作目录未知（服务默认目录）。',
  };
}
