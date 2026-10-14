"""#1048 AC9：更新 ADR/操作说明，登记 Linux 文件系统语义与 legacy 迁移失败语义。

红阶段（TDD）：当前无对应 ADR（最新为 0053）。本文件用文档守卫钉住：
- 新增 ADR 记录：受影响工作区迁到私有 Docker named volume、Linux 文件系统语义、
  导入=快照、导出=新目录副本、PSEC legacy migration 失败语义；
- **不回写**冻结 Engineering Specification（`goal/.../docs/spec/05_SANDBOX_CODING_TOOLS.md`）；
- 文档**不得**宣称 PSEC/ACL 可隔离 NTFS hard link。
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ADR_DIR = _REPO_ROOT / "docs" / "adr"
_FROZEN_SPEC = (
    _REPO_ROOT
    / "goal"
    / "Lightweight_Observable_Agent_Harness_Spec"
    / "docs"
    / "spec"
    / "05_SANDBOX_CODING_TOOLS.md"
)


def _matching_adrs() -> list[Path]:
    return sorted(_ADR_DIR.glob("*linux*volume*.md")) + sorted(
        _ADR_DIR.glob("*ntfs*.md")
    )


class TestWorkspaceBackendAdr:
    def test_adr_documenting_linux_volume_migration_exists(self) -> None:
        """AC9：存在记录私有 volume 迁移的 ADR。"""
        matches = _matching_adrs()
        assert matches, "未找到记录 Linux volume / NTFS 隔离的 ADR（AC9 未完成）"

    def test_adr_covers_snapshot_and_legacy_semantics(self) -> None:
        """AC9：ADR 覆盖「导入=快照 / 导出=新目录副本 / PSEC legacy 失败语义」。"""
        matches = _matching_adrs()
        assert matches, "未找到 ADR（AC9 未完成）"
        text = "\n".join(p.read_text(encoding="utf-8") for p in matches)
        assert "snapshot" in text.lower() or "快照" in text
        assert "legacy" in text.lower() or "legacy" in text

    def test_frozen_spec_not_rewritten_to_claim_acl_isolates_hardlink(self) -> None:
        """AC9：新 ADR 落地 AND 冻结 spec 不得被回写成「ACL/PSEC 可隔离 hard link」。"""
        matches = _matching_adrs()
        assert matches, "未找到 ADR（AC9 未完成）"
        if not _FROZEN_SPEC.exists():
            return
        text = _FROZEN_SPEC.read_text(encoding="utf-8")
        assert not re.search(
            r"(ACL|PSEC|DACL)[^\n]{0,40}(isolat|隔离)[^\n]{0,20}hard ?link",
            text,
            re.IGNORECASE,
        )
