# NSB RabbitMQ C++ Implementation

C++ client library for NSB using RabbitMQ as the message transport. Provides `NSBAppClientRMQ` (application send/receive) and `NSBSimClientRMQ` (simulator fetch/post), plus a standalone C++ daemon.

## Table of Contents
- [Prerequisites](#prerequisites)
  - [macOS (Homebrew)](#macos-homebrew)
  - [RabbitMQ Broker](#rabbitmq-broker)
- [Build](#build)
- [Run](#run)
  - [RabbitMQ backend](#rabbitmq-backend)
  - [Socket backend](#socket-backend)
- [API Overview](#api-overview)
  - [Class Hierarchy](#class-hierarchy)
  - [NSBAppClientRMQ](#nsbappclientrmq)
  - [NSBSimClientRMQ](#nsbsimclientrmq)
  - [NSBDaemonRMQ](#nsbdaemonrmq)
  - [Config & MessageEntry](#config--messageentry)

## Prerequisites

### macOS (Homebrew)

```bash
brew install rabbitmq-c
brew install simple-amqp-client
```

Protobuf generated files are already present under `cpp/proto`.

### RabbitMQ Broker

Start a broker if one is not already running:

```bash
# Docker (recommended)
docker run -d --name rabbitmq -p 5672:5672 -p 15672:15672 rabbitmq:4.1-management

# Or via Homebrew
brew services start rabbitmq
```

## Build

From the repository root:

```bash
mkdir -p cpp/rabbit/build && cd cpp/rabbit/build
cmake ..
make -j
```

Build artifacts:

- **`libnsb_rabbitmq_cpp.a`** — RabbitMQ-only static library
- **`libnsb_unified_cpp.a`** — unified static library (socket + RabbitMQ backends)
- **`example_send_recv`** — demo: two RabbitMQ app clients exchange a message
- **`example_daemon`** — standalone C++ RabbitMQ configuration daemon
- **`example_unified`** — demo: unified client with backend selection via CLI arg

## Run

### RabbitMQ backend

1. Start the RabbitMQ broker (see above).
2. Start a daemon (choose one):
   - **Python daemon** (recommended): `python3 rabbit/nsb_daemon.py`
   - **C++ daemon**: `./cpp/rabbit/build/example_daemon`
3. Run an example:
   ```bash
   ./cpp/rabbit/build/example_send_recv
   # or
   ./cpp/rabbit/build/example_unified rabbitmq
   ```

### Socket backend

The socket backend connects via raw TCP to the **main C++ socket daemon** (built from the top-level project, not from `cpp/rabbit/`). You must build and run that daemon first:

```bash
# Build the main NSB project (from repo root)
mkdir -p build && cd build
cmake ..
make -j

# Start the socket daemon (default port 5555)
./bin/nsb_daemon ../config.yaml
```

Then in another terminal:

```bash
./cpp/rabbit/build/example_unified socket
```


## API Overview

### Class Hierarchy

```
NSBClientRMQ              (base: INIT, PING, EXIT, config)
├── NSBAppClientRMQ       (send, receive, listen)
└── NSBSimClientRMQ       (fetch, post, listen)
```

### NSBAppClientRMQ

```cpp
#include "NSBAppClientRMQ.hpp"

nsb::NSBAppClientRMQ app("my_app", "localhost", 5672);
app.initialize();                       // INIT handshake with daemon

app.send("dest_id", "payload bytes");   // send to another client

nsb::MessageEntry msg;
if (app.receive(msg, /*timeout_sec=*/5)) {
    std::cout << msg.src_id << ": " << msg.payload << std::endl;
}

// Async listen (PUSH mode / background receive)
app.listen([](const nsb::MessageEntry& m) {
    std::cout << "Got: " << m.payload << std::endl;
});
app.stop_listen();
```

### NSBSimClientRMQ

```cpp
#include "NSBSimClientRMQ.hpp"

nsb::NSBSimClientRMQ sim("my_sim", "localhost", 5672);
sim.initialize();

nsb::MessageEntry msg;
if (sim.fetch(msg, /*timeout_sec=*/5)) {
    // simulate network effects...
    sim.post(msg.src_id, msg.dest_id, msg.payload);
}

// Async listen
sim.listen([](const nsb::MessageEntry& m) { /* ... */ });
sim.stop_listen();
```

### NSBDaemonRMQ

```cpp
#include "NSBDaemonRMQ.hpp"

nsb::NSBDaemonRMQ daemon("localhost", 5672);
daemon.set_config(/*sys_mode=*/0, /*sim_mode=*/0, /*use_db=*/false);
daemon.start();   // runs in background thread
// ...
daemon.stop();
```

### Config & MessageEntry

```cpp
// Filled after initialize()
const nsb::Config& cfg = app.config();
// cfg.sys_mode, cfg.sim_mode, cfg.use_db, cfg.db_address, cfg.db_port, cfg.db_num

// Returned by receive() / fetch()
struct MessageEntry {
    std::string src_id;
    std::string dest_id;
    std::string payload;
};
```

## Linking in Your Own Project

Add to your CMakeLists.txt:

```cmake
add_subdirectory(path/to/cpp/rabbit)
target_link_libraries(your_target nsb_rabbitmq_cpp)
```

Or link the static library directly:

```cmake
target_link_libraries(your_target
    /path/to/libnsb_rabbitmq_cpp.a
    SimpleAmqpClient rabbitmq protobuf
)
target_include_directories(your_target PRIVATE /path/to/cpp/rabbit/include)
```

## Cross-Language Interoperability

C++ and Python clients are **fully wire-compatible**. They use the same protobuf message format (`nsb.proto`) and the same RabbitMQ queue/exchange naming conventions. This means:

- A **C++ `NSBAppClientRMQ`** can send messages to a **Python `NSBAppClient`** (and vice versa) through the same broker.
- Either the **Python daemon** (`rabbit/nsb_daemon.py`) or the **C++ daemon** (`example_daemon`) can serve INIT requests to clients of either language.
- All clients share the same queue namespace (`nsb.{channel}.{client_id}`), so client IDs must be unique across all languages.

## File Index

```
cpp/rabbit/
├── CMakeLists.txt                  # Build configuration
├── README.md                       # This file
├── include/
│   ├── RabbitMQInterface.hpp       # Low-level RabbitMQ transport
│   ├── NSBClientRMQ.hpp            # Base client (Config, MessageEntry, INIT/PING/EXIT)
│   ├── NSBAppClientRMQ.hpp         # Application client (send/receive/listen)
│   ├── NSBSimClientRMQ.hpp         # Simulator client (fetch/post/listen)
│   └── NSBDaemonRMQ.hpp            # Standalone daemon
├── src/
│   ├── RabbitMQInterface.cpp
│   ├── NSBClientRMQ.cpp
│   ├── NSBAppClientRMQ.cpp
│   ├── NSBSimClientRMQ.cpp
│   └── NSBDaemonRMQ.cpp
└── examples/
    ├── example_send_recv.cpp       # App client demo
    └── example_daemon.cpp          # Daemon demo
```
