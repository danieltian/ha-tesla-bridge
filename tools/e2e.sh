#!/usr/bin/env bash
# End-to-end check of the app with a simulated car (no Tesla account needed).
#
# Builds the app image, runs it with a development-only "vehicle CA", connects
# tools/vehicle-sim over mutual TLS exactly like a car, and asserts that the
# data arrives on the integration's WebSocket API. Needs Docker and Go.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$ROOT/.dev/e2e-run"
VIN1=5YJSIM00000000001
VIN2=5YJSIM00000000002
PY="${PYTHON:-$ROOT/.venv/bin/python}"

rm -rf "$WORK" && mkdir -p "$WORK/data/sim" "$WORK/src"
docker rm -f tb-e2e-run >/dev/null 2>&1 || true

echo "==> fake vehicle certificates (Tesla's own test tooling)"
git clone -q --depth 1 --branch v0.9.5 https://github.com/teslamotors/fleet-telemetry.git "$WORK/src/fleet-telemetry"
(cd "$WORK/src/fleet-telemetry" && go run ./tools -client-ids="$VIN1,$VIN2" -server-ids=app \
    -san-domain=localhost -directory="$WORK/data/sim/" >/dev/null)
chmod 644 "$WORK"/data/sim/*

echo "==> simulator"
(cd "$ROOT/tools/vehicle-sim" && go build -o vehicle-sim .)

echo "==> app image"
docker buildx build --load -q -t tesla-bridge:e2e "$ROOT/apps/tesla_bridge" >/dev/null
cat > "$WORK/data/options.json" <<JSON
{"duckdns_domain": "simcar", "log_level": "debug",
 "dev_extra_vehicle_ca": "/data/sim/vehicle_device.CA.cert"}
JSON
docker run -d --name tb-e2e-run -p 28443:8443 -p 28099:8099 -v "$WORK/data:/data" tesla-bridge:e2e >/dev/null
trap 'docker rm -f tb-e2e-run >/dev/null 2>&1 || true' EXIT
for _ in $(seq 30); do curl -fs localhost:28099/api/v1/health >/dev/null && break; sleep 1; done

TOKEN="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["api_token"])' "$WORK/data/state.json")"
curl -fs -H "Authorization: Bearer $TOKEN" localhost:28099/api/v1/ca > "$WORK/ca.pem"
curl -fs -X PUT -H "Authorization: Bearer $TOKEN" -d "[\"$VIN1\"]" localhost:28099/api/v1/vins >/dev/null

echo "==> streaming"
"$PY" - "$TOKEN" > "$WORK/ws.json" <<'PYEOF' &
import asyncio, json, sys, aiohttp
async def main():
    events = []
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect("http://localhost:28099/api/v1/stream",
                                headers={"Authorization": f"Bearer {sys.argv[1]}"}) as ws:
            try:
                async with asyncio.timeout(15):
                    async for msg in ws:
                        events.append(json.loads(msg.data))
            except TimeoutError:
                pass
    print(json.dumps(events))
asyncio.run(main())
PYEOF
WS_PID=$!
sleep 1
SIM="$ROOT/tools/vehicle-sim/vehicle-sim"
for vin in $VIN1 $VIN2; do
  "$SIM" -host simcar.duckdns.org -connect 127.0.0.1 -port 28443 -vin "$vin" \
    -cert "$WORK/data/sim/vehicle_device.$vin.cert" -key "$WORK/data/sim/vehicle_device.$vin.key" \
    -ca "$WORK/ca.pem" -scenario types >/dev/null
done
wait "$WS_PID"

"$PY" - "$WORK/ws.json" <<'PYEOF'
import json, sys
events = json.load(open(sys.argv[1]))
types = [e["type"] for e in events]
assert types[:2] == ["hello", "snapshot"], types
data = [e for e in events if e["type"] == "data"]
assert len(data) == 1, data  # VIN2 is not on the allowlist
fields = data[0]["fields"]
assert data[0]["vin"] == "5YJSIM00000000001"
assert fields["BatteryLevel"] == 71.25 and fields["InsideTemp"] is None
assert fields["Location"] == {"latitude": 37.3947, "longitude": -122.1503}
conn = [e for e in events if e["type"] == "connectivity"]
assert [c["status"] for c in conn] == ["CONNECTED", "DISCONNECTED"], conn
print(f"OK: {len(events)} events, {len(fields)} fields, allowlist enforced")
PYEOF
