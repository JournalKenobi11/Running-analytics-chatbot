# 🧠 Engineering Guide: Architecture, Concepts & Mental Models

This document breaks down the software engineering concepts, design patterns, and architectural principles behind the **Dreeve Running Analytics Suite**.

---

## 1. Clearing Up the Big Confusion: Why MCP vs. Docker Chat?

If the Docker web chatbot runs on CPU, **why does MCP exist? Does the chatbot use MCP?**

> [!IMPORTANT]
> **No, the Docker web chatbot does NOT use MCP.**
> The web chatbot talks directly to Python (`import dreeve_engine`).

Here is the exact difference:

| Interface | Who is using it? | How does it communicate? | Does it need MCP? | Does it need an LLM? |
| :--- | :--- | :--- | :--- | :--- |
| **🐳 Docker Web Chatbot** (`app.py`) | **You (Human)** typing in a web browser | Direct Python function calls (`import dreeve_engine`) | ❌ **No** | ❌ **No** (Runs on local CPU) |
| **🔌 MCP Server** (`dreeve_mcp.py`) | **AI Coding Assistants** (Antigravity, Claude, ChatGPT) | Standardized JSON-RPC protocol over `stdio` | ✅ **Yes** | ✅ **Yes** (The external AI is the LLM) |
| **💻 CLI** (`dreeve-cli`) | **Terminal / Shell scripts** | Direct Python CLI execution via `argparse` | ❌ **No** | ❌ **No** (Runs on local CPU) |

### The "USB Port" Analogy:
* **`dreeve_engine.py` is the Hard Drive**: It has all your data, formulas, algorithms, and charts.
* **The Web Chatbot is a Monitor plugged straight into the hard drive**: You sit in front of it, press buttons, and see your data.
* **MCP is a Universal USB Port**: It lets an external visitor (like Google Antigravity or Claude) plug into your hard drive and ask questions on your behalf during pair programming.

---

## 2. What Is This Full-Stack Pattern Called?

This system combines several renowned software engineering paradigms:

### A. Hexagonal Architecture (Ports and Adapters)
In traditional messy code, database calls, UI code, and business math are tangled together. 
In **Hexagonal Architecture**:
1. **The Core (The Hexagon)**: `dreeve_engine.py` contains **pure business logic** (interval detection, cardiac drift math, telemetry decompression). It does not know or care if a browser, an AI, or a terminal is calling it.
2. **The Adapters (The Ports)**:
   - **Port 1 (Web UI Adapter)**: `app.py` translates user clicks into engine calls.
   - **Port 2 (Protocol Adapter)**: `dreeve_mcp.py` translates AI tool calls into engine calls.
   - **Port 3 (CLI Adapter)**: `dreeve-cli` translates terminal flags into engine calls.
   - **Port 4 (Storage Adapter)**: SQLite driver reading `dreeve.db`.

```text
                  +---------------------------+
                  |   Web Browser (Human)     |
                  +-------------+-------------+
                                |
                         [ Streamlit Port ]
                                |
+------------------+            v            +-------------------+
|  AI Assistant    | -> [ MCP Server Port ]  |   Terminal User   | -> [ CLI Port ]
| (Antigravity/    |                         |  (Bash / Cron)    |
|  Claude)         |                         +-------------------+
+------------------+            |
                                v
               +---------------------------------+
               |       CORE DOMAIN ENGINE        |
               |       (dreeve_engine.py)        |
               |                                 |
               |  - Stream Decompression (Zstd)  |
               |  - Interval Segmentation Math   |
               |  - Cardiac Drift Algorithms     |
               +----------------+----------------+
                                |
                       [ Storage Adapter ]
                                |
                                v
               +---------------------------------+
               |   Dreeve SQLite DB (Read-Only)  |
               +---------------------------------+
```

### B. "Local-First" & "Zero-Cloud / Sovereign Computing"
- **Zero API bills**: No external API keys or cloud tokens.
- **Data Sovereignty**: Sensitive health data (heart rate, cadence, GPS location) never touches third-party servers.
- **Edge Computing**: All processing happens on your local CPU where the data already lives.

### C. Agentic Tool Integration (MCP - Model Context Protocol)
- **Problem**: Large Language Models (LLMs) hallucinate and cannot access private databases. Sending raw SQL tables blows past token limits.
- **MCP Solution**: An open standard that gives LLMs "tools". The LLM inspects the tool schema (`dreeve_intervals`), decides when to call it, and receives a compact ~150-token payload instead of 50,000 raw rows.

### D. Time-Series Telemetry & Digital Signal Processing (DSP)
- Sensor data recorded every second creates noisy streams (instant pace spikes, GPS jitter).
- The engine uses **signal processing techniques** (rolling moving averages, hysteresis, and threshold segmentation) to detect intervals and cardiac drift accurately.

---

## 3. How the Natural Language Workout Parser Works

When you type:
> *"1k warmup + 4x1K @6:15 with 90s walk + 0.5k cooldown"*

It doesn't use a heavy neural network. It uses **Deterministic Grammar / Regex Parsing**:
1. **Tokenization**: Breaks the string by delimiters (`+`, `,`, `then`, `with`).
2. **Multiplier Extraction**: Detects repetition patterns like `4x1K` or `2*1.5k` ($\text{count} \times \text{distance}$).
3. **Pace & Rest Matching**:
   - Parses target paces (e.g. `@6:15` $\rightarrow 375 \text{ seconds/km}$).
   - Parses rest duration (e.g. `90s walk` $\rightarrow 90 \text{ seconds}$).
4. **Workout Plan Compilation**: Constructs an ordered sequence of target segments:
   - `[Warmup: 1000m]` $\rightarrow$ `[Work 1: 1000m @ 6:15]` $\rightarrow$ `[Rest 1: 90s]` $\rightarrow \dots \rightarrow$ `[Cooldown: 500m]`.
5. **Stream Alignment**: Scans the actual sensor timestamps in the database, maps elapsed meters, and computes deltas between your plan and your real execution.

---

## 4. Key Terminology Cheat Sheet

| Term | Simple Definition | Where It Lives in this Project |
| :--- | :--- | :--- |
| **Model Context Protocol (MCP)** | An open protocol allowing AI models to securely invoke external functions/tools. | `dreeve_mcp.py` |
| **Streamlit** | A rapid Python framework that turns data scripts into interactive reactive web UIs. | `app.py` |
| **Docker Compose** | A tool for defining and running multi-container Docker applications via YAML. | `docker-compose.yml` |
| **Zstandard (zstd)** | High-speed, high-ratio compression algorithm used by Dreeve to store sensor streams. | Used in `dreeve_engine.py` |
| **Cardiac Drift** | The upward creep of heart rate over time despite running at a steady pace (aerobic decoupling). | Computed in `get_splits_analysis()` |
| **WAL Mode** | "Write-Ahead Logging" in SQLite, allowing concurrent readers without locking the database. | Handled via SQLite URI `?mode=ro` |

---

## 5. Summary Mental Model

Think of this project as a **Swiss Army Knife for your Running Data**:
1. **The Blade (Engine)**: Does all the cutting, math, and plotting.
2. **The Screwdriver Handle (CLI)**: You use it directly in terminal.
3. **The Flashlight (Streamlit Web Chat)**: You view and interact with it in your browser.
4. **The Bluetooth Link (MCP Server)**: Allows external AI agents to operate the tool for you.
