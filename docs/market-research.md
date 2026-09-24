# Initial Etsy market scan — 2026-09-24

This is a qualitative scan of public Etsy shop pages, not a ranking of best-selling products. **Shop-wide lifetime sales do not establish that a specific design, niche, or AI workflow caused sales.** None of these pages establish that the sellers use AI for artwork. Prices and counts change; recheck them before deciding what to make.

| Shop | Public signal observed | What to learn | Limit |
| --- | --- | --- | --- |
| [CustomComfortColors](https://www.etsy.com/shop/CustomComfortColors) | About 29.5k lifetime shop sales; custom logo, photo, text, city, holiday shirts and sweatshirts; many Comfort Colors listings. | Personalization, clear blank options, and a wide but recognizable product catalog give shoppers an immediate use case. Test custom text variants after the base workflow is stable. | Sales span years and many products. Displayed sale prices can be low entry variants, not the final configured price or seller margin. |
| [VanguardTees](https://www.etsy.com/shop/VanguardTees) | 2,238 lifetime shop sales in the inspected page; nostalgia-led tees; owner describes a disclosed print-on-demand partner. | A clear audience and consistent visual language matter. Borrow the coherence and merchandising, not films, bands, games, characters, marks, or artwork. | Public total says nothing about item-level performance; much of this niche may carry IP risk. |
| [GaborStudioDesign](https://www.etsy.com/shop/GaborStudioDesign) | 9,985 lifetime shop sales in the inspected page; hand-drawn SVG/PNG graphics with commercial-use positioning and some POD fulfillment. | Files can themselves be a product if they are technically clean and licensed clearly; a human-art-directed original aesthetic is a way to stand out. | Mixed digital and physical catalog; totals cannot be assigned to either. |
| [DistantDaze](https://www.etsy.com/shop/DistantDaze) | 11 lifetime shop sales in the inspected page; original fictional vintage-brand and roadside-sign tees. | The fictional-brand visual direction is possible, but style alone is no demand signal. Specific customers, better presentation, and conversion testing are needed. | Very young shop and too little public data to infer why sales are low. |

## Hypotheses to test, not assumptions

1. **Who wants it?** Select a narrow audience with purchase intent, such as people who like boutique synths and independent music gear. Research the words they use before designing.
2. **What are we selling?** Start with one or two print surfaces, such as a good blank tee and a poster. Compare actual print quotes, shipping, size/color variants, and sample quality.
3. **What makes a design worth buying?** Compose a small collection of original imaginary companies, slogans, and emblems with cohesive art direction. Give each a reason to appeal to the chosen audience beyond looking like a generic logo.
4. **What does the shopper see?** Show the print at a believable size and color in a clean lead image, plus detail, size chart, blank information, and disclosure. Verify that every mockup depicts the actual product and print placement.
5. **Does it earn money?** Record price, blank/print cost, shipping subsidy, listing and transaction fees, payment processing, ads, refunds allowance, and model cost. Estimate margin by variant and revise from actual orders.

A first experiment could compare ten original designs in one audience and two product formats, then order samples of the strongest candidates. Record impressions, favorites, visits, orders, buyer feedback, and actual costs where Etsy provides them. Avoid inferring success from competitor shop totals.

## Rules that affect implementation

[Etsy's Creativity Standards](https://www.etsy.com/legal/creativity/) include seller-prompted AI creations under seller-designed goods and require AI use to be disclosed in the listing description. [Etsy's seller guidance](https://help.etsy.com/hc/en-us/articles/360024112614-What-Can-I-Sell-on-Etsy) requires disclosure of production partners for relevant listings. The agent should generate disclosure text for review and keep a per-design provenance record. This is a product requirement, not a promise that a proposed design is legally cleared.

[Etsy's fee page](https://help.etsy.com/hc/en-us/articles/115014483627-What-are-the-Fees-and-Taxes-for-Selling-on-Etsy) lists listing, transaction, payment processing and possible Offsite Ads fees. Rates and eligibility vary; fetch the current rates when the calculator is implemented. [The Etsy Open API listings tutorial](https://developers.etsy.com/documentation/tutorials/listings/) documents draft creation. Begin with offline drafts; add API publishing only after explicit owner approval.

## StarNet reference and distinction

[StarNet's README](https://github.com/androoAGI/starnet) describes a local-first agent harness whose visual world projects real runtime state. Hive Mint should adopt that honest feedback loop. Its stations, artwork, business records, and permission model should be original. Do not copy StarNet's UI assets or assume its full multi-agent harness is necessary for a first commercial experiment.

## What to measure next

Build an item-level sheet for a small, comparable Etsy sample: title, product type, advertised starting price, fully configured price if visible, photo quality, shipping promise, review count, review complaints, and freshness. Public data usually cannot reveal true item sales, revenue, costs, or conversion. Use Etsy shop analytics for those metrics once Hive Mint has a shop.
