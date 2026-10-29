# Tesla Bridge app

This app is the internet-facing half of **Tesla Bridge**. You normally don't
configure it by hand: the Tesla Bridge integration installs it, fills in the
options and starts it during setup.

## What it does

- **DuckDNS**: keeps your DuckDNS name pointed at your home's public IPv4
  address (every 5 minutes).
- **Let's Encrypt**: gets and renews a certificate for that name using a DNS
  challenge through DuckDNS, so no port 80 is needed.
- **Tesla public key**: serves your Tesla developer app's public key at
  `https://<name>.duckdns.org/.well-known/appspecific/com.tesla.3p.public-key.pem`
  on port **443**. Every other path returns 404.
- **Fleet Telemetry**: runs Tesla's open-source
  [fleet-telemetry](https://github.com/teslamotors/fleet-telemetry) server on
  port **8443**. Your cars connect to it directly and stream changes as they
  happen. It uses its own long-lived certificate authority, which is what the
  cars are told to trust.
- **Relay**: passes the stream to the Tesla Bridge integration over the
  internal Home Assistant network, keeps the last value of every field across
  restarts, and counts streamed values (Tesla bills per value).

## Router

Forward these TCP ports from the internet to your Home Assistant host:

| Port | Used by |
| ---- | ------- |
| 443  | Tesla (and the Tesla app) fetching your public key |
| 8443 | Your cars streaming data |

They must be plain TCP forwards. A reverse proxy or Cloudflare Tunnel will not
work: the cars authenticate with client certificates that only the telemetry
server itself can check. The integration can create the forwards with UPnP if
your router allows it.

If your router can't "hairpin" (reach its own public address from inside),
cars at home on Wi-Fi can't reach the server. The integration shows this as
**Reachable via public address: off**. Fix it with a local DNS entry that
points your DuckDNS name to Home Assistant's LAN address.

## Options

| Option | Meaning |
| ------ | ------- |
| `duckdns_domain` | Your DuckDNS name, e.g. `myhome` or `myhome.duckdns.org` |
| `duckdns_token` | The token shown on duckdns.org |
| `lets_encrypt_accept_terms` | Must be on to get a certificate |
| `lets_encrypt_email` | Optional contact address for Let's Encrypt |
| `lets_encrypt_staging` | Use Let's Encrypt's test servers (for development) |
| `external_telemetry_port` | The port cars connect to if you forward a port other than 8443 |
| `log_level` | `debug`, `info`, `warning` or `error` |

If you already run the official **Duck DNS** app with Let's Encrypt for the same
name, this app reuses that certificate from `/ssl` instead of requesting its
own: two apps can't share DuckDNS's single TXT record.

## Data

Everything lives in the app's `/data` folder and is included in Home Assistant
backups: the telemetry CA (`pki/`), the Let's Encrypt account and certificate
(`acme/`), and the last values of every car (`state.json`).
