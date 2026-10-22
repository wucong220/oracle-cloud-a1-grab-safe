# Windows 部署教程与审批门禁

本次只完成本地审计、修复和 Mock 检查。下述真实账户预检、依赖安装、任务启动和创建步骤 **尚未执行**；进入真实部署必须先得到用户明确批准。不升级 PAYG，不新增/修改 IAM、quota、预算、计费配置或网络。

## 1. 先离线复核交付

核对本地分支 `audit/hardened-free-tier-20261022`、审计基线和 [完整报告](../audit/SECURITY-AUDIT.md)。使用 Python 3.12 和现有 Git Bash：

```powershell
git status --short
git log -2 --oneline
py -3.12 -I tools/run_checks.py --bash 'C:/Program Files/Git/bin/bash.exe' --shellcheck 'C:/tools/shellcheck.exe'
```

本机审计缓存的 ShellCheck 位于 `.audit-tools/shellcheck.exe`；它是 shellcheck-py 0.11.0.1 的 Windows wheel 中的 0.11.0 二进制，wheel SHA256 为 `784156289ecb17e91c692cd783ab5152333309588cabb10032a047331c63e759`。不使用下载后直接执行的脚本。测试必须全部通过且无 skip；只能使用 tests/mock_oci.py。

## 2. 获得明确批准后，准备独立运行环境

用户已配置 Codex CLI 不代表已安装 OCI CLI。不要运行原版 setup.sh。保留用户已有 OCI 配置与私钥，不读取、打印、复制进仓库或上传它们。

本机审计已准备 `.audit-tools/hardened-wheelhouse`。其中 CLI wheel 是本地可复现的 metadata 修补版本：基于官方 3.95.1，仅将 certifi 约束改为 `==2026.10.19` 并重建 RECORD，不改变运行代码。官方原始约束不能选中已修复 CA 包。修补来源和所有 36 个 wheel 的 SHA256 在 [依赖清单](../audit/dependency-manifest.json)。不能用未修补 wheel 或直接在线 pip install oci-cli 代替。

取得部署批准后才手动安装到独立 venv，例如：

```powershell
py -3.12 -m venv "$env:LOCALAPPDATA/a1grab-oci-venv"
& "$env:LOCALAPPDATA/a1grab-oci-venv/Scripts/python.exe" -m pip install --no-index --find-links '.audit-tools/hardened-wheelhouse' --require-hashes -r requirements-oci-windows-py312.lock
```

这份 lock 只针对 Windows amd64 / Python 3.12。禁止 `--no-deps`、取消哈希或忽略版本冲突。运行前核心验证 venv、CLI launcher 哈希、固定版本、修补后的 METADATA、certifi 代码与 CA 数据哈希，失败就停止。

如果缓存被移除，可重新下载官方 `oci-cli==3.95.1` 的全部 wheel 到新目录（只下载，不安装），按清单验证 35 个未修改 artifact；单独从 PyPI 下载 certifi 2026.10.19，核对 SHA256 `42d35340cc5b9ba78e818ddbff0c5b1c2b779de448a340fd1b989f5fa8670e1b`，然后运行离线构建工具：

```powershell
py -3.12 -I tools/patch_cli_dependency.py --original-wheelhouse C:/private/original-wheels --patched-certifi-wheel C:/private/certifi-2026.10.19-py3-none-any.whl --output C:/private/hardened-wheels
```

工具强制核对官方 CLI 和修复包的原始哈希。输出的 patched CLI SHA256 必须为 `026d31775377ce99acfb8ecae29095bcfc1fc3031baa4e705042e7cd1fa2fbb7`，并再次执行依赖通告复核及离线 pip dry-run。上游版本或规则变化时重新审计，不能直接更新 lock。

## 3. 准备配置，仍保持只读

在 `%LOCALAPPDATA%/a1grab-config` 等不经云盘同步的私有目录中保存配置与审批文件。Windows 文件夹 ACL 仅应允许当前用户及受信任的系统管理员；Linux 目录 700，配置/审批/认证文件 600。不要放在当前百度同步工作区。真实 OCI 私钥继续由 OCI CLI 从用户现有安全位置读取；本程序不要求复制私钥。

```powershell
py -3.12 -I a1grab.py setup --config "$env:LOCALAPPDATA/a1grab-config/config.sh"
```

先自行创建私有父目录。填写现有 Tenancy、目标 compartment、Tokyo subnet 和经人工确认无许可费用的官方 ARM 平台镜像 OCID，以及 SSH **公钥**绝对路径、OCI_BIN、OCI_CONFIG_FILE。公钥文件不能是私钥或 authorized_keys options。Windows 路径使用 `C:/...`，不写 `$HOME`、变量或反斜杠转义。`OCI_CLI_SHA256` 用经过审核的 venv `Scripts/oci.exe` 计算，不输出任何凭据。

形状/CPU/内存/磁盘/区域/AD 保持模板硬限制。`DRY_RUN=1`、`ALLOW_CREATE=0` 保持关闭，`REVIEW_FILE` 先指向未来私有审批文件。程序不自动选镜像、子网、较小规格或其他 AD。

只读预检（得到真实部署阶段的批准后再运行）：

```powershell
py -3.12 -I a1grab.py run --config "$env:LOCALAPPDATA/a1grab-config/config.sh" --dry-run
```

只读清单包括账单 Usage POST 查询，它只是读取汇总数据，不是创建 API。Dry run 无法测试容量，不能用 launch 或 capacity-report 创建请求来“验证”。遇到错误/403/未知额度必须先处理证据，禁止通过扩大 IAM 或切换账户来继续。

## 4. 复核免费资格和具体创建请求

核实账户确为 FREE_TIER 且无升级/试用状态；Home Region、唯一 AD、全部 compartment 的现有 A1 和共享用量；所有启动盘/块盘/备份；官方镜像许可、Balanced 启动盘资格；现有网络没有会因新增实例触发的收费配置；所有其他创建来源已停止；旧脚本、旧 SUCCESS/instance.json/RESULT 和未知历史请求已核清。

Usage API 报表为空不是零用量证明。必须根据完整账单、已终止实例和迟到数据确定本月 A1 OCPU 小时、内存 GB 小时的可信上界；不能确认就停止。Budget 不是硬扣费阻断，本流程不会创建预算或更改账单设置。

在用户明确批准具体免费请求后，复制 `schemas/review.example.json` 到私有 `REVIEW_FILE`。填入预检返回的两个 SHA256；填写带时区的 reviewed_at / expires_at（最长 1 小时）、本月、账户类型和真实用量上界；逐项据实确认。模板全部 false/null，工具不自动批准。`approved=true` 只代表这次明确人工批准，不允许付费资源或超规格。

## 5. 仅在批准及证据均齐备时发送一次请求

把私有配置改为 `DRY_RUN=0`、`ALLOW_CREATE=1`，移除环境中的 DRY_RUN=1 veto。人工执行：

```powershell
py -3.12 -I a1grab.py run --config "$env:LOCALAPPDATA/a1grab-config/config.sh" --create
```

一次调用最多一次 launch；即使预检成功，也可能因资源变化、快照超过 60 秒或审批过期而停止。容量不足/限流时，等待持久化退避后再次人工运行；每次都重新核查。没有默认注册 Windows 计划任务，没有后台循环，也没有自动开机创建。

返回 UNKNOWN 时不要直接重试。按 [幂等恢复](IDEMPOTENCY.md) 核查；只有再次明确批准恢复且 token 仍有效、body 未改变，才使用 `--create --resume-unknown`。不要删除 journal 或换目录绕过门禁。超过 23h 后停止，需人工核清。

状态查询：

```powershell
py -3.12 -I a1grab.py status --config "$env:LOCALAPPDATA/a1grab-config/config.sh"
```

成功后恢复 `DRY_RUN=1` / `ALLOW_CREATE=0`，撤销本地审批，不删除 journal。未分配公网地址，访问使用已存在、已确认费用的私网方案；本流程不新增网络。Linux 的 `a1grab.service` 只是只读 user unit 模板，本次未安装/启动/验证真实 systemd。
