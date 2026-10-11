---
title: "Keyword search is conjunctive: one rare word zeroes the query"
type: gotcha
created: 2026-09-20
assistant: claude-code
tags: [qmd, search, recall]
---
# Keyword search is conjunctive: one rare word zeroes the query

"how should I organise obsidian note types and the graph view" returned 0 hits;
"obsidian note types graph view" returned 7. Strip stopwords, drop paths and dates, split
hyphenated words, and narrow the query before concluding nothing is known.
