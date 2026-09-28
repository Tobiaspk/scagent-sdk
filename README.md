# scagent-sdk

A skill-driven single-cell RNA-seq analysis agent.

## Setup

```bash
git clone git@github.com:hussenmi/scagent-sdk.git
cd scagent-sdk
source setup_gpu.sh
```

The first run builds the compute environments and may take a while.

## Run

The interactive interface is [scagent-oc-spike](https://github.com/hussenmi/scagent-oc-spike); follow its setup, then from your analysis directory:

```bash
scagent
```

Optional: put `TAVILY_API_KEY` in `.env` (see `.env.example`) to enable web research.
