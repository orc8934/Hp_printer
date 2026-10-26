# HP 4928 打印服务

给 **惠普 HP DeskJet Ink Advantage 4928**（DeskJet 4920 系列）在 Debian 系 Linux 上跑的
USB / 局域网打印服务：浏览器上传文件 → 服务转换成 PDF → 通过 CUPS 送到 USB 打印机。

- 端口 **10086**，监听 `0.0.0.0`，手机/同事电脑在局域网里就能用
- 账号密码登录（首次运行自动生成默认账号，登录后可改）
- 默认 **黑白 + A4 + 单面**
- **手动双面**：先打奇数页 → 停住等你翻面 → 点「我已翻面，继续打印」才打偶数页
- 支持 **PDF / Word（.doc/.docx）/ 图片**，图片默认按 **6 寸相纸（4R 102×152mm）** 排版
- USB 不可用时自动**降级为虚拟打印机**（输出 PDF），界面明确提示，不会假装打印成功
- 「高级设置」**逐项对齐已安装的 HP 官方驱动**（PPD / `lpoptions` 真实选项，不做臆造）

---

## 目录

1. [功能与需求对照](#1-功能与需求对照)
2. [快速开始](#2-快速开始)
3. [默认账号与改密](#3-默认账号与改密)
4. [项目结构](#4-项目结构)
5. [手动安装（含虚拟环境）](#5-手动安装含虚拟环境)
6. [配置 USB 打印机](#6-配置-usb-打印机)
7. [局域网访问](#7-局域网访问)
8. [使用手册](#8-使用手册)
9. [高级设置与官方驱动对照](#9-高级设置与官方驱动对照)
10. [降级与虚拟打印机](#10-降级与虚拟打印机)
11. [命令行参考](#11-命令行参考)
12. [配置参考](#12-配置参考)
13. [安全建议](#13-安全建议)
14. [故障排查](#14-故障排查)
15. [升级与卸载](#15-升级与卸载)
16. [已知限制](#16-已知限制)
17. [开发与测试](#17-开发与测试)

---

## 1. 功能与需求对照

| 需求 | 实现方式 | 位置 |
| --- | --- | --- |
| Debian 系 Linux、Python3 | Python 3.9+，纯 Python 依赖（Flask / waitress / pypdf / Pillow / PyYAML） | `hp4928/` |
| 跑在 10086 端口 | `server.port: 10086`，可用 `--port` 覆盖 | `config.yaml` |
| 账号密码 | scrypt 哈希 + 签名会话 + 登录限速 + CSRF；首次运行生成随机密码 | `hp4928/auth.py` |
| 默认账号可改 | 网页「设置 → 我的账号」或 `python -m hp4928.cli passwd` | `hp4928/cli.py` |
| USB 打印 | CUPS + HPLIP **hpcups** 驱动队列（USB 2.0 直连） | `scripts/setup-usb-printer.sh` |
| USB 行不通时降级 | USB 掉线 / 无 CUPS / 无队列 → 自动切虚拟打印机，任务留存为 PDF | `hp4928/printers.py` |
| 局域网打印 | 监听 `0.0.0.0:10086`，手机浏览器直接上传；可选 IP 白名单 | `server.host` / `allowed_cidrs` |
| 选择/切换打印机（USB ↔ 局域网） | 设置页管理 CUPS 队列：列表、扫描、添加 `ipp://`、切换、打测试页、删除 | `hp4928/printers.py`、`api.py` |
| 虚拟环境 | `scripts/install.sh` 或 `scripts/bootstrap-venv.sh` 建 `.venv` | `scripts/` |
| 默认黑白 A4 单面 | `color: mono`、`media: A4`、`duplex: off`、`quality: normal` | `hp4928/default_config.py` |
| 手动双面 + 继续按钮 | 两遍打印：奇数页 → 暂停 → 显式确认 → 偶数页（可逆序） | `hp4928/jobs.py` |
| Word 文档 | LibreOffice headless 转 PDF（保留版式），中文字体随装 | `hp4928/docs.py` |
| PDF 文档 | 原样投递，支持页码范围、每张多页、缩放 | `hp4928/docs.py` |
| 6 寸相纸 | 按 4×6 英寸（4R）排版，DPI/留白/裁剪可选，自动转正 | `hp4928/docs.py` |
| 高级设置对齐官方驱动 | 运行时读取 PPD / `lpoptions`，界面即驱动选项；无驱动时用官方能力画像 | `hp4928/ppd.py` |
| README | 本文件 | — |

---

## 2. 快速开始

### 2.1 一键安装（推荐）

```bash
# 把项目放到服务器上，例如 /root/hp4928-print
cd /root/hp4928-print
sudo ./scripts/install.sh
```

脚本会完成：

1. `apt` 安装依赖：`cups`、`cups-client`、`cups-filters`、`hplip`、`poppler-utils`、
   `libreoffice-writer`、`fonts-noto-cjk`、`python3-venv`
2. 创建系统用户 `hp4928`，把程序装到 `/opt/hp4928-print`，建虚拟环境 `.venv`，
   并安装命令行工具 `/usr/local/bin/hp4928`（任意目录可用）
3. 生成 `/etc/hp4928-print/config.yaml`（端口 10086、数据目录 `/var/lib/hp4928-print`、
   默认黑白 A4 单面）
4. 装 systemd 服务并开机自启
5. 初始化数据库 + **生成默认账号**
6. 调用 `scripts/setup-usb-printer.sh` 创建 CUPS 队列 `HP4928`（自动识别 4900/4920 系列 PPD）
7. 如果启用了 ufw，自动放行 `10086/tcp`

常用参数：

```bash
sudo ./scripts/install.sh --port 8080      # 换端口
sudo ./scripts/install.sh --no-office      # 不装 LibreOffice（Word 不可用）
sudo ./scripts/install.sh --no-printer     # 不配 CUPS，先跑虚拟模式
```

装完会直接打印访问地址和默认密码。

### 2.2 手动最小验证（不装服务、先看效果）

```bash
cd hp4928-print
./scripts/bootstrap-venv.sh          # 建 .venv 并装依赖
source .venv/bin/activate
python -m hp4928.cli init            # 建库 + 生成默认账号（会打印密码）
python -m hp4928.cli doctor          # 环境体检
python -m hp4928.cli serve --backend virtual   # 没有打印机也能先跑起来
```

浏览器打开 `http://<服务器IP>:10086/`，用打印出来的账号登录即可试用（任务会输出成 PDF）。

---

## 3. 默认账号与改密

首次启动（`serve` 或 `init`）时，如果没有用户，会创建：

| 项 | 值 |
| --- | --- |
| 用户名 | `admin`（配置项 `security.default_admin_user`） |
| 密码 | **随机 16 位**，同时打印在终端并写入 `<数据目录>/initial-admin.txt`（权限 0600） |

修改密码的三种方式：

1. **网页**：右上角「设置」→「我的账号」→ 填当前密码和新密码
2. **命令行**（推荐运维用，安装脚本已生成 `/usr/local/bin/hp4928`）：
   ```bash
   sudo hp4928 passwd --user admin            # 随机生成新密码
   sudo hp4928 passwd --user admin --generate
   sudo hp4928 passwd --user admin --force-change   # 要求下次登录后必须改
   ```
   没有该包装脚本时用完整路径（注意必须 `cd` 到程序目录，`-m` 只从当前目录找包）：
   ```bash
   cd /opt/hp4928-print
   sudo -u hp4928 env PYTHONPATH=/opt/hp4928-print HP4928_DATA_DIR=/var/lib/hp4928-print \
        .venv/bin/python -m hp4928.cli passwd --user admin \
        --config /etc/hp4928-print/config.yaml
   ```
3. **接口**：`POST /api/auth/password`（需登录 + CSRF）

改完密码后建议删掉凭据文件：

```bash
sudo rm /var/lib/hp4928-print/initial-admin.txt
```

添加更多用户：

```bash
sudo -u hp4928 /opt/hp4928-print/.venv/bin/python -m hp4928.cli users add \
     --username zhangsan --role user --config /etc/hp4928-print/config.yaml
```

或在网页「设置 → 账号管理」里添加（会自动生成初始密码，只显示一次）。

> 忘记密码只能重置（不存在找回）：用上面的 `passwd` 命令，或删掉数据库重建（会丢历史）。

---

## 4. 项目结构

```
hp4928-print/
├── hp4928/                  # Python 包
│   ├── __main__.py          # python -m hp4928
│   ├── cli.py               # 命令行：serve/init/passwd/doctor/options/printers/self-test
│   ├── web.py               # Flask 应用工厂 + 页面路由
│   ├── api.py               # JSON API（上传、任务、设置、账号）
│   ├── auth.py              # 账号、密码哈希、会话、CSRF、登录限速
│   ├── jobs.py              # 任务状态机 + 手动双面两遍 + 后台工作线程
│   ├── printers.py          # USB/CUPS 探测、后端选择、环境自检
│   ├── cups.py              # lp/lpstat/cancel 封装 + 虚拟打印机
│   ├── ppd.py               # PPD/lpoptions 解析、角色映射、lp 参数生成
│   ├── settings.py          # 打印设置校验（唯一入口）
│   ├── docs.py              # Word/PDF/图片 → 可打印 PDF、页面拆分、照片排版
│   ├── media.py             # 纸张规格与 HP 4928 支持范围
│   ├── config.py            # 分层配置加载
│   ├── default_config.py    # 默认配置（唯一事实来源）
│   ├── db.py                # SQLite（WAL、按线程连接）
│   ├── store.py             # 运行期状态容器
│   ├── proc.py              # 子进程封装（固定 locale、超时）
│   ├── templates/           # Jinja2 模板
│   └── static/              # app.css / app.js（无构建步骤）
├── tests/                   # pytest：86 个用例
├── scripts/
│   ├── install.sh           # Debian/Ubuntu 一键安装
│   ├── bootstrap-venv.sh    # 建 .venv 装依赖
│   └── setup-usb-printer.sh # 创建 USB 队列 + 写入官方默认设置
├── deploy/
│   └── hp4928-print.service # systemd 单元（含安全加固）
├── ui-prototype/            # NiceGUI 单文件 UI 原型（新界面设计稿，见其 README）
├── config.example.yaml      # 由默认配置导出
├── requirements.txt
├── pytest.ini
├── run.py                   # python run.py 等价入口
└── README.md
```

运行期目录（默认在项目下的 `data/`，服务安装后在 `/var/lib/hp4928-print/`）：

```
data/
├── hp4928.sqlite3      # 账号、任务、审计
├── secret_key          # 会话签名密钥（0600，自动生成）
├── initial-admin.txt   # 首次生成的账号（登录后请删）
├── uploads/            # 上传原件
├── prepared/<任务ID>/  # 转换结果：pass1.pdf / pass2.pdf
├── spool/              # 虚拟打印机的输出 PDF
└── logs/
```

---

## 5. 手动安装（含虚拟环境）

适合想自己控制路径/用户/端口的场景（以 Debian 12、`/opt/hp4928-print` 为例）。

```bash
# 1) 系统依赖
sudo apt update
sudo apt install -y python3 python3-venv python3-pip \
     cups cups-client cups-filters hplip \
     poppler-utils fonts-noto-cjk libreoffice-writer

# 2) 放置代码
sudo mkdir -p /opt/hp4928-print
sudo rsync -a --exclude .venv --exclude data ./ /opt/hp4928-print/

# 3) 虚拟环境
cd /opt/hp4928-print
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# 4) 初始化（生成数据库与默认账号）
.venv/bin/python -m hp4928.cli init

# 5) 环境自检
.venv/bin/python -m hp4928.cli doctor

# 6) 前台试跑
.venv/bin/python -m hp4928.cli serve
```

确认无误后交给 systemd：

```bash
sudo useradd --system --home /var/lib/hp4928-print --shell /usr/sbin/nologin hp4928
sudo mkdir -p /etc/hp4928-print /var/lib/hp4928-print
sudo chown -R hp4928:hp4928 /var/lib/hp4928-print
sudo cp deploy/hp4928-print.service /etc/systemd/system/
sudo cp config.example.yaml /etc/hp4928-print/config.yaml   # 按需修改
sudo systemctl daemon-reload
sudo systemctl enable --now hp4928-print
sudo systemctl status hp4928-print --no-pager
```

### 5.1 开机自启与启动顺序

`install.sh` 会执行 `systemctl enable`，所以**默认开机自启**。判断依据是 `enabled`，不是 `active`：

```bash
systemctl is-enabled hp4928-print     # enabled = 会自启；active 只代表“现在在跑”
systemctl is-enabled cups             # CUPS 同样要 enabled（apt 安装通常已启用）
systemctl show -p WantedBy hp4928-print
ls -l /etc/systemd/system/multi-user.target.wants/hp4928-print.service
```

单元里的启动关系：

```ini
After=network-online.target cups.service    # 等网络与 CUPS 就绪
Wants=network-online.target cups.service    # 同时把 CUPS 拉起来（软依赖）
Restart=on-failure
```

`Wants`（而非 `Requires`）是刻意的：**CUPS 起不来时本服务仍然可用**（降级为虚拟打印机、界面明确提示），
不会把整个服务拖死。

开机时打印机没通电/没插 USB 也没关系：

- 服务照常启动，`/api/status` 会显示「未检测到 USB 打印机 → 虚拟打印机」
- 打印机通电后**不需要重启服务**：每次任务提交时都会重新探测后端，下一次打印自动走真机
- 想立刻让界面状态从"虚拟"变回"真机"，点状态条右侧的「刷新」即可

真正重启验证一次：

```bash
sudo systemctl reboot
# 回来后：
systemctl is-active hp4928-print && curl -s http://127.0.0.1:10086/api/healthz; echo
```

数据不丢：账号/任务在 `/var/lib/hp4928-print/`，CUPS 队列在 `/etc/cups/printers.conf`，
配置在 `/etc/hp4928-print/config.yaml`，都是持久化的。

不想自启：

```bash
sudo systemctl disable --now hp4928-print
```

依赖说明：

| 包 | 用途 | 缺了会怎样 |
| --- | --- | --- |
| `cups` / `cups-client` | 真正把任务送到 USB/网络打印机 | 只能虚拟打印 |
| `cups-filters` | PDF → 打印机语言的过滤链 | 队列报 `document-format-not-supported` |
| `cups-ipp-utils` | `ippfind`，用于扫描局域网打印机（可选） | 只能看到 CUPS 已知的网络设备 |
| `hplip` | HP DeskJet 4900/4920 系列 **hpcups** PPD | 建不了队列 |
| `libreoffice-writer` | Word → PDF | 只能打 PDF/图片 |
| `fonts-noto-cjk` | 中文不乱码 | Word/中文 PDF 出方框 |
| `poppler-utils` | `pdfinfo`/`pdftoppm`（预览、PDF 转照片） | 预览不可用，PDF 当照片打不可用 |

---

## 6. 配置 USB 打印机

HP 4928 是 **USB 2.0 设备 + Wi-Fi** 机型，没有网口。Linux 下走 CUPS：

### 6.1 自动配置

```bash
sudo ./scripts/setup-usb-printer.sh
```

脚本会：

1. 用 `lpinfo -v` 找 `usb://HP/Deskjet...` 设备 URI
2. **从 URI 里解析型号系列号**（例如 `usb://HP/DeskJet%204900%20series` → `4900`），
   再匹配 `lpinfo -m` 里的 `hp-deskjet_4900_series.ppd`
3. `lpadmin -p HP4928 -E -v <URI> -m <PPD>` 建队列
4. 写入默认设置：`PageSize=A4`、`MediaType=Plain`、`InputSlot=Auto`、
   `Resolution=600dpi`、`ColorMode=Gray`、`Duplex=None`
   —— **只设置驱动确实拥有的选项**，不会写无效键
5. 打印 `lpstat -p -l` 和 `lpoptions -p HP4928 -l` 供核对

只查看不修改：`sudo ./scripts/setup-usb-printer.sh --show`

> **型号命名坑**：HP 4928 在 HPLIP 里属于 **DeskJet 4900 series**，打印机自报的
> USB 产品名也是 `DeskJet 4900 series`，所以 PPD 是 `hp-deskjet_4900_series.ppd`，
> 不是 `4920_series`。如果 `lpinfo -m | grep -i deskjet` 里看到的是 4900，
> 用 `--model` 直接指定即可：
> ```bash
> sudo ./scripts/setup-usb-printer.sh \
>      --model 'drv:///hpcups.drv/hp-deskjet_4900_series.ppd'
> ```

### 6.2 手动等价命令

```bash
lpinfo -v                                   # 找 usb://HP/Deskjet%204920%20series?serial=...
lpinfo -m | grep -i deskjet_492              # 找 PPD 名字
sudo lpadmin -p HP4928 -E \
     -v "usb://HP/Deskjet%204920%20series?serial=CN12345678" \
     -m "drv:///hpcups.drv/hp-deskjet_4920_series.ppd" \
     -D "HP DeskJet Ink Advantage 4928" -L "USB"
sudo cupsenable HP4928 && sudo cupsaccept HP4928
lp -d HP4928 /usr/share/cups/data/testprint  # 打一张测试页
```

### 6.3 验证清单

```bash
lsusb | grep -i 03f0              # 内核看到 HP 设备（03f0 = HP 厂商号）
lpinfo -v | grep -i usb           # CUPS 看到 USB 打印机
lpstat -p -d                      # 队列存在且不是 disabled
lpoptions -p HP4928 -l            # 驱动选项（就是这个服务界面里的选项）
```

四项都对之后，服务端 `python -m hp4928.cli doctor` 应显示 ✅。

### 6.4 用打印机自带 Wi-Fi 也行

如果 USB 实在不稳定，让打印机连上 Wi-Fi（4928 只支持 2.4 GHz），然后在
**设置 → 打印机与连接** 里扫描或手动添加（见 [6.5](#65-添加局域网打印机wifi--网线)）。
手动方式等价于：

```bash
lpinfo -v | grep -i ipp           # 或 dnssd://HP%20DeskJet...
sudo lpadmin -p HP4928-NET -E -m everywhere -v "ipp://192.168.1.50/ipp/print"
```

### 6.5 添加局域网打印机（Wi-Fi / 网线）

**先分清两个方向**，这是很多人的困惑点：

| 方向 | 含义 | 本服务怎么支持 |
| --- | --- | --- |
| 局域网设备 → 本服务 | 手机/同事电脑打开网页上传文件，由这台服务器送去打印 | 服务监听 `0.0.0.0:10086`，天生支持 |
| 本服务 → 局域网里的打印机 | CUPS 队列指向打印机的 IP（`ipp://`），不接 USB 也能打 | 设置页「打印机与连接」里添加/切换 |

4 种操作方式，任选：

**A. 网页（推荐）**：设置 → 打印机与连接
1. 表里列出本机所有 CUPS 队列，`USB` / `网络` 徽标一眼区分，点「使用」即切换
2. 点「🔍 扫描局域网打印机」→ 列出 mDNS/lpinfo 发现的地址 → 填个名字点「添加」
3. 或展开「手动添加网络打印机」，填地址 + 队列名（+ 可选驱动）
4. 点「打测试页」验证真的通

**B. 命令行**

```bash
hp4928 printers list                    # 队列 + 当前使用哪个
hp4928 printers discover                # 扫描局域网（mDNS + lpinfo）
hp4928 printers add --name HP4928-WIFI --uri ipp://192.168.1.50/ipp/print
hp4928 printers test HP4928-WIFI        # 打一张测试页
hp4928 printers use HP4928-WIFI         # 切换（立刻生效，重启保留）
hp4928 printers use                     # 不带名字 = 恢复配置里的 printer.name
hp4928 printers delete HP4928-WIFI      # 删除队列（当前使用中的不能删）
```

**C. 只改配置**：把 `/etc/hp4928-print/config.yaml` 的 `printer.name` 改成目标队列名，
`systemctl restart hp4928-print`。界面上选过的队列优先于这个配置项。

**D. 纯 CUPS**：

```bash
sudo lpadmin -p HP4928-NET -E -v ipp://192.168.1.50/ipp/print -m everywhere
sudo cupsenable HP4928-NET && sudo cupsaccept HP4928-NET
lp -d HP4928-NET /usr/share/cups/data/testprint
```

要点：

- **地址**一般是 `ipp://打印机IP/ipp/print`。4928 支持 AirPrint（URF），
  所以 `-m everywhere` 通常直接可用；`ippfind`/`lpinfo -v` 还能给出 `dnssd://...` 形式。
- **驱动**：`everywhere` 需要 CUPS ≥ 2.4（Debian 12 满足，Debian 11 是 2.3 → 要指定 PPD）。
  老机型或 `socket://`（JetDirect 9100 原始队列）必须给 PPD，
  例如 `--model drv:///hpcups.drv/hp-deskjet_4900_series.ppd`（界面里对应「驱动」输入框）。
- **权限**：增删队列要 `lpadmin` 组。服务账号默认已加入；若界面提示无权限：
  ```bash
  sudo usermod -aG lpadmin hp4928 && sudo systemctl restart hp4928-print
  ```
- **切换是运行时的**：选择存在 SQLite（`kv` 表），立即生效、重启保留，不用改配置。
  打印任务每次提交时按「界面选中 → `printer.name` → USB 优先 → 网络回退」的顺序挑队列。
- **USB 与网络可共存**：两条队列都在表里，随时切换；USB 掉线时若已有网络队列，
  自动回退到网络队列（配置 `printer.usb.allow_network_fallback: true`）。

---

## 7. 局域网访问

> 这一节讲的是「**别的设备访问这台服务**」。如果你要的是「**这台服务去连局域网里的打印机**」，
> 看 [6.5](#65-添加局域网打印机wifi--网线)。

服务默认监听 `0.0.0.0:10086`，同一网段的手机/电脑直接访问：

```
http://192.168.1.10:10086/
```

- **防火墙**：`sudo ufw allow 10086/tcp`（`install.sh` 检测到 ufw 会自动放行）
- **只想本机用**：`server.host: 127.0.0.1`
- **只允许某些网段**（推荐办公环境）：
  ```yaml
  server:
    allowed_cidrs: ["192.168.1.0/24", "10.8.0.0/16"]
  ```
  不在范围内的请求直接 403，并记入审计日志。
- **手机使用**：界面按手机宽度做了适配，上传、选设置、点「继续打印」都能在手机上完成。
  正在等待翻面的任务会弹窗提醒，即使关掉页面，任务也会一直停在「等待翻面」。

---

## 8. 使用手册

### 8.1 打印文档（默认黑白 A4 单面）

1. 首页拖入或点击选择文件（PDF / DOC / DOCX / 常见图片）
2. 保持「文档打印」模式
3. 需要时展开「纸张 / 质量」「版式 / 双面」调整
4. 点「开始打印」

默认值：**A4、黑白、单面、标准质量（600 dpi）、1 份**。

### 8.2 手动双面（重点）

HP 4928 **没有自动双面器**，HP 官方规格写的是 *Manual（driver support provided）*。
所以本服务采用「两遍打印 + 人工翻面」：

```
第一遍：只打奇数页 1,3,5…   →  服务暂停，任务状态「等待翻面」
        ↓  你从出纸口取走整叠纸，翻面，放回进纸盒
        ↓  页面弹出提示，点「✅ 我已翻面，继续打印」
第二遍：打偶数页（默认逆序 …6,4,2）
```

细节：

- **不点按钮，第二面永远不会打印** —— 服务在 `awaiting_flip` 状态停住，
  没有定时器、没有重试、没有"自动继续"。
- **翻转方向**：
  - 长边翻转（像翻书）：双面 → 长边绑定，适合纵向文档
  - 短边翻转（像翻日历）：双面 → 短边绑定，适合横向/表格
- **第二遍逆序**：默认开启（`reverse_second_pass: true`）。手动双面的标准做法，
  第二遍从最后一页开始打，翻面后页码顺序才对；不需要可在「高级」里关掉。
- **页码范围**：先按所选范围重新编号，再分奇偶。例：原文档 9 页，选 `2-5` →
  第一遍打第 2、4 页，第二遍打第 5、3 页。
- **单页文档**：只有 1 页时直接完成，提示"无需第二面"。
- **超时保护**：停在「等待翻面」超过 60 分钟（`print.manual_duplex_timeout_minutes`）
  会自动取消，避免纸张状态混乱；设置为 0 表示永不超时。
- **重复点击**：第二次点击「继续打印」返回 409，不会重复出纸（数据库原子状态跃迁）。
- **与每张多页互斥**：手动双面 + 每张 2/4/6/9/16 页会被拒绝，界面会提示。

> 打印时服务向 CUPS 发送的是 `sides=one-sided`，**不会**发送 `two-sided`。
> 因为该机型没有自动双面单元，发 `two-sided` 只会导致第二面被打印机丢弃或空白。

### 8.3 打印 6 寸照片

1. 上传图片（或含图片的 PDF），界面会自动切到「6 寸照片」模式
2. 或手动点「6 寸照片」模式 / 快捷预设「6 寸照片 4R」
3. 参数：
   - **相纸尺寸**：默认 `4x6in`（4R，102 × 152 mm）；也可选 10×15cm、5×7、8×10
   - **照片怎么放**：完整显示（留白）/ 铺满裁切（不变形）/ 原始尺寸（1:1）
   - **输出分辨率**：300 / 600 / 1200 dpi
   - **安全边距**：默认 3 mm
   - **自动旋转**：横竖自动匹配纸向
4. 照片模式会自动使用**照片纸 + 最佳质量**（对应 HP 官方照片预设）

关于「无边距」：**HP 4928 官方规格明确标注 Borderless printing: No**，
所以界面里的「无边距」默认禁用并说明原因；照片会保留约 3 mm 白边。
如果你换了支持无边距的机型（或驱动提供了 `Borderless` 选项），该开关会自动变为可用。

纸张放在**主进纸盒**即可，一次放一张 6 寸相纸最稳（该机型只有 60 页主纸盒，无独立照片纸盒）。

### 8.4 打印 Word

`.docx` / `.doc` 上传后由 LibreOffice 无界面转 PDF，再用 PDF 流程打印，**版式保持不变**。

- 需要 `libreoffice-writer`（`install.sh` 默认安装）
- 中文文档需要中文字体（默认装 `fonts-noto-cjk`），否则可能方框
- 转换超时默认 180 秒（`conversion.timeout`），复杂文档可调大
- 每个任务用**独立的 LibreOffice profile**（`data/soffice-profile/<任务ID>`），
  并发转换不会互锁

### 8.5 任务列表

每张任务卡片显示：状态徽标、纸张/色彩/双面摘要、页数、提示信息、可下载的 PDF、
以及**服务实际执行的 `lp` 命令**（点「投递命令」展开）——方便核对到底传了什么参数。

状态含义：

| 状态 | 含义 |
| --- | --- |
| 排队中 | 已提交，等工作线程处理 |
| 正在准备文件 | 正在转换 Word / 拆分页面 / 排版照片 |
| 正在打印第一面 | 已投递 CUPS |
| **等待翻面** | **必须点「继续打印」** |
| 正在打印第二面 | 第二遍已投递 |
| 已完成 | 投递完毕 |
| 失败 | 见任务上的红色错误信息 |
| 已取消 | 人工取消或超时 |

---

## 9. 高级设置与官方驱动对照

### 9.1 界面选项是**从驱动读出来的**，不是写死的

服务启动后按以下顺序获取能力清单：

1. `lpoptions -p <队列> -l` —— CUPS 运行时的驱动选项（最准确，含当前默认值）
2. `/etc/cups/ppd/<队列>.ppd` —— 直接解析 PPD（`*OpenUI` / `*Default*` / 各取值）
3. 内置**官方能力画像** —— 没装 CUPS 时兜底（界面会标注"内置能力画像"）

所以：**驱动里有什么选项、取值叫什么，界面里就是什么**。
「设置 → 打印驱动」页会显示来源（`lpoptions` / `ppd` / `builtin`）、PPD 路径、
选项总数，以及语义映射（哪个界面项绑定到哪个驱动选项）。

界面同时提供「驱动原生选项」折叠区：**逐项列出驱动里全部选项及全部取值**，
可直接指定，原样透传给 `lp -o`。这就是"完全匹配官方设置"的直通路径。

### 9.2 语义映射表

| 界面项 | 发送给 CUPS | 选项从哪来 | HP 4928 官方能力 |
| --- | --- | --- | --- |
| 纸张尺寸 | `PageSize=A4`（驱动有该选项时）；否则 `media=A4` | 驱动 `PageSize` | A4 / B5 / A6 / DL；自定义 76×127–215×355 mm |
| 纸张类型 | `MediaType=Plain｜Photo｜Brochure｜...` | 驱动 `MediaType` | 普通纸、相纸、宣传册纸、信封、特殊喷墨纸 |
| 纸张来源 | `InputSlot=Auto｜Main` | 驱动 `InputSlot` | 单一 60 页主纸盒 |
| 打印质量 | `Resolution=300dpi｜600dpi｜1200dpi` | 驱动 `Resolution` | 黑白最高 1200×1200 dpi；彩色最高 4800×1200 优化 |
| 色彩 | `ColorMode=Gray｜Color` | 驱动 `ColorMode` | 2 墨盒（黑色 + 三色） |
| 双面 | **应用层两遍** + `sides=one-sided` | 不交给驱动 | **手动双面**（无自动双面器） |
| 每张页数 | `number-up=N` | CUPS 标准 | 由 CUPS 排版后栅格化 |
| 缩放方式 | `print-scaling=fit｜fill｜none` | CUPS 标准（2.2+） | — |
| 方向 | `orientation-requested=3｜4` | CUPS 标准 | — |
| 出纸顺序 | `outputorder=normal｜reverse` | CUPS 标准 | — |
| 页码范围 | `page-ranges=1-3,5` | CUPS 标准 | — |
| 份数 | `lp -n N` | CUPS 标准 | — |
| 无边距 | `Borderless=true`（仅当驱动提供） | 驱动 `Borderless` | **不支持**（官方规格） |
| 照片 DPI / 留白 / 裁剪 | 排版阶段本地栅格化后投递 | 应用层 | 4×6 相纸按自定义尺寸 |

找不到对应驱动选项时会退回 **IPP 标准属性**（`media=`、`media-type=photographic`、
`print-quality=3/4/5`、`print-color-mode=monochrome`），并在任务里给出提醒，
不会静默改纸型。

### 9.3 快捷预设（对应官方"快捷设置"）

| 预设 | 等效设置 |
| --- | --- |
| 黑白 A4 单面（默认） | `PageSize=A4 MediaType=Plain ColorMode=Gray Resolution=600dpi sides=one-sided` |
| 黑白双面（手动） | 同上 + 应用层两遍打印 |
| 彩色文档 | `ColorMode=Color Resolution=1200dpi` |
| 省墨草稿 | `Resolution=300dpi ColorMode=Gray` |
| 6 寸照片 4R | `PageSize=4x6in MediaType=Photo Resolution=1200dpi` + 300dpi 排版 |
| 每张 2 页 | `number-up=2` |

### 9.4 自己核对实际参数

任务卡片里「投递命令」显示的就是真实执行的命令，形如：

```bash
lp -d HP4928 -t "报告.pdf" -o PageSize=A4 -o MediaType=Plain -o InputSlot=Auto \
   -o Resolution=600dpi -o ColorMode=Gray -o sides=one-sided -- /path/pass1.pdf
```

也可以手工复现：

```bash
lp -d HP4928 -o PageSize=A4 -o ColorMode=Gray -o Resolution=600dpi \
   -o sides=one-sided 报告.pdf
lpoptions -p HP4928 -l          # 看驱动全部选项
python -m hp4928.cli options    # 看语义映射结果
```

---

## 10. 降级与虚拟打印机

后端选择逻辑（`printer.backend`）：

| 配置 | 行为 |
| --- | --- |
| `auto`（默认） | 能用 CUPS 就用；USB 掉线/无队列/无 CUPS → 虚拟打印机 |
| `cups` | 强制真机；任一条件不满足就**报错**（适合"绝不接受静默降级"的场景） |
| `virtual` | 强制虚拟（调试、演示、无打印机） |

`auto` 模式下的判定顺序：

1. 有 `lp` / `lpstat`？没有 → 虚拟
2. CUPS 服务在跑？没跑 → 虚拟
3. 有队列？没有 → 虚拟
4. 队列绑的是 `usb://` 且 `printer.usb.require_device: true`：
   检测不到 HP USB 设备 → 虚拟（并说明"USB 设备不在线"）
5. 否则 → 真机

虚拟打印机的输出：

- 目录：`<数据目录>/spool/`（默认 `/var/lib/hp4928-print/spool/`）
- 文件名：`时间-标题-随机.pdf`，旁边有同名 `.json` 记录设置与来源
- 界面上任务带 **「虚拟」** 徽标，状态条也会说明原因
- 「设置 → 虚拟打印输出」可直接下载，或 `GET /api/spool`

> 这是"USB 行不通"时的保底：**任务不会丢失**，可以下载 PDF 后用别的方式打印。

---

## 11. 命令行参考

```bash
python -m hp4928.cli [全局参数] <命令> [命令参数]

全局参数（可写在子命令前后）：
  --config PATH        配置文件路径
  --data-dir PATH      数据目录
  --backend {auto,cups,virtual}
  --json               部分命令输出 JSON
```

| 命令 | 说明 |
| --- | --- |
| `serve [--host H] [--port P] [--dev]` | 启动服务（默认读配置的 `0.0.0.0:10086`） |
| `init` | 建目录/库、生成默认账号（幂等） |
| `passwd --user admin [--password P] [--generate] [--force-change]` | 重置密码 |
| `users list｜add｜delete --username X [--role admin]` | 账号管理 |
| `doctor [--json]` | 环境体检（CUPS/USB/驱动/字体/依赖/端口/磁盘） |
| `status [--json]` | 打印机与队列状态 |
| `printers list｜use｜add｜delete｜test｜discover` | 队列切换、添加局域网打印机、打测试页 |
| `options [--json]` | 打印驱动选项 + 语义映射 |
| `config-dump [--output FILE]` | 导出示例配置 |
| `self-test [--dry-run] [--pages N] [--media A4]` | 生成并投递测试页（验证整条链路） |
| `cleanup` | 清理过期任务文件 |

示例：

```bash
# 装了 install.sh 之后，任意目录直接用
hp4928 doctor
hp4928 printers list
hp4928 printers add --name HP4928-WIFI --uri ipp://192.168.1.50/ipp/print
hp4928 printers test HP4928-WIFI
hp4928 self-test

# 没有 /usr/local/bin/hp4928 时用完整路径（必须在 /opt/hp4928-print 下，-m 只从当前目录找包）
cd /opt/hp4928-print
sudo -u hp4928 env PYTHONPATH=/opt/hp4928-print HP4928_DATA_DIR=/var/lib/hp4928-print \
     .venv/bin/python -m hp4928.cli doctor --config /etc/hp4928-print/config.yaml
```

---

## 12. 配置参考

配置按优先级合并：**内置默认值 → `/etc/hp4928-print/config.yaml` → `./config.yaml`
→ `./config.local.yaml` → 环境变量**。完整示例见 `config.example.yaml`。

关键项：

```yaml
server:
  host: 0.0.0.0            # 只本机用改 127.0.0.1
  port: 10086
  threads: 8               # waitress 工作线程
  session_hours: 12        # 登录有效期
  max_upload_mb: 64        # 上传上限（同时作为 Flask MAX_CONTENT_LENGTH）
  trust_proxy: false       # 放在 Nginx 后面才 true，否则 X-Forwarded-For 可伪造
  behind_https: false      # true 时会话 Cookie 加 Secure
  allowed_cidrs: []        # 例：["192.168.1.0/24"]

printer:
  name: HP4928             # CUPS 队列名
  backend: auto            # auto | cups | virtual
  usb:
    require_device: true   # USB 不在线就降级，而不是把任务堆在 CUPS 里
    allow_network_fallback: true
  cups:
    timeout: 60
  virtual:
    dir: ""                # 空=数据目录/spool

print:
  media: A4
  color: mono              # mono | color | gray
  duplex: "off"            # off | long-edge | short-edge（手动双面）
  quality: normal          # draft | normal | best | photo
  copies: 1
  pages_per_sheet: 1
  scale: fit               # fit | fill | none
  orientation: auto
  reverse_second_pass: true
  manual_duplex_timeout_minutes: 60   # 0=永不超时
  photo:
    media: "4x6in"
    dpi: 300
    border_mm: 3.0
    fit: fit
    auto_rotate: true

conversion:
  soffice: ""              # 空=自动探测
  timeout: 180

security:
  default_admin_user: admin
  force_password_change: false
  login_max_attempts: 5
  login_window_seconds: 300
  min_password_length: 8

retention:
  days: 14                 # 任务文件保留天数
  keep_failed_days: 30

logging:
  level: INFO
  file: ""                 # 空=stdout（journalctl 看）
```

环境变量覆盖（systemd 的 `EnvironmentFile` 也支持）：

```
HP4928_CONFIG / HP4928_DATA_DIR / HP4928_HOST / HP4928_PORT
HP4928_BACKEND / HP4928_PRINTER / HP4928_LOG_LEVEL
HP4928_SESSION_HOURS / HP4928_TRUST_PROXY / HP4928_BEHIND_HTTPS
HP4928_ALLOWED_CIDRS="192.168.1.0/24,10.0.0.0/8"
HP4928_MAX_UPLOAD_MB / HP4928_FORCE_PASSWORD_CHANGE / HP4928_SECRET_KEY
```

改完配置：`sudo systemctl restart hp4928-print`。

---

## 13. 安全建议

- **务必修改默认密码**，并删除 `/var/lib/hp4928-print/initial-admin.txt`
- 密码用 **scrypt** 哈希存储；会话 Cookie 是 `HttpOnly` + `SameSite=Lax` 的签名 Cookie
- 所有写操作（含「继续打印」）都校验 **CSRF token**；登录接口除外（方便脚本调用）
- 登录失败 **5 次 / 5 分钟** 后限速（按 IP + 用户名），返回 `429` 与 `Retry-After`
- 可用 `server.allowed_cidrs` 把访问限制在办公网段
- 服务默认 **HTTP**。要跨公网请务必放 Nginx/Caddy 后面加 HTTPS，例如：

  ```nginx
  server {
      listen 443 ssl http2;
      server_name print.example.lan;
      ssl_certificate     /etc/ssl/fullchain.pem;
      ssl_certificate_key /etc/ssl/privkey.pem;
      client_max_body_size 64m;
      location / {
          proxy_pass http://127.0.0.1:10086;
          proxy_set_header Host $host;
          proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
          proxy_set_header X-Forwarded-Proto $scheme;
      }
  }
  ```

  同时设置 `server.trust_proxy: true` 与 `HP4928_BEHIND_HTTPS=true`，
  并把 `server.host` 改回 `127.0.0.1`。
- systemd 单元已启用 `NoNewPrivileges`、`ProtectSystem=strict`、`PrivateTmp`、
  `ReadWritePaths=/var/lib/hp4928-print`，服务只有数据目录可写
- 审计日志表 `audit` 记录登录、上传、提交、继续打印、取消、改密等操作，
  管理员可用 `GET /api/audit` 查看

---

## 14. 故障排查

### 14.1 先跑自检

```bash
sudo -u hp4928 /opt/hp4928-print/.venv/bin/python -m hp4928.cli doctor \
     --config /etc/hp4928-print/config.yaml
```

网页「设置 → 运行环境自检」是同一套检查。逐项看 `❌` 和「处理建议」。

### 14.2 USB 相关

| 现象 | 检查 | 处理 |
| --- | --- | --- |
| `lsusb` 看不到 HP 设备 | 换 USB 线/口，`dmesg \| tail -20` | 线材或供电问题；避免 USB Hub |
| `lsusb` 有但 `lpinfo -v` 没 `usb://` | `systemctl status cups` | 重启 CUPS；确认没被 `usblp` 抢占 |
| `lpinfo -m` 没有匹配的 PPD | `lpinfo -m \| grep -i deskjet` | 4928 属于 **4900 series**；见 [6.1](#61-自动配置) |
| 队列是 `disabled` | `lpstat -p` | 看打印机面板是否缺纸/卡纸/盖子没关；`sudo cupsenable HP4928` |
| 任务提交成功但不出纸 | `lpstat -o` 看队列 | 检查墨盒、纸张、打印机是否离线 |
| 打印乱码/空白 | `lpoptions -p HP4928 -l` | 驱动不匹配，重新 `lpadmin -m` 指定 PPD |

USB 不可用时服务会自动降级为虚拟打印机（界面有提示），可先用它验证上传/转换/双面逻辑。

### 14.3 Word 转换失败

```
Word 转 PDF 失败：找不到 soffice / LibreOffice 未安装
```

```bash
sudo apt install -y libreoffice-writer fonts-noto-cjk
python -m hp4928.cli doctor          # 确认 LibreOffice 与中文字体两项通过
```

转换卡住/超时：`pkill -f soffice` 后重试；残留 profile 可删
`rm -rf /var/lib/hp4928-print/soffice-profile/*`。
复杂文档把 `conversion.timeout` 调到 300 秒。

中文变方框：`fc-list :lang=zh` 应为空则装字体。
也可以在服务器上用 `fc-cache -fv` 重建缓存后重启服务。

### 14.4 端口与启动

```bash
ss -lntp | grep 10086            # 谁占了 10086
journalctl -u hp4928-print -n 100 --no-pager
systemctl restart hp4928-print
```

端口被占用时：`sudo ./scripts/install.sh --port 8080`，或改配置 `server.port`，
或用环境变量 `HP4928_PORT=8080` 临时覆盖。

**两个装完之后最常见的启动失败**（都已在新版脚本里修掉，旧版本按下面手动处理）：

1. **`ModuleNotFoundError: No module named 'hp4928'`**
   `python -m hp4928.cli` 只从**当前目录**找包。必须在 `/opt/hp4928-print` 下执行，
   或带上 `PYTHONPATH`：
   ```bash
   cd /opt/hp4928-print
   sudo -u hp4928 env PYTHONPATH=/opt/hp4928-print HP4928_DATA_DIR=/var/lib/hp4928-print \
        /opt/hp4928-print/.venv/bin/python -m hp4928.cli init \
        --config /etc/hp4928-print/config.yaml
   ```
   新版 install.sh 会安装 `/usr/local/bin/hp4928` 包装脚本，之后任意目录直接
   `hp4928 doctor` / `hp4928 passwd --user admin` 即可。

2. **服务起不来，journal 里是 `Read-only file system: '/opt/hp4928-print/data'`**
   配置没写 `paths.data_dir` 时默认落在程序目录，而 systemd 单元用了
   `ProtectSystem=strict`（整机只读，只放开 `ReadWritePaths`）。修法二选一：
   ```yaml
   # /etc/hp4928-print/config.yaml
   paths:
     data_dir: /var/lib/hp4928-print
   ```
   或在单元里加环境变量后 `systemctl daemon-reload`（新版单元已内置）：
   ```bash
   sudo sed -i '/^Environment=PYTHONUNBUFFERED=1/a Environment=HP4928_DATA_DIR=/var/lib/hp4928-print' \
        /etc/systemd/system/hp4928-print.service
   sudo systemctl daemon-reload
   sudo systemctl restart hp4928-print
   ```
   启动日志里现在会直接给出这条提示，不再只有一行 traceback。

### 14.5 手动双面相关

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 一直停在「等待翻面」 | 没人点按钮（设计如此） | 去界面点「继续打印」，或取消任务 |
| 点了没反应 | 页面太久没刷新，任务已超时取消 | 刷新页面看状态；重新提交 |
| 第二面字是倒的 | 翻面方向选错 | 长边/短边换一个再试；或关掉「第二遍逆序」 |
| 第二面页码顺序乱 | 打印机出纸顺序与假设不同 | 把「出纸顺序」改成逆序试试 |
| 提示"不能和每张多页同时使用" | 设计限制 | 双面时把「每张页数」改回 1 |

### 14.6 上传/预览

- 超过 `server.max_upload_mb` → 413，改配置或压缩文件
- 预览空白：需要 `poppler-utils`（`pdftoppm`），装完重启服务
- `.txt`/`.exe` 之类会被拒绝（415），只接受 PDF / Word / 图片

---

## 15. 升级与卸载

### 15.1 用 git 部署与更新（推荐）

仓库里已经放了 `.gitignore`（排除 `data/`、虚拟环境、缓存）和 `.gitattributes`
（强制 `.sh/.py/.md/...` 用 LF，避免 Windows 检出后 bash 报 `$'\r': command not found`），
`scripts/*.sh` 的可执行位也已记进索引。所以克隆下来就能直接跑。

**方式 A：把服务器当 git 源（不依赖 GitHub）**

在服务器上建裸仓库：

```bash
ssh user@192.168.5.20
sudo apt install -y git                     # 没装的话
mkdir -p ~/repos && cd ~/repos
git init --bare hp4928-print.git
```

本地（Windows）加远程并推送：

```bash
cd hp4928-print
git remote add origin user@192.168.5.20:repos/hp4928-print.git
git push -u origin main
```

服务器上克隆并安装：

```bash
cd ~
git clone ~/repos/hp4928-print.git
cd hp4928-print
sudo ./scripts/install.sh                   # 幂等，保留 /etc 配置与 /var/lib 数据
```

以后更新：

```bash
# 本地
git add -A && git commit -m "..." && git push
# 服务器
cd ~/hp4928-print && git pull && sudo ./scripts/install.sh
```

**方式 B：GitHub / Gitee 私有仓库**

```bash
# 本地
git remote add origin git@github.com:<你>/hp4928-print.git
git push -u origin main

# 服务器
sudo apt install -y git
git clone git@github.com:<你>/hp4928-print.git ~/hp4928-print
cd ~/hp4928-print && sudo ./scripts/install.sh
```

**方式 C：完全没有远程（U 盘 / scp 传一个文件）**

```bash
# 本地打包成一个自包含文件
git bundle create hp4928.bundle --all

# 传到服务器后
git clone hp4928.bundle hp4928-print
cd hp4928-print && git remote remove origin && sudo ./scripts/install.sh
```

> 不要用 zip 传源码：zip 会丢掉可执行位，`./scripts/install.sh` 会提示 Permission denied。
> 真遇到了就 `chmod +x scripts/*.sh` 补救。

### 15.2 升级（保留数据）

```bash
cd <新版本目录>
sudo ./scripts/install.sh          # 会覆盖 /opt 下代码、重建依赖、重启服务
# 数据目录 /var/lib/hp4928-print 与配置 /etc/hp4928-print 不受影响
```

### 15.3 卸载

```bash
sudo ./scripts/install.sh --uninstall
```

会停服务、删 `/opt/hp4928-print`、`/etc/hp4928-print`、systemd 单元，
并询问是否删除 `/var/lib/hp4928-print`（打印历史）。CUPS 队列需另行删除：
`sudo lpadmin -x HP4928`。

---

## 16. 已知限制

- **不支持无边距**：HP 4928 官方规格即为 No，照片固定保留安全边距（默认 3 mm）
- **无自动双面**：机型无双面单元，只能手动翻面；双面任务必须人工确认第二遍
- **只有主纸盒**：没有独立照片纸盒 / 手动进纸槽，6 寸相纸直接放主纸盒
- **单进程**：任务状态在 SQLite + 进程内队列，请用 `waitress`（默认）或
  `gunicorn -w 1 --threads N`；不要多进程多 worker
- **不提供扫描/复印**：4928 有扫描功能，但本服务只做打印
- **Word 依赖 LibreOffice**：不装就只能打 PDF/图片；`--no-office` 安装时同理
- **PDF 当照片打** 需要 `poppler-utils`（`pdftoppm` 逐页栅格化）
- **HEIC/HEIF** 不支持（Pillow 默认不带），请先转 JPG
- 打印任务完成后状态即"已完成"，不与打印机耗材/纸尽状态联动

---

## 17. 开发与测试

```bash
# 依赖（开发）
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest

# 单元 + 集成测试（用虚拟打印机，不需要真机）
.venv/bin/python -m pytest -q

# 只跑双面流程
.venv/bin/python -m pytest tests/test_jobs.py -q

# 前台调试（自动重载）
python -m hp4928.cli serve --dev --backend virtual
```

测试覆盖（86 个用例）：

| 文件 | 覆盖点 |
| --- | --- |
| `test_auth.py` | 未登录拦截、错误密码、限速、CSRF、改密、退出 |
| `test_ppd.py` | PPD/lpoptions 解析、角色映射、`lp` 参数生成、原生选项透传、Schema |
| `test_media.py` | 毫米/点/像素换算、别名、自定义尺寸、官方支持范围 |
| `test_docs.py` | 类型嗅探、页码范围、奇偶拆分、逆序、照片页面几何、Word 命令拼装 |
| `test_jobs.py` | **手动双面全流程**、不点按钮不出第二面、重复确认被拒、取消/重试、越权 |
| `test_api.py` | 上传校验、设置冲突、照片默认值、权限、页面渲染 |
| `test_printers.py` | 队列列表/切换持久化、添加网络打印机、lpadmin 失败信息、删除保护、发现与权限 |
| `test_config.py` | 环境变量覆盖、数据目录优先级、不可写目录的可操作报错、密钥持久化 |

接口速查：

```
GET  /api/healthz                  健康检查（免登录）
GET  /api/status                   后端/队列/USB/驱动状态
GET  /api/printers                 CUPS 队列列表 + 当前选中 + 能否管理
GET  /api/printers/discover        扫描局域网打印机（lpinfo + ippfind）
POST /api/printers/select          切换队列 {name}（空=恢复配置）
POST /api/printers/add             添加网络打印机 {name, uri, model?, select?}
DELETE /api/printers/<name>        删除队列（当前使用中的拒绝）
POST /api/printers/test            往指定队列打测试页 {name}
GET  /api/schema[?refresh=1]       高级设置结构（语义控件 + 驱动原生选项）
POST /api/upload                   上传（multipart: file）
POST /api/jobs                     创建任务 {upload_id, mode, settings, title}
GET  /api/jobs?limit=30            任务列表
GET  /api/jobs/<id>                任务详情
POST /api/jobs/<id>/continue       手动双面：确认翻面，打印第二面
POST /api/jobs/<id>/cancel         取消
POST /api/jobs/<id>/retry          重试
GET  /api/jobs/<id>/file/<which>   下载 source / pass1 / pass2
GET  /api/jobs/<id>/preview        首页预览 PNG
GET  /api/spool                    虚拟打印机输出列表
GET  /api/media                    纸张规格表
GET  /api/doctor                   环境自检（管理员）
GET  /api/users                    账号列表（管理员）
```

---

## 附：官方依据

- HP DeskJet Ink Advantage Ultra 4928 产品规格（手动双面、无边距、介质、分辨率）：
  <https://www.hp.com/lk-en/products/printers/product-details/product-specifications/2101619025>
- CUPS 命令行与 `lp` 选项：<https://openprinting.github.io/cups/doc/man-lp.html>
- HPLIP（hpcups 驱动、`hp-setup`）：<https://developers.hp.com/hp-linux-imaging-and-printing>
- IPP 标准属性（`media`、`print-scaling`、`print-quality`）：
  <https://www.pwg.org/ipp/>

关键官方事实（决定了本服务的实现方式）：

| 项目 | HP 官方规格 |
| --- | --- |
| 双面打印 | **Manual（driver support provided）** —— 无自动双面器 |
| 无边距 | **No** |
| 介质尺寸 | A4 / B5 / A6 / DL；自定义 76 × 127 ～ 215 × 355 mm |
| 最高分辨率 | 黑白 1200 × 1200 dpi；彩色 4800 × 1200 优化 dpi |
| 进纸/出纸 | 60 页主纸盒 / 25 页出纸 |
| 连接 | 1 × USB 2.0 + Wi-Fi 802.11b/g/n 2.4 GHz |
| 墨盒 | 2 个（黑色 + 三色） |
