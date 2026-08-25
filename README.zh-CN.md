# XJTU UDP Remote Terminal Lab

[English](README.md) | 简体中文

这是西安交通大学计算机网络专题实验的课程项目，实验题目为“基于 UDP 的远程终端程序设计与实现”。

项目在 UDP 之上定义了一套应用层协议，通过 ACK、序列号、滑动窗口、乱序缓存和超时重传处理丢包与乱序。用户认证成功后，服务端为客户端创建独立的 PTY 和 Shell 进程；客户端将键盘输入发送到服务端，再把终端输出写回本地终端。因此，普通命令、方向键、`Ctrl+C` 以及 `top`、`vim` 等交互式程序都可以通过同一条 UDP 通道运行。

## 实现内容

- 固定二进制报文头、报文类型和 CRC32 校验
- ACK、序列号、去重、按序交付、超时重传和滑动窗口
- 多客户端 PTY Session、心跳检测和资源清理
- PBKDF2-HMAC-SHA256 密码哈希与登录认证
- 终端 Raw Mode、ANSI 控制序列透传和窗口大小同步
- 只读监控 HTTP API 与可选的 Streamlit 监控页面
- 协议、可靠传输、认证和监控接口单元测试

## 目录结构

```text
.
├── udp_remote_terminal/
│   ├── client.py             # UDP 客户端
│   ├── server.py             # UDP 服务端与 PTY 管理
│   ├── protocol.py           # 报文格式与校验
│   ├── reliable.py           # 可靠传输层
│   ├── monitor.py            # 只读监控 HTTP API
│   ├── streamlit_monitor.py  # 可选监控页面
│   └── users.example.json    # 演示用户配置
└── tests/
```

核心客户端、服务端和监控 API 只使用 Python 标准库。Streamlit 仅用于图形监控页面。

## 快速开始

运行环境为 Python 3.10+，服务端需要 Linux 或 macOS 等能够创建 PTY 的系统。

启动服务端：

```bash
python3 udp_remote_terminal/server.py
```

另开一个交互式终端，启动客户端：

```bash
python3 udp_remote_terminal/client.py --host 127.0.0.1
```

仓库中的演示账号为 `alice` / `123456` 和 `bob` / `123456`。`users.example.json` 只用于本地实验；实际使用时应创建自己的用户配置并通过 `--user-db` 指定路径。

若要让其他主机连接，可以显式监听所有网络接口：

```bash
python3 udp_remote_terminal/server.py --host 0.0.0.0
```

此时应同时限制防火墙或云安全组的来源地址。

## 监控页面

服务端默认在 `127.0.0.1:9100` 启动只读监控 API。安装可选依赖后，可以运行 Streamlit 页面：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

streamlit run udp_remote_terminal/streamlit_monitor.py \
  -- --api http://127.0.0.1:9100
```

可以通过环境变量为监控 API 设置 Bearer Token：

```bash
export UDPTERM_MONITOR_TOKEN='replace-with-your-token'
python3 udp_remote_terminal/server.py --monitor-token "$UDPTERM_MONITOR_TOKEN"
```

## 测试

单元测试不依赖 Streamlit：

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖报文解析、CRC 错误、滑动窗口、重传、重复包与乱序包处理、认证失败、用户目录边界以及监控 API 权限。

## 实验边界

这个项目用于理解 UDP 应用层协议、可靠传输和终端 I/O，不是 SSH 的替代品。

- 登录密码会出现在 UDP Payload 中，网络传输没有加密。
- 不同用户拥有独立的初始目录，但 Shell 进程仍由同一个操作系统用户运行，这不是强隔离。
- 监控 API 默认只监听本机；如需远程访问，应使用 SSH Tunnel 或受保护的反向代理。
- 不要把演示账号和默认配置用于公网服务。

## License

[MIT](LICENSE)
