# TechLingo Agent Framework

A framework for building and deploying AI agents.

## Getting Started

### Prerequisites

- Python 3.10+
- pip

### Installation

```bash
pip install -r requirements.txt
```

### Usage

```bash
# 1) Create .env from the template and set your key
cp example.env .env

# 2) Run the A1–A5 workflow on the included sample input
python main.py run --input-file sample.txt --out-dir outputs
```

Notes:
- You must set both `OPENAI_API_KEY` and `OPENAI_CHAT_MODEL_ID` in `.env` (or pass `--model-id`).

### Configuration

You can customize the course structure (number of modules, lessons, exercises, etc.) by modifying the `workflow_config.json` file in the root directory.

To use a different configuration file:

```bash
python main.py run --input-file sample.txt --config my_config.json
```

Default configuration (`workflow_config.json`):
```json
{
  "modules_count": 6,
  "min_lessons_total": 20,
  "max_lessons_total": 25,
  "exercises_per_lesson": 8,
  ...
}
```

### Outputs
Each run writes a folder under `outputs/run-YYYYMMDD-HHMMSS/` containing:
- `course.json` (final structured output)
- `course.md` (human-readable outline)
- `validation_report.json` (constraint checks)
- `artifacts/` (A1–A5 intermediate JSON)

## Simple UI (browse + quiz)

```bash
streamlit run ui/app.py
```

## Development

```bash
# Install development dependencies
pip install -r requirements-dev.txt

# Run tests
pytest
```

## License

MIT

