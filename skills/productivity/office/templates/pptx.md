# Slide templates: a new deck's slides

Build a new deck from these eight slides, in the order of one of the decks at the end. Each call adds its slide last,
and a new deck's empty title slide gives way to the first; send one or two slides per call (two in one call are two
entries in `slides`). Put in the user's path, words and figures in place of the samples, and keep the styles: the slides
then sit together as one deck. Each fits a 16:9 deck and a 4:3 one alike, as its boxes grow with their text: only the
rules and the bars have a height.

The look: navy #14285A, paper #FBFBF8, mist #DCE3F0 and amber #F2C14E, in Arial. Type at 128 px (the title and the big
number), 64 (`<h2>`), 44 (`<h3>` and the subtitle), 32 (`<p>` and lists) and 24 (a source): `<h2>`, `<h3>` and `<p>`
take their size from the tag. Text is navy on paper or mist, and paper, mist or amber on navy, always at full strength.
A heading says the point ("The North brings a third of all orders"), not the topic ("Orders by region"). The `<aside>`
holds the speaker's notes: what to say, in a sentence or two.

The line under each call says how much its slide holds. More does not shrink to fit: the slide is refused, so split it
over two slides.

## Title

The deck's first slide: what it is about, who gives it and when.

```json
{"path": "decks/q3-review.pptx", "ops": [{"op": "slides", "slides": [{"html": "<section style=\"background:#14285A;color:#FBFBF8;font-family:Arial, sans-serif;padding:128px;justify-content:center;gap:40px\"><div style=\"width:160px;height:12px;background:#F2C14E\"></div><h1 style=\"font-size:128px\">Online shop: Q3 review</h1><p style=\"font-size:44px;color:#DCE3F0\">Orders, customers and the plan for Q4</p><p style=\"color:#DCE3F0\">Sales and product teams, 14 October 2026</p><aside>Open with the result: 38% more orders. Then what drove it, and the decision we need today.</aside></section>"}]}]}
```

Replace the title (at most eight words), the subtitle (at most 15), the team and the date.

## Statement

One sentence the audience should remember: the point of the deck, or of the part it opens.

```json
{"path": "decks/q3-review.pptx", "ops": [{"op": "slides", "slides": [{"html": "<section style=\"background:#FBFBF8;color:#14285A;font-family:Arial, sans-serif;padding:128px;justify-content:center;gap:40px\"><div style=\"width:160px;height:12px;background:#F2C14E\"></div><h2>Customers who are set up in a day stay: we cut setup from two weeks to one day.</h2><p>What changed in Q3</p><aside>Say the sentence, then pause: every slide after this one is the evidence for it.</aside></section>"}]}]}
```

Write the one sentence, at most 30 words, and its context under it in a few words.

## Big number

One figure that carries the point: a result, a size, a cost.

```json
{"path": "decks/q3-review.pptx", "ops": [{"op": "slides", "slides": [{"html": "<section style=\"background:#14285A;color:#FBFBF8;font-family:Arial, sans-serif;padding:128px;justify-content:center;gap:40px\"><div style=\"width:160px;height:12px;background:#F2C14E\"></div><h1 style=\"font-size:128px;color:#F2C14E\">38%</h1><h2>more orders in Q3 than in Q2</h2><p style=\"color:#DCE3F0\">Online shop, July to September against April to June</p><aside>13,400 orders against 9,700 in Q2. The next slides show where they came from.</aside></section>"}]}]}
```

Put the figure in the `<h1>`, at most seven characters (38%, $1.2M, 13,400), what it counts in the `<h2>` in at most 15
words, and its source in the last line.

## Cards

Three or four things side by side: changes, reasons, options, features.

```json
{"path": "decks/q3-review.pptx", "ops": [{"op": "slides", "slides": [{"html": "<section style=\"background:#FBFBF8;color:#14285A;font-family:Arial, sans-serif;padding:128px;gap:48px\"><h2>Three changes behind the growth</h2><div style=\"display:flex;gap:32px;margin:auto 0\"><div style=\"flex:1;background:#DCE3F0;border-top:12px solid #F2C14E;padding:40px;gap:16px\"><h3>One-day setup</h3><p>A guided import replaced the two-week setup project.</p></div><div style=\"flex:1;background:#DCE3F0;border-top:12px solid #F2C14E;padding:40px;gap:16px\"><h3>Faster delivery</h3><p>Orders now ship within 24 hours of payment.</p></div><div style=\"flex:1;background:#DCE3F0;border-top:12px solid #F2C14E;padding:40px;gap:16px\"><h3>Clearer prices</h3><p>One price list, delivery included, for every region.</p></div></div><aside>One card at a time: what we changed, and the figure it moved.</aside></section>"}]}]}
```

Three cards, or a fourth copied from them: each a title of two or three short words and at most 20 words of text. Five
things are two slides.

## Table

Figures the audience compares across rows: plan against actual, options against criteria.

```json
{"path": "decks/q3-review.pptx", "ops": [{"op": "slides", "slides": [{"html": "<section style=\"background:#FBFBF8;color:#14285A;font-family:Arial, sans-serif;padding:128px;gap:48px\"><h2>Every measure but one beat the plan</h2><table style=\"text-align:left;margin:auto 0\"><tr style=\"background:#14285A;color:#FBFBF8\"><th style=\"width:40%\">Measure</th><th style=\"width:20%\">Plan</th><th style=\"width:20%\">Actual</th><th style=\"width:20%\">Change</th></tr><tr><td>Orders</td><td>12,000</td><td>13,400</td><td>+12%</td></tr><tr style=\"background:#DCE3F0\"><td>New customers</td><td>900</td><td>1,050</td><td>+17%</td></tr><tr><td>Average order</td><td>$84</td><td>$81</td><td>−4%</td></tr><tr style=\"background:#DCE3F0\"><td>Support tickets</td><td>2,400</td><td>1,900</td><td>−21%</td></tr></table><p style=\"font-size:24px\">Source: order system, 30 September 2026</p><aside>Average order is the one miss: new customers buy more often, in smaller orders.</aside></section>"}]}]}
```

At most six rows under the header and five columns, a figure or a few words per cell, and every second row mist. Each
header cell's width is its column's share: 40% for the first, the other 60% split among the rest (20% each for three
more, 15% each for four).

## Chart

Values of one measure side by side: by region, product or month.

```json
{"path": "decks/q3-review.pptx", "ops": [{"op": "slides", "slides": [{"html": "<section style=\"background:#FBFBF8;color:#14285A;font-family:Arial, sans-serif;padding:128px;gap:48px\"><h2>The North brings a third of all orders</h2><div style=\"display:grid;grid-template-columns:240px 1fr 160px;gap:32px;align-items:center;margin:auto 0\"><p>North</p><div style=\"width:100%;height:56px;background:#F2C14E\"></div><p>4,500</p><p>South</p><div style=\"width:80%;height:56px;background:#14285A\"></div><p>3,600</p><p>East</p><div style=\"width:62%;height:56px;background:#14285A\"></div><p>2,800</p><p>West</p><div style=\"width:38%;height:56px;background:#14285A\"></div><p>1,700</p><p>Islands</p><div style=\"width:18%;height:56px;background:#14285A\"></div><p>800</p></div><p style=\"font-size:24px\">Source: order system, Q3 2026</p><aside>The North leads, all of it shipped from one crowded depot: the case for a new one.</aside></section>"}]}]}
```

One bar per value, at most six: largest first, or months in their order. A bar's width is its value as a share of the
largest: 3,600 of 4,500 is 80%. The bar the heading is about is amber, the others navy; each value is written beside its
bar.

## Timeline

Steps in order: a plan, a launch, a history.

```json
{"path": "decks/q3-review.pptx", "ops": [{"op": "slides", "slides": [{"html": "<section style=\"background:#FBFBF8;color:#14285A;font-family:Arial, sans-serif;padding:128px;gap:48px\"><h2>Q4: four steps to the new depot</h2><div style=\"display:flex;gap:32px;margin:auto 0\"><div style=\"flex:1;border-top:12px solid #F2C14E;padding-top:32px;gap:16px\"><h3>October</h3><p>Sign the lease and hire the depot team</p></div><div style=\"flex:1;border-top:12px solid #F2C14E;padding-top:32px;gap:16px\"><h3>November</h3><p>Fit out the building and test the systems</p></div><div style=\"flex:1;border-top:12px solid #F2C14E;padding-top:32px;gap:16px\"><h3>December</h3><p>Ship the first orders from the new depot</p></div><div style=\"flex:1;border-top:12px solid #F2C14E;padding-top:32px;gap:16px\"><h3>January</h3><p>Move all northern orders to the new depot</p></div></div><aside>Each step has an owner; the lease is the one that can slip.</aside></section>"}]}]}
```

Three to five steps: each a date or a phase in a word or two, and at most 15 words on what happens.

## Closing

The deck's last slide: the decision or the next steps, and who to ask.

```json
{"path": "decks/q3-review.pptx", "ops": [{"op": "slides", "slides": [{"html": "<section style=\"background:#14285A;color:#FBFBF8;font-family:Arial, sans-serif;padding:128px;justify-content:center;gap:40px\"><div style=\"width:160px;height:12px;background:#F2C14E\"></div><h2>Approve the new depot today</h2><ul><li>Budget: $240,000, paid back in 18 months</li><li>Opens: 1 December, with a team of six</li><li>Next review: 15 January 2027</li></ul><p style=\"color:#DCE3F0\">Questions: sales team, sales@example.com</p><aside>Ask for the decision, then take questions.</aside></section>"}]}]}
```

The ask is the heading, at most 12 words; then at most four points of one line each, and who to ask.

## Decks

**Pitch**, to win support for an idea, a product or a budget: Title; Statement of the problem, in the audience's words;
Big number for how large it is; Cards for the answer's three parts; Chart for the proof so far; Table for the cost or
the options; Timeline for the plan; Closing with the ask and its amount.

**Onboarding**, for a new joiner, team or customer: Title; Statement of what the team is for; Cards for the three or
four things to know first; Timeline for the first weeks; Table of who to ask about what; Closing with the first steps
today and who to ask.

**Review**, of a quarter, a project or a campaign: Title; Statement of the result; Big number for the headline figure;
Chart for what lies behind it; Table of plan against actual; Cards for what worked, what did not and what changes;
Timeline for the next period; Closing with the decisions asked.

A kind can come twice: two figures that matter are two Big number slides, and a Statement can open each part of a longer
deck.
