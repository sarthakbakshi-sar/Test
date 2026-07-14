#!/bin/bash
ENV_FILE="$CLAUDE_PROJECT_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    while IFS='=' read -r key val; do
        [[ -z "$key" || "$key" =~ ^# ]] && continue
        echo "export $key=\"$val\"" >> "$CLAUDE_ENV_FILE"
    done < "$ENV_FILE"
    echo "Telegram ready"
else
    echo "No .env found. Run: bash setup.sh"
fi
