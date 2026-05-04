# 计算机网络专题实验 实验七报告

姓名：姓名1、姓名2  
班级：待补充

## 实验名称

题目5：基于 UDP 的远程终端程序设计与实现。

## 实验原理

UDP 是无连接、不可靠、面向报文的传输层协议。它不需要建立连接，一次 `sendto` 对应一个独立 UDP 数据报；但 UDP 本身不保证报文可靠到达，也不保证顺序，网络中可能出现丢包、乱序、重复包。因此，本实验在 UDP 之上设计自定义应用层协议，并实现 ACK、序列号、超时重传、重复包去重和按序交付，使远程终端交互在不可靠 UDP 之上尽量稳定运行。

远程终端的核心思想是：客户端采集本地键盘输入，将普通字符、回车、退格、Tab、方向键、Ctrl+C 等控制字符通过 UDP 发送给服务端；服务端为每个客户端创建独立伪终端（PTY）和 shell 子进程，把客户端输入写入 PTY；PTY 产生的输出再通过 UDP 返回客户端，由客户端写入本地 stdout。PTY 可以让服务端 shell 认为自己连接的是一个真实终端，因此能够支持 `top`、`vim` 等全屏交互式程序。

## 实验目的

1. 掌握 UDP socket 服务端和客户端程序编写方法。
2. 理解 UDP 无连接、不可靠、面向报文的特点。
3. 设计固定头部的自定义应用层协议，实现合法性校验和类型区分。
4. 在应用层实现 ACK、序列号、去重、超时重传和滑动窗口可靠传输。
5. 掌握 PTY 远程终端原理，处理控制字符、ANSI 转义序列和窗口大小同步。
6. 实现多客户端并发、心跳保活和异常清理。

## 实验内容

### 基本功能

- 客户端可指定服务端 IP 和 UDP 端口。
- 服务端绑定 UDP 端口，持续接收客户端报文。
- 自定义协议正确解析魔数、版本、报文类型、客户端 ID、序列号、ACK、数据长度和 checksum。
- 非法报文直接丢弃，不导致程序崩溃。
- 通过 ACK、序列号、去重、超时重传实现 UDP 应用层可靠传输。
- 支持 `pwd`、`ls`、`ps`、`ip addr` 等基础命令。
- 服务端根据 `client_id` 为不同客户端维护独立 session。
- 客户端周期性发送心跳，服务端心跳超时后清理对应 session。

### 高级功能

- 客户端进入 raw terminal 模式，逐字节透传键盘输入。
- 支持 Ctrl+C、退格、Tab、方向键等终端控制字符。
- ANSI 转义序列透明转发，支持彩色输出和光标控制。
- 服务端使用 PTY，使 `ping` 能实时输出，`top`/`vim` 能进行基础全屏交互。
- 客户端捕获 `SIGWINCH`，向服务端发送窗口行列数；服务端用 `TIOCSWINSZ` 同步 PTY 大小。
- 可靠层采用默认窗口大小 8 的滑动窗口，提升连续输出场景下的传输效率。

## 实验实现

### 人员分工

- 姓名1：待补充，建议填写协议设计、服务端实现、PTY 调试。
- 姓名2：待补充，建议填写客户端实现、可靠传输测试、报告整理。

### 实验设计

系统采用 C/S 模式：客户端和服务端通过 UDP 通信。服务端使用一个 UDP socket 接收所有客户端报文，并通过 `client_id` 区分不同用户。每个客户端第一次发送 `HEARTBEAT`、`RESIZE` 或 `DATA` 报文后，服务端创建一个独立 PTY shell session。服务端主循环使用 `selectors` 同时监听 UDP socket 和多个 PTY fd，因此无需为每个客户端创建独立线程。

数据流如下：

1. 客户端 stdin 有输入时，可靠层分配序列号并发送 `DATA`。
2. 服务端收到 `DATA` 后立即回复 `ACK`，可靠层去重并按序交付给 PTY。
3. PTY 有输出时，服务端按不超过 1200 字节切片，发送 `DATA` 给客户端。
4. 客户端收到服务端 `DATA` 后回复 `ACK`，按序写入 stdout。
5. 客户端定时发送 `HEARTBEAT`；服务端超过 15 秒未收到某客户端报文则清理该 session。

### 协议

协议采用固定 28 字节二进制 header + 可变 payload，网络字节序编码。header 字段如下：

| 字段 | 长度 | 说明 |
| --- | ---: | --- |
| `magic` | 2 字节 | 固定 `0x5554`，识别合法 UDP 远程终端报文 |
| `version` | 1 字节 | 协议版本，当前为 1 |
| `type` | 1 字节 | 报文类型 |
| `flags` | 1 字节 | 扩展控制标志 |
| `header_len` | 1 字节 | 固定头部长度，当前为 28 |
| `client_id` | 4 字节 | 客户端 ID，用于区分多客户端 session |
| `seq` | 4 字节 | DATA 发送序列号 |
| `ack` | 4 字节 | ACK 确认序列号 |
| `payload_len` | 2 字节 | 数据段长度 |
| `rows` | 2 字节 | 终端窗口行数，用于 RESIZE |
| `cols` | 2 字节 | 终端窗口列数，用于 RESIZE |
| `checksum` | 4 字节 | CRC32 校验值 |

报文类型：

- `DATA = 0x01`：终端输入或输出数据。
- `ACK = 0x02`：确认报文。
- `HEARTBEAT = 0x03`：心跳保活。
- `RESIZE = 0x04`：窗口大小同步。
- `CLOSE = 0x05`：关闭连接。

非法报文处理规则：

- `magic`、`version` 或 `header_len` 不正确，丢弃。
- UDP 数据长度与 `payload_len` 不一致，丢弃。
- `checksum` 校验失败，丢弃。
- 未知 `type`，丢弃。

### UI 设计

本实验为命令行终端程序，没有图形界面。客户端启动后进入 raw terminal 模式，本地终端显示来自远程 PTY 的完整输出。用户看到的交互效果接近 SSH：输入字符、方向键、Tab、Ctrl+C 等均发送到服务端 shell；服务端输出直接显示在客户端终端。

### 框架结构

```text
lab7/
├── udp_remote_terminal/
│   ├── __init__.py
│   ├── client.py
│   ├── protocol.py
│   ├── reliable.py
│   ├── server.py
│   └── README.md
├── tests/
│   ├── test_protocol.py
│   └── test_reliable.py
└── 实验七_题目5_基于UDP的远程终端实验报告.md
```

服务端框架：

- UDP socket 绑定指定端口并设置非阻塞。
- `selectors` 同时监听 UDP socket 和所有客户端 PTY fd。
- `sessions` 字典以 `client_id` 为 key，保存客户端地址、PTY fd、shell pid、可靠传输状态、心跳时间和窗口大小。
- 收到 `CLOSE`、PTY 关闭或心跳超时时，注销 selector、关闭 fd、向 shell 发送 `SIGHUP` 并删除 session。

客户端框架：

- UDP socket 连接指定服务端地址。
- stdin 切换 raw 模式并注册到 selector。
- stdin 输入进入可靠发送队列；UDP 输出按序写入 stdout。
- 捕获 `SIGWINCH` 后发送 `RESIZE`。
- `finally` 中发送 `CLOSE` 并恢复终端属性，避免本地 shell 状态异常。

### 关键代码的描述

- `protocol.py`：自主编写。使用 `struct.pack/unpack` 实现固定头部二进制协议，使用 `zlib.crc32` 计算 checksum。`unpack_packet()` 对非法数据统一返回 `None`，保证网络接收循环不会因异常数据崩溃。
- `reliable.py`：自主编写。`ReliableEndpoint` 维护 `_send_queue`、`_unacked`、`_expected_seq` 和 `_receive_buffer`。`get_packets_to_send()` 控制滑动窗口，`get_packets_to_retransmit()` 扫描超时包，`on_data()` 实现重复包过滤和按序交付。
- `server.py`：自主编写。通过 `pty.fork()` 为每个客户端创建独立 shell；用 `selectors` 复用 UDP socket 和 PTY fd；用 `TIOCSWINSZ` 同步窗口大小；根据心跳超时清理资源。
- `client.py`：自主编写。用 `tty.setraw()` 进入 raw 模式，使用 `selectors` 同时监听 stdin 和 UDP socket；所有控制字符透明传输；退出时恢复终端属性。

## 测试及结果分析

### 测试1：协议与可靠传输单元测试

测试命令：

```bash
python3 -m unittest discover -s lab7/tests
```

覆盖内容：

- 正常封包/解包。
- magic 错误、checksum 错误、payload 长度不一致、header 长度不足时丢弃。
- 空 payload 和最大 payload 正常处理。
- 滑动窗口不超过默认窗口大小。
- ACK 后窗口前移。
- 超时后重传。
- 重复包只处理一次，乱序包缓存后按序交付。

> 此处放置单元测试通过截图。

结果分析：单元测试用于验证协议和可靠层的核心逻辑，能够证明非法报文不会导致崩溃，可靠层能在重复、乱序和超时场景下保持正确状态。

### 测试2：基础命令执行

测试步骤：

```bash
python3 lab7/udp_remote_terminal/server.py --host 0.0.0.0 --port 9000
python3 lab7/udp_remote_terminal/client.py --host 127.0.0.1 --port 9000
```

客户端连接后执行：

```bash
pwd
ls
ps
ip addr
```

预期结果：命令输出实时显示在客户端，输出格式与直接在 Linux shell 中执行基本一致。

> 此处放置基础命令执行截图。

结果分析：普通命令输出经服务端 PTY 读取后分片发送给客户端，客户端按序写入 stdout，说明基础远程终端链路正常。

### 测试3：多客户端并发

测试步骤：

1. 启动一个服务端。
2. 打开两个终端，分别启动两个客户端。
3. 两个客户端分别执行不同命令，例如一个执行 `pwd`，另一个执行 `ps`。

预期结果：两个客户端输出互不混淆；一个客户端退出不影响另一个客户端。

> 此处放置多客户端并发截图。

结果分析：服务端通过 `client_id` 区分 session，每个 session 拥有独立 PTY fd、shell pid 和可靠层状态，因此多客户端不会共享输入输出缓冲区。

### 测试4：心跳与离线检测

测试步骤：

1. 客户端连接服务端后保持空闲。
2. 观察服务端收到心跳后持续保持 session。
3. 强制关闭客户端或断开网络。
4. 等待超过服务端 `--heartbeat-timeout`，观察服务端日志。

预期结果：服务端正常回复心跳 ACK；客户端异常离线后服务端在超时后清理 PTY 和 session。

> 此处放置心跳与离线检测截图。

结果分析：心跳机制解决 UDP 无连接场景下“无法天然感知对端断开”的问题，避免服务端长期保留无效 PTY 资源。

### 测试5：实时输出与 Ctrl+C

测试命令：

```bash
ping 8.8.8.8
```

随后按 `Ctrl+C`。

预期结果：`ping` 输出逐行实时显示；按 `Ctrl+C` 后远端命令被中断，并回到 shell 提示符。

> 此处放置 ping 与 Ctrl+C 测试截图。

结果分析：客户端 raw 模式不会本地拦截 Ctrl+C，而是将字节 `0x03` 发送给服务端 PTY。PTY 将其解释为终端中断信号，从而中断正在运行的 `ping`。

### 测试6：top / vim 全屏程序

测试命令：

```bash
top
```

按 `q` 退出。可选测试：

```bash
vim test.txt
```

预期结果：`top` 界面能正常显示并刷新，按 `q` 退出；`vim` 可进入全屏界面并进行基础输入、保存和退出操作。

> 此处放置 top 或 vim 测试截图。

结果分析：全屏程序依赖 PTY、ANSI 转义序列和窗口大小信息。本实现对控制字符和 ANSI 序列进行透明传输，并通过 `RESIZE` 报文同步行列数，因此能够支持基础全屏交互。

### 测试7：抓包验证

抓包命令：

```bash
sudo tcpdump -i any udp port 9000 -X
```

预期观察结果：

- UDP 数据报中出现固定 magic `0x5554`。
- `DATA` 报文携带终端输入或输出。
- `ACK` 报文携带确认序列号。
- 空闲时周期性出现 `HEARTBEAT`。
- 改变窗口大小时出现 `RESIZE`，其中 rows/cols 字段发生变化。
- 退出客户端时出现 `CLOSE`。

> 此处放置 tcpdump 或 Wireshark 抓包截图。

结果分析：抓包可以验证本实验确实基于 UDP 传输，并且可靠性、心跳和窗口同步均由自定义应用层协议完成，而不是依赖 TCP。

## 实验结论

本实验完成了基于 UDP 的远程终端程序。程序通过自定义固定头部协议解决了 UDP 报文类型区分、合法性校验和元数据传递问题；通过 ACK、序列号、滑动窗口、超时重传和去重机制增强了 UDP 传输可靠性；通过 PTY 和 raw terminal 透传控制字符，实现了接近真实终端的远程 shell 交互。服务端使用 `selectors` 支持多客户端并发，并通过心跳机制清理异常离线 session。

## 总结及心得体会

UDP 编程的难点不在于 socket API，而在于协议层需要自行处理可靠性、乱序、重复、连接状态和异常退出。远程终端程序还需要理解终端控制字符、ANSI 转义序列和 PTY 行为；如果只用普通子进程管道，很难支持 `top`、`vim` 等交互式程序。通过本实验，可以更清楚地理解 TCP 提供可靠字节流服务背后的复杂机制，也能体会到应用层协议设计对系统健壮性的影响。

## 附件

1. 源码文件：`udp_remote_terminal/protocol.py`、`udp_remote_terminal/reliable.py`、`udp_remote_terminal/server.py`、`udp_remote_terminal/client.py`。
2. 测试文件：`tests/test_protocol.py`、`tests/test_reliable.py`。
3. 运行说明：`udp_remote_terminal/README.md`。
4. 参考资料：实验指导书《高阶 题目5 基于UDP的远程终端实验指导书.docx》、实验七 PPT《实验七_Socket网络编程实验.pptx》、Python 官方文档中 `socket`、`selectors`、`pty`、`termios` 模块说明。
