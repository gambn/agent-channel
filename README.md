# agent-channel · 通道代理

**一个给 Agent 用的本地通道：让 Agent 能调用海外主流 AI 模型的 API；浏览器只是附带的开关。**

技术上基于 **sing-box 内核**，在本机 `127.0.0.1:20849` 上开一个代理端口，并可选地把系统代理也指向它。

**本工具不提供、不内置任何节点或订阅**，需要你自己准备好可用的节点链接或订阅地址。

- 单一 sing-box 1.14.3 内核，零本地编译（用官方预编译文件）
- Python 3.10+，**运行时零第三方依赖**（纯标准库）
- 双壳共享内核：CLI（Agent / 脚本用）+ 本地 Web 面板（人用）
- 零权限：只写 HKCU 系统代理，不装驱动、不动路由表
- 三个端口都只监听 `127.0.0.1`：代理 `20849`、面板 `18888`

> **定位**：它是一个**接驳件**。只做四件事——本地环回可用、Agent 能一键调起、装的是官方预编译内核、完整性可验。sing-box 或现成客户端已经做了的事，它不重复做。

---

## 一、它能做什么

**把通道接给你的 Agent，也接给你的浏览器。**

- **给 Agent 的接口**：一条本地 HTTP API 加一个 CLI，Agent 或脚本可以查状态、启停通道、测速、切换出站、导入节点、探测落地位置——全程本地，不需要把节点配置写进对话。
- **给人用的面板**：`http://127.0.0.1:18888`，节点配置、出站选择、测速、内核更新、系统代理开关都在这一个页面上。
- **落地可验**：通道运行时，面板会**走当前通道的真实出口**探测一次地理位置，用「城市 + 天气」间接验证出口 IP 的物理位置（见 §五）。

### 接入方式与覆盖范围

通道在 `127.0.0.1:20849` 开一个混合端口（socks5 + http），可选把系统代理指向它。**不做 TUN / 透明代理，不全局接管流量**，也只监听本机、不供局域网其他设备共享。

所以**不读系统代理的程序**（不少 CLI、Node / Python 工具链、自带网络栈的运行时）需要手动指过来：

```bash
export HTTP_PROXY=http://127.0.0.1:20849
export HTTPS_PROXY=http://127.0.0.1:20849
```

两条边界先说清：HTTP 代理只转 TCP，走 QUIC / HTTP3 的流量不经过本通道；程序被强杀或断电后，下次启动会自动关掉悬挂的系统代理，但**不会还原你原有的代理设置**（原值只在进程内存里，退出即丢）。

> 另一个 PAC 限定：Windows 上若你原本用的是**自动配置脚本（PAC）**，正常「停止通道」一次也会变成手动代理或直接关闭——因为停止时的还原只按「开关 + 地址」恢复，PAC 的自动配置模式不在其中。上面「不会还原」说的是强杀/断电路径；PAC 这条连正常停止都会触发。

## 二、协议支持

内核覆盖的协议栈：

- **有通行 URI 格式的**：`vless`（含 Reality）、`vmess`、`trojan`、`shadowsocks`、`hysteria2`、`tuic`
- **只走 JSON 的**：`naive`、`ssh`、`anytls`、`shadowtls`、`hysteria`(v1) —— 这些**没有通行的 URI 格式**，工具不为它们发明私有格式，一律用 sing-box JSON 表达

节点有三种输入形态，解析层都吃：

| 形态 | 入口 | 说明 |
| :--- | :--- | :--- |
| **单节点链接** | 面板「单节点」/ `set-node` | `vless://` `vmess://` `trojan://` `ss://` `hysteria2://` `tuic://` |
| **订阅链接** | 面板「订阅」/ `set-sub` | 拉取后提取节点 |
| **sing-box JSON** | 面板「导入 JSON」 | 支持裸单出站、`{"outbounds":[…]}`、naiveproxy 客户端配置格式 |

两点如实说明：

- **导入≠启用**：导入只把节点放进 `json` 槽的候选池，**不会改当前来源**；要真正启用，得显式选一次来源。
- **`naive` 需要伴生库**：内核从官方包解出时会一并安装 `libcronet.dll`；若你的旧版本曾失败过，重装一次内核即可。

## 三、来源与出站选择：三个槽位，各自独立

**三个来源槽位（`node` / `sub` / `json`）彼此独立：候选只在本槽内组装，永不跨槽并池。**

- **`active_source`**：当前激活的来源，取值 `node`/`sub`/`json`（空串＝还没配任何来源）。**由你显式选择，不再按输入形态自动推断。**
- **`outbound_selections`**：每个槽位各存一份出站选择——`{"mode": "" | "auto" | "pinned", "node_key": …}`。三个槽位互不干扰，来回切换不重选。
- **`node_key`**：`pinned` 时的节点键，形如 `vless|203.0.113.10|443`（`type|server|port`）。

语义要点：

- **默认未选择**：槽内 ≥2 个节点时，启用前必须显式选一次。**程序不替你决定出口 IP。** 未选择就启用会明确报「请先选择出站」。
- **只解析出一个节点**（单节点链接，或只含一个节点的 JSON）：面板只读显示「将使用：<节点>」，不提供「自动优选」——一个元素没有可优选的余地。
- **解析出 ≥2 个节点**：展开为「**自动优选 / 指定节点**」。
  - **指定节点**：出口 IP 固定为你选的那一台；它慢、它挂，**都不自动换别人**，失败就明确报失败。
  - **自动优选**：在该槽位内部按延迟排序、逐个做真实转发验证，取第一个能通的。出口 IP 会变。
- **指定的节点失效**（从订阅或 JSON 里移除了）：明确报「所选节点已失效」，**不悄悄换别人**。
- **启用以请求为准**：启用时按请求里给出的来源与地址解析；请求里说不出来源就**明确拒绝**，不回落到 `config.json` 里的既有来源（写盘是结果记录，不是判据来源）。

## 四、测速与落地甄别

测速只测**当前来源**的节点。对每个节点依次四步：

1. **TCP 握手延迟**：UDP-only 协议（`hysteria2`/`tuic`）无 TCP 监听，跳过；
2. **真实转发验证**：起临时内核实际请求一次海外探测地址，确认数据能往返——**不是「端口通」就算过**。有些节点端口通但握手失败，只看延迟会被骗。
3. **落地 IP 探测**：转发通过后，顺带经临时内核拿到该节点的**真实落地 IP**；
4. **中转甄别**：落地 IP 与该节点声明的 server 解析出的 IP 不一致 → 标「中转」。

测速结果示例（虚拟数据）：

```text
示例节点A  [vless 203.0.113.10:443]  TCP 173ms  转发正常  落地 203.0.113.10
示例节点B  [vmess 198.51.100.7:443]  TCP 85ms   转发正常  落地 192.0.2.5 中转
```

标「中转」的节点是隧道/CDN 中转——**测速可能很快，但落地在别处**，一眼能认出来，不用真连上去才踩坑。

延迟缓存在进程内（重启即失），出站下拉每一项直接显示延迟（未测显示 `—`）；「自动优选」那一项显示「本批最快：<节点名> X ms」。

## 五、落地位置：用城市天气体现出口 IP

通道运行时，面板走**当前通道的真实出口**探测一次地理位置：

```text
落地位置：东京 · 日本（示例 ISP）｜ 22°C 多云
```

探测请求经节点出口发出，定位服务看到的是**节点的落地 IP**，不是本机的公网 IP。城市与 ISP 来自 IP 地理定位，天气来自 Open-Meteo。**等于用「城市 + 天气」间接验证出口 IP 的物理位置**——工具本身不内置 IP 地理库，也不联网查本机位置。

## 六、快速开始

### Windows：用安装包（推荐）

> 安装包与 zip 产物在仓库 [Releases](https://github.com/gambn/agent-channel/releases) 页下载；若该版本未附带产物，按 §十 自行打包（`python packaging/build.py`，内核清单已钉 v1.14.3）。

1. 双击 `agent-channel-0.1.0-setup.exe`；
2. 按向导走完（可选安装目录、桌面快捷方式、是否开机自启）；
3. 安装完自动启动，托盘出现图标，浏览器自动打开控制面板 `http://127.0.0.1:18888`（默认端口，被占用会自动换）。

安装**不需要管理员权限**，不会弹 UAC。

### Windows：免安装 zip / 精简单文件

- **zip 版**：解压到任意目录（路径避免特殊字符），双击 `agent-channel.exe`；已内置内核，离线可用。
- **单文件版**（约 10 MB）：**不含内核**，首次运行会自动联网下载官方包（约 33 MB，解压后约 91 MB），期间界面可能看起来没反应，等它下完即可。

### macOS / Linux

两条路，任选；**推荐 A**（省去 Python 版本与 PATH 的一切麻烦）：

**A. 原生包**（在仓库 [Releases](https://github.com/gambn/agent-channel/releases) 页下载；若该 Release 未附带产物，走 B）

- **Linux `.deb`（x86_64）**：`sudo apt install ./agent-channel_0.1.0_amd64.deb`
- **通用 tar.gz（Linux / macOS）**：解包后执行 `./install.sh` —— 装到 `~/.local/opt/agent-channel`、命令软链到 `~/.local/bin`，**不需要 root**；`./install.sh --uninstall` 卸载（保留 `~/.agent_channel` 里的配置与内核）
- 两种包都**内置官方编译的 sing-box 内核**，装完即用，不依赖系统 Python

**B. 源码安装（开发者路线）**

需要 **Python 3.10+**：macOS 自带的 `/usr/bin/python3` 是 3.9，**不满足要求**，请显式指定版本（如 `python3.12`）。

```bash
# 1) 拉源码
git clone https://github.com/gambn/agent-channel.git && cd agent-channel

# 2) 建虚拟环境安装（之后所有命令都在 .venv/bin/ 下）
python3 -m venv .venv
.venv/bin/pip install .

# 3) 装官方预编译内核（按当前平台自动选资产）
.venv/bin/agent-channel core list
.venv/bin/agent-channel core install

# 4) 配置节点并启动
.venv/bin/agent-channel set-sub "<你的订阅链接>"
.venv/bin/agent-channel start

# 5) 打开控制面板
.venv/bin/agent-channel ui
```

> 为什么默认虚拟环境、而不是 `pip install --user`：macOS 上 `--user` 会把命令装进 `$(python3 -m site --user-base)/bin`（实测为 `/Users/<你>/Library/Python/3.x/bin`），**该目录默认不在 PATH**，第 3 步就会 `command not found`；另外 macOS 的 Homebrew Python 与 Debian/Ubuntu 等发行版 Python 都会因 **PEP 668** 直接拒绝 `--user` 安装（`--break-system-packages` 能绕过，但仍绕不过 PATH 那条）。

三点说明（都是代码事实）：

- 非 Windows **没有托盘图标**；「启动/停止通道」「退出并还原系统代理」改用 CLI 的 `start` / `stop`。
- macOS 首次运行可能要给终端「网络」权限；Linux 的系统代理（`gsettings`）只对遵循 GNOME 代理规范的桌面生效（GNOME / Cinnamon / MATE），KDE 等需手动把代理指向 `127.0.0.1:20849`。
- 内核下载走 `api.github.com`，无法访问 GitHub 的网络需先解决（与 Windows 相同）。

### 配置节点（三步）

1. 在面板「节点配置与管理」里选一种输入形态填进去：**单节点链接** / **订阅链接** / **导入 JSON**；
2. 点「保存配置」；
3. 在「**启用时的出站**」里选择：只有一个节点时它只读显示将使用谁；有多个节点时选「自动优选」或点选某一个；然后点「**启动通道**」。

启用成功后状态卡变「运行中」并显示当前活跃出站。

### 面板上另外三个动作

- **一键测试**：实测能不能连上 OpenAI / Gemini / Anthropic，会显示延迟；
- **测速**：测当前来源的节点（含真实转发验证 + 落地 IP + 中转标记），结果**只是诊断**，把延迟数字喂给出站下拉；
- **检查内核更新**：读本地版本号、与官方最新比一次，有新版就提示。

> 说明：出站下拉是**唯一**的节点选择入口。测速结果里没有「使用」按钮——那是同一件事的第二个入口，会与下拉显示不一致，已移除。

## 七、托盘图标（Windows）

程序运行时托盘常驻一个图标，**右键**有菜单：**打开控制面板** / **启动通道** / **停止通道** / **退出**（退出会还原系统代理）。**双击**图标 = 打开控制面板。macOS / Linux 上没有托盘，启动即打开面板。

## 八、命令行用法

```text
agent-channel status                              查看当前状态（轻量只读）
agent-channel set-sub  <订阅链接>                 保存订阅配置
agent-channel set-node <vless://...>              保存单节点配置（也接受 JSON 片段）
agent-channel set-json <文件|->                   从文件或 stdin 导入 sing-box JSON 节点（只进 json 槽候选池）
agent-channel start [--source node|sub|json]      按已保存的配置启用通道（可临时指定来源槽位）
agent-channel stop                                停止通道并还原系统代理
agent-channel test                                测试海外 AI 接口连通性
agent-channel benchmark --sub <订阅链接>           测速（--node 测单节点，--json 出 JSON）
agent-channel config                              查看配置（默认脱敏，--reveal 看完整）
agent-channel core list                           列出官方可选内核版本
agent-channel core install v1.14.3                安装指定版本内核
agent-channel ui                                  启动控制面板
```

另有一个无人值守守护入口（仅从源码运行时有）：`python launch_service.py`。它启动控制面板但**不自动打开浏览器**——地址只打印在控制台，需自行访问；同时按 `config.json` 自动接通通道并常驻，适合开机自启等后台场景。要启动即弹出面板，用上面的 `agent-channel ui` 或安装包。

无人值守使用前须先在面板里选一次出站：本入口只按已保存的配置接通，池来源（≥2 个节点）在未选择出站时按三档文案如实报「已配置 N 个候选节点，但尚未选择出站：请在面板「启用时的出站」选择」并停在面板，不会替你挑选出口——这与「未选择出站时不启用」一致，但开机自启时人不在面板前，容易把「未选择」误读成「没配节点」。

日志输出 UTF-8，中文节点名不会乱码。

## 九、配置与数据模型

`config.json`（Windows 位于 `%LOCALAPPDATA%\agent_channel\`）：

```json
{
  "active_source": "",
  "node_uri": "",
  "subscription_url": "",
  "imported_nodes": [],
  "running_source": null,
  "outbound_selections": {
    "node": {"mode": "", "node_key": null},
    "sub":  {"mode": "", "node_key": null},
    "json": {"mode": "", "node_key": null}
  }
}
```

- `active_source`：当前激活的来源槽位 `node`/`sub`/`json`（空串＝还没配任何来源）；
- `running_source`：通道运行时落笔的来源快照（`enable` 成功时写、停止时清空）。状态卡显示 `running_source ?? active_source`，「启动后又切了标签」仍能看到现在跑的是哪条线；
- `imported_nodes`：导入的节点列表，作为 `json` 槽的候选；
- **凭据只落 `config.json`**：任何 API 响应与面板都**不回传** `uuid` / `password` / `private_key`；
- **旧配置自动迁移**：更早版本的 `mode`、`outbound_mode`/`outbound_node_key`、`outbound`/`pinned_node`、`strategy`/`custom_node` 都会归位到三槽位结构并删除旧键；无法映射则降级为「未选择」并给出告警，不抛异常。

### HTTP API（面板与 Agent 共用，均监听 `127.0.0.1`）

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/api/status` | 通道状态 + 系统代理 + 当前来源（`source` = 浏览中的标签，`running_source` = 跑的那条线）+ 配置告警 |
| GET | `/api/config` | 当前配置（脱敏，不含凭据；`imported_nodes` 只回数量 `imported_count`） |
| GET | `/api/outbound_options` | 指定来源（`?source=`）候选 + 可用数 + 当前选择 + 每项延迟 |
| GET | `/api/uuid` | 生成一个 v4 UUID |
| GET | `/api/geolocation` | 走通道出口探测落地城市 / ISP / 天气 |
| GET | `/api/core_info`、`/api/check_update` | 内核版本 / 检查更新 |
| POST | `/api/config` | 增量保存配置（只写显式给出的键，不清空其它字段） |
| POST | `/api/enable` | 启用通道；**必须显式给 `source`**（`node`/`sub`/`json`），且该来源地址非空 |
| POST | `/api/disable` | 停止通道并还原系统代理 |
| POST | `/api/benchmark` | 测指定来源节点（`{source}`，缺省 `active_source`）（含落地 IP + 中转标记） |
| POST | `/api/set_outbound` | 设出站 `{mode, node_key?, source?}` |
| POST | `/api/import_nodes` | 导入 / 清空节点（只写 `json` 槽候选，**不改当前来源**） |
| POST | `/api/sysproxy` | 开关系统代理 |
| POST | `/api/test_ai` | 测试 AI 接口连通性（结果带 `via` = `channel`/`direct` 与 `channel_running` 标注） |
| POST | `/api/update_core` | 下载并替换官方内核（内核被占用时明确拒绝，不白下几十兆） |

**写接口契约**（上面所有 POST）：

- 必须带 `Content-Type: application/json`，否则 **415**；body 可以是空（等同 `{}`）；
- 必须是合法 JSON **对象**，否则 **400**（旧行为是兜底成空对象继续执行，已取消）；
- 请求体上限 **8 MiB**，超限 **413**；
- 带 `Origin` 的请求必须与本机同源（按 `Host` 头归一化，`127.0.0.1` / `localhost` / `[::1]` 均认），跨站写请求一律 **403**。**无 `Origin` 的本机调用（curl / CLI / 托盘 / 守护进程）不受影响。**

> 这道同源校验挡的是：浏览器允许任意网页向 `127.0.0.1:<端口>` 发不触发预检的简单请求，而写接口能停通道、改订阅（等于把流量引向别人的节点）、替换内核 —— 响应读不到无所谓，副作用当场发生。

### 编程调用

```python
from agent_channel import channel_state, enable_channel, load_config

st = channel_state(load_config())
if st["state"] != "ok":
    print(st["message"])  # 未配置 / 配了但没认出来 / 未选出站 / 所选节点失效，各有各的话
else:
    enable_channel(**st["kwargs"], auto_patch=True, set_sys_proxy=True)
# 此时 127.0.0.1:20849 可用
```

`channel_state(config)` 返回 `{state, kwargs, candidates, message}`：`state == "ok"` 才可启用；否则 `message` 是给用户看的一句话（未配置 / 配了但没认出来 / 未选出站 / 所选节点失效四档，全仓库唯一定义点）。`channel_kwargs` 是它的兼容薄壳（`ok` 给 `kwargs`，否则空 dict）。

## 十、从源码构建与测试

```bash
pip install PyInstaller
python packaging/build.py            # onedir + zip，预置内核，离线可用
python packaging/build.py --no-core  # 单文件精简版，首次运行联网下载内核
python packaging/build.py --installer  # 产出 setup.exe（仅 Windows，需 Inno Setup）

python tests/run_tests.py            # 全套测试（自写 runner，不经 pytest）
```

## 十一、常见问题

**装好后浏览器打不开面板？**
手动访问 `http://127.0.0.1:18888`。端口被占时程序会自动换端口，启动时控制台会打印实际地址。

**启用了通道但网页还是上不去？**
先点「一键测试」看接口通不通。若失败，多半是节点本身不可用——测速一轮，换一个「转发正常」的节点，或在出站下拉里换一个。

**会不会影响国内网站访问？**
不会。程序只把系统代理指向本地通道；若发现某些国内站点异常，把面板上的「同步为本地系统代理」关掉即可。

**能同时开两个吗？**
不建议。本地代理端口固定为 `20849`，两个实例会抢同一个端口。

**程序会不会自己上传我的数据？**
不会。它只做本地流量转发。唯一的外部网络访问分两类：**直连**（检查内核更新、拉取你配置的订阅，都绕过系统代理）；**走节点出口**（你主动点的测速、落地探测）。

**卸载**

- 安装包装的：从「设置 → 应用」里卸载，或运行安装目录下的 `unins000.exe`（会一并移除随包内核）；
- zip / 单文件版：直接删目录；程序只在 `%LOCALAPPDATA%\agent_channel\` 下存配置与内核，一并删掉即完全清理。
- 卸载前若程序还在运行，先右键托盘选「退出」。

## 十二、免责声明

本工具为纯技术实现的网络通道管理工具，**不提供、不内置、不推荐任何节点或订阅资料**。使用者需自行准备合法的网络出口通道，并对自己的使用行为负责。

## 十三、许可证

本项目采用开源 [MIT License](LICENSE) 协议发布，Copyright (c) 2026 gambn。完整条款见仓库根目录的 [LICENSE](LICENSE) 文件。
