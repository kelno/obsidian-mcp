#!/bin/sh
podman run --rm --privileged --tmpfs /tmp --network omcp-test \
  -e PGVECTOR_TEST_ADMIN_URL=postgresql+asyncpg://postgres:test@omcp-test-pg:5432/postgres \
  -e TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@omcp-test-pg:5432/postgres \
  -v "/d/work/obsidian-mcp:/repo:ro" omcp-test sh -c \
  "mkdir /tmp/r && tar -C /repo --exclude=.git -cf - . | tar -C /tmp/r -xf - && cd /tmp/r && python -m pytest $*"
