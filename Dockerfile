# Go build of qoderbuddy2api. Single static binary, no interpreter.
#
# The image is built from the repository root context so it can also carry the
# pre-built admin console (web/dist).

# ---- builder ----
FROM golang:1.27-alpine AS builder
WORKDIR /build

# The module proxy is overridable so a restricted network can point at a mirror.
ARG GOPROXY=https://proxy.golang.org,direct
ENV GOPROXY=${GOPROXY} CGO_ENABLED=0 GOOS=linux

# Dependencies are resolved before the sources are copied so this layer survives
# ordinary code edits.
COPY go.mod go.sum ./
RUN go mod download

COPY cmd ./cmd
COPY internal ./internal
RUN go build -trimpath -ldflags="-s -w" -o /out/qb2api ./cmd/qb2api

# ---- runtime ----
FROM alpine:3.22
# ca-certificates: upstream HTTPS. tzdata: the check-in scheduler resolves
# Asia/Shanghai by name, and a missing zone silently falls back to UTC.
RUN apk add --no-cache ca-certificates tzdata \
 && adduser -D -u 10001 qb2api

WORKDIR /app
COPY --from=builder /out/qb2api /app/qb2api
# The admin console is served from this directory (resolveWebDir checks next to
# the executable first).
COPY web/dist /app/web/dist

# Data, logs and the model config arrive as volumes.
VOLUME ["/data", "/logs", "/config"]
EXPOSE 9998

USER qb2api
ENTRYPOINT ["/app/qb2api"]
