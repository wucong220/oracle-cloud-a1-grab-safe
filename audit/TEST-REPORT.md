# 自动化测试报告

最终完整运行结果：**88 项测试通过，97 个参数子场景，0 failure、0 error、0 skip**。ShellCheck 和逐文件 Bash 语法检查共 6 项全部通过。执行环境：Windows、Python 3.12.14、Git Bash、ShellCheck 0.11.0。精确开始/结束时间、每个测试名称和被测源码 SHA256 在 [test-results.json](test-results.json)。

所有 OCI 行为都使用可执行 `tests/mock_oci.py` 或注入的 Mock runner。测试不读取真实 OCI 配置、不使用用户私钥、不调用真实 OCI CLI、不进行真实 OCI API 请求。**真实 launch 数为 0。** 基线回归测试执行的是经过审查的旧脚本副本，PATH 和 HOME 被隔离，唯一可用 OCI 命令是生成的 fake Bash OCI。

## 关键验收结果

| 验收 / 故障场景 | 观察到的安全行为 |
|---|---|
| 原版 DRY_RUN | Mock 记录到 1 次 launch，重现原始严重缺陷；没有真实创建。 |
| 新版默认 / DRY_RUN / 环境 veto | 0 写调用，不创建 journal/重试意图；即使私有配置开启创建，DRY_RUN 也不发送。 |
| 成功返回 | Mock 只产生 1 个实例，OCID 立即持久化；重启只读核查，不再次 launch。 |
| 容量不足 | 至少 600 秒持久化退避；再次尝试前重新查实例；相同 token 与请求摘要，最终仍只有 1 个 Mock 实例。 |
| 429 限流两种措辞 | 两种均识别；尊重 Retry-After；重启不绕过退避。 |
| CLI 返回读取超时、结果不明 | UNKNOWN 持久化；普通重启禁止自动重试。 |
| 服务端已接受但客户端超时 | 重启找到 PROVISIONING 实例，核实成功；不再创建。 |
| 超时且列表暂不可见 | 显式恢复后用同一 token/body 重放，Mock 服务端幂等合并为 1 个资源。 |
| 子进程硬超时 | 在 IN_FLIGHT 后模拟 TimeoutExpired，保持 UNKNOWN，不生成新意图。 |
| 409 / duplicate / token invalidated / 未知 5xx / 非 JSON | 保留未知结果并停止；先核查，不盲目重试。 |
| 发送前进程崩溃 | durable IN_FLIGHT 保留；重启不猜测“没有发送”；只在明确恢复后发送同一意图。 |
| 接受后、成功落盘前崩溃 | 实际子进程退出 77；重启通过 Mock 库存找回实例；仍仅 1 次 launch。 |
| 实际多进程竞争 | 第一个持有 OS 锁，第二个停止；总计 1 次 launch / 1 个 Mock 实例。 |
| fsync 写失败 | 意图无法持久化，0 launch。 |
| 已有 A1 / 启动中 A1 / 停止或终止中 A1 | 全部阻止新建；子 compartment 内实例同样计入；untagged/null tags 不绕过检查。 |
| 查询失败、未知数量/状态、全分页未取得 | 直接停止；17 类读操作逐项模拟失败。 |
| boot + block / 性能 / 复制 / 备份 | 合并核算；超过 200GB、未知大小、高性能或自动调优/复制、超备份额均停止。 |
| 历史账单、迟到数据、空 Usage | 当前实例为空不消除历史用量；无可信人工上界/审批则禁止创建。 |
| 账户类型 / 区域 / AD / 规格 / 镜像 / quota | PAYG、试用或未知计划、错误区域/AD、任意越界参数、不明官方镜像资格、用量不一致均停止。 |
| Token 23h 过期 / 时钟回退 / body 改变 / journal 损坏 | 不自动换 token，不开始新意图。 |
| 命令/JSON 注入与日志泄露 | 配置被当作数据；source/eval/shell=True 禁止；私钥/多行公钥拒绝；敏感 stderr sentinel 不出现在输出；token 不在进程命令行中。 |
| Windows 空格路径和 Shell 入口 | setup 本地模板生成正确，通知参数不回显；CLI file:// 使用原生路径格式。 |
| 依赖本地修补 | 只改 METADATA 与 RECORD；重复构建字节一致，所有 RECORD 哈希正确；原始 hash 不符拒绝。 |
| CA 运行前校验 | 修改已确认的依赖文件后，在执行真实 CLI 之前停止。 |

## 静态和 API 契约检查

`ShellCheck --severity=style` 检查 grab.sh、setup.sh、notify.sh、status.sh、config.example.sh；全部无诊断。配置模板仅对“由 Python 使用的未引用赋值”显式关闭 SC2034，并声明 Bash 语法；其他规则没有整体禁用。5 个文件各自通过 `bash --noprofile --norc -n`。

从哈希验证过的 OCI CLI 3.95.1 与 SDK 2.189.0 wheel **读取 AST，未导入/执行 SDK**，提取 18 个读命令的支持参数与实际模型字段。[契约证据](oci-cli-contract.json) 支撑完整分页、OSP collection/items、LaunchInstance JSON、Windows file:// 参数和 raw-request 非 2xx 也可能 exit 0 的处理。Mock 的安装环境核验只在专用测试 worker 中替换，真实运行前核验函数另有独立文件篡改测试。

当前 36 个依赖全版本/哈希锁定，离线 pip `--dry-run --no-index --require-hashes` 解析通过，实际未安装包。[依赖解析证据](dependency-resolution.json)。修改前 certifi 的两个通告 ID 对应同一问题；修复后 OSV 当前版本查询为 0 命中。[前后通告结果](dependency-advisories-before.json)、[修复后结果](dependency-advisories.json)。这不宣称不存在尚未收录的漏洞。

## 复现

```powershell
py -3.12 -I tools/run_checks.py --bash 'C:/Program Files/Git/bin/bash.exe' --shellcheck '.audit-tools/shellcheck.exe'
```

该命令不联网、不安装依赖、不运行真实 OCI。Bash 或 ShellCheck 缺失会失败，不会跳过。需保留固定基线 Git 对象，以便运行原版回归测试。

实际账户只读预检、真实 API 的账户资格/延迟核验、真实 systemd 或 Windows 任务启动均未执行；它们属于必须得到用户另行批准的部署阶段。当前自动化结论限于完整源代码与上述 Mock/静态验证，不提供未经核验的“账单必为零”结论。
