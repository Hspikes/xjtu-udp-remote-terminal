# UDP Remote Terminal（实验七题目5）

本目录实现“基于 UDP 的远程终端程序”。核心 UDP 服务端、客户端和监控 HTTP API 均仅使用 Python 3 标准库，面向 Linux/类 Unix 终端环境；可选的图形监控页面使用 Streamlit。

## 文件说明

- `protocol.py`：自定义 UDP 应用层协议，包含固定二进制 header、CRC32 校验、封包/解包。
- `reliable.py`：滑动窗口可靠传输层，支持 ACK、序列号、去重、按序交付、超时重传。
- `server.py`：UDP 服务端，先完成用户认证，再为每个客户端创建独立 PTY shell，并用 `selectors` 复用 UDP socket 与多个 PTY fd。
- `monitor.py`：只读监控 HTTP API，和 UDP 服务端同进程运行，提供连接状态、最近命令和文件列表。
- `client.py`：UDP 客户端，先提示用户名和密码，认证成功后进入 raw terminal 模式，透传键盘输入和 ANSI 控制序列，同步窗口大小。
- `streamlit_monitor.py`：Streamlit 监控页面，通过 HTTP API 展示在线 session、当前目录、文件列表和最近命令。
- `users.json`：演示用户库，保存 PBKDF2-HMAC-SHA256 密码哈希和用户初始目录配置。
- `user_homes/`：认证用户的初始工作目录根目录，用户目录不存在时由服务端自动创建。

## 运行环境

- Python 3.10+（核心功能未使用第三方依赖）
- Linux 或 macOS 终端；推荐 Linux
- 服务端需要能启动 `/bin/bash` 或当前 `$SHELL`
- 可选监控前端需要安装 Streamlit。Ubuntu/Debian 的系统 Python 可能启用 PEP 668，直接执行 `python3 -m pip install ...` 会出现 `externally-managed-environment`，推荐使用虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r lab7/requirements.txt
```

`requirements.txt` 已固定到 Python 3.12 可用的 Streamlit 版本，避免 pip 解析时回退到旧 Streamlit/Tornado。网络较慢或代理不稳定时，使用较长超时或国内镜像：

```bash
python -m pip install --default-timeout 120 -r lab7/requirements.txt
# 或
python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --default-timeout 120 -r lab7/requirements.txt
```

如果提示缺少 `venv` 模块，先安装系统包：

```bash
sudo apt install python3.12-venv
```

核心 UDP 终端、服务端内置 HTTP 监控 API 和单元测试不依赖 Streamlit；只有图形监控页面需要安装该依赖。

## 启动方法

在仓库根目录执行：

```bash
python3 lab7/udp_remote_terminal/server.py \
  --host 0.0.0.0 \
  --port 9000 \
  --user-db lab7/udp_remote_terminal/users.json \
  --home-root lab7/udp_remote_terminal/user_homes
```

服务端默认同时在 `127.0.0.1:9100` 启动只读监控 HTTP API。如需禁用：

```bash
python3 lab7/udp_remote_terminal/server.py --disable-monitor
```

另开一个交互式终端：

```bash
python3 lab7/udp_remote_terminal/client.py --host 127.0.0.1 --port 9000
```

客户端会先提示：

```text
Username:
Password:
```

演示用户为 `alice` / `123456` 和 `bob` / `123456`。认证成功后才会进入远程 shell，两个用户默认进入不同的初始目录。

如果服务端在云服务器，将 `127.0.0.1` 替换为服务器公网 IP，并确保安全组只对本地公网 IP 放行 UDP 9000，例如来源设置为 `本地公网IP/32`。

## 监控页面

启动服务端后，另开终端运行：

```bash
streamlit run lab7/udp_remote_terminal/streamlit_monitor.py \
  -- --api http://127.0.0.1:9100
```

如果服务端设置了监控 token：

```bash
export UDPTERM_MONITOR_TOKEN='your-token'
python3 lab7/udp_remote_terminal/server.py --monitor-token "$UDPTERM_MONITOR_TOKEN"
streamlit run lab7/udp_remote_terminal/streamlit_monitor.py \
  -- --api http://127.0.0.1:9100 --token "$UDPTERM_MONITOR_TOKEN"
```

也可以直接访问 API：

```bash
curl http://127.0.0.1:9100/healthz
curl http://127.0.0.1:9100/api/stats
curl http://127.0.0.1:9100/api/sessions
```

设置 token 后，`/api/*` 需要携带 `Authorization: Bearer <token>`，`/healthz` 仍保持公开用于健康检查。监控接口只读，不提供执行命令能力；文件列表限制在对应用户 home 目录内，API 和页面只显示 home 内相对路径。

## 常用参数

服务端：

```bash
python3 lab7/udp_remote_terminal/server.py \
  --host 0.0.0.0 \
  --port 9000 \
  --shell /bin/bash \
  --user-db lab7/udp_remote_terminal/users.json \
  --home-root lab7/udp_remote_terminal/user_homes \
  --heartbeat-timeout 15 \
  --window-size 8 \
  --monitor-host 127.0.0.1 \
  --monitor-port 9100 \
  --monitor-token optional-token
```

如果要关闭监控 API，增加 `--disable-monitor`。

客户端：

```bash
python3 lab7/udp_remote_terminal/client.py \
  --host 127.0.0.1 \
  --port 9000 \
  --client-id 1001 \
  --auth-timeout 10 \
  --server-timeout 20 \
  --window-size 8
```

## 测试

单元测试：

```bash
python3 -m unittest discover -s lab7/tests
```

也可以进入 `lab7` 目录后运行：

```bash
python3 -m unittest discover -s tests
```

建议手工验证：

1. 正确密码：使用 `alice` 登录，执行 `pwd`、`ls`、`touch hello.txt`
2. 目录隔离：使用 `bob` 登录，确认 `pwd` 与 `alice` 不同
3. 错误密码：输入错误密码，确认客户端退出且服务端不创建 shell
4. 多客户端：同时启动两个客户端，分别执行不同命令
5. 实时输出：执行 `ping 8.8.8.8`，按 `Ctrl+C` 中断
6. 全屏程序：执行 `top`，按 `q` 退出；可选测试 `vim test.txt`
7. 窗口同步：运行 `top` 时改变客户端窗口大小
8. 监控总览：启动 Streamlit 页面，确认在线用户数、session 表、窗口大小和最后活跃时间更新
9. 命令采集：在 `alice` 中执行 `pwd`、`touch a.txt`、`ls`，确认页面显示当前目录、`a.txt` 和最近命令
10. 安全边界：访问 `/api/sessions/<client_id>/files?path=../`，确认返回 403

## 抓包

```bash
sudo tcpdump -i any udp port 9000 -X
```

可观察到自定义 header 中的 magic、type、client_id、seq、ack、payload_len、rows、cols、checksum，以及 `DATA`、`ACK`、`HEARTBEAT`、`RESIZE`、`CLOSE` 报文。
认证阶段还可以观察到 `AUTH`、`AUTH_OK`、`AUTH_FAIL` 报文；其中密码在 UDP payload 中仍是明文，只适合配合云安全组限制来源 IP 的课堂演示。

## 注意事项

- 单个 payload 默认限制为 1200 字节，避免常见 MTU 下发生 IP 分片。
- 客户端会切换本地终端到 raw 模式；异常退出后如终端显示异常，可执行 `reset` 或 `stty sane`。
- `top`/`vim` 依赖 PTY 和 ANSI 转义序列透传，需在真实交互式终端中测试。
- bash hook 在 prompt 阶段更新当前目录和最近命令；长时间运行命令时，监控页面显示的是上一轮 prompt 的目录。
- 监控 API 默认绑定 `127.0.0.1`。如需公网查看，建议使用 SSH tunnel 或反向代理，并设置 `--monitor-token`。
- 当前目录隔离只是“独立初始目录”，不是强沙箱。如果服务端仍以同一个 Linux 用户运行 shell，登录用户仍可能通过 `cd ..` 访问该 Linux 用户有权限访问的文件。
- 如需公网或强隔离使用，应改用 SSH，或进一步加入加密认证、Linux 系统用户权限、`setuid`/`setgid`、`chroot`、容器或 namespace。
