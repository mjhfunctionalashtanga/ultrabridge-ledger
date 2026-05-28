#!/bin/bash
# Deploy the Ultrabridge Ledger Processor
# Run from local: bash deploy.sh <user@host>
#
# Example: bash deploy.sh root@72.62.86.151

set -e

REMOTE="${1:?Usage: bash deploy.sh user@host}"
REMOTE_DIR="/opt/ultrabridge-ledger"

echo "==> Creating remote directory"
ssh "$REMOTE" "mkdir -p $REMOTE_DIR"

echo "==> Uploading processor"
scp "$(dirname "$0")/process-ledger.py" "$REMOTE:$REMOTE_DIR/process-ledger.py"

echo "==> Uploading .env.example"
scp "$(dirname "$0")/.env.example" "$REMOTE:$REMOTE_DIR/.env.example"

echo "==> Setting up Python venv and installing dependencies"
ssh "$REMOTE" "
apt-get install -y -qq python3-venv 2>/dev/null
cd $REMOTE_DIR
python3 -m venv venv 2>/dev/null || true
$REMOTE_DIR/venv/bin/pip install --quiet requests Pillow anthropic
"

echo "==> Setting up cron (every 15 minutes)"
ssh "$REMOTE" "
crontab -l 2>/dev/null | grep -v 'process-ledger' | crontab -
(crontab -l 2>/dev/null; echo '*/15 * * * * cd $REMOTE_DIR && $REMOTE_DIR/venv/bin/python3 process-ledger.py >> /var/log/ledger-processor.log 2>&1') | crontab -
"

echo ""
echo "==> Done! Next steps:"
echo "   1. SSH in: ssh $REMOTE"
echo "   2. Copy and fill .env: cp $REMOTE_DIR/.env.example $REMOTE_DIR/.env"
echo "   3. Set ANTHROPIC_API_KEY and UB_TASKS_PASS in .env"
echo "   4. Test: cd $REMOTE_DIR && venv/bin/python3 process-ledger.py"
