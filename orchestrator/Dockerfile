# ARM64 container for the orchestrator on Amazon Bedrock AgentCore Runtime.
FROM python:3.13-slim-bookworm
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOCKER_CONTAINER=1 \
    AWS_REGION=us-east-1 \
    AWS_DEFAULT_REGION=us-east-1

COPY requirements.txt requirements.txt
# aws-opentelemetry-distro (ADOT) auto-instruments boto3/bedrock-runtime so model
# calls emit gen_ai spans. Its bundled LangChain instrumentation is disabled at
# runtime via OTEL_PYTHON_DISABLED_INSTRUMENTATIONS (set by Terraform) — see the
# note in terraform/main.tf.
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir aws-opentelemetry-distro==0.19.0

EXPOSE 8080

RUN useradd -m -u 1000 bedrock_agentcore
USER bedrock_agentcore

# Ship the application package (agents, graph, runtime, config).
COPY app ./app

# Dispatches on AGENT_ID: orchestrator (unset) or a single dedicated agent (set).
CMD ["opentelemetry-instrument", "python", "-m", "app.entry"]
