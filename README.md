# XJTU UDP Remote Terminal Lab

English | [简体中文](README.zh-CN.md)

This repository contains a course project for the Computer Networks Lab at Xi'an Jiaotong University (XJTU). The assigned task was to design and implement a remote terminal over UDP.

The project defines an application-layer protocol over UDP and handles packet loss and reordering with acknowledgements, sequence numbers, a sliding window, out-of-order buffering, and timeout-based retransmission. After authentication, the server creates a separate PTY and shell process for each client. Keyboard input is sent to the server and terminal output is returned to the local terminal, allowing regular commands, arrow keys, `Ctrl+C`, and interactive programs such as `top` and `vim` to use the same UDP channel.

## Implemented features

- Fixed binary packet header, packet types, and CRC32 validation
- Acknowledgements, sequence numbers, deduplication, ordered delivery, retransmission, and a sliding window
- Multi-client PTY sessions, heartbeats, and resource cleanup
- Login authentication with PBKDF2-HMAC-SHA256 password hashes
- Raw terminal mode, ANSI escape-sequence forwarding, and terminal resize synchronization
- Read-only monitoring HTTP API and an optional Streamlit dashboard
- Unit tests for the protocol, reliable transport, authentication, and monitoring API

## Repository layout

```text
.
├── udp_remote_terminal/
│   ├── client.py             # UDP client
│   ├── server.py             # UDP server and PTY management
│   ├── protocol.py           # Packet format and validation
│   ├── reliable.py           # Reliable transport layer
│   ├── monitor.py            # Read-only monitoring HTTP API
│   ├── streamlit_monitor.py  # Optional monitoring dashboard
│   └── users.example.json    # Demo user configuration
└── tests/
```

The core client, server, and monitoring API use only the Python standard library. Streamlit is required only for the optional dashboard.

## Quick start

The project requires Python 3.10+ and a system that can create PTYs, such as Linux or macOS.

Start the server:

```bash
python3 udp_remote_terminal/server.py
```

Open another interactive terminal and start the client:

```bash
python3 udp_remote_terminal/client.py --host 127.0.0.1
```

The demo accounts are `alice` / `123456` and `bob` / `123456`. `users.example.json` is intended for local experiments only. For other uses, create a separate user database and pass its path with `--user-db`.

To accept connections from other hosts, explicitly bind the server to all network interfaces:

```bash
python3 udp_remote_terminal/server.py --host 0.0.0.0
```

Restrict the allowed source addresses with a firewall or cloud security group when using this option.

## Monitoring dashboard

The server starts a read-only monitoring API on `127.0.0.1:9100` by default. Install the optional dependency to run the Streamlit dashboard:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

streamlit run udp_remote_terminal/streamlit_monitor.py \
  -- --api http://127.0.0.1:9100
```

A Bearer Token can be set for the monitoring API through an environment variable:

```bash
export UDPTERM_MONITOR_TOKEN='replace-with-your-token'
python3 udp_remote_terminal/server.py --monitor-token "$UDPTERM_MONITOR_TOKEN"
```

## Tests

The unit tests do not require Streamlit:

```bash
python3 -m unittest discover -s tests -v
```

They cover packet parsing, CRC failures, sliding-window behavior, retransmission, duplicate and out-of-order packets, authentication failures, user-directory boundaries, and monitoring API authorization.

## Scope and limitations

This project is intended for studying UDP application protocols, reliable transport, and terminal I/O. It is not a replacement for SSH.

- Login passwords are carried in UDP payloads without transport encryption.
- Each user has a separate initial directory, but all shell processes still run as the same operating-system user. This is not strong isolation.
- The monitoring API listens only on localhost by default. Remote access should use an SSH tunnel or a protected reverse proxy.
- Do not expose the demo accounts or default configuration to the public Internet.

## License

[MIT](LICENSE)
