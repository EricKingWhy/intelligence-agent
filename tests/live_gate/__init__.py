"""Live Gate（`#307`）的 credential-free 测试包。

三条纪律（写在这里，因为包的每个文件都受它约束）：

1. **不替真实证据**：这里跑的是 runner / validator / scanner 的**机制**，不是 Live Gate 结论。
   任何本包构造的证据只落 `tmp_path`，**绝不**写进 `docs/live_gate/`（`#305`：Fake 不得计入
   Live Gate）；
2. **不发网络请求、不读凭证值**：真实模型路径由 `scripts/live_gate.py run` 在有凭证的环境里跑
   （证据入库）；本包在**没有凭证**的机器上必须全绿；
3. **不碰系统凭据管理器与全局 provider 目录**：`#203` 的测试事故（探针供应商写进全局
   `model-providers.json`、假 key 写进 OS 凭据管理器且删不掉）在这里复现一次都算事故。
"""
