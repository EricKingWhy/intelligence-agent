# intelligence-agent

一个从零实现的 AI Coding Agent：模型驱动循环，工具在隔离沙箱内读写文件、跑命令、改代码。
本文件是项目的领域词汇表，只定义概念，不写实现细节。

## Language

**Sandbox**:
模型发起的命令与文件操作实际运行的隔离执行环境，是 Runtime 的安全边界而非 Prompt 约束。
_Avoid_: container, executor, environment

**Workspace**:
Sandbox 内部允许 Coding Tool 读写的唯一目录；越界访问会被 Sandbox 拒绝。
_Avoid_: working dir, project folder, bind mount, volume

**Coding Tool**:
在 Sandbox 内执行、按 `Tool` 契约暴露给模型的工具（read / write / edit / bash）。
_Avoid_: code tool, file tool, action

**Host**:
用户真实机器，Sandbox 的隔离边界之外的环境。
_Avoid_: 本机, server, node

**Container**:
由 Docker 提供的隔离执行环境，是 Sandbox 的一种后端实现。
_Avoid_: VM, jail, pod

**ExecResult**:
Sandbox 执行一条 shell 命令后返回的原生结果（exit_code / stdout / stderr / duration），由 bash 工具映射成 `ToolResult`。
_Avoid_: CommandResult, ShellResult, RunOutput

**命令业务失败**:
shell 命令返回非零 exit_code 但 Sandbox 调用本身成功（如 pytest 测试不通过）。这不是 Tool Runtime 异常，Agent 应读 stdout/stderr 决定下一步。
_Avoid_: tool failure, execution error, transient error
