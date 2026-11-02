FROM python:3.12-alpine AS builder
RUN apk add --no-cache build-base linux-headers
WORKDIR /build
COPY . .
RUN pip wheel --no-cache-dir --wheel-dir=/wheels .

FROM python:3.12-alpine
RUN addgroup -S mcp && adduser -S -G mcp -u 10001 mcp
WORKDIR /app
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels
USER 10001
EXPOSE 8712
ENTRYPOINT ["mcp-server-shieldtv", "serve", "--http"]
