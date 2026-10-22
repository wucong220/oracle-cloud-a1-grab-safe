# Oracle Cloud A1 本地安全加固分支

审计基线：`b6bc5cb20e0d8f5e51e4a4c1691c5573ef3c9e61`。
项目 [FBX-Dev/oracle-cloud-a1-grab](https://github.com/FBX-Dev/oracle-cloud-a1-grab)，2026-10-22 查询为 **1 Star**。

本分支固定目标为东京 Home Region `ap-tokyo-1`、唯一 AD-1、`VM.Standard.A1.Flex`、2 OCPU / 12GB、50GB Balanced 启动盘。
默认只读。没有有效的具体请求审批、账户证明和完整额度证据，创建必定停止。本次工作没有调用真实 OCI 接口，没有部署或升级账户。

- [完整源代码审计与风险对比](audit/SECURITY-AUDIT.md)
- [测试报告](audit/TEST-REPORT.md) / [机器可读结果](audit/test-results.json)
- [部署教程与审批条件](docs/DEPLOYMENT.md)
- [幂等状态机与保证边界](docs/IDEMPOTENCY.md)
- [回滚和未知请求处置](docs/ROLLBACK.md)
- [完整加固 diff](audit/hardening.patch)
- [固定源码清单](audit/source-manifest.json)
- [依赖与本地补丁来源](audit/dependency-manifest.json)

`setup.sh` 只在本地生成未填写的配置模板；不安装依赖、不处理私钥、不启动服务、不修改系统定时器。
`config.sh` 按纯数据解析，禁止 source/eval/命令替换。旧配置需要按新模板重新审核。
`notify.sh` 只输出固定的本地提醒；旧 PushPlus token 和 webhook 文件不再读取。

Windows 本地测试（Python 3.12、Git Bash、ShellCheck 0.11.0）：

```powershell
py -3.12 -I tools/run_checks.py --bash 'C:/Program Files/Git/bin/bash.exe' --shellcheck 'C:/tools/shellcheck.exe'
```

测试只使用可执行 Mock OCI 和合成账户数据，不读取用户 OCI 配置。原版 DRY_RUN 缺陷也通过 Mock 复现，不执行真实 launch。

真实只读预检和任何后续部署步骤见部署教程。本分支一次命令最多发送一次请求；重试必须重新执行全部核查，沿用同一持久化请求和 opc-retry-token，并遵守持久化退避。未知结果不会自动重试，token 超过安全有效期不会自动换新。

当前官方 Free Tier 说明为每月 1,500 OCPU 小时和 9,000 GB 小时，A1 总计 2 OCPU / 12GB；Home Region 的启动盘与块盘合计 200GB。服务 limit 数值不等于免费额度。[Oracle 官方说明](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)

保留原 MIT 许可证。上游原始文件可通过固定基线 commit 查看；不再保留其“一键启动即创建”、PAYG 建议或不真实的免费保证。
