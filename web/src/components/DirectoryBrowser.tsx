/** DirectoryBrowser — 「新建项目」里内嵌的宿主目录浏览器（WS-7 / #170，ADR-0028）。
 *
 *  存在的理由：Web 平台拿不到本地目录的真实绝对路径（`<input type=file>` 只给
 *  `C:\fakepath\…`，没有目录路径 API），所以"选目录"这件事只能由宿主端列举候选、
 *  前端把它画出来。契约（ADR-0028 D3–D5）：只读、**一层**（不递归、不展开 symlink）、
 *  仅目录、超上限如实标注截断。
 *
 *  纯呈现组件：数据与请求在 `useDirectoryListing`（可复用），这里只负责画和转发。
 *  路径条用"受控 + editing 覆盖"而不是 effect 同步 props：`editing === null` 时显示
 *  当前目录，用户一输入就切到自己的文本，回车跳转后清掉 editing 又跟着当前目录走。
 *
 *  「当前目录高亮」落在路径条那一行（`dir-browser-current`，accent 色 + FolderOpen）：
 *  列表里都是**子目录**，当前目录本身不在其中，所以"你在哪"只能由这一行回答。
 *
 *  向上：`listing.parent === null` 表示已在盘根（或根模式），按钮禁用而不是猜一个
 *  上一级（Windows 驱动器相对路径的歧义不值得在这里赌）。 */

import { useState } from 'react';
import { ArrowUp, Folder, FolderCheck, FolderOpen, Loader } from 'lucide-react';
import type { HostDirsListing } from '../types';

interface Props {
  listing: HostDirsListing | null;
  loading: boolean;
  /** 后端 detail 原文（403/404/422…）——原样显示，不翻译。 */
  error: string | null;
  /** 跳转：路径条回车 / 向上 / 点子目录。null = 列根。 */
  onGoto: (path: string | null) => void;
  /** 「选择此目录」→ 回填表单（父级据此填路径输入框）。 */
  onPick: (path: string) => void;
}

export function DirectoryBrowser({ listing, loading, error, onGoto, onPick }: Props) {
  // editing：用户正在路径条里输入的内容；null = 跟随当前目录。
  const [editing, setEditing] = useState<string | null>(null);
  // 点过「选择此目录」后的就地回执——否则那个按钮看起来"什么都没发生"
  // （路径早就被导航同步回填了）。
  const [picked, setPicked] = useState<string | null>(null);
  const current = listing?.path ?? null;
  const barValue = editing ?? current ?? '';

  const jump = () => {
    const target = barValue.trim();
    setEditing(null);
    if (target) onGoto(target);
  };

  const pick = () => {
    if (!current) return;
    onPick(current);
    setPicked(current);
  };

  return (
    <section className="dir-browser" aria-label="宿主目录浏览器">
      <div className="dir-browser-bar">
        <button
          className="icon-btn"
          onClick={() => onGoto(listing?.parent ?? null)}
          disabled={!listing?.parent}
          aria-label="向上一级"
          title={listing?.parent ? `向上一级：${listing.parent}` : '已经是根目录'}
        >
          <ArrowUp size={13} />
        </button>
        <input
          className="dir-browser-path mono"
          value={barValue}
          onChange={(e) => setEditing(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') jump();
          }}
          placeholder="输入绝对路径后回车跳转（留空 = 盘符/根）"
          aria-label="当前路径"
          spellCheck={false}
        />
        {loading && <Loader size={13} className="dir-browser-spinner" aria-label="加载中" />}
      </div>

      <div className="dir-browser-head">
        <span
          className="dir-browser-current"
          title={current ?? '根'}
          data-root={current === null ? 'true' : undefined}
        >
          <FolderOpen size={12} aria-hidden="true" />
          {current ?? '根：选一个盘符或根目录开始'}
        </span>
        <button
          className="project-btn"
          onClick={pick}
          disabled={!current}
          title={current ? `用这个目录：${current}` : '根模式下没有可选择的目录'}
        >
          <FolderCheck size={12} aria-hidden="true" /> 选择此目录
        </button>
      </div>

      {error && (
        // 用**自己的** class（不是 .project-error）：同一个对话框里"表单提交失败"
        // 与"目录列举失败"是两个不同的面，共用 class 会让 `.project-dialog .project-error`
        // 这类既有选择器同时命中两个盒子（strict mode 撞车），也会让用户分不清
        // 哪条错误属于哪一步。样式与 .project-error 共用一条 CSS 规则。
        <div className="dir-browser-error" role="alert">
          {error}
        </div>
      )}

      {listing && listing.entries.length === 0 && (
        <div className="dir-browser-empty">
          {current === null ? '没有可用的盘符或根目录。' : '这个目录下没有子目录。'}
        </div>
      )}

      {listing && listing.entries.length > 0 && (
        // 不用 role="list"/"listitem"：这些是按钮，标成 listitem 会让读屏丢掉
        // "可按"的语义（同 ProjectDialogs 的 AttachForm）。
        <div className="dir-browser-list">
          {listing.entries.map((entry) => (
            <button
              key={entry.path}
              className="dir-browser-item"
              onClick={() => onGoto(entry.path)}
              title={entry.path}
            >
              <Folder size={13} aria-hidden="true" />
              <span className="dir-browser-item-name">{entry.name}</span>
            </button>
          ))}
        </div>
      )}

      {listing?.truncated && (
        // 截断必须说话：否则用户会以为"这个目录里就这么多"。
        <div className="dir-browser-truncated" role="status">
          子目录太多，这里只列出了前一部分（按名称排序）。
        </div>
      )}

      {picked && (
        <div className="project-notice" role="status">
          已选择：{picked}——确认无误后点「注册项目」。
        </div>
      )}
    </section>
  );
}
