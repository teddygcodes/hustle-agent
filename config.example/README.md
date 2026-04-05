# Config Setup

Copy this entire directory to `config/` and fill in your credentials:

```bash
cp -r config.example/ config/
```

Then edit each file:

| File | Where to get it |
|------|----------------|
| `kalshi.json` | https://kalshi.com/account/api — create an API key, download the PEM |
| `kalshi-private-key.pem` | Downloaded when you create your Kalshi API key |
| `telegram.json` | Create a bot at https://t.me/BotFather, get your chat ID via https://t.me/userinfobot |
| `sports_data.json` | https://the-odds-api.com — free tier, 500 requests/month |
| `therundown.json` | https://therundown.io/api — free tier, 20K data points/day |
| `fred.json` | https://fred.stlouisfed.org/docs/api/api_key.html — free, instant |

The `config/` directory is gitignored — your credentials will never be committed.
