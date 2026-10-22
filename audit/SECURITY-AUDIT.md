# 源代码安全审计与本地加固报告

审计日期：2026-10-22（Asia/Shanghai）。对象：[FBX-Dev/oracle-cloud-a1-grab](https://github.com/FBX-Dev/oracle-cloud-a1-grab)，本次页面查询 **1 Star**。固定基线：`b6bc5cb20e0d8f5e51e4a4c1691c5573ef3c9e61`；本地分支：`audit/hardened-free-tier-20261022`。精确文件大小、SHA256 和 Git tree 见 [source-manifest.json](source-manifest.json)。

**结论：原版不满足安全验收，尤其不能执行其 DRY_RUN 或 setup.sh。已在本地实现加固和自动化验证。真实部署保持关闭。** 本次没有调用任何真实 OCI API，没有读取用户 OCI 私钥、Token 或配置，没有修改云资源、IAM、计费或账户类型，也没有 push。完整源码审计发现的是实际安全缺陷；没有发现固定源码树内隐藏上传私钥、挖矿、反向连接、混淆载荷或下载后直接执行的后门证据。该结论不等于证明作者意图或所有第三方依赖绝对安全。

## 审计覆盖

逐行检查固定基线的全部 **10 个文件**，包括全部 README；同时对可达的 **5 个 commit / 50 个文件版本**扫描私钥标记与高置信凭据赋值，仅记录命中位置，不记录匹配内容。[历史扫描结果](history-scan.json)：这些规则未命中。Git 对象完整性检查通过。没有 submodule、仓库内二进制、安装钩子、GitHub Actions 或 vendored 依赖。

| 原始文件 | 审计结果与加固处置 |
|---|---|
| `.gitattributes` | LF 换行和图片 binary 规则；无可执行过滤器，保留。 |
| `.gitignore` | 原有凭据忽略规则合理，但不能阻止强制提交、其他备份或百度同步。增加运行状态、审批和工具缓存忽略；真实状态改放 Windows 本地 AppData。 |
| `LICENSE` | MIT 文本，无执行或联网行为，保留。 |
| `README.md` | 免费额度、幂等和 DRY_RUN 保证与实际行为矛盾；包含自动启动、关闭 timer、PAYG 和任意规格建议。重写为真实安全行为、限制和审批流程。 |
| `grab.sh` | F01–F05、F10；原有程序由固定路径的 Python 核心替代，Shell 只传递参数，不再 source 配置。 |
| `setup.sh` | F05、F08、F09；改为本地模板生成，不安装、复制凭据、鉴权、改服务或启动任务。 |
| `notify.sh` | F06、F07；改为固定本地提醒，不读取旧 token/webhook，不发送第三方消息。 |
| `status.sh` | 原来输出完整结果、日志，可能连带暴露通知返回内容；改为只输出白名单状态和尝试次数。 |
| `config.example.sh` | 原来允许任意规格、自动镜像、自动 AD、区域继承和可执行 Shell；改为纯数据，固定 Tokyo / AD-1 / A1 / 2 / 12 / 50GB / 10 VPU。 |
| `a1grab.service` | 原来 `Restart=always`、无启动速率限制和进程加固；改为默认只读 user unit、oneshot、`Restart=no`、最小权限与私有文件模式。 |

新增 `a1grab.py` 的配置解析、API 允许列表、凭据边界、全租户核查、账单门禁、锁、状态持久化、错误分支和日志输出均经过人工检查，并由 Mock 测试覆盖。测试、证据生成和依赖修补工具也已审查：生产核心只经 OCI CLI 访问 OCI；可选 `check_dependencies.py` 仅向 OSV 发送公开包名/版本；其余新增工具离线读 Git 或 wheel 数据，不执行安装包。

## 风险对比与可复现依据

以下行号均指固定基线，避免与加固后文件混淆。

| 编号 / 严重性 | 修改前的触发与后果 | 修改后 / 自动化证据 |
|---|---|---|
| F01 / 严重 | `grab.sh:154–185`：DRY_RUN 在第 166 行执行真实 launch；碰到容量会创建实例，且不写正常成功标记。 | 默认只读，DRY_RUN 不进入唯一写入口、不生成创建意图；原版缺陷由 fake Bash OCI 复现，新版断言零写调用。 |
| F02 / 高 | `grab.sh:70–74,219–259`：只查本地 SUCCESS，没有互斥；创建后先等待 VNIC，最终才写标记。并发、崩溃、删文件、复制目录均可重复。 | 与 checkout 无关的 OS 身份 / 租户锁；发送前 fsync 意图；成功 OCID 立即持久化；Mock 多进程与崩溃测试。 |
| F03 / 高 | `grab.sh:219–301`：没有 opc-retry-token；超时、返回未知或重复请求按普通异常进入下轮。 | 固定请求体、持久化 retry token；UNKNOWN 先只读核查，需显式恢复；同 token 重放；超过 23h 停止且不自动换 token。 |
| F04 / 严重 | `grab.sh:50–61,166–229`：不查询现有实例、启动中实例、磁盘、账户计划或月度使用量；MODEL/CPU/内存可任意改，启动盘不明确，区域继承认证文件。 | 每次发送前核查全部 compartment、全部非 TERMINATED A1、共享额度、磁盘、备份、controller；固定目标与启动盘，不明即停。 |
| F05 / 高（配置被篡改时） | `grab.sh:42–44`：source 配置可执行任意系统命令。SSH key 和配置值拼接 JSON，特殊字符可能改变请求或使参数失效。 | 白名单 KEY=value 数据解析；拒绝展开/数组/命令替换；JSON 序列化；公钥结构校验、禁止私钥、多行及额外 metadata。没有 eval 或 shell=True。 |
| F06 / 高（通知配置不可信时） | `notify.sh:43–49`：任意 curl 地址，无协议/主机允许列表，也缺少 URL/option 隔离。 | 删除通用外部发送路径；不再读取旧地址文件。 |
| F07 / 中 | `notify.sh:30–37`：标题/内容未做 JSON 转义；token 作为 curl 命令行参数；原始返回内容可落盘并进入日志。 | 本地固定文本，不输出参数、token 或外部响应。生产 stdout/stderr 只包含稳定状态码和非敏感汇总。 |
| F08 / 高 | `setup.sh:196–218`：默认 sudo disable 多个 dnf/PCP timer，改变系统更新缓存和监控行为。 | 完全移除。没有执行过旧 setup，也没有更改任何现有 timer。 |
| F09 / 高 | `setup.sh:55–65,223–245`：未固定版本/哈希升级 pip 和安装依赖；默认 enable/restart 服务，设置完就可能真实创建。 | setup 仅生成模板；依赖全量哈希锁定，安装与服务启动由后续人工部署流程控制。 |
| F10 / 中 | `a1grab.service:6,18–19`：无限重启可能重复触发异常分支；系统 service 中 `%h` 与 `User=` 的假设也不可靠。 | 改为明确的 user unit，只读单次运行，不自动重启；Windows 使用原生 Python 入口。 |
| F11 / 中 | `README.md:6,63,350–372,500–501,680–710`：额度和“零扣费/绝不重复”保证不成立，服务 limit 被混用为免费保障。 | 规则来源、月度用量、账户计划分别验证；不将任何 limit 或空列表解释为免费证明。 |
| F12 / 高（当前依赖解析） | 当前官方 OCI CLI 3.95.1 将 certifi 限制在 2026 年以前；按该约束解析得到 2025.11.12，命中 CA 信任问题。 | 对固定官方 wheel 做可复现的单行依赖 metadata 修补，使用 certifi 2026.10.19，重建 RECORD；不改 CLI 运行代码。运行前校验修补 metadata 和 CA 文件哈希。 |

F12 的两个 advisory ID（`GHSA-6q48-6c7w-9gw2` 与 `PYSEC-2026-4320`）是同一问题的别名。[公开通告](https://osv.dev/vulnerability/GHSA-6q48-6c7w-9gw2) 与 [修复版本](https://pypi.org/project/certifi/2026.10.19/)。[修改前依赖扫描](dependency-advisories-before.json) 和 [修改后扫描](dependency-advisories.json) 均保留；修改后 36 个依赖未命中 OSV 当前收录通告。没有压制该告警，也没有用 `--no-deps` 绕过解析；哈希锁定的离线依赖 dry-run 解析成功。没有安装或执行真实 OCI CLI。

## 免费额度、账户与账单门禁

本次查阅的 [Oracle Always Free 官方规则](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm) 为 A1 每月 1,500 OCPU 小时、9,000 GB 小时，Free Tier 总计 2 OCPU / 12GB；Home Region 内启动盘和块盘合计 200GB、备份总计 5 个。2/12 连续 31 天分别为 1,488 / 8,928 小时。已有或已终止实例的本月历史用量仍需计入，不能仅看当前实例数。

每次创建之前必须完成：Home Region 与唯一 AD 查询；root + 全部活动子 compartment 的完整分页核查；所有 A1 状态计数（含 PROVISIONING / STARTING / STOPPED / TERMINATING）；启动盘和块盘合计；共享 A1 compute/container 额度；其他 controller/容量预留排查；指定官方平台镜像/子网验证；[OSP subscription](https://docs.oracle.com/en-us/iaas/tools/oci-cli/latest/oci_cli_docs/cmdref/osp-gateway/subscription-service/subscription/list.html) 返回 FREE_TIER，且无升级状态；[Usage API](https://docs.oracle.com/en-us/iaas/tools/oci-cli/latest/oci_cli_docs/cmdref/usage-api/usage-summary/request-summarized-usages.html) 查询本月所有分页。任一失败、无权限、未知字段、状态或用量均停止。

限额查询仅证明服务配额余量，不证明价格。账单报告可能延迟，因此空 Usage 结果也不能当作零用量；[Oracle 报告延迟说明](https://docs.oracle.com/en-us/iaas/Content/Billing/Concepts/costusagereportsoverview.htm)。必须另有 **最长 1 小时有效**、绑定租户和完整请求 SHA256 的人工复核，给出覆盖迟到数据/已终止实例的本月用量上界，确认官方镜像与 Balanced 启动盘资格、全 compartment 可见性、没有其他创建者或未结请求。该文件是人的证明材料，不能由空列表或程序自动勾选生成。无法给出可信上界时，禁止创建并报告。

程序核算“人工确认的已用上界 + 本月剩余时间的 2/12 满载使用量 + 1 小时粒度余量”，不得超过月度免费总量。审批不能放宽硬限制。新建请求禁用公网 IPv4/IPv6、AI Enterprise、默认容量预留，不注入 user-data，不创建网络、额外磁盘、备份策略或任何其他资源。现有子网后续路由/服务的费用和实例中应用的后续行为需在部署复核时确认；本程序不会配置它们。

## 幂等和真实部署的边界

[详细状态机](../docs/IDEMPOTENCY.md)。支持同一 OS 用户在该 Windows 电脑上多进程、多 checkout 和脚本重启；OS 提供共同状态路径和文件锁。已接受/未知的意图不会自动变成新意图。旧 token 过期、状态损坏、磁盘写入失败、已有实例或查询不确定都会停止。Oracle 的 token 有 24h 有效期且可能提前失效，本实现以 23h 保守上限停止，409/失效错误进入 UNKNOWN。[Oracle retry-token 语义](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/usingapi.htm)

OCI 列表查询与创建不是账户级原子事务。本地锁不能约束控制台、其他电脑或其他用户，也不能在本地状态被删除后恢复无限期历史；不作此类保证。对此没有悄悄降低验收标准：程序要求明确确认独占创建者及状态完整，无法确认便禁止真实创建；没有分布式互斥证明时不支持多主机部署，也没有通过创建云锁、改 IAM 或改 quota 来绕开本次禁令。

当前尚无用户真实账户类型、实时资源/账单上界、镜像资格或部署批准的核验证据，因此 **真实部署不获放行**。这不影响本次完整源码审计、本地修复和 Mock 验证的交付；任何后续部署必须另获用户明确批准，并重新完成所有门禁。未来价格变化、恶意本机管理员或被整体替换的 Python/OS 不在代码可保证范围内。

## 交付与复核

[测试报告](TEST-REPORT.md)、[完整 diff](hardening.patch)、[部署教程](../docs/DEPLOYMENT.md)、[回滚方案](../docs/ROLLBACK.md)。对固定源代码的静态检查与 Mock 结果，不替代真实账户只读复核或真实系统服务兼容性验证；本次严格没有进行真实部署测试。可用 `git diff b6bc5cb20e0d8f5e51e4a4c1691c5573ef3c9e61..HEAD` 复核本地提交。
