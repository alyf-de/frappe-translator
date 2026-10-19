# Model comparison: Haiku vs. Sonnet

This page shows the results of one test run that compared Claude Haiku 5.5 and Claude Sonnet 5.5 as translation models for frappe-translator. It was done on 2026-10-17.

## Result

Haiku gives good translations, but Sonnet is better. Haiku makes approximately 4 to 5 times more bad translations. Almost all of these errors are short, ambiguous business terms.

- Use Haiku for a first pass on a large backlog, followed by a review from a native speaker.
- Use Sonnet when translations go into a release without a review.

## Method

1. Make a copy of a bench. The real PO files do not change.
2. Select strings that have an existing translation in 70% or more of the app's locales:
   - **erpnext:** 60 strings × 39 locales
   - **banking:** 30 strings × 5 locales

   Each app sample has three types of strings: short terms (3 words or fewer), sentences, and strings with placeholders or HTML.

3. In the copy, remove the existing translations of these strings. Then run the normal pipeline once with `--model haiku` and once with `--model sonnet`. The glossary step is enabled.
4. Five Opus agents give a score from 1 to 5 to each Haiku, Sonnet and existing translation. They do not know which translation comes from which source. A score of 2 or lower is a "bad" translation (wrong meaning, wrong term, wrong script, or an untranslated string).

There are 1,886 compared strings in 35 locales. For 4 locales (bg, ro, km, ta), no sampled string had an existing translation, so these locales are not in the results.

## Numbers

|                                       | Haiku     | Sonnet   | Existing translation |
| ------------------------------------- | --------- | -------- | -------------------- |
| Average score (1–5)                   | 4.72      | 4.84     | 4.17                 |
| Bad translations (score ≤ 2)          | 1.4% (26) | 0.3% (6) | 9.2%                 |
| Failed calls or changed placeholders  | 0         | 0        | –                    |
| Run time (both runs at the same time) | 412 s     | 347 s    | –                    |

- In most languages, Haiku is a little behind Sonnet. The difference is largest in pt, ru, mn and nl (approximately 0.3 to 0.45 points).
- In de, tr, pl, vi and zh_TW, Haiku and Sonnet are approximately equal. In af and cs, Haiku is a little better.
- Both models get a higher score than the existing community translations. The judge is also a language model and can prefer machine-style text. Thus, read this result as "both models are usable", not as "both models are better than human translators".

## Typical Haiku errors

Haiku often translates an ERP term with its everyday meaning:

| Source                      | Haiku error                           | Languages          |
| --------------------------- | ------------------------------------- | ------------------ |
| "Opening" (balance)         | "hole", "open" (verb)                 | fi, th, vi         |
| "Party Link"                | "celebration", "batch"                | mn, da             |
| "BOM Rate"                  | "exchange rate", "level"              | bs, hr, hu, sl, tr |
| "Returns"                   | "proceeds"                            | af                 |
| "Banking Reference Mapping" | not translated                        | de, es, fr, it, nl |
| "Submit"                    | "übermitteln" (ERPNext uses "buchen") | de                 |

Sonnet translated most of these terms correctly.

## Possible improvements

- Most Haiku errors are glossary terms. A glossary translated by Sonnet or Opus (`glossary` command) and Haiku for `translate` can possibly close a large part of the gap. This was not tested.
- References to DocType JSON files have no line number. The pipeline skips them ("Skipping malformed source ref (no colon)"). Thus, DocType and field labels get no source context. This is a possible cause of untranslated DocType names such as "Banking Reference Mapping".

## Limits of this test

- The sample is small (90 strings).
- The judge is a language model. It is less reliable for languages such as mn, my and uz.
- One judge group (de, fr, es, it, nl, pt, pt_BR, sv) gave explicit scores only when the candidates were different and gave 5 to all others. This makes the differences in these languages smaller.
- The runs used the Claude CLI. Thus, this test does not measure token use or cost.
