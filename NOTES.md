# NOTES: a knowledge system that knows what's true *now*

Submission folder: `submission/` (entry point `submission/solution.py` → `build()`).

```bash
python harness/run_eval.py --submission submission --eval eval/public_eval.jsonl
```

**No API keys and no third-party packages.** It uses only the Python standard library (tested on 3.11 and 3.14), and
`requirements.txt` is intentionally empty. It is deterministic: the same question always gets the same answer.
Ingesting all five snapshots takes about 1.5 s, and answers take about 10–50 ms (p95 ≈ 0.05 s).

> The repo does not include the website snapshots (`snapshots_zip/`, `snapshots/0*/`), as the brief asks.
> Copy the kit's `snapshots_zip/` folder into the repo root before running; the scorer unpacks it on first run.

## Public practice set

On `eval/public_eval.jsonl` the system scores **accuracy 0.87**, with consistency and speed at 1.0. That
gives **0.87 final** on the public set, which has no conflict gold.

I also kept three sets of my own in `dev/`, so I wasn't only tuning on the 40 public questions:
- `dev_eval.jsonl` has 34 new questions: other products, tiers, "as of" dates, a withdrawn special and refusals.
  It scores 1.0.
- `paraphrase_eval.jsonl` has 31 rewordings of public questions. It scores 0.93.
- `review_eval.jsonl` has 95 questions written by an independent adversarial reviewer, covering fees, contact
  facts, odd phrasings, dates and refusals. It started at 0.62. Fixing the root causes it exposed took it to
  0.93; that number is now partly fitted.

The harness self-test is a different, synthetic credit union. It scores 1.0, including conflict F1 = 1.0.

## How it works

### 1. Keep history, not just the latest copy (`kb/store.py`, `kb/parse.py`)

Every snapshot keeps its own view of the site. Identical content is parsed and indexed once.

Each page version is split into:
- **passages**: heading-scoped chunks, indexed with BM25 (`kb/search.py`, stdlib).
- **rate tables**: markdown tables, plus short "label: 3.50%" sentences on product pages. Each table keeps:
  - its own **"Rates are effective as of" date**. This is the line *after* the table; footnotes that restate
    older dates are skipped.
  - the **product context** above it, including old layouts where the product name lives only in a tab
    link or the footnote.
  - any **"offer ended …"** date.

Every page also gets a **kind**, from its URL, which sets its authority:

| Kind | Authority |
| --- | --- |
| rate sheet | 5 |
| product / contact page | 4 |
| promo landing page, guide, PDF | 2 |
| calculator, blog / newsroom article | 1 |

Articles also get their **publish date** from the header. "Handpicked for You" teaser cards are cut off, because
they carry other articles' dates and numbers.

Pages missing from a later download are **carried forward** as a low-priority fallback, since crawls miss pages.
They answer only when nothing current does.

### 2. Decide what "current" means (the rules)

1. **`as_of` picks the snapshot.** With a date, I use the newest snapshot taken on or before it; without one, the
   newest snapshot. A date in the question itself also counts ("as of May 2026" means 31 May 2026).
2. **Same rate, many copies: the newest effective date wins.** The /rates table effective 08/31/2026 (5.39%) beats
   the calculator and switch-and-earn copies effective 1/1/2026 (5.34%). Ties go to the more authoritative page.
   Losing copies are never quoted. They appear in `conflict_note` and in `conflicts()`.
3. **Qualifiers are different facts, not conflicts.** The question is parsed into:
   - term ("60-month", "five year" = 60 months)
   - model year, matched by numeric range ("2025" falls in "2024-27" and in the reversed "2026-23")
   - balance or loan amount, matched against tier ranges ("$10,000 – $24,999.99", "$25,000+",
     "$100,000 or less")
   - jumbo / refinance / Roth / introductory / business, and metric (APR / APY / dividend)

   Rows that contradict a qualifier are dropped. Rows carrying a qualifier the member did not ask about are
   dropped too: a plain "10-month special" never returns the jumbo rate. Business products answer only
   business questions, except "what cards do you offer" lists, which cover both.
4. **Tables that are copies of each other compete; different products never do.** Two tables are copies when
   they share row labels, or share tier boundaries and the same product name. Tiers alone are not enough: Money
   Market and HSA both have a "$25,000+" tier. Two tables on one page are never copies.
5. **Restated facts in articles: the newest publish date wins.** Branch counts in press-release boilerplate say
   24 / 23 / 22 depending on the year, and the newest article (22) is current. Recency only breaks ties
   between equally relevant sentences, so a newer article about a different fact can't win.
6. **Expired promotions and withdrawn products.** A promo past its "offer ended" date is ignored. A product that
   had rates in an earlier snapshot but has none now gets "no longer offered", with no number.
7. **Be honest.** The system refuses when:
   - a rate-shaped question has no current rate table for its product (7/1 ARM, construction or student loans,
     a 10-year mortgage). A named product type the table doesn't mention means it's the wrong table.
   - an hours or address question names a branch the site never gives hours or an address for (Mullan Avenue).
   - the question's key word appears nowhere on the site ("SWIFT code").
   - the question's rarest word, the named thing, isn't on the page the answer would come from.
   Refusals contain **no numbers**, not even a phone number, because "states no number" is part of a correct
   refusal.

### 3. Answering (`kb/engine.py`, `kb/rates.py`, `kb/text.py`)

- **Rate questions** go to the rate tables (rules 2–4), and the answer quotes only the winning rows.
- **Hours questions** are answered from the "Lobby / Drive-up" schedule blocks across branch articles. The
  answer gives the most common schedule plus the variants, or one branch's hours when it's named.
- **Address questions** look at the branch locator (contact page) first, using only "Dupaco Branch" entries
  (ATMs are skipped). They fall back to a sentence that names the branch together with a street address.
- **General contact questions** ("main phone number", "mailing address", "routing number") come from the
  contact page's labelled fields. Questions about a specific team go to normal retrieval.
- **Fee questions** are answered with the single best-matching fee-schedule line ("Stop payment … $30.00 per
  item"), never its neighbours, which are other fees.
- **"How many" questions** pick the newest dated statement of the count.
- **Everything else** uses sentence-level retrieval:
  - BM25 finds candidate passages. Each sentence is then scored on how much of the question it covers, plus
    heading, URL path and page authority.
  - The question's rarest word, usually a name like Home$tart, Hy-Vee or Galena, must appear in the sentence
    or its page title.
  - The expected answer type is preferred: a date for "when", a 9-digit number for "routing number", money
    for "how much".
- Every answer cites the page(s) it came from, and `conflict_note` says when older copies were ignored.

### 4. `conflicts()` (`kb/conflicts.py`)

The list is short and precise (about 60 entries), because the score is F1. It has three parts:
- **Stale copies in one snapshot.** The same product/tier/metric shows different values on two pages, such as the
  rate sheet vs a calculator, promo page or guide. Resolution: the newest effective date, then authority.
- **Article boilerplate.** Branch-office count, membership total, assets, and how many times Forbes has
  recognized Dupaco. Resolution: the newest publish date.
- **The official rate sheet changed between downloads.** One entry per product row with the full value history.
  Resolution: the newest snapshot.

Rate vs APY, different tiers and different terms are never reported as conflicts.

## Limits / what I'd do next

- Several public misses need joining facts across paragraphs, which extractive answers can't do:
  - "who is eligible" needs "live or work in our charter area" plus "Iowa, Illinois and Wisconsin".
  - "update contact info" needs the phone number from another page.
  - The volunteer-time-off count is ambiguous: the site has 2023, 2024 and 2025 figures.

  An optional LLM step could write the answer from evidence that has already been filtered. Its inputs would
  hold only current, winning values, so the stale-value traps stay closed. I left it out to keep the
  system key-free and deterministic.
- The page-kind rules and the few synonyms (mortgage ↔ home loan, CD ↔ certificate, share certificate) are
  written for a credit-union site. They're listed in one place each (`parse.page_kind`, `rates.ALIASES`).
