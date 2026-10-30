# Tesla Bridge for Home Assistant

One integration for your **Tesla cars and Powerwall**:

- **Cars stream to your own Home Assistant within seconds.** The integration
  uses Tesla's official Fleet Telemetry, sent to a server running in your home,
  so there's no polling, no waking the cars, and no third-party cloud.
- **Powerwall updates every 5 seconds**, read from the gateway on your LAN. The
  controls (backup reserve, operation mode, Storm Watch, grid charging,
  exports) go through Tesla's cloud.
- **Commands** (climate, locks, charging, trunks, windows, seats, media,
  HomeLink, software updates) are signed with your own key.
- **Most of the setup is automated.** The integration installs and configures
  its companion app, gets the certificates, registers with Tesla, configures
  every car and can take over your existing Tesla Fleet / Tesla Powerwall
  entities with their history.

It replaces the built-in **Tesla Fleet** and **Tesla Powerwall** integrations.

## How it works

```
 your car ──(internet, port 8443, mutual TLS)──▶ router ──▶ Tesla Bridge app ──▶ Tesla Bridge integration
 Tesla ────(https://<you>.duckdns.org/.well-known/…, port 443)──▶ Tesla Bridge app (public key)
 Home Assistant ──(Fleet API: commands, energy settings)──▶ Tesla
 Home Assistant ──(LAN, every 5 s)──▶ Powerwall gateway
```

The **app** (`apps/tesla_bridge`) runs Tesla's open-source
[fleet-telemetry](https://github.com/teslamotors/fleet-telemetry) server. It
also keeps a free DuckDNS name pointed at your home, gets a Let's Encrypt
certificate for it, and serves your Tesla public key. The **integration**
(`custom_components/tesla_bridge`) does everything else.

## Requirements

- Home Assistant OS (or Supervised) 2026.10 or newer. Other install types work
  too, with one extra step: run the app's container yourself (see
  [Without the Supervisor](#without-the-supervisor)).
- A Tesla developer app (developer.tesla.com). If you used the Tesla Fleet
  integration, you already have one: Tesla Bridge reuses its client ID, secret
  and key, so your cars stay paired.
- A free [DuckDNS](https://www.duckdns.org) name and token.
- A public IPv4 address (no carrier-grade NAT) and the ability to forward TCP
  443 and 8443 to Home Assistant (by hand, or automatically with UPnP).
- Car firmware 2024.26 or newer.

## Install

1. In HACS, add this repository as a custom repository (type **Integration**),
   download **Tesla Bridge** and restart Home Assistant.
2. Go to **Settings → Devices & services → Add integration → Tesla Bridge** and
   follow the steps. It:
   1. adds the app repository, installs the app and asks for your DuckDNS name
      and token;
   2. tells you which two ports to forward (or opens them with UPnP);
   3. waits for the Let's Encrypt certificate;
   4. asks you to add `https://<you>.duckdns.org` to your Tesla developer app's
      **Allowed Origins**, then signs you in to Tesla;
   5. registers the name with Tesla and checks each car trusts your key (it
      shows a QR code only for cars that don't);
   6. sends each car a signed streaming configuration;
   7. connects to your Powerwall gateway locally (imported from the Tesla
      Powerwall integration if you had it);
   8. offers to take over your Tesla Fleet / Tesla Powerwall entities, keeping
      their entity IDs and history.
3. Each car starts streaming the next time it wakes up.

If you close the setup before it finishes, start it again from **Settings →
Apps → Tesla Bridge → Open Web UI → Continue setup** (or add the integration
again). The app keeps your earlier answers, so they're filled in.

## Without the Supervisor

On Home Assistant Container or Core installs, build and run the app yourself on
a host that can receive the two forwarded ports:

```bash
docker build -t tesla-bridge apps/tesla_bridge
mkdir -p tesla-bridge-data
cat > tesla-bridge-data/options.json <<'JSON'
{"duckdns_domain": "myhome", "duckdns_token": "<your DuckDNS token>",
 "lets_encrypt_accept_terms": true}
JSON
docker run -d --name tesla-bridge --restart unless-stopped \
  -p 443:443 -p 8443:8443 -p 8099:8099 \
  -v "$PWD/tesla-bridge-data:/data" tesla-bridge
```

Only Home Assistant should be able to reach port 8099 (firewall it from
everything else). When you add the integration, it asks for the container's
address, port 8099, and the `api_token` from `tesla-bridge-data/state.json`.

## Cost

Tesla bills Fleet API use, with a $10 monthly credit per account. Streaming
costs about $1 per 150,000 values, even though the cars send them straight to
your server. Parked, sleeping cars send nothing, and the default *Standard*
preset sends each value only when it changes, at most once per interval. A car
that drives or charges about an hour a day typically uses $0.20–0.30 a month on
*Essential* or *Standard*, and about $1.50 on *Everything* (location and speed
every second). The integration never polls a sleeping car.

To keep an eye on it:

- **Tesla API cost this month** and **Tesla API cost projected for this month**
  (on the *Tesla Bridge app* device) add up streaming plus Tesla Bridge's own
  data requests, commands and wake-ups at Tesla's prices; **Tesla API requests
  this month** counts the requests by category. Each car also has its own
  streaming cost sensor.
- The projection uses the rate since Tesla Bridge started counting; its *full
  month at this rate* attribute shows what a whole month would cost, which is
  the useful number in your first, partial month.
- A repair appears when either is projected to pass $8.
- Your developer app's billing and usage page on developer.tesla.com is the
  authoritative number. It also includes anything else using the same developer
  app, such as the Tesla Fleet integration.

With Tesla's default billing limit of $0, going over the credit doesn't create
a bill: Tesla turns the developer app off until the next month and removes the
cars' streaming setups. Tesla Bridge checks each car's setup daily and sends it
again once Tesla allows it.

## Development

```bash
uv venv --python 3.14 .venv && uv pip install --python .venv/bin/python -r requirements_test.txt
.venv/bin/python -m pytest tests/integration            # integration (HA test harness)
.venv/bin/python -m pytest tests/app -p no:homeassistant # app (plain pytest)
TESLA_JWS_BIN=… .venv/bin/python -m pytest tests/jws -p no:homeassistant  # vs Tesla's Go signer
tools/e2e.sh   # app image + simulated car end to end (needs Docker and Go)
```

`tools/vehicle-sim` is a fake car. It uses Tesla's own test-certificate tooling
and protocol code, so the whole streaming path can be tested without a vehicle.

## License

Apache-2.0. Not affiliated with Tesla, Inc.
