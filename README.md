# Getting Started 

## Overview

This Slack bot downloads files users upload in a channel, indexes their contents with a RAG pipeline, and keeps the knowledge base up to date automatically. Users can then ask questions in the channel and the bot answers using uploaded documents as context.
## Running locally

### 1. Setup environment variables

```zsh
# Replace with your tokens
export SLACK_BOT_TOKEN=<your-bot-token>
export SLACK_APP_TOKEN=<your-app-level-token>
export OPENAI_API_KEY=<your-openai-key>
```

### 2. Setup your local project

```zsh
# Clone this project onto your machine
git clone https://github.com/anishka-v/mcp-issue-orchestrator

# Change into this project
cd mcp-issue-orchestrator

# Setup virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install the dependencies
pip install -r requirements.txt
```

### 3. Start servers

```zsh
python3 app.py
```

