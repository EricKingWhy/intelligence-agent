## gate0 收据（receipt）

- 收据引用**统一用入库文件名**：`docs/gate/<40 位被测树 sha>.json`（就是文件里 `sha` 字段那个值，
  也是它自己的文件名）。**不引用 PR tip**，也不引用"按父提交命名"的文件——此前的混淆（如 #1309）
  正是 guards 的 `receipt-present` 检查要消灭的。
- 规则出处：`docs/SDD_WORKFLOW_PROTOCOL.md` §8.1（冻结树 / 读数传递）、issue #1338 改动 3。

- [ ] 收据已入库：`docs/gate/<sha>.json` 存在且 gate0 全部车道 PASS（`result: PASS`、`passed == total`；车道数以 `scripts/gate0.py --list` 为准）
