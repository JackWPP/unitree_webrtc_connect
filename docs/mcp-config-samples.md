# MCP 客户端配置示例

把 `unitree-robot-mcp` 接到各种 agent host 上。所有示例都假定：

- 仓库路径：`/Users/me/unitree_webrtc_connect`（换成你自己的）
- Python 解释器：`/Users/me/.venvs/unitree/bin/python`（**必须是用它装过 `.[agent]` 的那个**）

> ⚠️ 用绝对路径填 `command`。agent host 通常不会继承你的 shell 环境，
> `python` / `unitree-robot-mcp` 这类裸命令经常找不到或者找错解释器。

---

## CodeBuddy / Claude Desktop（stdio）

```json
{
  "mcpServers": {
    "unitree-robot": {
      "command": "/Users/me/.venvs/unitree/bin/python",
      "args": ["-m", "unitree_robot_agent.cli", "serve"],
      "env": {
        "PYTHONPATH": "/Users/me/unitree_webrtc_connect",
        "UNITREE_AGENT_ROBOT": "R1",
        "UNITREE_AGENT_CONNECTION": "sta-l",
        "UNITREE_ROBOT_IP": "192.168.10.225",
        "UNITREE_AES_128_KEY": "0123456789abcdef0123456789abcdef",
        "UNITREE_AGENT_IDLE_DISCONNECT_S": "45",
        "UNITREE_AGENT_MAX_MOVE_S": "5"
      }
    }
  }
}
```

如果已经 `pip install -e .` 到那个解释器里，可以更简洁：

```json
{
  "mcpServers": {
    "unitree-robot": {
      "command": "/Users/me/.venvs/unitree/bin/unitree-robot-mcp",
      "args": ["serve"],
      "env": { "UNITREE_AGENT_ROBOT": "R1", "UNITREE_ROBOT_IP": "192.168.10.225" }
    }
  }
}
```

## Go2 变体

```json
{
  "mcpServers": {
    "unitree-go2": {
      "command": "/Users/me/.venvs/unitree/bin/python",
      "args": ["-m", "unitree_robot_agent.cli", "serve", "--robot", "Go2"],
      "env": {
        "PYTHONPATH": "/Users/me/unitree_webrtc_connect",
        "UNITREE_ROBOT_IP": "192.168.8.181",
        "UNITREE_AGENT_ALLOW_DANGEROUS": "false"
      }
    }
  }
}
```

## 只读 / 演示安全模式

演示或第一次接真机时，先只给读权限：

```json
{
  "mcpServers": {
    "unitree-robot-readonly": {
      "command": "/Users/me/.venvs/unitree/bin/unitree-robot-mcp",
      "args": ["serve", "--robot", "R1", "--safe-mode"],
      "env": { "UNITREE_ROBOT_IP": "192.168.10.225" }
    }
  }
}
```

这个模式下移动类工具会返回 `guard_rejected`，只读工具和 `robot_stop` 正常。

## 离线演示（无机器人）

不接机器人也能让 Agent 熟悉工具面：

```json
{
  "mcpServers": {
    "unitree-robot-mock": {
      "command": "/Users/me/.venvs/unitree/bin/unitree-robot-mcp",
      "args": ["serve", "--robot", "R1", "--backend", "mock"]
    }
  }
}
```

Mock 后端会复刻 811→830 重定向、冻结上报、1001/1003、静默 api 这些坑，
所以 Agent 在 mock 上练出来的行为对真机同样成立。

## streamable-http（多客户端 / 远程）

先起服务：

```bash
UNITREE_AGENT_ROBOT=R1 UNITREE_ROBOT_IP=192.168.10.225 \
  unitree-robot-mcp serve --transport streamable-http --host 0.0.0.0 --port 8765
```

客户端：

```json
{
  "mcpServers": {
    "unitree-robot": {
      "type": "streamable-http",
      "url": "http://192.168.10.50:8765/mcp"
    }
  }
}
```

**注意**：
- 只绑 `127.0.0.1` 是默认值，远程访问必须显式 `--host 0.0.0.0`。
- 这个端点等价于机器人的控制权，**不要暴露到公网**，需要的话前面加一层带鉴权的反向代理。
- 多个客户端同时调用是安全的（会话层有互斥锁），但会导致「谁在控制」难以追溯——
  建议只给一个 Agent 用。

## 环境变量放不进 JSON 时

`env` 块里放密码/key 不方便的话，用 `.env` 文件：

```bash
# /Users/me/unitree_webrtc_connect/.env
UNITREE_AGENT_ROBOT=R1
UNITREE_ROBOT_IP=192.168.10.225
UNITREE_AES_128_KEY=0123456789abcdef0123456789abcdef
```

server 启动时会自动读取（当前工作目录 → `~/.unitree-agent/.env` → `$UNITREE_AGENT_ENV_FILE`）。

## 配置后自检

```bash
# 1. 工具面是否正常
unitree-robot-mcp tools --robot R1

# 2. 连通性（会用同样的环境变量）
unitree-robot-mcp doctor

# 3. 离线全链路
unitree-robot-mcp selftest --robot R1

# 4. 在 agent host 里，让模型先调 robot_doctor
```
