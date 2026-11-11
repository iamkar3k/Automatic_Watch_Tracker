# Daily refresh runbook

Dashboard: https://claude.ai/artifact/THm9MdL8MBV528Zc5xkwH2
Tracked watches: the 11 models in README.md. US market, official or authorized retailers only, no second-hand marketplaces.

Each run:
1. WebSearch each watch for the current US price (Timex.com, Casio US, Citizen US or authorized dealers, Orient Watch USA, Seiko USA). Record price, regular price, retailer and product URL. Leave a price blank if none is found; never guess.
2. Append one row per watch to `price_log.csv` (Date, Watch, Model/Ref, Retailer, Price USD, List/Regular USD, Source type, Notes, URL).
3. Compute `past_low` = lowest Price USD logged for that watch before today (today's price if no history).
4. Set `status`: `baseline` if no earlier price is logged, `best` if today <= past_low, otherwise `above`. A new low is `best`.
5. Build `prices.json` (fields: id, watch, ref, retailer, today, past_low, regular, off_regular, status, buy_url, checked, note), upload it to the dashboard with the Artifact tool (`asset: true`), then update `datasets/prices` with ArtifactData: new `source.url` and `updated`.
6. Commit and push price_log.csv on branch claude/adoring-knuth-kg95ry.
7. Only if a watch has status `best` and is not `baseline`, send a PushNotification naming the watch, price, past low and buy link. Otherwise stay silent.

## Optional services (set as GitHub repository secrets)
- `FIRECRAWL_API_KEY`: used only when a page blocks both the plain request and the headless browser.
- `EXA_API_KEY`: adds an Exa web search per watch (authorized retailer domains only). A result counts only if the model number is in its title or address and the price is in range.
Without these secrets the checker skips both and behaves as before.
