# Pin the same Go toolchain used by CI. Keep compilation on Amazon Linux so
# CGO-backed SQLite uses a libc compatible with the final runtime image.
FROM golang:1.26.9-bookworm@sha256:d9c68c2c51161e12fd77e4c6320687c9cd86e1af1e3ad6e6cd63ff970641453c AS go-toolchain

# Build Stage
FROM public.ecr.aws/amazonlinux/amazonlinux:2023 AS build-env

COPY --from=go-toolchain /usr/local/go /usr/local/go
ENV PATH="/usr/local/go/bin:${PATH}"

# Apply all available security updates before installing packages. Amazon Linux
# 2023 locks DNF to the release snapshot the base image was built from, so
# --releasever=latest is required to pick up newer patched packages. In this
# stage the pinned Go toolchain is copied from the official image above.
# https://docs.aws.amazon.com/linux/al2023/ug/deterministic-upgrades-usage.html
#
# We tell DNF not to install Recommends and Suggests packages, which are
# weak dependencies in DNF terminology, thus keeping our installed set of
# packages as minimal as possible.
RUN dnf --releasever=latest upgrade -q -y \
    && \
    dnf --setopt=install_weak_deps=False install -q -y \
    git \
    gcc \
    glibc-devel \
    && \
    dnf clean all

RUN mkdir -p "${GOPATH}/src" "${GOPATH}/bin" /appsrc

WORKDIR /appsrc

ENV GOPROXY=https://proxy.golang.org,direct
# Bound compiler parallelism on CI runners; these arguments are build-only.
ARG GOMAXPROCS=2
ARG GOMEMLIMIT=1536MiB

COPY go.mod go.sum ./
RUN go mod download

COPY . .

RUN go build -o main main.go

# Final stage
FROM public.ecr.aws/amazonlinux/amazonlinux:2023

ENV APPUSER=appuser
ENV APPUID=1000
ENV APPGID=1000

# Apply all available security updates before installing packages. Amazon Linux
# 2023 locks DNF to the release snapshot the base image was built from, so
# --releasever=latest is required to pick up newer patched packages.
# https://docs.aws.amazon.com/linux/al2023/ug/deterministic-upgrades-usage.html
#
# We tell DNF not to install Recommends and Suggests packages, which are
# weak dependencies in DNF terminology, thus keeping our installed set of
# packages as minimal as possible.
RUN dnf --releasever=latest upgrade -q -y \
    && \
    dnf --setopt=install_weak_deps=False install -q -y \
    shadow-utils \
    && \
    dnf clean all

RUN useradd \
    --home "/app" \
    --create-home \
    --user-group \
    --uid "$APPUID" \
    "$APPUSER"

WORKDIR /app
USER appuser

COPY --chown=appuser:appuser --from=build-env /appsrc/main /app/

COPY ./ATTRIBUTION.md ./LICENSES.md

ENV GIN_MODE=release

ENTRYPOINT ["/app/main"]
