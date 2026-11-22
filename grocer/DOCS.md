# Groceries

Paste a grocery list, review the matched products, and add them to your Kroger-family store cart
(Fred Meyer, Kroger, QFC, Ralphs, ...). The add-on remembers what you pick, so your usual items are
chosen automatically next time. Nothing is added to your cart until you press **Add to cart**.

## Setup

1. Register an app at <https://developer.kroger.com> with the **Products** and **Cart** APIs.
   Add `http://localhost:8000/callback` as a redirect URI. (It can take a few minutes to take effect.)
2. Put the app's client ID and secret in this add-on's **Configuration** tab and start the add-on.
3. Open **Groceries** from the sidebar, go to **Settings**, find your store by ZIP code, and
   **Sign in to Kroger**. After signing in, Kroger sends your browser to a `localhost` address that
   won't load. Copy that address and paste it back into Settings.

## Using it

- **List:** paste your list, one item per line. `2 oat milk`, `milk x2`, `bread (gluten free)`,
  `diet: vegan` for the whole list. Then **Run**.
- **Review:** items you buy often are picked automatically. New items show the top matches to choose
  from. Cheaper options that meet the same needs are pointed out, with a one-tap switch.
- **Add to cart**, then review and check out in the Kroger app. Pickup or delivery is chosen there.
- **Purchase history (optional):** on **Saved → Purchase history**, drag the **Collect grocery trip**
  button to your bookmarks bar. Click it on each trip's Purchase Details page on your store's website,
  then **Download all** and upload the file. Products you've bought are then suggested from the first run.

## Limits

- Kroger's public API can add to a cart but can't read or change it. Fix mistakes in the app.
- Running the same list twice adds everything twice.
- Dietary checks use Kroger's allergen, ingredient and label data. They are a shortlist, not a
  guarantee. Always read the label for allergies.

## Data

Everything is stored in the add-on's `/data` folder (included in Home Assistant backups): your picks,
run history, and the Kroger sign-in tokens.
