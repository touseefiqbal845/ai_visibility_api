# AI Visibility Intelligence API

A Flask API that discovers the questions people ask AI assistants in a business's
competitive space, checks whether that business actually gets named in the answers,
and proposes content to close the gaps it finds.

Three agents run in sequence behind one endpoint:

```
POST /api/v1/profiles/{uuid}/run
   │
   ├─ Agent 1  Query Discovery ......... 15 candidate questions + intent labels
   ├─ DataForSEO ...................... real search volume + keyword difficulty
   ├─ Agent 2  Visibility Scoring ...... per query: does the domain get named?
   └─ Agent 3  Content Recommendation .. 5 content briefs for the biggest gaps
```

---

## Setup

Two options. Both need API keys in `.env` before the pipeline endpoint will work;
everything else runs without them.

### Docker (recommended)

```bash
cp .env.example .env      # then add ANTHROPIC_API_KEY and DataForSEO credentials
docker compose up --build
```

Postgres, migrations and gunicorn come up together. API on `http://localhost:5000`.

### Local

```bash
cp .env.example .env      # then add your keys
./setup.sh                # venv, deps, migrations
. .venv/bin/activate && FLASK_APP=wsgi.py flask run --port 5000
```

Defaults to SQLite. Requires Python 3.11+.

### Tests

```bash
pytest              # no network, no keys needed - both providers are faked
pytest --cov=app
```

### Smoke test

```bash
curl -X POST localhost:5000/api/v1/profiles -H 'Content-Type: application/json' -d '{
  "name": "Frase",
  "domain": "frase.io",
  "industry": "SEO Content Tools",
  "description": "AI-powered content briefs and SEO research",
  "competitors": ["surferseo.com", "marketmuse.com", "clearscope.io"]
}'

curl -X POST localhost:5000/api/v1/profiles/<uuid>/run          # 20-40s
curl "localhost:5000/api/v1/profiles/<uuid>/queries?min_score=0.5"
curl "localhost:5000/api/v1/profiles/<uuid>/recommendations"
```

---

## Endpoints

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/profiles` | Register a business. `201` |
| GET | `/api/v1/profiles/{uuid}` | Profile + summary stats + last run |
| POST | `/api/v1/profiles/{uuid}/run` | Runs all three agents. Rate limited |
| GET | `/api/v1/profiles/{uuid}/queries` | `?min_score= &status= &page= &per_page=` |
| GET | `/api/v1/profiles/{uuid}/recommendations` | |
| GET | `/api/v1/profiles/{uuid}/runs` | Run history |
| GET | `/api/v1/queries/{uuid}` | Single query with score breakdown |
| POST | `/api/v1/queries/{uuid}/recheck` | Re-runs Agent 2 only |

Errors share one shape, so clients parse one thing:

```json
{"error": {"code": "not_found", "message": "Profile 'abc' does not exist", "details": {}}}
```

---

## Architecture decisions

**Why the visibility check is a real LLM call, not a heuristic.** Agent 2 poses the
discovered question to a model exactly as a user would, then scans the answer for the
target domain and each competitor. "Visible" means the model named you; position is
order of first mention. This is the honest reading of the brief — the thing being
measured is whether an assistant mentions you, so the only way to measure it is to ask
one. The alternative (inferring visibility from search rankings) measures something
else and calls it AI visibility.

**Why search volume comes from DataForSEO and not the model.** Asking an LLM to
estimate search volume produces confident, stable, invented numbers. Volume and
difficulty come from DataForSEO's Google Ads endpoint in one batched call per run
rather than one per query, which keeps a 15-query run to a single billable request.
The model is used only for what it can actually do: generating plausible questions,
answering them, and writing content briefs.

**Model selection.** Discovery and recommendation run once per pipeline and are the
two calls where output quality is visible to the user, so they use Sonnet. Scoring
runs once per discovered query — 15× the call volume — and its job is to produce a
representative answer, not a creative one, so it uses Haiku at temperature 0.2.
Discovery runs at 0.8: at low temperature it returns fifteen rephrasings of the same
two questions. All three are overridable per-agent via env vars.

**Failure isolation.** The orchestrator distinguishes "produced nothing" from
"produced less than we wanted", and only the first is a failed run:

| Failure | Result |
|---|---|
| Agent 1 fails | Run fails — nothing downstream to do |
| DataForSEO fails | Run continues; score formula redistributes those weights; `completed_with_errors` |
| Agent 2 fails on one query | That query is flagged, the batch continues |
| Agent 3 fails | Scored queries kept, recommendations empty, `completed_with_errors` |

Warnings accumulate on the run record, so a partial run explains itself.

**Malformed JSON handling.** Every agent goes through one call-parse-retry path.
Parsing tries the raw text, then fenced content, then the first balanced brace-scanned
block (string-aware, so braces inside titles don't break it), then a trailing-comma
repair. If all fail, the model gets one corrective turn that shows it its own bad
output — a model that has drifted into prose usually stays drifted on a plain retry
but corrects reliably when shown the failure. Responses are also prefilled with `{`
to suppress preamble. Beyond that, individual malformed *items* are dropped with a
warning rather than failing the batch: one bad recommendation shouldn't cost the other
four.

**Recommendations map back by index, not UUID.** Agent 3 gets a numbered list and
returns `query_index`. Models transcribe long identifiers wrong often enough that
echoing UUIDs quietly orphans recommendations; an integer index is bounds-checkable.

**Schema notes.** UUID primary keys throughout, so identifiers can be handed out
without leaking row counts. `competitors` and `target_keywords` are JSON columns —
they're read as whole lists and never queried by element, so a join table would add
cost for no gain. Queries are keyed per profile and reused across runs rather than
duplicated, so a re-run updates history instead of forking it; that's what makes
`recheck` meaningful after publishing content. `PipelineRun` stores token counts and
warnings so a run can be audited after the fact.

---

## Opportunity score

Four factors, each normalised to 0–1, combined as a weighted sum:

```
score = 0.35·visibility_gap + 0.30·volume + 0.20·(1 − difficulty) + 0.15·intent
```

**Visibility gap (0.35)** — the largest weight, because it is the only factor that
measures the thing the product exists to fix. Tiered rather than binary: absent 1.0,
mentioned but late 0.6, top three 0.3, already first 0.05. A brand mentioned last in a
ten-brand answer still has real upside; one already cited first has almost none.

**Volume (0.30)** — `log10(1+v) / log10(1+50000)`, capped at 1.0. Keyword volume is
heavily right-skewed, and the jump from 50 to 500 searches matters far more than
20,000 to 20,450. A linear scale would let a handful of head terms flatten everything
else to near-zero.

**Difficulty (0.20)** — inverted `1 − (difficulty/100)`, so easier queries score
higher. Uses DataForSEO keyword difficulty, falling back to the Ads competition index
when the Labs endpoint has nothing for a long-tail conversational query. Both are
0–100, so the fallback stays on-scale.

**Commercial intent (0.15)** — comparison 1.0, commercial 0.7, informational 0.35.
Labelled by Agent 1 at discovery time. "Frase vs Surfer SEO" is a user with a wallet
open; "what is an SEO content brief" usually isn't. Weighted lowest because it's the
one factor that is a model judgement rather than a measurement.

**Missing factors are dropped, not defaulted.** If DataForSEO returns nothing for a
query, volume and difficulty are excluded and the remaining weights are renormalised
to sum to 1. Substituting a placeholder volume would quietly pull every affected query
toward the same score and make the ranking meaningless in exactly the case where you
can least afford it. Each score ships with a breakdown of its factors, the weights
actually applied, and any notes — so a number can be explained rather than trusted.

The weights are a judgement call and are the part of this I'd most expect to revise
against real outcome data. The tests assert the properties the formula should have —
ordering, bounds, log behaviour, graceful degradation — rather than exact values.

---

## Tradeoffs and what I left out

- **Synchronous pipeline.** A run takes 20–40s. The brief allowed sync, so the
  complexity of a broker and result backend went to failure handling and prompt
  robustness instead. Under real load this belongs on a queue; `PipelineRun` already
  carries the status fields a polling endpoint would need.
- **Rate limiting is keyed per profile, not per IP.** Every run spends real credits at
  two providers, so the thing worth protecting is the profile, not the caller. Backed
  by in-memory storage — fine for one process, needs Redis for more than one.
- **No auth.** Explicitly out of scope.
- **Visibility is a single sample.** One model, one call, one point in time. Answers
  vary between runs, so a production version would sample several times and report a
  frequency rather than a boolean. `recheck` exists so a query can be re-measured, but
  it doesn't aggregate history yet.
- **Brand matching is string-based** with word boundaries and spaced/acronym variants
  (`surferseo.com` also matches "Surfer SEO"). It will miss a brand referred to only
  by an unusual paraphrase. An LLM extraction pass would be more robust and much more
  expensive per query.
- **Test coverage is deliberately narrow**: the scoring formula, JSON recovery, each
  agent's parsing and fallbacks, and the orchestrator's failure paths. No tests assert
  what the model actually says, because that isn't a stable property.

---

## AI tools used

I used Claude while building this, mainly for drafting the agent prompts and iterating
on the JSON-recovery edge cases, plus review passes over the orchestrator's failure
handling. The architecture, the failure policy, the scoring formula and its weights,
and the schema are my decisions; I've documented the reasoning for each above and can
walk through any of them.
#
