# DiscordFTCAgent codebase structure

*Current implementation snapshot as of August 31, 2026; architectural boundaries follow `CLAUDE.md`.*

---

## 🗂️ Current structure

```mermaid
flowchart TB
    accTitle: Current Discord FTC Agent Structure
    accDescr: Implemented codebase layers showing burst-aware Discord intake, channel-agnostic ingest policy, persistence and model services, notebook export, and development checks. The dashed export path marks the caller that does not exist yet.

    team(["👥 FTC team in Discord"])

    subgraph channel_layer ["🌐 Channel adapter"]
        discord_bot["channels/discord_bot.py<br/>Routes ambient, /log, and replies"]
    end

    subgraph core_layer ["⚙️ Channel-agnostic core"]
        inbox["core/inbox.py<br/>Burst coalescing"]
        pipeline["core/pipeline.py<br/>Ingest and reply policy"]
        triage["core/triage.py<br/>Cheap pre-filter"]
        followup["core/followup.py<br/>Merge and stop gates"]
        agent["core/agent.py<br/>Parse, log, and merge entry points"]
        prompts[["core/prompts/*.md<br/>Ambient, /log, and reply prompts"]]
        schema["core/schema.py<br/>Records, enums, and follow-up ledger"]
        storage["core/storage.py<br/>Persistence and semantic search"]
    end

    subgraph output_layer ["📤 Output adapter"]
        export_gap["⚠️ No storage-to-export caller<br/>scripts/export.py is absent"]
        notebook["exporters/notebook.py<br/>Logged entries to Markdown"]
    end

    subgraph external_services ["🔌 External services"]
        deepseek(["🧠 DeepSeek chat API"])
        postgres[("💾 PostgreSQL and pgvector")]
        embeddings(["🔗 Optional embedding API"])
    end

    subgraph development_tools ["🧪 Development checks"]
        smoke["scripts/Smoke.py<br/>Ambient and reply smoke path"]
        scoring["scripts/try_parse.py<br/>Prompt scoring harness"]
        conversation_scoring["scripts/try_conversation.py<br/>Rounds and nags"]
        samples["tests/samples.py<br/>Fifteen invented samples"]
        conversations["tests/conversations.py<br/>Invented conversation fixtures"]
        tests["tests/test_core.py<br/>Offline policy checks"]
    end

    team --> discord_bot
    discord_bot -->|ambient bursts| inbox
    inbox -->|flush complete burst| pipeline
    discord_bot -->|/log and replies| pipeline

    pipeline --> agent
    pipeline --> storage
    pipeline --> triage
    pipeline --> followup

    agent -->|loads| prompts
    agent -->|validates output| schema
    agent -->|model calls| deepseek
    followup -->|merges records| schema
    storage -->|validates rows| schema
    storage -->|read and write| postgres
    storage -.->|optional vectors| embeddings

    storage -.-> export_gap
    export_gap -.-> notebook
    notebook -->|reads LoggedEntry| schema

    smoke --> agent
    smoke --> schema
    scoring --> agent
    scoring --> samples
    samples --> schema
    conversation_scoring --> agent
    conversation_scoring --> triage
    conversation_scoring --> followup
    conversation_scoring --> conversations
    conversations --> schema
    tests --> triage
    tests --> inbox
    tests --> followup
    tests --> storage
    tests --> schema
    tests --> notebook

    classDef adapter fill:#dbeafe,stroke:#2563eb,stroke-width:2px,color:#1e3a5f
    classDef core fill:#ede9fe,stroke:#7c3aed,stroke-width:2px,color:#3b0764
    classDef external fill:#f3f4f6,stroke:#6b7280,stroke-width:2px,color:#1f2937
    classDef warning fill:#fef9c3,stroke:#ca8a04,stroke-width:2px,color:#713f12

    class discord_bot,notebook adapter
    class inbox,pipeline,triage,followup,agent,prompts,schema,storage core
    class team,deepseek,postgres,embeddings,smoke,scoring,conversation_scoring,samples,conversations,tests external
    class export_gap warning
```

## 📌 Boundary notes

- `channels/discord_bot.py` owns Discord-specific input/output and burst wiring; `core/pipeline.py` owns ingest and reply decisions
- `core/inbox.py` buffers opaque items and has no Discord dependency
- `core/agent.py` is the only module that knows the chat-model provider; `core/storage.py` alone owns PostgreSQL, pgvector, and the optional embedding provider
- `exporters/notebook.py` is a pure transform and does not query storage
- The export pipeline is not connected in the current tree because `scripts/export.py` has not been implemented
- `tests/samples.py` and `tests/conversations.py` both contain invented fixtures rather than real team data
