#!/usr/bin/env bash
# Run once on the temporary GPU server (infra/gpu_prepare/), as root. Moves the
# images and weights into infra/artifacts/, then tries the engine the way the
# VPC will run it: image from ECR, weights from S3, spans through the Collector.
#
#   bash gpu_prepare.sh <bucket> <otel-collector.yaml>
#
# Needs the AWS CLI, to fetch this script in the first place.
#
# Everything measured goes to $OUT and then to s3://<bucket>/prepare/out/, since
# the server and its disk are thrown away.
set -euo pipefail

BUCKET=$1
COLLECTOR_CONFIG=$2

REGION=ap-northeast-1
VLLM_TAG=v0.31.0
COLLECTOR_TAG=0.163.1 # the newest release at least two weeks old on 2026-10-31
MODEL_REPO=Qwen/Qwen3-8B
MODEL_REVISION=b968826d9c46dd6066d109eabc6255188de91218 # the benchmark's
MAX_MODEL_LEN=16384

OUT=/root/out
PORT=8000
S3_MODEL=s3://$BUCKET/models/$MODEL_REPO/$MODEL_REVISION
mkdir -p "$OUT"

step() { echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }

# Save what was found so far, even if a later step fails.
save_out() {
  docker logs vllm > "$OUT/vllm.log" 2>&1 || true
  docker logs collector > "$OUT/collector.log" 2>&1 || true
  aws s3 sync "$OUT" "s3://$BUCKET/prepare/out/" --only-show-errors || true
}
trap save_out EXIT

step "AMI"
grep -E '^(PRETTY_NAME)=' /etc/os-release | tee "$OUT/os.txt"
nvidia-smi | tee "$OUT/nvidia-smi.txt"
docker --version | tee "$OUT/docker.txt"
aws --version

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGISTRY=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com
VLLM_IMAGE=$REGISTRY/agent-latency-tracer/vllm:$VLLM_TAG
COLLECTOR_IMAGE=$REGISTRY/agent-latency-tracer/otel-collector:$COLLECTOR_TAG

step "pull the public images"
docker pull -q "vllm/vllm-openai:$VLLM_TAG"
docker pull -q "otel/opentelemetry-collector-contrib:$COLLECTOR_TAG"

step "the GPU from a container"
docker run --rm --gpus all --entrypoint nvidia-smi "vllm/vllm-openai:$VLLM_TAG" | tee "$OUT/nvidia-smi-container.txt"

step "versions in the image"
docker run --rm --entrypoint python3 "vllm/vllm-openai:$VLLM_TAG" -c '
from importlib.metadata import PackageNotFoundError, version
for name in ("vllm", "transformers", "torch", "opentelemetry-sdk", "opentelemetry-exporter-otlp"):
    try:
        print(name, version(name))
    except PackageNotFoundError:
        print(name, "NOT INSTALLED")
' | tee "$OUT/image-versions.txt"

step "images to ECR"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"
docker tag "vllm/vllm-openai:$VLLM_TAG" "$VLLM_IMAGE"
docker tag "otel/opentelemetry-collector-contrib:$COLLECTOR_TAG" "$COLLECTOR_IMAGE"
docker push -q "$VLLM_IMAGE"
docker push -q "$COLLECTOR_IMAGE"
# From here on use only the ECR copies, as the VPC will.
docker rmi -f "vllm/vllm-openai:$VLLM_TAG" "$VLLM_IMAGE" "otel/opentelemetry-collector-contrib:$COLLECTOR_TAG" "$COLLECTOR_IMAGE"
docker pull -q "$VLLM_IMAGE"
docker pull -q "$COLLECTOR_IMAGE"

step "weights to S3"
mkdir -p /models/download
docker run --rm -v /models/download:/models/download --entrypoint python3 "$VLLM_IMAGE" -c "
from huggingface_hub import snapshot_download
snapshot_download('$MODEL_REPO', revision='$MODEL_REVISION', local_dir='/models/download')
"
aws s3 sync /models/download "$S3_MODEL" --exclude '.cache/*' --only-show-errors
du -sh /models/download | tee "$OUT/weights-size.txt"
rm -rf /models/download

step "weights back from S3, onto the instance store"
# Loading took 42 s from here and 580 s from the root volume (docs/DESIGN.md §8).
MODEL_DIR=/opt/dlami/nvme/Qwen3-8B
mkdir -p "$MODEL_DIR"
time aws s3 sync "$S3_MODEL" "$MODEL_DIR" --only-show-errors

step "start the Collector"
docker run -d --name collector --network host \
  -v "$COLLECTOR_CONFIG:/etc/otelcol-contrib/config.yaml:ro" \
  "$COLLECTOR_IMAGE"

step "start vLLM"
docker run -d --name vllm --network host --gpus all \
  -v "$MODEL_DIR:/models/Qwen3-8B:ro" \
  -e HF_HUB_OFFLINE=1 \
  -e OTEL_SERVICE_NAME=vllm \
  "$VLLM_IMAGE" \
  /models/Qwen3-8B --served-model-name "$MODEL_REPO" \
  --host 127.0.0.1 --port "$PORT" \
  --enable-auto-tool-choice --tool-call-parser hermes \
  --max-model-len "$MAX_MODEL_LEN" \
  --enable-per-request-metrics --enable-prompt-tokens-details \
  --otlp-traces-endpoint http://127.0.0.1:4317
# Up to 10 minutes; a start from the instance store took about 3.5.
for _ in $(seq 120); do
  if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null; then break; fi
  if [ "$(docker inspect -f '{{.State.Running}}' vllm)" != true ]; then
    echo "vLLM exited" >&2
    exit 1
  fi
  sleep 5
done
curl -sf "http://127.0.0.1:$PORT/health" >/dev/null
docker logs vllm 2>&1 | grep 'GPU KV cache size' | tee "$OUT/kv.txt" || true
nvidia-smi --query-gpu=memory.used,memory.total --format=csv | tee "$OUT/gpu-memory.txt"

step "one request with a traceparent"
# X-Ray-format trace ID: the first 8 hex digits are the time (docs/DESIGN.md §5).
TRACE_ID=$(printf '%08x' "$(date +%s)")$(openssl rand -hex 12)
PARENT_ID=$(openssl rand -hex 8)
echo "$TRACE_ID" | tee "$OUT/trace-id.txt"
BODY=$(cat <<EOF
{"model": "$MODEL_REPO",
 "messages": [{"role": "user", "content": "品目X001の在庫を調べて"}],
 "tools": [{"type": "function", "function": {"name": "find_records",
   "description": "Find records of one type.",
   "parameters": {"type": "object", "properties": {"type": {"type": "string"}}, "required": ["type"]}}}],
 "temperature": 0,
 "chat_template_kwargs": {"enable_thinking": false}}
EOF
)
post() { # $1: the output file; stdin: the JSON body
  curl -sS "http://127.0.0.1:$PORT/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -H "traceparent: 00-$TRACE_ID-$PARENT_ID-01" \
    -d @- | tee "$OUT/$1"
  echo
}
echo "$BODY" | post response.txt
# The client streams (system/llm/client.py), so its breakdown must be there too.
echo "$BODY" | python3 -c '
import json, sys
body = json.load(sys.stdin)
body.update(stream=True, stream_options={"include_usage": True})
print(json.dumps(body))
' | post response-stream.txt
curl -s "http://127.0.0.1:$PORT/metrics" > "$OUT/metrics.txt"

step "let the batched spans go out"
sleep 30

step "done"
touch "$OUT/DONE"
