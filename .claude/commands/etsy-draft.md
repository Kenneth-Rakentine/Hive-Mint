---
description: Fill in an Etsy listing as a DRAFT in the browser for an approved Hive-Mint product; the owner presses Publish
argument-hint: <product_id>
---

You are filling in the Etsy listing for Hive-Mint product **$ARGUMENTS** so the owner only has to check it and press Publish. This runs in a session the owner starts, never headless and never in a shift.

## Hard rules

- **Save as draft. Never press Publish**, "Publish listing", "Copy and publish" or anything else that makes a listing live, and never confirm a payment or a listing fee. If the only way forward is a button that publishes or charges, stop and tell the owner.
- Never change shop settings, payment, billing, tax, policies or the owner's account. Never sign in for the owner or type a password: if Etsy asks him to sign in or verify, stop and tell him.
- Only the product named here. Use exactly the words, price, files and pictures from its listing package. Do not rewrite them.
- If a field on the form isn't covered below, leave Etsy's default and mention it in your summary.

## 1. Check the product is approved

Run `.venv\Scripts\python -m app.cli review $ARGUMENTS`. Go on only if its status is **READY_TO_PUBLISH**. If `$ARGUMENTS` is empty, run `.venv\Scripts\python -m app.cli review` and ask which product to draft. Don't guess.

Read `listing.json`, `LISTING.txt` and `UPLOAD_STEPS.txt` in the package folder the review shows (`products\<id>\v<n>\listing\`). Note:
- the pictures in `images\`, in file-name order (the first is the thumbnail);
- the download ZIP in `upload\`;
- title, description, 13 tags, price, SKU, category.

## 2. Fill in the listing in the browser

Use the owner's preferred browser. Open `https://www.etsy.com/your/shops/me/listing-editor/create`.

1. **Photos:** add every picture from `images\`, in order. If the browser tools can't attach files from the PC, don't work around it: leave the photos for the owner and list the folder and file order in your summary.
2. **Title:** exactly `title`.
3. **About this listing:** Who made it = *I did*; What is it = *A finished product*; When was it made = the range that includes this year.
4. **Category:** `category`, or the closest one Etsy offers.
5. **Type:** Digital files.
6. **Description:** exactly `description`, including the AI-use disclosure at the end.
7. **Tags:** the 13 `tags`, one at a time.
8. **Price:** `price`. **Quantity:** 999. **SKU:** `sku`.
9. **Digital files:** upload the ZIP from `upload\` (same rule as photos if files can't be attached).
10. **Renewal:** Automatic.
11. Personalization, variations, shipping: leave them off or at Etsy's default.

Then choose **Save as draft** and confirm the draft appears under Shop Manager > Listings > Drafts.

## 3. Report

Tell the owner, in a few lines: the draft is saved (or how far you got and why you stopped), anything you couldn't fill in (photos or files to add by hand, with the folder), any field you left at Etsy's default, and the one thing left for him: open the draft, check it, and press Publish.

Record it so the database knows: `.venv\Scripts\python -m app.cli drafted $ARGUMENTS --note "<anything left for the owner, e.g. photos to add>"`.
