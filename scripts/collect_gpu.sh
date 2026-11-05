#!/usr/bin/env bash
# Hand back what the GPU server holds, before destroy: the engine's and the
# Collector's logs, vLLM's /metrics now, the boot log and the 5-second records
# (gpu.csv, vmstat.log). None of it can be had once the server is gone.
#
#   bash scripts/collect_gpu.sh        # from the repository, with infra/ applied
#
# The server copies its /opt/out to s3://<artifacts bucket>/runs/<instance id>/
# (the only place its role may write), and this script copies that to
# out/gpu/<instance id>/.
set -euo pipefail
cd "$(dirname "$0")/.."

ID=$(terraform -chdir=infra output -raw gpu_instance_id)
BUCKET=agent-latency-tracer-artifacts-$(aws sts get-caller-identity --query Account --output text)
DEST=s3://$BUCKET/runs/$ID

REMOTE="set -e; cd /opt/out
docker logs vllm > vllm.log 2>&1 || true
docker logs collector > collector.log 2>&1 || true
curl -s localhost:8000/metrics > metrics-\$(date -u +%Y%m%dT%H%M%SZ).txt || true
cp /var/log/cloud-init-output.log boot.log || true
aws s3 sync /opt/out $DEST --only-show-errors
ls -l /opt/out"

CMD_ID=$(aws ssm send-command --instance-ids "$ID" --document-name AWS-RunShellScript \
  --parameters "$(python3 -c 'import json,sys; print(json.dumps({"commands": [sys.argv[1]]}))' "$REMOTE")" \
  --query Command.CommandId --output text)
while :; do
  STATUS=$(aws ssm get-command-invocation --command-id "$CMD_ID" --instance-id "$ID" --query Status --output text 2>/dev/null || echo Pending)
  case "$STATUS" in Pending|InProgress|Delayed) sleep 5 ;; *) break ;; esac
done
aws ssm get-command-invocation --command-id "$CMD_ID" --instance-id "$ID" \
  --query '[Status, StandardOutputContent, StandardErrorContent]' --output text
[ "$STATUS" = Success ] || { echo "collect failed on the server: $STATUS" >&2; exit 1; }

mkdir -p "out/gpu/$ID"
aws s3 sync "$DEST" "out/gpu/$ID" --only-show-errors
echo "collected into out/gpu/$ID"
