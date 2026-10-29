# Tesla's fleet-telemetry image is distroless (no shell), so only its binary is
# reused. The binary links against glibc, hence the Debian app base.
FROM tesla/fleet-telemetry:v0.9.5 AS fleet-telemetry

FROM ghcr.io/home-assistant/base-debian:trixie-2026.10.1

ARG LEGO_VERSION=5.5.2
ARG LEGO_SHA256_AMD64=2a35505089e7772c92e1e9ac144df91151ef2eca8568630db0ff91fca06d9bef
ARG LEGO_SHA256_ARM64=15b14ec2ab14fde69cc8396eb0204c5ce4327e31a486953225a6059b26db3e8c

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        python3 \
        python3-aiohttp \
        python3-cryptography \
        python3-zmq \
    && rm -rf /var/lib/apt/lists/*

# lego (ACME client) from the upstream release; Debian's package is too old.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "${arch}" in \
        amd64) sha="${LEGO_SHA256_AMD64}" ;; \
        arm64) sha="${LEGO_SHA256_ARM64}" ;; \
        *) echo "unsupported architecture ${arch}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/lego.tar.gz \
        "https://github.com/go-acme/lego/releases/download/v${LEGO_VERSION}/lego_v${LEGO_VERSION}_linux_${arch}.tar.gz"; \
    echo "${sha}  /tmp/lego.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/lego.tar.gz -C /usr/bin lego; \
    rm /tmp/lego.tar.gz; \
    lego --version

COPY --from=fleet-telemetry /fleet-telemetry /usr/bin/fleet-telemetry
COPY rootfs /

LABEL \
    io.hass.type="app" \
    io.hass.name="Tesla Bridge" \
    org.opencontainers.image.source="https://github.com/danieltian/ha-tesla-bridge"
