# Free sportsbook-source research (2026-09-16)

## Recommendation

Add FanDuel first and BetRivers/Kambi second. Both currently expose the same
unauthenticated JSON used by their frontends, both answered ordinary HTTPS
requests from hosted infrastructure, and both cover NFL and CFB player props
plus alternate thresholds. Keep Bovada as an independent third book. Do not
make DraftKings, BetMGM, Caesars, or Pinnacle a production dependency right now.

| Priority | Source | Access | Realistic data | Hosting/reliability |
| --- | --- | --- | --- | --- |
| 1 | FanDuel | `sbapi.nj.sportsbook.fanduel.com/api/content-managed-page` for event discovery, then `/api/event-page?eventId=...&tab=popular&_ak=...` | Main and alternate passing, rushing, receiving, reception, and passing-TD props; selection/market IDs; status; event start/live flag | Plain unauthenticated GET worked from hosted egress. CloudFront advertises `max-age=15`, so polling faster than 15 seconds is wasteful. No per-quote source timestamp; use receipt time. The frontend `_ak` value can rotate. |
| 2 | BetRivers/Kambi | `eu-offering-api.kambicdn.com/offering/v2018/rsiusnj/listView/...`, then `/betoffer/event/{id}.json` | Very broad event books, main O/U props and dense one-sided `N+` alternates, IDs/status, event start/live state | Plain unauthenticated GET worked from hosted egress. No cookies, account, browser, CAPTCHA, or TLS impersonation. No useful source quote timestamp; use receipt time. Operator/path names can change. |

The implementation is in
[`scripts/collect_live_sportsbook_props.py`](../scripts/collect_live_sportsbook_props.py)
and uses ordinary `requests` for these two books. It deliberately does not copy
the browser-fingerprinting behavior used by some public scrapers.

## Live verification

All calls below were read-only and made on 2026-09-16 from this hosted coding
environment, not from a residential browser session.

| Probe | Result |
| --- | --- |
| FanDuel NFL league page | HTTP 200, 1,053,299 bytes, 34 events |
| FanDuel NFL event (DET at BUF) | HTTP 200, 689,491 bytes, 92 markets, 68 player markets, 437 player runners |
| FanDuel CFB league page | HTTP 200, 2,074,884 bytes, 115 matchup events |
| BetRivers NFL league page | HTTP 200, 28,246 bytes, 16 events |
| BetRivers NFL event (DET at BUF) | HTTP 200, 677,863 bytes, 664 offers |
| BetRivers CFB league page | HTTP 200, 124,532 bytes, 72 events |
| BetRivers CFB event (Houston at Texas Tech) | HTTP 200, 232,566 bytes, 230 offers, 178 with player participants |

End-to-end normalized collector smoke tests:

| Game | FanDuel | BetRivers |
| --- | ---: | ---: |
| NFL: Detroit at Buffalo | 274 quotes (245 alternates, 29 two-sided mains) | 215 quotes (186 alternates, 18 two-sided mains) |
| CFB: Houston at Texas Tech | 258 quotes (234 alternates, 24 two-sided mains) | 109 quotes (93 alternates; main sides were individually suspended at probe time) |

The alternates are stored using the repo's existing semantics: a displayed
`40+ yards` selection becomes an over at `39.5`. Stable market and selection
IDs are retained so change-only storage can be added later without remapping
names.

## What not to add now

- **DraftKings:** the current `sportsbook-nash` frontend league endpoint
  returned HTTP 403 with an Akamai Access Denied page from hosted egress. This
  matches the prior local-versus-Railway failure. A current community reference
  documents the `sportscontent` endpoint, but being current does not make it
  cloud-safe: [sports-odds-fetch endpoint notes](https://github.com/nchemb/sports-odds-fetch/blob/master/references/draftkings-endpoints.md).
- **BetMGM:** its client-config bootstrap returned HTTP 403 before an access ID
  could be discovered. The underlying API is well structured and documents a
  two-second refresh interval, but the public-web bootstrap is not cloud-safe:
  [BetMGM Sports API](https://sportsapi.wv.betmgm.com/offer/swagger/index.html).
- **Caesars:** the `api.americanwagering.com` NFL schedule request returned HTTP
  403. Community reports and clients exist, but the current cloud result makes
  it unsuitable as a primary feed.
- **Pinnacle:** the official general-public API has been closed since
  2025-07-23 and now requires approved authenticated access. The guest Arcadia
  approach depends on extracting a rotating frontend key, which is outside this
  project's constraints: [official Pinnacle API documentation](https://github.com/pinnacleapi/pinnacleapi-documentation).

## Open-source approaches checked

- [`sportsdata-mcp`](https://github.com/DanielTomaro13/sportsdata-mcp) was pushed
  on 2026-09-06 and exposes FanDuel sportsbook REST calls via the frontend `_ak`
  key. It is the strongest evidence that this FanDuel shape is in current use.
- [`oddswrap`](https://github.com/sjhouston23/oddswrap) was pushed on 2026-06-03
  and has adapters for FanDuel event pages, BetRivers/Kambi event offers,
  DraftKings sportscontent, BetMGM CDS, Caesars, and Bovada. Its FanDuel and
  Kambi shapes matched the live responses. Its README says adapters use
  `curl_cffi` browser impersonation; this implementation only adopted the two
  endpoints that also worked with plain requests.
- [`sportsbook-odds-scraper`](https://github.com/declanwalpole/sportsbook-odds-scraper)
  is a useful older catalog of direct JSON approaches, but its last code push
  was 2025-04-15 and it explicitly warns that books reject requests by IP
  location. Treat it as endpoint archaeology, not a dependency.
- [`sports-odds-fetch`](https://github.com/nchemb/sports-odds-fetch) was pushed
  on 2026-04-13 and confirms the newer DraftKings endpoint family. The live 403
  means it is not a Railway answer for us.

## Railway use

Create one service per book with `railway.fanduel.toml` or
`railway.betrivers.toml`, attach a persistent volume, and set for example:

```text
SLATE_ID=nfl-2026-09-20
SPORTSBOOK_SPORT=nfl
SPORTSBOOK_GAMES=Detroit Lions @ Buffalo Bills
SPORTSBOOK_POLL_SECONDS=30
```

Run one deployment as a smoke test and require each service's
`collector_status` record to be `ok` before relying on it. A 15–30 second poll
interval is appropriate for FanDuel's cache behavior. The separate services
keep failures isolated; alert on repeated errors or unexpected empty responses.
Receipt timestamps are preserved because neither source supplies a trustworthy
price-update timestamp.

These are undocumented consumer endpoints, not licensed feeds. They can change
without notice and their permitted use should be checked against each site's
terms before production use or redistribution.
