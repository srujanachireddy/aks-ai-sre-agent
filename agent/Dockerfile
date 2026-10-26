# Small, non-root image for the AI SRE agent (webhook server + MCP server).
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install ".[kubernetes,azure]"

FROM python:3.12-slim
RUN useradd --uid 10001 --create-home sreagent
COPY --from=build /install /usr/local
USER 10001
EXPOSE 8080 8765
ENTRYPOINT ["sre-agent-server"]
