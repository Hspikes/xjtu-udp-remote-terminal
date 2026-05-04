# UDP Remote Terminal（实验七题目5）

本目录实现“基于 UDP 的远程终端程序”。代码仅使用 Python 3 标准库，面向 Linux/类 Unix 终端环境。

## 文件说明

- `protocol.py`：自定义 UDP 应用层协议，包含固定二进制 header、CRC32 校验、封包/解包。
- `reliable.py`：滑动窗口可靠传输层，支持 ACK、序列号、去重、按序交付、超时重传。
- `server.py`：UDP 服务端，为每个客户端创建独立 PTY shell，并用 `selectors` 复用 UDP socket 与多个 PTY fd。
- `client.py`：UDP 客户端，进入 raw terminal 模式，透传键盘输入和 ANSI 控制序列，同步窗口大小。

## 运行环境

- Python 3.10+（未使用第三方依赖）
- Linux 或 macOS 终端；推荐 Linux
- 服务端需要能启动 `/bin/bash` 或当前 `$SHELL`

## 启动方法

在仓库根目录执行：

```bash
python3 lab7/udp_remote_terminal/server.py --host 0.0.0.0 --port 9000
```

另开一个交互式终端：

```bash
python3 lab7/udp_remote_terminal/client.py --host 127.0.0.1 --port 9000
```

如果服务端在云服务器，将 `127.0.0.1` 替换为服务器公网 IP，并确保 UDP 9000 端口已放行。

## 常用参数

服务端：

```bash
python3 lab7/udp_remote_terminal/server.py \
  --host 0.0.0.0 \
  --port 9000 \
  --shell /bin/bash \
  --heartbeat-timeout 15 \
  --window-size 8
```

客户端：

```bash
python3 lab7/udp_remote_terminal/client.py \
  --host 127.0.0.1 \
  --port 9000 \
  --client-id 1001 \
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

1. 普通命令：`pwd`、`ls`、`ps`、`ip addr`
2. 多客户端：同时启动两个客户端，分别执行不同命令
3. 实时输出：执行 `ping 8.8.8.8`，按 `Ctrl+C` 中断
4. 全屏程序：执行 `top`，按 `q` 退出；可选测试 `vim test.txt`
5. 窗口同步：运行 `top` 时改变客户端窗口大小

## 抓包

```bash
sudo tcpdump -i any udp port 9000 -X
```

可观察到自定义 header 中的 magic、type、client_id、seq、ack、payload_len、rows、cols、checksum，以及 `DATA`、`ACK`、`HEARTBEAT`、`RESIZE`、`CLOSE` 报文。

## 注意事项

- 单个 payload 默认限制为 1200 字节，避免常见 MTU 下发生 IP 分片。
- 客户端会切换本地终端到 raw 模式；异常退出后如终端显示异常，可执行 `reset` 或 `stty sane`。
- `top`/`vim` 依赖 PTY 和 ANSI 转义序列透传，需在真实交互式终端中测试。
