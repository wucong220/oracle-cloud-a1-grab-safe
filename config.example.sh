# ============================================================================
#  抢机配置模板
#
#  用法： cp config.example.sh config.sh   然后按注释填写
#
#  ⚠️ config.sh 已被 .gitignore 忽略，不会误提交到仓库。
#     但里面含你的 tenancy OCID，属于个人信息，仍不建议随手分享。
#
#  第一次配置好后，建议先跑一次空转检查（不会创建任何实例）：
#      DRY_RUN=1 ./grab.sh
# ============================================================================

# ---- 必填：compartment OCID ----
# 个人账号直接填 tenancy OCID（控制台右上角头像 → Tenancy: <名字> → 复制 OCID）
# 想装到子 compartment 就填子 compartment 的 OCID
COMPARTMENT="ocid1.tenancy.oc1..xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

# ---- 选填：子网 OCID ----
# 留空 = 自动发现。只有在你的 compartment 里【恰好只有一个子网】时才敢自动挑，
# 否则脚本会报错并让你手工填。
# 形状是区域级子网（availability-domain 为 None）的最省事，三个可用域通用。
# SUBNET="ocid1.subnet.oc1.xxxxx..xxxxxxxx"

# ---- 选填：可用域列表 ----
# 留空 = 自动发现该区域全部可用域。也可以手工指定（空格分隔），例如：
#   ADS=("xxxx:PHX-AD-1" "xxxx:PHX-AD-2" "xxxx:PHX-AD-3")
ADS=""

# ---- 选填：镜像 ----
# auto = 自动挑该区域最新的 ARM 镜像（推荐）
# 也可以直接写镜像 OCID
IMAGE="auto"
IMAGE_OS="Oracle Linux"      # 可选 "Canonical Ubuntu" / "Oracle Linux" 等

# ---- 选填：规格 ----
# Always Free 额度上限是 4 OCPU / 24 GB。
# 但【1 OCPU / 6 GB 更容易捡到漏】，强烈建议先用小规格抢到再说。
OCPUS=1
MEMGB=6

# ---- 选填：轮询节奏 ----
# 每轮间隔 = INTERVAL + (0 ~ JITTER) 秒的随机值，避免固定节奏被识别。
# 一轮要打 可用域数量 次 API 请求，别调得太激进。
INTERVAL=45
JITTER=25

# ---- 选填：实例显示名 ----
NAME="a1-free"

# ---- 必填：登录公钥 ----
# 用来注入到新实例的 authorized_keys。私钥留在你本地，别放服务器上。
SSH_PUBKEY_FILE="$HOME/.ssh/id_rsa.pub"

# ---- 选填：登录用户名 ----
# Oracle Linux / Ubuntu 官方镜像默认都是 opc
USER_NAME="opc"
