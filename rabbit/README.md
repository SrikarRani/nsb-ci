# NSB RabbitMQ Implementation

## Table of Contents
- [Architecture Overview](#architecture-overview)
  - [Key Architectural Advantage](#key-architectural-advantage)
  - [Key Components](#key-components)
  - [Queue Architecture](#queue-architecture)
  - [Message Flow](#message-flow)
    - [Client Initialization](#client-initialization)
    - [Message Sending (App Client)](#message-sending-app-client)
    - [Message Receiving (App Client)](#message-receiving-app-client)
    - [Message Fetching (Simulator Client)](#message-fetching-simulator-client)
- [Usage](#usage)
  - [Starting the Daemon](#starting-the-daemon)
  - [Creating an Application Client (RabbitMQ-only module)](#creating-an-application-client-rabbitmq-only-module)
  - [Creating a Simulator Client (RabbitMQ-only module)](#creating-a-simulator-client-rabbitmq-only-module)
  - [Using the Unified Client (recommended)](#using-the-unified-client-recommended)
  - [Asynchronous Operations](#asynchronous-operations)
- [Configuration](#configuration)
  - [System Modes](#system-modes)
  - [Simulator Modes](#simulator-modes)
  - [Database Configuration](#database-configuration)
- [Cross-Language Interoperability](#cross-language-interoperability)
- [Architecture Differences from Socket Implementation](#architecture-differences-from-socket-implementation)
  - [Similarities](#similarities)
  - [Differences from Socket Implementation](#differences-from-socket-implementation)
- [Dependencies](#dependencies)
- [Testing](#testing)
  - [Test Suite](#test-suite)
  - [Examples](#examples)
  - [Quick Inline Test](#quick-inline-test)

## Architecture Overview

### Key Architectural Advantage

Unlike the socket-based implementation where the daemon acts as a message router, the RabbitMQ implementation leverages the broker's built-in routing capabilities. Clients consume messages directly from their queues without daemon intermediation for message delivery. The daemon's role is limited to configuration management during client initialization.

### Key Components

1. **RabbitMQInterface** (`nsb_rabbitmq.py`)
   - Implements the same interface as `SocketInterface` from the socket-based implementation
   - Manages three communication channels (CTRL, SEND, RECV) using RabbitMQ queues
   - Provides both synchronous and asynchronous message operations
   - Handles connection management and error recovery

2. **NSB Clients** (`nsb_rabbitmq.py`)
   - `NSBAppClient`: Application client for sending and receiving payloads
   - `NSBSimClient`: Simulator client for fetching and posting payloads
   - Both maintain identical semantics to the socket-based clients

3. **NSB Daemon** (`nsb_daemon.py`)
   - Separate service that handles configuration management
   - Listens for INIT requests from clients and responds with system-wide configuration
   - Handles PING and EXIT messages
   - Runs independently and can be started/stopped separately

4. **Unified Client** (`nsb_unified.py`)
   - Drop-in replacement that supports both socket and RabbitMQ backends
   - Backend selected at runtime via the `backend` parameter (`"socket"` or `"rabbitmq"`)
   - Identical API to the standalone clients — no code changes needed when switching backends

### Queue Architecture

The implementation uses a topic-based routing scheme with the following queue naming convention:

```
nsb.{channel}.{client_id}
├── nsb.ctrl.{client_id}      # Control channel for INIT, PING, EXIT
├── nsb.send.{client_id}      # Send channel (for app clients)
├── nsb.recv.{client_id}      # Receive channel (for app/sim clients)
├── nsb.config.request        # Daemon configuration request queue
└── nsb.config.response.{client_id}  # Client-specific config response
```

### Message Flow

#### Client Initialization

```
1. Client creates NSBAppClient or NSBSimClient
2. Client sends INIT message to nsb.config.request queue
3. Daemon receives INIT, responds to nsb.config.response.{client_id}
4. Client receives config and initializes
5. Client is ready to send/receive or fetch/post
```

#### Message Sending (App Client)

```
1. App Client calls send(dest_id, payload)
2. Message is published with routing_key=nsb.recv.{dest_id}
3. RabbitMQ broker routes to destination's RECV queue
4. Destination client calls receive()
5. Message consumed directly from RECV queue (no daemon mediation necessary)
```

#### Message Receiving (App Client)

```
1. App Client calls receive(timeout=5)
2. Client consumes directly from nsb.recv.{client_id} queue
3. Message returned as MessageEntry or None on timeout
4. No daemon polling required - direct RMQ queue consumption
```

#### Message Fetching (Simulator Client)

```
1. Simulator Client calls fetch()
2. Simulator consumes directly from nsb.recv.{simulator_id} queue
3. RabbitMQ broker has already routed SEND messages here
4. Message returned as MessageEntry
5. Simulator calls post() to return processed message
```

## Usage

### Starting the Daemon

```python
from nsb_daemon import NSBDaemon, NSBDaemonConfig

# Create configuration
config = NSBDaemonConfig(
    sys_mode=0,      # PULL mode
    sim_mode=0,      # SYSTEM_WIDE mode
    use_db=False,    # Disable database
    db_address="localhost",
    db_port=6379
)

# Create and start daemon
daemon = NSBDaemon("localhost", 5672, config)
daemon.start()
```

Or run directly:

```bash
python nsb_daemon.py
```

### Creating an Application Client (RabbitMQ-only module)

```python
from nsb_rabbitmq import NSBAppClient

# Create client
client = NSBAppClient("app_node_0", "localhost", 5672)

# Send a message
client.send("app_node_1", b"Hello, World!")

# Receive a message
msg = client.receive(timeout=5)
if msg:
    print(f"Received from {msg.src_id}: {msg.payload}")

# Cleanup
client.exit()
```

### Creating a Simulator Client (RabbitMQ-only module)

```python
from nsb_rabbitmq import NSBSimClient

# Create client
sim = NSBSimClient("simulator", "localhost", 5672)

# Fetch a message to simulate
msg = sim.fetch(timeout=5)
if msg:
    print(f"Simulating: {msg.src_id} -> {msg.dest_id}")
    # Process the message...
    
    # Post the result back
    sim.post(msg.src_id, msg.dest_id, b"Processed payload")

# Cleanup
sim.exit()
```

### Using the Unified Client (recommended)

The unified client (`nsb_unified.py`) lets you switch between socket and RabbitMQ backends with a single parameter:

```python
import nsb_unified as nsb

# RabbitMQ backend
app = nsb.NSBAppClient("app_node_0", "localhost", 5672, backend="rabbitmq")
app.send("app_node_1", b"Hello, World!")
msg = app.receive(timeout=5)
app.exit()

# Socket backend — same code, different backend
app = nsb.NSBAppClient("app_node_0", "localhost", 5555, backend="socket")
```

You can also select the backend via environment variable:

```bash
export NSB_BACKEND=rabbitmq
export NSB_PORT=5672
```

### Asynchronous Operations

```python
import asyncio
from nsb_rabbitmq import NSBAppClient

async def listen_for_messages():
    client = NSBAppClient("listener", "localhost", 5672)
    
    while True:
        msg = await client.listen()
        if msg:
            print(f"Received: {msg.payload}")

# Run async listener
asyncio.run(listen_for_messages())
```

## Configuration

### System Modes

- **PULL Mode (0)**: Clients poll the daemon/broker for messages. Recommended for most configurations.
- **PUSH Mode (1)**: Daemon automatically forwards messages to clients. Better latency but may not work with all configurations.

### Simulator Modes

- **SYSTEM_WIDE (0)**: Single simulator client fetches all messages. Good for top-down simulators (ns-3).
- **PER_NODE (1)**: Each node has its own simulator client. Good for bottom-up simulators (OMNeT++).

### Database Configuration

The implementation supports optional Redis database storage for large payloads:

```python
config = NSBDaemonConfig(
    use_db=True,
    db_address="localhost",
    db_port=6379,
    db_num=0
)
```

When enabled, large payloads are stored in Redis and only the key is transmitted via RabbitMQ.

## Cross-Language Interoperability

Python and C++ clients are **fully wire-compatible**. They use the same protobuf message format (`nsb.proto`) and the same RabbitMQ queue/exchange naming conventions (`nsb.{channel}.{client_id}`). This means:

- A **Python `NSBAppClient`** can send messages to a **C++ `NSBAppClientRMQ`** (and vice versa) through the same broker.
- Either the **Python daemon** (`nsb_daemon.py`) or the **C++ daemon** can serve INIT requests to clients of either language.
- Client IDs must be unique across all languages sharing the same broker.

See `cpp/rabbit/README.md` for C++ build instructions and API reference.

## Architecture Differences from Socket Implementation

### Similarities

- Maintains identical 3-channel architecture (CTRL, SEND, RECV)
- Uses same protobuf message format (nsbm)
- Supports both PULL and PUSH modes
- Supports SYSTEM_WIDE and PER_NODE simulator modes
- Optional Redis database integration
- Same client API (send, receive, fetch, post, listen)

### Differences from Socket Implementation

- **Transport**: RabbitMQ queues instead of TCP sockets
- **Routing**: Broker handles routing instead of daemon
- **Daemon Role**: Configuration service instead of message router
- **Scalability**: Better horizontal scaling with RabbitMQ
- **Async**: Uses asyncio-based polling instead of socket select

## Dependencies

- `pika`: RabbitMQ Python client
- `redis`: Redis Python client (optional, for database support)
- `proto.nsb_pb2`: NSB protobuf definitions

Install dependencies:

```bash
pip install pika redis
```

## Testing

### Test Suite

A comprehensive test suite is available in `tests/test_unified.py`. It covers Redis integration, PULL/PUSH modes, backend switching, simulator operations, and includes latency benchmarks.

```bash
# Start RabbitMQ broker and daemon first
python3 nsb_daemon.py

# Run tests (RabbitMQ backend; socket tests disabled by default)
cd tests
python3 test_unified.py
```

See `tests/testing.txt` for full prerequisites, environment variables, and troubleshooting.

### Examples

- **`example_usage.py`** — 5 standalone examples using the RabbitMQ-only module
- **`tests/example_unified.py`** — 7 examples using the unified client with both backends

```bash
# Standalone RabbitMQ examples
python3 example_usage.py

# Unified client examples
cd tests
python3 example_unified.py
```

### Quick Inline Test

```python
from nsb_rabbitmq import NSBAppClient, NSBSimClient
import time

# Start daemon first: python nsb_daemon.py

# Create clients
app = NSBAppClient("app", "localhost", 5672)
sim = NSBSimClient("sim", "localhost", 5672)

# Send message
app.send("app", b"test payload")

# Fetch and post
msg = sim.fetch(timeout=5)
if msg:
    sim.post(msg.src_id, msg.dest_id, msg.payload)

# Receive
received = app.receive(timeout=5)
print(f"Received: {received.payload if received else 'Nothing'}")

# Cleanup
app.exit()
sim.exit()
```
