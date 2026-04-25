# shopify-oos-sorter

Automatically moves out-of-stock products to the end of every collection in your Shopify store. Runs on a configurable interval so your collections stay clean without any manual work.

## What it does

1. Fetches every collection in your store
2. Sets each collection's sort order to **Manual** (if not already)
3. Pushes all out-of-stock products to the end, preserving the existing relative order of in-stock products
4. Repeats on a configurable interval (default: every 30 minutes)

## Requirements

- Python 3.10+
- A Shopify custom app with the `write_products` and `read_products` Admin API scopes

## Setup

### 1. Create a Shopify custom app

1. In your Shopify admin go to **Settings → Apps and sales channels → Develop apps**
2. Click **Create an app**, give it a name (e.g. `OOS Sorter`)
3. Under **Configuration**, add the following Admin API scopes:
   - `read_products`
   - `write_products`
4. Click **Install app**, then copy the **Client ID** and **Client secret**

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment variables

```bash
cp .env.example .env
```

Then fill in your values:

```env
SHOPIFY_SHOP_URL=your-store.myshopify.com
SHOPIFY_CLIENT_ID=your_client_id
SHOPIFY_CLIENT_SECRET=your_client_secret

# Optional — how often to run in seconds (default: 1800 = 30 minutes)
INTERVAL_SECONDS=1800
```

### 4. Run

```bash
python main.py
```

Leave the terminal open. The script runs once immediately, then sleeps for the configured interval and repeats. Press `Ctrl+C` to stop.

## How sorting works

Products are split into two groups based on Shopify's `totalInventory` field:

| Group | Condition |
|---|---|
| In stock | `totalInventory` is `null` (not tracked) or `> 0` |
| Out of stock | `totalInventory` is `0` or negative |

Within each group, products are sorted **newest first** by creation date. OOS products at the end are also sorted newest first among themselves.

## Configuration reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `SHOPIFY_SHOP_URL` | Yes | — | Your store domain, e.g. `my-store.myshopify.com` |
| `SHOPIFY_CLIENT_ID` | Yes | — | Custom app client ID |
| `SHOPIFY_CLIENT_SECRET` | Yes | — | Custom app client secret |
| `INTERVAL_SECONDS` | No | `1800` | Seconds between runs |

## Notes

- The script re-authenticates at the start of every run so token expiry is never an issue
- Shopify's GraphQL API only accepts 250 moves per request — large collections are batched automatically
- Throttle handling is built in; the script backs off automatically if the API cost budget runs low
