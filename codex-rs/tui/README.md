# Codex TUI - WebSocket Streaming Integration

Real-time streaming of Codex CLI output to external applications via WebSocket.

## Overview

The Codex TUI now supports WebSocket broadcasting, allowing external applications to receive LLM output in real-time as it's being generated. This is useful for:
- Building custom integrations with Codex
- Monitoring Codex output remotely
- Processing streaming output in external applications
- Building custom UIs on top of Codex

## Architecture

```
┌─────────────────┐
│  Codex CLI (Rust) │
│                   │
│  ┌─────────────┐  │
│  │ ChatWidget  │  │
│  │             │  │
│  │ Event:      │  │
│  │ "Hello      │  │
│  │  world!"    │  │
│  │             │  │
│  │ stream_tx ───────┼──► WebSocket Server (127.0.0.1:8765)
│  └─────────────┘  │
└───────────────────┘
         │
         │ ws://127.0.0.1:8765
         │
┌────────▼────────┐
│  Python Client   │
│  (or any WS client) │
│                  │
│  "Hello"         │
│  " "             │
│  "world!"        │
└──────────────────┘
```

## Quick Start

### 1. Start Codex TUI

The WebSocket server is embedded in the Codex TUI and starts automatically when you run Codex:

```bash
cd /Users/wiggel/IntelliJIDEA/codex_clone/codex
./bazel-bin/codex-rs/cli/codex
```

The WebSocket server listens on `ws://127.0.0.1:8765`.

### 2. Run the Python Client

In another terminal:

```bash
cd codex-rs/tui

# Set up Python environment (first time only)
uv venv .venv
uv pip install websockets

# Run the client
.venv/bin/python python_websocket_client.py
```

### 3. Test It

Type something in Codex - you should see the output appear in the Python client in real-time!

## Python Client Usage

### Basic Usage

```bash
python python_websocket_client.py
```

### Custom Server URI

```bash
python python_websocket_client.py --uri ws://192.168.1.100:8765
```

### Disable Auto-Reconnect

```bash
python python_websocket_client.py --no-reconnect
```

### Custom Reconnect Delay

```bash
python python_websocket_client.py --reconnect-delay 5
```

## API Reference

### WebSocket Message Format

The server sends plain text messages. Each message contains a chunk of text (could be a single character or multiple characters).

Example:
```
H
e
l
l
o

w
o
r
l
d
!
```

### Server Information

- **Default Address**: `ws://127.0.0.1:8765`
- **Message Type**: Plain text (UTF-8)
- **Connection**: WebSocket (RFC 6455)
- **Auto-reconnect**: Supported by client

### Client Examples

#### Python

```python
import asyncio
import websockets

async def stream_codex():
    uri = "ws://127.0.0.1:8765"
    async with websockets.connect(uri) as ws:
        async for message in ws:
            text = message if isinstance(message, str) else message.decode('utf-8')
            print(text, end='', flush=True)
            # Process the text in your application
            await my_send_to_app(text)

asyncio.run(stream_codex())
```

#### JavaScript (Node.js or Browser)

```javascript
const ws = new WebSocket('ws://127.0.0.1:8765');

ws.onopen = () => {
    console.log('Connected to Codex WebSocket');
};

ws.onmessage = (event) => {
    const text = event.data;
    process.stdout.write(text);
    // Or: document.getElementById('output').textContent += text;
};

ws.onclose = () => {
    console.log('Codex WebSocket connection closed');
};
```

#### cURL (HTTP-style testing)

```bash
# Note: cURL doesn't support WebSocket, use websocat instead
websocat ws://127.0.0.1:8765
```

## Troubleshooting

### "Could not connect to ws://127.0.0.1:8765"

**Problem**: Client cannot connect to the WebSocket server.

**Solutions**:
- Make sure Codex is running
- Check if the WebSocket server started (look for "WebSocket streaming server listening" message in Codex terminal)
- Verify the bind address matches (`ws://127.0.0.1:8765` by default)

### No output appears in Python client

**Problem**: Client connected successfully but no text appears.

**Solutions**:
- Check if the Codex TUI is actually generating output (try typing something in Codex)
- Look for "WebSocket client connected from ..." message in Codex terminal
- Make sure you're in an active conversation where Codex is generating output

### Connection drops frequently

**Problem**: WebSocket connection disconnects and reconnects often.

**Solutions**:
- This is normal if Codex is restarted - the client should auto-reconnect
- Check network stability for remote connections
- Increase reconnect delay with `--reconnect-delay` option

## Configuration

### Server-side (Rust)

The WebSocket server is configured in `codex-rs/tui/src/app.rs`:

```rust
// Enable WebSocket streaming for real-time output
let _ = self.chat_widget.enable_websocket_streaming("127.0.0.1:8765".to_string());
```

To change the bind address:
- Edit the address string in `app.rs`
- For external access, use `"0.0.0.0:8765"` (allows connections from other machines)
- Rebuild with `bazel build //codex-rs/tui:codex-tui`

### Client-side (Python)

The Python client supports these options:

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--uri` | `-u` | `ws://127.0.0.1:8765` | WebSocket server URI |
| `--no-reconnect` | | `false` | Disable auto-reconnect on disconnect |
| `--reconnect-delay` | | `2.0` | Delay between reconnection attempts (seconds) |
| `--verbose` | `-v` | `false` | Enable verbose output |

## Development

### File Structure

```
codex-rs/tui/
├── src/
│   ├── chatwidget.rs         # WebSocket server implementation
│   └── app.rs                # WebSocket enable call
├── python_websocket_client.py  # Python client
├── pyproject.toml            # Python dependencies
├── requirements.txt          # Alternative Python deps
└── README.md                 # This file
```

### Key Components

1. **ChatWidget** (`chatwidget.rs`)
   - `stream_tx`: Channel for broadcasting text
   - `enable_websocket_streaming()`: Spawns WebSocket server
   - `handle_streaming_delta()`: Sends text to WebSocket

2. **Python Client** (`python_websocket_client.py`)
   - Async WebSocket connection
   - Auto-reconnect logic
   - Real-time character streaming

## Future Enhancements

Potential improvements for the WebSocket integration:

- [ ] Bidirectional communication (send messages TO Codex via WebSocket)
- [ ] Multiple client support with connection metadata
- [ ] Authentication/authorization for WebSocket connections
- [ ] Protocol messages for metadata (turn start/end, errors, etc.)
- [ ] Binary/JSON mode for structured data
- [ ] Per-client filtering (e.g., only stream certain events)
- [ ] WebSocket endpoint configuration in config.toml

## License

This WebSocket integration is part of the Codex project and follows the same license terms.

## Support

For issues or questions:
1. Check the troubleshooting section above
2. Review `codex-rs/tui/src/chatwidget.rs` for server implementation details
3. Review `python_websocket_client.py` for client implementation details
